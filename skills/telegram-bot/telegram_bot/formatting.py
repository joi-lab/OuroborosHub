"""Reviewed standalone copy of the native Telegram presentation helpers.

Source: razzant/ouroboros, commit 82508f56e02918819cdf3db9f7c4d80038740690,
skills/telegram/lib/telegram_api.py. Source functions below are unchanged;
local wrappers prepare explicit provider payloads. See FORMATTER_PROVENANCE.md
and LICENSE.formatting. No runtime dependency on another installed skill.
"""

from __future__ import annotations

import html as html_lib
import re

_TELEGRAM_TEXT_LIMIT = 4096


_TABLE_MAX_ROWS = 30


_TABLE_MAX_COLUMNS = 6


_TABLE_MAX_CELL_CHARS = 24


def _u16len(value: str) -> int:
    """Return Telegram's text length: UTF-16 code units, not code points."""
    return sum(2 if ord(char) > 0xFFFF else 1 for char in value)


def _take_u16_prefix(value: str, budget: int) -> tuple[str, str]:
    """Split value at a UTF-16-unit boundary without bisecting a code point."""
    used = 0
    index = 0
    while index < len(value):
        width = 2 if ord(value[index]) > 0xFFFF else 1
        if used + width > budget:
            break
        used += width
        index += 1
    return value[:index], value[index:]


def _chunk_raw_text(text: str, limit: int = _TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split raw text into <=limit UTF-16-unit pieces on line/space boundaries."""
    if _u16len(text) <= limit:
        return [text]
    chunks: list[str] = []
    buf = ""
    for line in text.split("\n"):
        while _u16len(line) > limit:
            # A single very long line: break on the last space within the window,
            # else hard-cut at the limit.
            prefix, remainder = _take_u16_prefix(line, limit)
            cut = prefix.rfind(" ")
            if cut > 0:
                piece = line[:cut]
                line = line[cut:].lstrip(" ")
            else:
                piece = prefix
                line = remainder
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(piece)
        candidate = f"{buf}\n{line}" if buf else line
        if _u16len(candidate) > limit:
            if buf:
                chunks.append(buf)
            buf = line
        else:
            buf = candidate
    if buf:
        chunks.append(buf)
    return chunks or [""]


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _split_table_row(line: str) -> list[str]:
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith(r"\|"):
        row = row[:-1]
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for char in row:
        if char == "|" and not escaped:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(char)
        if char == "\\" and not escaped:
            escaped = True
        else:
            escaped = False
    cells.append("".join(current).strip())
    return [cell.replace(r"\|", "|") for cell in cells]


def _is_table_delimiter(line: str, header: str) -> bool:
    cells = _split_table_row(line)
    return (
        "|" in line
        and len(cells) == len(_split_table_row(header))
        and all(re.fullmatch(r":?-+:?", cell) for cell in cells)
    )


def _is_non_code_indent(line: str) -> bool:
    """Return whether CommonMark treats the line as non-code indentation."""
    columns = 0
    for char in line:
        if char == " ":
            columns += 1
        elif char == "\t":
            columns += 4 - (columns % 4)
        else:
            break
        if columns >= 4:
            return False
    return True


def _is_table_start(header_line: str, delimiter_line: str) -> bool:
    return (
        _is_non_code_indent(header_line)
        and "|" in header_line
        and _is_table_delimiter(delimiter_line, header_line)
    )


def _truncate_table_cell(cell: str) -> str:
    if len(cell) <= _TABLE_MAX_CELL_CHARS:
        return cell
    return cell[: _TABLE_MAX_CELL_CHARS - 1] + "…"


def _table_html(lines: list[str]) -> str:
    rows = [_split_table_row(lines[0])]
    rows.extend(_split_table_row(line) for line in lines[2:])
    column_count = min(max((len(row) for row in rows), default=0), _TABLE_MAX_COLUMNS)
    truncated = len(rows) > _TABLE_MAX_ROWS or any(len(row) > _TABLE_MAX_COLUMNS for row in rows)
    visible_rows = rows[:_TABLE_MAX_ROWS]
    normalized: list[list[str]] = []
    for row in visible_rows:
        normalized.append(
            [_truncate_table_cell(row[index] if index < len(row) else "") for index in range(column_count)]
        )
    widths = [
        max(3, max((len(row[index]) for row in normalized), default=0))
        for index in range(column_count)
    ]

    def render(row: list[str]) -> str:
        return " | ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip()

    grid: list[str] = []
    if normalized:
        grid.append(render(normalized[0]))
        grid.append("-+-".join("-" * width for width in widths))
        grid.extend(render(row) for row in normalized[1:])
    if truncated:
        grid.append("…table truncated")
    return f"<pre>{_escape_html(chr(10).join(grid))}</pre>"


def _replace_gfm_tables(text: str, pre_placeholder_map: dict[str, str]) -> str:
    lines = text.splitlines(keepends=True)
    output: list[str] = []
    index = 0
    while index < len(lines):
        body = lines[index].rstrip("\r\n")
        if (
            index + 1 < len(lines)
            and _is_table_start(body, lines[index + 1].rstrip("\r\n"))
        ):
            end = index + 2
            while end < len(lines):
                candidate = lines[end].rstrip("\r\n")
                if not candidate.strip() or "|" not in candidate:
                    break
                end += 1
            placeholder = f"\x00PRE{len(pre_placeholder_map)}\x00"
            pre_placeholder_map[placeholder] = _table_html(
                [line.rstrip("\r\n") for line in lines[index:end]]
            )
            newline = "\n" if lines[end - 1].endswith(("\n", "\r")) else ""
            output.append(placeholder + newline)
            index = end
            continue
        output.append(lines[index])
        index += 1
    return "".join(output)


def markdown_to_telegram_html(text: str) -> str:
    """Convert standard rich Markdown text into Telegram-compliant HTML syntax."""
    if not text:
        return text

    # Placeholder dictionaries
    pre_placeholder_map: dict[str, str] = {}
    code_placeholder_map: dict[str, str] = {}
    literal_placeholder_map: dict[str, str] = {}

    # 1. Protect fenced blocks before table detection and inline formatting.
    def replace_pre(match: re.Match) -> str:
        code_content = match.group(1)
        placeholder = f"\x00PRE{len(pre_placeholder_map)}\x00"
        pre_placeholder_map[placeholder] = f"<pre>{_escape_html(code_content)}</pre>"
        return placeholder

    text = re.sub(
        r"```(?:[A-Za-z0-9_-]*[ \t]*\r?\n)?(.*?)```",
        replace_pre,
        text,
        flags=re.DOTALL,
    )

    # 2. Preserve supported LaTeX delimiters as literal text. Placeholders keep
    # emphasis and link regexes from interpreting math source.
    def replace_literal(match: re.Match) -> str:
        placeholder = f"\x00LITERAL{len(literal_placeholder_map)}\x00"
        literal_placeholder_map[placeholder] = _escape_html(match.group(0))
        return placeholder

    text = re.sub(r"\$\$(.+?)\$\$", replace_literal, text, flags=re.DOTALL)
    text = re.sub(r"\\\((.+?)\\\)", replace_literal, text, flags=re.DOTALL)
    text = re.sub(r"\\\[(.+?)\\\]", replace_literal, text, flags=re.DOTALL)

    # 3. Convert GFM pipe tables to bounded monospace grids, then escape all
    # remaining literal HTML from the source.
    text = _replace_gfm_tables(text, pre_placeholder_map)
    text = _escape_html(text)

    # 4. Extract inline code blocks.
    def replace_code(match: re.Match) -> str:
        inner = match.group(1)
        placeholder = f"\x00CODE{len(code_placeholder_map)}\x00"
        code_placeholder_map[placeholder] = f"<code>{inner}</code>"
        return placeholder

    text = re.sub(r"`([^`\n]+)`", replace_code, text)

    # 5. Headers, task lists, and ordinary list formatting line-by-line.
    lines = []
    for line in text.split("\n"):
        header_match = re.match(r"^(\s*)#{1,6}\s+(.+)$", line)
        if header_match:
            indent = header_match.group(1) or ""
            content = header_match.group(2)
            lines.append(f"{indent}<b>{content}</b>")
        else:
            task_match = re.match(r"^(\s*)(?:[-*+]|\d+\.)\s+\[([ xX])\]\s+(.+)$", line)
            if task_match:
                glyph = "☑" if task_match.group(2).lower() == "x" else "☐"
                lines.append(f"{task_match.group(1)}{glyph} {task_match.group(3)}")
                continue
            # Replace starting list bullet * or - with •
            bullet_match = re.match(r"^(\s*)[*-]\s+(.+)$", line)
            if bullet_match:
                lines.append(f"{bullet_match.group(1)}• {bullet_match.group(2)}")
            else:
                lines.append(line)
    text = "\n".join(lines)

    # 6. Bold and Italic replacing outside of protected blocks.
    # Asterisk patterns match anywhere — `**bold**` and `*italic*` are unambiguous.
    # Underscore patterns require non-word context on both sides so identifiers
    # like `chat_id`, `state_dir`, `OUROBOROS_MODEL` inside bold spans do NOT
    # trigger spurious italic wraps that cross outer tag boundaries (which
    # would produce malformed nested HTML and a Telegram 400 Bad Request).
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i>\1</i></b>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\w)__(?=\S)([^_\n]+?)(?<=\S)__(?!\w)", r"<b>\1</b>", text)
    text = re.sub(r"\*(.+?)\*", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_(?=\S)([^_\n]+?)(?<=\S)_(?!\w)", r"<i>\1</i>", text)

    # 7. Links [text](url) -> <a href="url">text</a>
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r'<a href="\2">\1</a>', text)

    # 8. Reconstruct protected blocks (longest placeholder first prevents
    # substring prefix collisions such as PLACEHOLDER1/PLACEHOLDER10).
    placeholders = sorted(
        [
            *pre_placeholder_map.items(),
            *code_placeholder_map.items(),
            *literal_placeholder_map.items(),
        ],
        key=lambda item: len(item[0]),
        reverse=True,
    )
    for _ in range(len(placeholders) + 1):
        reconstructed = text
        for placeholder, replacement in placeholders:
            reconstructed = reconstructed.replace(placeholder, replacement)
        if reconstructed == text:
            break
        text = reconstructed

    # Telegram rejects NUL bytes even without parse mode. Remove any source NUL
    # or unresolved sentinel defensively before this text reaches a send path.
    text = text.replace("\x00", "")

    return text


def _markdown_blocks(text: str) -> list[str]:
    """Split markdown without bisecting fenced, table, or quote blocks."""
    lines = text.splitlines(keepends=True)
    blocks: list[str] = []
    index = 0

    def body(at: int) -> str:
        return lines[at].rstrip("\r\n")

    def starts_special(at: int) -> bool:
        if re.match(r"^\s*```", body(at)) or re.match(r"^\s*>", body(at)):
            return True
        return (
            at + 1 < len(lines)
            and _is_table_start(body(at), body(at + 1))
        )

    while index < len(lines):
        start = index
        if re.match(r"^\s*```", body(index)):
            index += 1
            while index < len(lines):
                closing = bool(re.match(r"^\s*```\s*$", body(index)))
                index += 1
                if closing:
                    break
        elif (
            index + 1 < len(lines)
            and _is_table_start(body(index), body(index + 1))
        ):
            index += 2
            while index < len(lines) and body(index).strip() and "|" in body(index):
                index += 1
        elif re.match(r"^\s*>", body(index)):
            index += 1
            while index < len(lines) and re.match(r"^\s*>", body(index)):
                index += 1
        elif not body(index).strip():
            index += 1
            while index < len(lines) and not body(index).strip():
                index += 1
        else:
            index += 1
            while index < len(lines) and body(index).strip() and not starts_special(index):
                index += 1
        blocks.append("".join(lines[start:index]))
    return blocks or [""]


_HTML_TOKEN_RE = re.compile(r"<[^>]+>|&(?:#[0-9]+|#x[0-9A-Fa-f]+|[A-Za-z]+);|\s|[^\s<&]+|[<&]")


_HTML_TAG_RE = re.compile(r"</?([A-Za-z0-9]+)")


def _split_html_balanced(value: str, limit: int = _TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split one oversized HTML block while closing and reopening active tags."""
    chunks: list[str] = []
    current = ""
    stack: list[tuple[str, str]] = []

    def suffix(active_stack: list[tuple[str, str]]) -> str:
        return "".join(f"</{name}>" for name, _opening in reversed(active_stack))

    def closing_suffix() -> str:
        return suffix(stack)

    def reopen_prefix() -> str:
        return "".join(opening for _name, opening in stack)

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append(current + closing_suffix())
        current = reopen_prefix()

    def append_slices(raw: str) -> None:
        while raw:
            piece, raw = _take_u16_prefix(raw, limit)
            if not piece:
                piece, raw = raw[0], raw[1:]
            chunks.append(piece)

    def hard_reset() -> None:
        nonlocal current
        payload = current + closing_suffix()
        if payload:
            if _u16len(payload) <= limit:
                chunks.append(payload)
            else:
                append_slices(payload)
        current = ""
        stack.clear()

    tokens = _HTML_TOKEN_RE.findall(value)
    iterations = 0
    for token_index, token in enumerate(tokens):
        iterations += 1
        if iterations > 200_000:
            hard_reset()
            append_slices("".join(tokens[token_index:]))
            return chunks or [""]

        tag_match = _HTML_TAG_RE.match(token) if token.startswith("<") else None
        if tag_match:
            name = tag_match.group(1).lower()
            is_close = token.startswith("</")
            output = token
            if is_close:
                matching_index = next(
                    (index for index in range(len(stack) - 1, -1, -1) if stack[index][0] == name),
                    None,
                )
                if matching_index is None:
                    output = _escape_html(token)
                    projected_stack = list(stack)
                else:
                    intervening = stack[matching_index + 1 :]
                    output = suffix(intervening) + token + "".join(
                        opening for _tag, opening in intervening
                    )
                    projected_stack = stack[:matching_index] + intervening
            elif token.endswith("/>"):
                projected_stack = list(stack)
            else:
                projected_stack = stack + [(name, token)]

            projected_suffix = suffix(projected_stack)
            projected_length = _u16len(current) + _u16len(output) + _u16len(projected_suffix)
            if projected_length > limit and current != reopen_prefix():
                flush()
                projected_length = _u16len(current) + _u16len(output) + _u16len(projected_suffix)
            if projected_length > limit:
                hard_reset()
                append_slices(_escape_html(token))
                continue
            current += output
            stack[:] = projected_stack
            continue

        remaining = token
        while remaining:
            iterations += 1
            if iterations > 200_000:
                hard_reset()
                append_slices(remaining + "".join(tokens[token_index + 1 :]))
                return chunks or [""]

            available = limit - _u16len(current) - _u16len(closing_suffix())
            if available <= 0:
                flush()
                available = limit - _u16len(current) - _u16len(closing_suffix())
                if available <= 0:
                    hard_reset()
                    append_slices(remaining)
                    remaining = ""
                    break
            if _u16len(remaining) <= available:
                current += remaining
                break
            if remaining.startswith("&") and remaining.endswith(";"):
                flush()
                if _u16len(remaining) > limit - _u16len(current) - _u16len(closing_suffix()):
                    hard_reset()
                    append_slices(_escape_html(remaining))
                    remaining = ""
                continue
            piece, remaining = _take_u16_prefix(remaining, available)
            if not piece:
                hard_reset()
                append_slices(remaining)
                remaining = ""
                break
            current += piece
            flush()
    if current:
        chunks.append(current + closing_suffix())
    return chunks or [""]


def markdown_to_telegram_chunks(text: str, limit: int = _TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Convert markdown block-by-block, then pack balanced Telegram HTML."""
    chunks: list[str] = []
    buffer = ""

    def append_visible(chunk: str) -> None:
        if _telegram_html_to_plain(chunk).strip():
            chunks.append(chunk)

    for block in _markdown_blocks(text):
        converted = markdown_to_telegram_html(block)
        if _u16len(converted) > limit:
            if buffer:
                append_visible(buffer)
                buffer = ""
            for chunk in _split_html_balanced(converted, limit):
                append_visible(chunk)
        elif _u16len(buffer) + _u16len(converted) <= limit:
            buffer += converted
        else:
            if buffer:
                append_visible(buffer)
            buffer = converted
    if buffer:
        append_visible(buffer)
    return chunks


def _telegram_html_to_plain(value: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", "", value))


def prepare_text(text: str, *, markdown: bool = True) -> list[dict[str, str]]:
    """Render once; callers persist these exact chunks before first delivery."""
    chunks = markdown_to_telegram_chunks(text) if markdown else _chunk_raw_text(text)
    return [{"text": chunk, "parse_mode": "HTML" if markdown else ""} for chunk in chunks]


def prepare_caption(caption: str, *, markdown: bool = True) -> dict[str, str]:
    """Keep caption contents intact or report the provider limit before upload."""
    text = markdown_to_telegram_html(caption) if markdown else caption
    visible = _telegram_html_to_plain(text) if markdown else text
    if _u16len(visible) > 1024:
        raise ValueError("Telegram captions are limited to 1024 UTF-16 units; send a shorter caption and put the remaining content in a separate text message")
    return {"text": text, "parse_mode": "HTML" if markdown else ""}
