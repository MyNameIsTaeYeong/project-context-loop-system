"""parser 모듈 테스트."""

from pathlib import Path

from context_loop.processor.parser import (
    compute_content_hash,
    extract_title_from_content,
    html_to_markdown,
    is_supported_file,
    normalize_content,
)


def test_is_supported_file() -> None:
    assert is_supported_file(Path("doc.md")) is True
    assert is_supported_file(Path("doc.txt")) is True
    assert is_supported_file(Path("doc.html")) is True
    assert is_supported_file(Path("doc.HTML")) is True
    assert is_supported_file(Path("doc.pdf")) is False
    assert is_supported_file(Path("doc.docx")) is False


def test_compute_content_hash() -> None:
    h1 = compute_content_hash("hello")
    h2 = compute_content_hash("hello")
    h3 = compute_content_hash("world")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 64  # SHA-256


def test_html_to_markdown_headings() -> None:
    html = "<h1>Title</h1><h2>Subtitle</h2>"
    md = html_to_markdown(html)
    assert "# Title" in md
    assert "## Subtitle" in md


def test_html_to_markdown_formatting() -> None:
    html = "<strong>bold</strong> and <em>italic</em>"
    md = html_to_markdown(html)
    assert "**bold**" in md
    assert "*italic*" in md


def test_html_to_markdown_links() -> None:
    html = '<a href="https://example.com">link</a>'
    md = html_to_markdown(html)
    assert "[link](https://example.com)" in md


def test_html_to_markdown_lists() -> None:
    html = "<ul><li>item1</li><li>item2</li></ul>"
    md = html_to_markdown(html)
    assert "- item1" in md
    assert "- item2" in md


def test_html_to_markdown_entities() -> None:
    html = "&amp; &lt; &gt; &quot;"
    md = html_to_markdown(html)
    assert "& < > \"" == md


def test_normalize_content_html() -> None:
    result = normalize_content("<p>Hello</p>", "html")
    assert "Hello" in result
    assert "<p>" not in result


def test_normalize_content_md() -> None:
    result = normalize_content("  # Hello  ", "md")
    assert result == "# Hello"


def test_extract_title_from_heading() -> None:
    content = "# My Document\n\nSome content here"
    assert extract_title_from_content(content) == "My Document"


def test_extract_title_no_heading() -> None:
    content = "Just a plain text line\nAnother line"
    assert extract_title_from_content(content) == "Just a plain text line"


def test_extract_title_empty() -> None:
    assert extract_title_from_content("") == "Untitled"
