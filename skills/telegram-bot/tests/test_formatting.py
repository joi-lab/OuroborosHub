"""Native presentation semantics, included with source provenance in the payload."""

from xml.etree import ElementTree as ET

import pytest

from telegram_bot.formatting import (
    _telegram_html_to_plain,
    _u16len,
    markdown_to_telegram_chunks,
    markdown_to_telegram_html,
    prepare_caption,
    prepare_text,
)


def balanced(value):
    ET.fromstring(f"<root>{value}</root>")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "**bold** and *ital* and ***both***",
            "<b>bold</b> and <i>ital</i> and <b><i>both</i></b>",
        ),
        ("**chat_id** then state_dir", "<b>chat_id</b> then state_dir"),
        ("Use `chat_id` and _italic_", "Use <code>chat_id</code> and <i>italic</i>"),
        ("# Heading\n- [ ] todo\n- [x] done", "<b>Heading</b>\n☐ todo\n☑ done"),
        (
            "[source](https://example.org) <tag>",
            '<a href="https://example.org">source</a> &lt;tag&gt;',
        ),
    ],
)
def test_standard_markdown_matches_native_examples(source, expected):
    rendered = markdown_to_telegram_html(source)
    assert rendered == expected
    balanced(rendered)


def test_tables_and_fences_keep_native_presentation():
    table = "| Name | Score |\n| --- | ---: |\n| Ada | 9 |\n| Grace | 10 |"
    assert (
        markdown_to_telegram_html(table)
        == "<pre>Name  | Score\n------+------\nAda   | 9\nGrace | 10</pre>"
    )
    fence = "```text\n" + "x" * 3600 + "\n```"
    assert len(markdown_to_telegram_chunks(fence)) == 1
    assert markdown_to_telegram_chunks(fence)[0].startswith("<pre>")


def test_long_rich_text_balances_tags_and_counts_astral_characters():
    chunks = markdown_to_telegram_chunks("**" + "word 😀 " * 1600 + "**")
    assert len(chunks) > 1
    for chunk in chunks:
        assert _u16len(chunk) <= 4096
        balanced(chunk)
    assert all(chunk.startswith("<b>") and chunk.endswith("</b>") for chunk in chunks)
    literal = prepare_text("😀" * 3000, markdown=False)
    assert len(literal) == 2
    assert "".join(chunk["text"] for chunk in literal) == "😀" * 3000
    assert all(
        _u16len(chunk["text"]) <= 4096 and not chunk["parse_mode"] for chunk in literal
    )


def test_literal_xml_is_display_content_not_a_hidden_tool_field():
    source = '**Report**\n</caption>\n<parameter name="request_id">example-id'
    literal = prepare_caption(source, markdown=False)
    assert literal == {"text": source, "parse_mode": ""}
    formatted = prepare_caption(source)
    assert formatted["text"].startswith("<b>Report</b>")
    assert _telegram_html_to_plain(formatted["text"]).endswith(
        '</caption>\n<parameter name="request_id">example-id'
    )


def test_caption_limit_is_visible_text_and_fails_without_truncation():
    assert (
        prepare_caption("**" + "a" * 1024 + "**")["text"] == "<b>" + "a" * 1024 + "</b>"
    )
    assert prepare_caption("😀" * 512, markdown=False)["text"] == "😀" * 512
    with pytest.raises(ValueError, match="shorter caption"):
        prepare_caption("😀" * 513)
