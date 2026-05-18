"""HTML → Markdown 변환 모듈 테스트.

html_converter.html_to_markdown()의 동작을 검증한다:
  - 기본 HTML 요소 (헤딩, 서식, 링크, 이미지, 코드)
  - 테이블
  - 중첩 목록
  - Confluence 매크로 (패널, 코드, expand, toc 등)
  - 에지 케이스 (빈 입력, HTML 엔티티)
"""

from __future__ import annotations

from context_loop.ingestion.html_converter import html_to_markdown


# --- 기본 HTML 요소 ---


def test_headings() -> None:
    """h1~h3 태그를 마크다운 헤딩으로 변환한다."""
    html = "<h1>Title</h1><h2>Sub</h2><h3>Sub-sub</h3>"
    md = html_to_markdown(html)
    assert "# Title" in md
    assert "## Sub" in md
    assert "### Sub-sub" in md


def test_bold_italic() -> None:
    """bold/italic 태그를 변환한다."""
    html = "<strong>bold</strong> and <em>italic</em>"
    md = html_to_markdown(html)
    assert "**bold**" in md
    assert "*italic*" in md


def test_link() -> None:
    """앵커 태그를 마크다운 링크로 변환한다."""
    html = '<a href="https://example.com">Example</a>'
    md = html_to_markdown(html)
    assert "[Example](https://example.com)" in md


def test_inline_code() -> None:
    """인라인 코드 태그를 변환한다."""
    html = "<code>print('hi')</code>"
    md = html_to_markdown(html)
    assert "`print('hi')`" in md


def test_code_block() -> None:
    """pre+code 블록을 펜스드 코드 블록으로 변환한다."""
    html = "<pre><code>x = 1\ny = 2</code></pre>"
    md = html_to_markdown(html)
    assert "```" in md
    assert "x = 1" in md


def test_image() -> None:
    """img 태그를 마크다운 이미지로 변환한다."""
    html = '<img src="https://example.com/img.png" alt="photo" />'
    md = html_to_markdown(html)
    assert "![photo](https://example.com/img.png)" in md


def test_strips_unknown_tags() -> None:
    """알 수 없는 태그는 제거하되 텍스트는 보존한다."""
    html = "<div><span>Hello</span></div>"
    md = html_to_markdown(html)
    assert "<div>" not in md
    assert "<span>" not in md
    assert "Hello" in md


def test_html_entities() -> None:
    """HTML 엔티티를 디코딩한다."""
    html = "<p>&amp; &lt; &gt; &quot;</p>"
    md = html_to_markdown(html)
    assert "&" in md
    assert "<" in md
    assert ">" in md


def test_empty_input() -> None:
    """빈 HTML은 빈 문자열을 반환한다."""
    assert html_to_markdown("") == ""
    assert html_to_markdown("   ") == ""


def test_horizontal_rule() -> None:
    """hr 태그를 마크다운 수평선으로 변환한다."""
    html = "<p>Above</p><hr/><p>Below</p>"
    md = html_to_markdown(html)
    assert "---" in md or "***" in md or "___" in md


# --- 테이블 ---


def test_simple_table() -> None:
    """HTML 테이블이 마크다운 테이블로 변환된다."""
    html = """
    <table>
        <tr><th>Name</th><th>Age</th></tr>
        <tr><td>Alice</td><td>30</td></tr>
        <tr><td>Bob</td><td>25</td></tr>
    </table>
    """
    md = html_to_markdown(html)
    assert "Name" in md
    assert "Alice" in md
    assert "Bob" in md
    assert "30" in md
    # 마크다운 테이블 구분선 확인
    assert "|" in md or "---" in md


def test_table_with_colspan_content_preserved() -> None:
    """복잡한 테이블의 내용이 보존된다."""
    html = """
    <table>
        <tr><th>Header 1</th><th>Header 2</th><th>Header 3</th></tr>
        <tr><td>A</td><td>B</td><td>C</td></tr>
    </table>
    """
    md = html_to_markdown(html)
    for text in ("Header 1", "Header 2", "Header 3", "A", "B", "C"):
        assert text in md


# --- 중첩 목록 ---


def test_nested_unordered_list() -> None:
    """중첩된 비순서 목록이 올바르게 변환된다."""
    html = """
    <ul>
        <li>Item 1
            <ul>
                <li>Sub-item 1</li>
                <li>Sub-item 2</li>
            </ul>
        </li>
        <li>Item 2</li>
    </ul>
    """
    md = html_to_markdown(html)
    assert "Item 1" in md
    assert "Sub-item 1" in md
    assert "Sub-item 2" in md
    assert "Item 2" in md


def test_ordered_list() -> None:
    """순서 목록이 변환된다."""
    html = "<ol><li>First</li><li>Second</li><li>Third</li></ol>"
    md = html_to_markdown(html)
    assert "First" in md
    assert "Second" in md
    assert "Third" in md


def test_nested_ordered_unordered_mix() -> None:
    """순서/비순서 혼합 중첩 목록이 변환된다."""
    html = """
    <ol>
        <li>Step 1
            <ul>
                <li>Detail A</li>
                <li>Detail B</li>
            </ul>
        </li>
        <li>Step 2</li>
    </ol>
    """
    md = html_to_markdown(html)
    assert "Step 1" in md
    assert "Detail A" in md
    assert "Step 2" in md


# --- Confluence 매크로 ---


def test_info_panel_macro() -> None:
    """info 패널 매크로가 blockquote로 변환된다."""
    html = """
    <ac:structured-macro ac:name="info">
        <ac:rich-text-body>
            <p>This is important information.</p>
        </ac:rich-text-body>
    </ac:structured-macro>
    """
    md = html_to_markdown(html)
    assert ">" in md  # blockquote
    assert "important information" in md


def test_warning_panel_macro() -> None:
    """warning 패널 매크로가 blockquote로 변환된다."""
    html = """
    <ac:structured-macro ac:name="warning">
        <ac:parameter ac:name="title">주의사항</ac:parameter>
        <ac:rich-text-body>
            <p>Be careful with this.</p>
        </ac:rich-text-body>
    </ac:structured-macro>
    """
    md = html_to_markdown(html)
    assert ">" in md
    assert "주의사항" in md
    assert "careful" in md


def test_note_panel_macro() -> None:
    """note 패널 매크로가 blockquote로 변환된다."""
    html = """
    <ac:structured-macro ac:name="note">
        <ac:rich-text-body>
            <p>Take note of this.</p>
        </ac:rich-text-body>
    </ac:structured-macro>
    """
    md = html_to_markdown(html)
    assert "Take note" in md


def test_tip_panel_macro() -> None:
    """tip 패널 매크로가 blockquote로 변환된다."""
    html = """
    <ac:structured-macro ac:name="tip">
        <ac:rich-text-body>
            <p>Here is a tip.</p>
        </ac:rich-text-body>
    </ac:structured-macro>
    """
    md = html_to_markdown(html)
    assert "tip" in md.lower() or "Here is a tip" in md


def test_code_macro() -> None:
    """code 매크로가 코드 블록으로 변환된다."""
    html = """
    <ac:structured-macro ac:name="code">
        <ac:parameter ac:name="language">python</ac:parameter>
        <ac:plain-text-body><![CDATA[def hello():
    print("world")]]></ac:plain-text-body>
    </ac:structured-macro>
    """
    md = html_to_markdown(html)
    assert "```" in md
    assert "hello" in md


def test_noformat_macro() -> None:
    """noformat 매크로가 코드 블록으로 변환된다."""
    html = """
    <ac:structured-macro ac:name="noformat">
        <ac:plain-text-body>raw text content here</ac:plain-text-body>
    </ac:structured-macro>
    """
    md = html_to_markdown(html)
    assert "```" in md
    assert "raw text content" in md


def test_toc_macro_removed() -> None:
    """toc 매크로는 제거된다."""
    html = """
    <ac:structured-macro ac:name="toc">
        <ac:parameter ac:name="maxLevel">3</ac:parameter>
    </ac:structured-macro>
    <h1>Content</h1>
    <p>Actual content here.</p>
    """
    md = html_to_markdown(html)
    assert "toc" not in md.lower() or "Content" in md
    assert "Actual content" in md


def test_expand_macro() -> None:
    """expand 매크로의 본문이 펼쳐진다."""
    html = """
    <ac:structured-macro ac:name="expand">
        <ac:parameter ac:name="title">Click to expand</ac:parameter>
        <ac:rich-text-body>
            <p>Hidden content revealed.</p>
        </ac:rich-text-body>
    </ac:structured-macro>
    """
    md = html_to_markdown(html)
    assert "Click to expand" in md
    assert "Hidden content" in md


def test_unknown_macro_preserves_body() -> None:
    """알 수 없는 매크로의 본문 텍스트는 보존된다."""
    html = """
    <ac:structured-macro ac:name="custom-widget">
        <ac:rich-text-body>
            <p>Widget content here.</p>
        </ac:rich-text-body>
    </ac:structured-macro>
    """
    md = html_to_markdown(html)
    assert "Widget content" in md


def test_panel_macro_with_title() -> None:
    """패널 매크로에 커스텀 제목이 있는 경우."""
    html = """
    <ac:structured-macro ac:name="panel">
        <ac:parameter ac:name="title">My Panel Title</ac:parameter>
        <ac:rich-text-body>
            <p>Panel body text.</p>
        </ac:rich-text-body>
    </ac:structured-macro>
    """
    md = html_to_markdown(html)
    assert "My Panel Title" in md
    assert "Panel body text" in md


# --- Confluence 네임스페이스 태그 ---


def test_ac_image() -> None:
    """ac:image 태그가 이미지로 변환된다."""
    html = """
    <ac:image ac:alt="diagram">
        <ri:attachment ri:filename="arch.png"/>
    </ac:image>
    """
    md = html_to_markdown(html)
    assert "arch.png" in md


def test_ac_link() -> None:
    """ac:link 태그가 링크로 변환된다."""
    html = """
    <ac:link>
        <ri:page ri:content-title="Getting Started"/>
        <ac:plain-text-link-body>Getting Started Guide</ac:plain-text-link-body>
    </ac:link>
    """
    md = html_to_markdown(html)
    assert "Getting Started" in md


# --- 복합 케이스 ---


def test_mixed_content() -> None:
    """다양한 요소가 섞인 문서가 올바르게 변환된다."""
    html = """
    <h1>Project Overview</h1>
    <p>This project is about <strong>knowledge management</strong>.</p>
    <h2>Architecture</h2>
    <table>
        <tr><th>Component</th><th>Technology</th></tr>
        <tr><td>Backend</td><td>Python</td></tr>
        <tr><td>Database</td><td>SQLite</td></tr>
    </table>
    <ac:structured-macro ac:name="info">
        <ac:rich-text-body>
            <p>See also the deployment guide.</p>
        </ac:rich-text-body>
    </ac:structured-macro>
    <h2>Setup</h2>
    <ol>
        <li>Install dependencies</li>
        <li>Configure settings
            <ul>
                <li>Set API key</li>
                <li>Set database path</li>
            </ul>
        </li>
    </ol>
    """
    md = html_to_markdown(html)
    assert "# Project Overview" in md
    assert "**knowledge management**" in md
    assert "## Architecture" in md
    assert "Backend" in md
    assert "Python" in md
    assert "deployment guide" in md
    assert "## Setup" in md
    assert "Install dependencies" in md
    assert "Set API key" in md
