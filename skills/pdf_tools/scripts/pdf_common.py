"""Shared helpers for the pdf_tools skill: JSON output, path policy, deps, fonts.

Every public entry point of this skill goes through :func:`resolve_path` so file
access stays inside the Ouroboros working directories, and through :func:`emit`
so the agent always receives machine-readable output.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile


def emit(payload: dict) -> None:
    """Print one JSON object on stdout (the skill's only output channel)."""
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def fail(message: str, *, hint: str = "", status: str = "error", code: int = 2) -> None:
    payload = {"status": status, "error": message}
    if hint:
        payload["hint"] = hint
    emit(payload)
    raise SystemExit(code)


def skill_dir() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def state_dir() -> pathlib.Path:
    raw = os.environ.get("OUROBOROS_SKILL_STATE_DIR", "").strip()
    if raw:
        return pathlib.Path(raw).expanduser().resolve()
    return skill_dir() / "_state"


def data_root() -> pathlib.Path | None:
    """Derive the Ouroboros data root from the injected state dir.

    The host sets ``OUROBOROS_SKILL_STATE_DIR`` to ``<data>/state/skills/<name>``,
    so the data root is three levels up. Returns ``None`` when the layout does
    not match (then only the state dir and the temp dir are writable).
    """
    sd = state_dir()
    if sd.parent.name == "skills" and sd.parent.parent.name == "state":
        return sd.parent.parent.parent
    return None


def allowed_roots() -> list[pathlib.Path]:
    roots: list[pathlib.Path] = [state_dir()]
    root = data_root()
    if root is not None:
        roots += [
            root / "uploads",
            root / "task_results",
            root / "task_drives",
            # Both Deliverables layouts are real and intentional: cloud installs
            # keep it inside the data root (/data/Deliverables), desktop installs
            # keep it beside it (~/Ouroboros/Deliverables). Non-existent ones are
            # simply never matched.
            root / "Deliverables",
            root.parent / "Deliverables",
        ]
    roots.append(pathlib.Path(tempfile.gettempdir()).resolve())
    seen: list[pathlib.Path] = []
    for candidate in roots:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved not in seen:
            seen.append(resolved)
    return seen


def _contained(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_path(raw: str, *, must_exist: bool) -> pathlib.Path:
    """Resolve *raw* for READING and refuse anything outside the allowed roots."""
    if not str(raw).strip():
        fail("empty path")
    path = pathlib.Path(str(raw)).expanduser()
    try:
        path = path.resolve()
    except OSError as exc:
        fail(f"cannot resolve path: {exc}")
    roots = allowed_roots()
    if not any(_contained(path, root) for root in roots):
        fail(
            f"path outside the allowed working directories: {path}",
            hint="allowed roots: " + ", ".join(str(r) for r in roots),
        )
    if must_exist and not path.is_file():
        fail(f"file not found: {path}")
    return path


def resolve_output_path(raw: str, suffix: str) -> pathlib.Path:
    """Resolve a WRITE destination, confined to ``<state>/outputs/``.

    A bare file name (or relative path) lands under ``<state>/outputs/``. The
    required ``suffix`` is applied BEFORE the containment check, and the final
    resolved path must stay strictly inside ``outputs/`` — so traversal
    (``--out ..``) and suffix tricks cannot land a sibling of the state
    directory. Anything else is refused with an explanation instead of
    silently writing into shared task directories.
    """
    text = str(raw).strip()
    if not text:
        fail("empty output path")
    candidate = pathlib.Path(text).expanduser()
    outputs = state_dir() / "outputs"
    if not candidate.is_absolute():
        candidate = outputs / candidate
    try:
        path = candidate.resolve()
    except OSError as exc:
        fail(f"cannot resolve output path: {exc}")
    if path.suffix.lower() != suffix:
        path = path.with_suffix(suffix)
    if path == outputs or not _contained(path, outputs):
        fail(
            f"output path outside the skill outputs directory: {path}",
            hint=(
                "pass a bare file name instead; the file is created under "
                f"{outputs} and the returned JSON carries its absolute path — "
                "deliver it to the user with send_file afterwards"
            ),
            status="output_outside_state_dir",
        )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        fail(f"cannot create output directory: {exc}")
    return path


def add_isolated_site_packages() -> None:
    """Make this skill's reviewed isolated dependencies importable."""
    env_root = skill_dir() / ".ouroboros_env"
    if not env_root.is_dir():
        return
    patterns = (
        "python/lib/python*/site-packages",
        "python/Lib/site-packages",
        "lib/python*/site-packages",
    )
    for pattern in patterns:
        for candidate in sorted(env_root.glob(pattern)):
            entry = str(candidate)
            if candidate.is_dir() and entry not in sys.path:
                sys.path.insert(0, entry)


def require(module: str, package: str):
    add_isolated_site_packages()
    try:
        return __import__(module)
    except ImportError as exc:
        fail(
            f"dependency '{package}' is not installed for this skill ({exc})",
            hint="install the skill dependencies from the Skills panel, then retry",
            status="dependency_missing",
            code=3,
        )


FONT_CANDIDATES = (
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/freefont/FreeSans.ttf",
     "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ("/Library/Fonts/Arial Unicode.ttf", ""),
    ("/System/Library/Fonts/Supplemental/Arial Unicode.ttf", ""),
    ("/System/Library/Fonts/Supplemental/Arial.ttf",
     "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/seguisb.ttf"),
)


def find_font(explicit: str = "") -> tuple[pathlib.Path, pathlib.Path | None]:
    """Return (regular, bold_or_None) TrueType paths for Unicode text.

    An explicit ``--font`` path wins. Otherwise the first existing candidate is
    used. Callers must treat a failure here as an honest refusal, not a
    fallback to a Latin-only core font (which would render Cyrillic as boxes).
    """
    if explicit.strip():
        regular = pathlib.Path(explicit).expanduser()
        if not regular.is_file():
            fail(f"font file not found: {regular}")
        return regular.resolve(), None
    for regular_raw, bold_raw in FONT_CANDIDATES:
        regular = pathlib.Path(regular_raw)
        if not regular.is_file():
            continue
        bold = pathlib.Path(bold_raw) if bold_raw else None
        return regular, (bold if bold and bold.is_file() else None)
    fail(
        "no Unicode TrueType font found on this host",
        hint="pass --font /path/to/Font.ttf (a font covering Cyrillic, e.g. DejaVuSans.ttf)",
        status="font_missing",
        code=4,
    )
    raise AssertionError("unreachable")  # pragma: no cover
