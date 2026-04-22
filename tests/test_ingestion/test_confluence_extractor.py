"""Confluence Storage Format 추출기 테스트.

confluence_extractor.extract()가 아래 산출물을 올바르게 추출하는지 검증한다:
  - sections: 헤딩 레벨, 경로, 앵커, 섹션별 본문
  - outbound_links: 페이지/사용자/첨부/외부/Jira 링크
  - code_blocks: 언어 태그와 섹션 경로
  - tables: 헤더/행 구조
  - mentions: 사용자, Jira 엔티티 참조
  - 에지 케이스: 빈 입력, 헤딩 없는 문서, content-id 없는 페이지 링크
"""

from __future__ import annotations

from context_loop.ingestion.confluence_extractor import (
    ExtractedDocument,
    extract,
)

# ---------------------------------------------------------------------------
# 고정 샘플: 가상의 "결제 시스템 아키텍처" 페이지
# ---------------------------------------------------------------------------

PAYMENT_PAGE_HTML = """
<h1>결제 시스템 아키텍처</h1>
<p>전반적 구조를 설명한다. 인증은
  <ac:link>
    <ri:page ri:content-title="인증 서비스 설계" ri:space-key="ARCH"/>
    <ac:plain-text-link-body><![CDATA[인증 서비스 설계]]></ac:plain-text-link-body>
  </ac:link>
  참고.
</p>

<h2>주요 엔드포인트</h2>
<table><tbody>
  <tr><th>Method</th><th>Path</th><th>설명</th></tr>
  <tr><td>POST</td><td>/v1/payments</td><td>결제 생성</td></tr>
  <tr><td>GET</td><td>/v1/payments/{id}</td><td>결제 조회</td></tr>
</tbody></table>

<h2>요청 예시</h2>
<ac:structured-macro ac:name="code">
  <ac:parameter ac:name="language">bash</ac:parameter>
  <ac:plain-text-body><![CDATA[curl -X POST https://api.example.com/v1/payments \\
  -H "Authorization: Bearer $TOKEN" -d '{"amount": 10000}']]></ac:plain-text-body>
</ac:structured-macro>

<ac:structured-macro ac:name="info">
  <ac:rich-text-body><p>테스트 환경에서는 토큰 생략 가능.</p></ac:rich-text-body>
</ac:structured-macro>

<h2>관련 이슈</h2>
<ul><li>
  <ac:structured-macro ac:name="jira">
    <ac:parameter ac:name="key">PAY-1234</ac:parameter>
  </ac:structured-macro>
</li></ul>

<h2>담당자</h2>
<p><ac:link><ri:user ri:userkey="uk_abc123"/></ac:link> 가 주 담당.</p>
"""


# --- 기본 ---


def test_empty_html_returns_empty_document() -> None:
    """빈 입력은 기본값으로 채워진 빈 ``ExtractedDocument``를 반환한다."""
    doc = extract("")
    assert isinstance(doc, ExtractedDocument)
    assert doc.plain_text == ""
    assert doc.sections == []
    assert doc.outbound_links == []
    assert doc.code_blocks == []
    assert doc.tables == []
    assert doc.mentions == []


def test_whitespace_only_returns_empty_document() -> None:
    """공백만 있는 입력도 빈 문서로 처리한다."""
    assert extract("   \n\t ") == ExtractedDocument()


def test_plain_text_uses_markdown_converter() -> None:
    """``plain_text``는 ``html_to_markdown`` 결과를 담는다."""
    doc = extract("<h1>Hello</h1><p>world</p>")
    assert "# Hello" in doc.plain_text
    assert "world" in doc.plain_text


# --- 섹션 ---


def test_sections_capture_hierarchy() -> None:
    """헤딩 레벨에 따라 섹션 경로가 누적된다."""
    html = "<h1>A</h1><p>a</p><h2>B</h2><p>b</p><h3>C</h3><p>c</p><h2>D</h2>"
    doc = extract(html)
    assert [s.title for s in doc.sections] == ["A", "B", "C", "D"]
    assert [s.level for s in doc.sections] == [1, 2, 3, 2]
    assert doc.sections[0].path == ["A"]
    assert doc.sections[1].path == ["A", "B"]
    assert doc.sections[2].path == ["A", "B", "C"]
    assert doc.sections[3].path == ["A", "D"]  # H3 C는 H2 D에서 pop


def test_section_md_content_stops_at_same_or_higher_heading() -> None:
    """섹션 본문은 다음 동일·상위 레벨 헤딩 전까지만 포함한다."""
    html = (
        "<h2>A</h2><p>alpha</p>"
        "<h3>A-sub</h3><p>alpha-sub</p>"
        "<h2>B</h2><p>beta</p>"
    )
    doc = extract(html)
    sec_a = next(s for s in doc.sections if s.title == "A")
    sec_b = next(s for s in doc.sections if s.title == "B")
    assert "alpha" in sec_a.md_content
    assert "alpha-sub" in sec_a.md_content  # H3 하위는 포함
    assert "beta" not in sec_a.md_content  # 다음 H2는 미포함
    assert "beta" in sec_b.md_content


def test_heading_anchor_is_slugified() -> None:
    """한글/공백을 포함한 헤딩도 앵커로 변환된다."""
    doc = extract("<h2>주요 엔드포인트</h2>")
    section = doc.sections[0]
    assert section.anchor == "주요-엔드포인트"


def test_document_without_headings_has_empty_sections() -> None:
    """헤딩이 없으면 ``sections``는 빈 리스트."""
    doc = extract("<p>no headings here</p>")
    assert doc.sections == []


# --- 페이지 링크 ---


def test_page_link_extracts_title_and_space() -> None:
    """``ri:page`` 링크에서 제목/스페이스/앵커 텍스트를 추출한다."""
    html = """
    <p>see <ac:link>
      <ri:page ri:content-title="인증 서비스 설계" ri:space-key="ARCH"/>
      <ac:plain-text-link-body><![CDATA[인증 서비스 설계]]></ac:plain-text-link-body>
    </ac:link></p>
    """
    doc = extract(html)
    links = [link for link in doc.outbound_links if link.kind == "page"]
    assert len(links) == 1
    link = links[0]
    assert link.target_title == "인증 서비스 설계"
    assert link.target_space == "ARCH"
    assert link.anchor_text == "인증 서비스 설계"
    assert link.target_id is None  # content-id 없는 경우


def test_page_link_with_content_id_is_captured() -> None:
    """``ri:content-id``가 있으면 ``target_id``에 담긴다."""
    html = """
    <ac:link>
      <ri:page ri:content-id="123456" ri:content-title="X"/>
    </ac:link>
    """
    doc = extract(html)
    link = doc.outbound_links[0]
    assert link.kind == "page"
    assert link.target_id == "123456"
    assert link.target_title == "X"


# --- 사용자 멘션 ---


def test_user_mention_emits_outlink_and_mention() -> None:
    """``ri:user`` 링크는 outbound_link와 mention에 모두 기록된다."""
    html = '<p><ac:link><ri:user ri:userkey="uk_abc"/></ac:link></p>'
    doc = extract(html)
    user_links = [link for link in doc.outbound_links if link.kind == "user"]
    assert len(user_links) == 1
    assert user_links[0].target_id == "uk_abc"
    assert doc.mentions == [
        type(doc.mentions[0])(kind="user", ref="uk_abc")
    ]


def test_user_account_id_fallback() -> None:
    """``ri:userkey``가 없으면 ``ri:account-id``를 사용한다."""
    html = '<p><ac:link><ri:user ri:account-id="acc-999"/></ac:link></p>'
    doc = extract(html)
    user_links = [link for link in doc.outbound_links if link.kind == "user"]
    assert user_links[0].target_id == "acc-999"


# --- 첨부/외부 링크 ---


def test_attachment_link_is_captured() -> None:
    """``ri:attachment`` 링크를 추출한다."""
    html = """
    <ac:link>
      <ri:attachment ri:filename="diagram.png"/>
      <ac:plain-text-link-body><![CDATA[다이어그램]]></ac:plain-text-link-body>
    </ac:link>
    """
    doc = extract(html)
    att = [link for link in doc.outbound_links if link.kind == "attachment"]
    assert len(att) == 1
    assert att[0].target_id == "diagram.png"
    assert att[0].anchor_text == "다이어그램"


def test_plain_anchor_tag_is_captured_as_url() -> None:
    """표준 ``<a href>`` 링크는 kind='url'로 기록된다."""
    html = '<p><a href="https://example.com/docs">docs</a></p>'
    doc = extract(html)
    url_links = [link for link in doc.outbound_links if link.kind == "url"]
    assert len(url_links) == 1
    assert url_links[0].target_id == "https://example.com/docs"
    assert url_links[0].anchor_text == "docs"


def test_fragment_only_anchor_is_skipped() -> None:
    """``#heading`` 같은 문서 내부 링크는 수집하지 않는다."""
    html = '<p><a href="#section">go</a></p>'
    doc = extract(html)
    assert [link for link in doc.outbound_links if link.kind == "url"] == []


# --- Jira ---


def test_jira_macro_emits_outlink_and_mention() -> None:
    """Jira 매크로는 outbound_link와 mention 양쪽에 기록된다."""
    html = """
    <ac:structured-macro ac:name="jira">
      <ac:parameter ac:name="key">PAY-1234</ac:parameter>
    </ac:structured-macro>
    """
    doc = extract(html)
    jira = [link for link in doc.outbound_links if link.kind == "jira"]
    assert len(jira) == 1
    assert jira[0].target_id == "PAY-1234"
    assert any(m.kind == "jira" and m.ref == "PAY-1234" for m in doc.mentions)


def test_jira_without_key_is_skipped() -> None:
    """``key`` 파라미터가 없는 Jira 매크로는 무시한다."""
    html = '<ac:structured-macro ac:name="jira"></ac:structured-macro>'
    doc = extract(html)
    assert [link for link in doc.outbound_links if link.kind == "jira"] == []


# --- 코드 블록 ---


def test_code_macro_preserves_language_and_body() -> None:
    """``ac:structured-macro[name=code]``의 언어 태그와 본문을 보존한다."""
    html = """
    <ac:structured-macro ac:name="code">
      <ac:parameter ac:name="language">python</ac:parameter>
      <ac:plain-text-body><![CDATA[def f():
    return 1]]></ac:plain-text-body>
    </ac:structured-macro>
    """
    doc = extract(html)
    assert len(doc.code_blocks) == 1
    block = doc.code_blocks[0]
    assert block.language == "python"
    assert "def f():" in block.content
    assert "return 1" in block.content


def test_code_block_without_language() -> None:
    """언어 태그가 없으면 language는 빈 문자열."""
    html = """
    <ac:structured-macro ac:name="code">
      <ac:plain-text-body><![CDATA[plain]]></ac:plain-text-body>
    </ac:structured-macro>
    """
    doc = extract(html)
    assert doc.code_blocks[0].language == ""


def test_noformat_macro_is_captured() -> None:
    """``noformat`` 매크로도 코드 블록으로 추출된다."""
    html = """
    <ac:structured-macro ac:name="noformat">
      <ac:plain-text-body><![CDATA[raw text]]></ac:plain-text-body>
    </ac:structured-macro>
    """
    doc = extract(html)
    assert len(doc.code_blocks) == 1
    assert doc.code_blocks[0].content.strip() == "raw text"


def test_standard_pre_code_is_captured() -> None:
    """Confluence 매크로가 아닌 표준 ``<pre><code>``도 수집한다."""
    html = '<pre><code class="language-go">package main</code></pre>'
    doc = extract(html)
    assert len(doc.code_blocks) == 1
    assert doc.code_blocks[0].language == "go"
    assert "package main" in doc.code_blocks[0].content


def test_pre_code_inside_macro_not_double_counted() -> None:
    """매크로 내부 ``<pre>``는 표준 경로에서 재추출하지 않는다.

    BeautifulSoup 파싱 후 매크로가 원자적으로 남아있을 때 중복 추출을 방지한다.
    """
    html = """
    <ac:structured-macro ac:name="code">
      <ac:parameter ac:name="language">sh</ac:parameter>
      <ac:plain-text-body><pre><code>echo hi</code></pre></ac:plain-text-body>
    </ac:structured-macro>
    """
    doc = extract(html)
    assert len(doc.code_blocks) == 1


# --- 테이블 ---


def test_table_headers_and_rows() -> None:
    """헤더 행과 데이터 행을 분리한다."""
    html = """
    <table><tbody>
      <tr><th>A</th><th>B</th></tr>
      <tr><td>1</td><td>2</td></tr>
      <tr><td>3</td><td>4</td></tr>
    </tbody></table>
    """
    doc = extract(html)
    assert len(doc.tables) == 1
    table = doc.tables[0]
    assert table.headers == ["A", "B"]
    assert table.rows == [["1", "2"], ["3", "4"]]


def test_table_without_explicit_headers() -> None:
    """``<th>``가 없는 테이블은 모든 행이 ``rows``로 들어간다."""
    html = "<table><tr><td>x</td><td>y</td></tr></table>"
    doc = extract(html)
    table = doc.tables[0]
    assert table.headers == []
    assert table.rows == [["x", "y"]]


# --- in_section 경로 연동 ---


def test_payment_page_end_to_end() -> None:
    """전체 샘플 페이지에서 모든 산출물과 section 경로가 올바르게 채워진다."""
    doc = extract(PAYMENT_PAGE_HTML)

    # sections
    titles = [s.title for s in doc.sections]
    assert titles == [
        "결제 시스템 아키텍처",
        "주요 엔드포인트",
        "요청 예시",
        "관련 이슈",
        "담당자",
    ]
    root = doc.sections[0]
    endpoint = next(s for s in doc.sections if s.title == "주요 엔드포인트")
    assert root.path == ["결제 시스템 아키텍처"]
    assert endpoint.path == ["결제 시스템 아키텍처", "주요 엔드포인트"]

    # page link: H1 서론 섹션
    page_links = [link for link in doc.outbound_links if link.kind == "page"]
    assert len(page_links) == 1
    assert page_links[0].target_title == "인증 서비스 설계"
    assert page_links[0].in_section == ["결제 시스템 아키텍처"]

    # Jira
    jira_links = [link for link in doc.outbound_links if link.kind == "jira"]
    assert len(jira_links) == 1
    assert jira_links[0].target_id == "PAY-1234"
    assert jira_links[0].in_section == ["결제 시스템 아키텍처", "관련 이슈"]

    # user mention
    user_links = [link for link in doc.outbound_links if link.kind == "user"]
    assert user_links[0].target_id == "uk_abc123"
    assert user_links[0].in_section == ["결제 시스템 아키텍처", "담당자"]

    # code block
    assert len(doc.code_blocks) == 1
    block = doc.code_blocks[0]
    assert block.language == "bash"
    assert "curl -X POST" in block.content
    assert block.in_section == ["결제 시스템 아키텍처", "요청 예시"]

    # table
    assert len(doc.tables) == 1
    table = doc.tables[0]
    assert table.headers == ["Method", "Path", "설명"]
    assert table.rows == [
        ["POST", "/v1/payments", "결제 생성"],
        ["GET", "/v1/payments/{id}", "결제 조회"],
    ]
    assert table.in_section == ["결제 시스템 아키텍처", "주요 엔드포인트"]

    # plain_text는 마크다운으로 변환돼 있어야 함
    assert "# 결제 시스템 아키텍처" in doc.plain_text
    assert "## 주요 엔드포인트" in doc.plain_text
