#!/usr/bin/env python3
"""Extract the readable text layer of a PDF, page by page.

Honest about scans: when a document carries no usable text layer, the result is
``status="no_text_layer"`` with an explicit note instead of empty strings that
could be mistaken for an empty document.
"""

from __future__ import annotations

import argparse
import sys

import pdf_common as common


def parse_pages(spec: str, total: int) -> list[int]:
    """Parse a 1-based page spec such as ``1-3,7`` into sorted page numbers."""
    if not spec.strip():
        return list(range(1, total + 1))
    wanted: set[int] = set()
    for chunk in spec.replace(" ", "").split(","):
        if not chunk:
            continue
        if "-" in chunk:
            start_raw, _, end_raw = chunk.partition("-")
            try:
                start, end = int(start_raw), int(end_raw)
            except ValueError:
                common.fail(f"bad page range: {chunk}")
            if start > end:
                start, end = end, start
            wanted.update(range(start, end + 1))
        else:
            try:
                wanted.add(int(chunk))
            except ValueError:
                common.fail(f"bad page number: {chunk}")
    pages = sorted(page for page in wanted if 1 <= page <= total)
    if not pages:
        common.fail(f"page selection is empty for a {total}-page document")
    return pages


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Extract PDF text per page.")
    parser.add_argument("path", help="PDF file inside the Ouroboros working directories")
    parser.add_argument("--pages", default="", help="page selection, e.g. 1-3,7 (default: all)")
    parser.add_argument("--max-chars", type=int, default=200_000,
                        help="total character budget for extracted text (default 200000)")
    parser.add_argument("--meta-only", action="store_true",
                        help="report file name and page count only")
    args = parser.parse_args(argv)

    path = common.resolve_path(args.path, must_exist=True)
    pypdf = common.require("pypdf", "pypdf")

    try:
        reader = pypdf.PdfReader(str(path))
        if getattr(reader, "is_encrypted", False):
            try:
                # decrypt("") returns PasswordType.NOT_DECRYPTED (falsy) when
                # the empty password does not open the file — it does not raise.
                decrypted = bool(reader.decrypt(""))
            except Exception:  # noqa: BLE001 — any failure means we cannot read it
                decrypted = False
            if not decrypted:
                common.fail(
                    f"PDF is encrypted and cannot be opened without a password: {path.name}",
                    status="encrypted",
                )
        total = len(reader.pages)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — surface any parser failure honestly
        common.fail(f"cannot read PDF: {exc}", status="unreadable")

    meta = {}
    try:
        raw_meta = reader.metadata or {}
        for key in ("/Title", "/Author", "/Subject"):
            value = raw_meta.get(key)
            if value:
                meta[key.lstrip("/").lower()] = str(value)
    except Exception:  # noqa: BLE001 — metadata is a nicety, never a failure
        meta = {}

    if args.meta_only:
        common.emit({
            "status": "ok",
            "file": str(path),
            "name": path.name,
            "pages": total,
            "bytes": path.stat().st_size,
            "metadata": meta,
        })
        return 0

    selected = parse_pages(args.pages, total)
    budget = max(1_000, int(args.max_chars))
    used = 0
    truncated = False
    extracted: list[dict] = []
    page_errors: list[dict] = []

    for number in selected:
        try:
            text = reader.pages[number - 1].extract_text() or ""
        except Exception as exc:  # noqa: BLE001 — one bad page must not kill the read
            page_errors.append({"page": number, "error": str(exc)})
            text = ""
        text = text.replace("\r\n", "\n").strip()
        remaining = budget - used
        if remaining <= 0:
            truncated = True
            break
        if len(text) > remaining:
            text = text[:remaining]
            truncated = True
        used += len(text)
        extracted.append({"page": number, "chars": len(text), "text": text})

    has_text = any(item["chars"] > 0 for item in extracted)
    payload = {
        "status": "ok" if has_text else "no_text_layer",
        "file": str(path),
        "name": path.name,
        "pages": total,
        "pages_read": [item["page"] for item in extracted],
        "metadata": meta,
        "total_chars": used,
        "truncated": truncated,
        "extracted_pages": extracted,
    }
    if page_errors:
        payload["page_errors"] = page_errors
    if not has_text:
        payload["note"] = (
            "No usable text layer was found — this document is most likely a scan. "
            "Do not invent its contents: render the page as an image and inspect it "
            "(view_image / vlm_query), or use a dedicated OCR skill if one is installed."
        )
    common.emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
