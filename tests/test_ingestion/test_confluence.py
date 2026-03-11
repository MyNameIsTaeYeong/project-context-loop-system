"""Confluence 임포트 모듈 테스트 — HTML→MD 변환 위주."""

from __future__ import annotations

from context_loop.ingestion.confluence import _html_to_markdown


def test_html_to_markdown_headings() -> None:
    """h1~h3 태그를 마크다운 헤딩으로 변환한다."""
    html = "<h1>Title</h1><h2>Sub</h2><h3>Sub-sub</h3>"
    md = _html_to_markdown(html)
    assert "# Title" in md
    assert "## Sub" in md
    assert "### Sub-sub" in md


def test_html_to_markdown_bold_italic() -> None:
    """bold/italic 태그를 변환한다."""
    html = "<strong>bold</strong> and <em>italic</em>"
    md = _html_to_markdown(html)
    assert "**bold**" in md
    assert "*italic*" in md


def test_html_to_markdown_link() -> None:
    """앵커 태그를 마크다운 링크로 변환한다."""
    html = '<a href="https://example.com">Example</a>'
    md = _html_to_markdown(html)
    assert "[Example](https://example.com)" in md


def test_html_to_markdown_code() -> None:
    """코드 태그를 변환한다."""
    html = "<code>print('hi')</code>"
    md = _html_to_markdown(html)
    assert "`print('hi')`" in md


def test_html_to_markdown_strips_unknown_tags() -> None:
    """알 수 없는 태그는 제거한다."""
    html = "<div><span>Hello</span></div>"
    md = _html_to_markdown(html)
    assert "<div>" not in md
    assert "<span>" not in md
    assert "Hello" in md


def test_html_to_markdown_entities() -> None:
    """HTML 엔티티를 디코딩한다."""
    html = "&amp; &lt; &gt; &quot; &nbsp;"
    md = _html_to_markdown(html)
    assert "&" in md
    assert "<" in md
    assert ">" in md


def test_html_to_markdown_empty() -> None:
    """빈 HTML은 빈 문자열을 반환한다."""
    assert _html_to_markdown("") == ""
    assert _html_to_markdown("   ") == ""
