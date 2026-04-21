"""Confluence Storage Format HTML → 구조화된 중간 표현 추출 모듈.

한 번의 BeautifulSoup 파싱으로 하류 파이프라인이 소비할 모든 정보를 추출한다.
``html_converter``가 HTML을 마크다운으로 평탄화하기 전에 호출되어
다음 정보를 보존한다:

  - ``sections``: 헤딩 계층 구조 (H1~H6, 섹션 경로, 섹션별 본문 마크다운)
  - ``outbound_links``: 페이지 간 링크, 외부 URL, 첨부파일 링크 (그래프 엣지 원천)
  - ``code_blocks``: ``ac:structured-macro[name=code]`` / ``<pre><code>`` (언어 태그 포함)
  - ``tables``: HTML 테이블 (헤더 + 행 단위 구조화)
  - ``mentions``: ``ri:user``, Jira 이슈 키 등 엔티티 참조
  - ``plain_text``: 마크다운 변환 결과 (임베딩/Classifier 입력용)

각 산출물은 ``in_section`` 경로를 함께 가지고 있어 청크의 ``section_path``
메타를 바로 채울 수 있다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag

from context_loop.ingestion.html_converter import html_to_markdown

_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")
_CODE_MACRO_NAMES = {"code", "noformat"}
_JIRA_MACRO_NAME = "jira"


@dataclass
class Section:
    """문서 내 헤딩 단위 섹션.

    Attributes:
        level: 헤딩 레벨 (1~6).
        title: 헤딩 텍스트.
        anchor: URL fragment 용 앵커 (제목에서 공백 제거·소문자화).
        path: 루트부터 현재 헤딩까지의 제목 경로. 청크 ``section_path``의 원천.
        md_content: 이 섹션 본문(하위 섹션 제외)의 마크다운 변환 결과.
    """

    level: int
    title: str
    anchor: str
    path: list[str]
    md_content: str = ""


@dataclass
class OutLink:
    """문서가 외부로 향하는 링크.

    Attributes:
        kind: ``"page"`` | ``"user"`` | ``"attachment"`` | ``"url"`` | ``"jira"``.
        target_id: 페이지 ID / 사용자 키 / 첨부 파일명 / Jira 키 등.
            ``kind="page"``이고 MCP 응답에 ``content-id``가 없으면 ``None``이 될 수 있다.
        target_title: ``ri:content-title`` 등 표시용 제목 (알 수 있을 때).
        target_space: Confluence 스페이스 키 (페이지 링크일 때).
        anchor_text: 링크 텍스트.
        in_section: 링크가 등장한 섹션 경로.
    """

    kind: str
    target_id: str | None = None
    target_title: str | None = None
    target_space: str | None = None
    anchor_text: str = ""
    in_section: list[str] = field(default_factory=list)


@dataclass
class CodeBlock:
    """코드 블록 (``ac:structured-macro[name=code]`` 또는 ``<pre><code>``).

    Attributes:
        language: 언어 태그 (예: ``"python"``, ``"bash"``). 없으면 빈 문자열.
        content: 코드 본문 텍스트.
        in_section: 코드가 등장한 섹션 경로.
    """

    language: str
    content: str
    in_section: list[str] = field(default_factory=list)


@dataclass
class Table:
    """HTML 테이블.

    Attributes:
        headers: 헤더 셀 텍스트 목록. 없으면 빈 리스트.
        rows: 데이터 행 (각 행은 셀 텍스트 목록).
        in_section: 테이블이 등장한 섹션 경로.
    """

    headers: list[str] = field(default_factory=list)
    rows: list[list[str]] = field(default_factory=list)
    in_section: list[str] = field(default_factory=list)


@dataclass
class Mention:
    """엔티티 참조.

    Attributes:
        kind: ``"user"`` | ``"jira"`` 등.
        ref: 사용자 키, Jira 키 등 식별자.
    """

    kind: str
    ref: str


@dataclass
class ExtractedDocument:
    """Confluence HTML에서 추출한 구조화 중간 표현.

    ``plain_text``만 써도 기존 파이프라인과 호환되며, 나머지 필드는
    청크 분할·그래프 엣지 생성·코드 심볼 매칭 단계에서 소비된다.
    """

    plain_text: str = ""
    sections: list[Section] = field(default_factory=list)
    outbound_links: list[OutLink] = field(default_factory=list)
    code_blocks: list[CodeBlock] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    mentions: list[Mention] = field(default_factory=list)


def extract(html: str) -> ExtractedDocument:
    """Confluence HTML을 파싱하여 구조화된 표현을 반환한다.

    Args:
        html: Confluence Storage Format HTML 문자열.

    Returns:
        ``ExtractedDocument``. 빈 입력이면 빈 문서를 반환한다.
    """
    if not html or not html.strip():
        return ExtractedDocument()

    soup = BeautifulSoup(html, "html.parser")

    sections = _extract_sections(soup)
    outbound_links, mentions = _extract_links_and_mentions(soup, sections)
    code_blocks = _extract_code_blocks(soup, sections)
    tables = _extract_tables(soup, sections)
    plain_text = html_to_markdown(html)

    return ExtractedDocument(
        plain_text=plain_text,
        sections=sections,
        outbound_links=outbound_links,
        code_blocks=code_blocks,
        tables=tables,
        mentions=mentions,
    )


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def _extract_sections(soup: BeautifulSoup) -> list[Section]:
    """헤딩 태그를 스캔해 섹션 목록을 생성한다.

    각 섹션의 ``md_content``는 해당 헤딩 다음부터 동일·상위 레벨 헤딩 직전까지의
    형제 요소들을 모아 마크다운으로 변환한 결과이다.
    """
    headings = soup.find_all(_HEADING_TAGS)
    if not headings:
        return []

    sections: list[Section] = []
    path_stack: list[tuple[int, str]] = []

    for heading in headings:
        level = int(heading.name[1])
        title = heading.get_text(strip=True)
        if not title:
            continue

        while path_stack and path_stack[-1][0] >= level:
            path_stack.pop()
        path_stack.append((level, title))
        path = [t for _, t in path_stack]

        md_content = _section_body_markdown(heading, level)

        sections.append(
            Section(
                level=level,
                title=title,
                anchor=_slugify(title),
                path=path,
                md_content=md_content,
            )
        )

    return sections


def _section_body_markdown(heading: Tag, level: int) -> str:
    """헤딩 다음부터 동일·상위 레벨 헤딩 직전까지의 본문을 마크다운으로 반환한다."""
    parts: list[str] = []
    for sibling in heading.next_siblings:
        if isinstance(sibling, Tag) and sibling.name in _HEADING_TAGS:
            sibling_level = int(sibling.name[1])
            if sibling_level <= level:
                break
        parts.append(str(sibling))

    if not parts:
        return ""
    combined_html = "".join(parts)
    return html_to_markdown(combined_html)


def _slugify(text: str) -> str:
    """헤딩 텍스트를 URL fragment 용 앵커로 변환한다."""
    text = text.lower().strip()
    text = re.sub(r"\s+", "-", text)
    text = re.sub(r"[^\w\-가-힣]", "", text)
    return text


# ---------------------------------------------------------------------------
# Links & mentions
# ---------------------------------------------------------------------------


def _extract_links_and_mentions(
    soup: BeautifulSoup, sections: list[Section]
) -> tuple[list[OutLink], list[Mention]]:
    """페이지/사용자/첨부/외부 링크와 멘션을 추출한다."""
    outbound: list[OutLink] = []
    mentions: list[Mention] = []

    for ac_link in soup.find_all("ac:link"):
        in_section = _locate_section(ac_link, sections)
        page_ref = ac_link.find("ri:page")
        user_ref = ac_link.find("ri:user")
        attachment_ref = ac_link.find("ri:attachment")
        body = ac_link.find("ac:link-body") or ac_link.find(
            "ac:plain-text-link-body"
        )
        anchor_text = body.get_text(strip=True) if body else ""

        if page_ref is not None:
            title = page_ref.get("ri:content-title") or None
            space = page_ref.get("ri:space-key") or None
            content_id = page_ref.get("ri:content-id") or None
            outbound.append(
                OutLink(
                    kind="page",
                    target_id=content_id,
                    target_title=title,
                    target_space=space,
                    anchor_text=anchor_text or (title or ""),
                    in_section=in_section,
                )
            )
        elif user_ref is not None:
            user_key = (
                user_ref.get("ri:userkey")
                or user_ref.get("ri:account-id")
                or ""
            )
            if user_key:
                outbound.append(
                    OutLink(
                        kind="user",
                        target_id=user_key,
                        anchor_text=anchor_text,
                        in_section=in_section,
                    )
                )
                mentions.append(Mention(kind="user", ref=user_key))
        elif attachment_ref is not None:
            filename = attachment_ref.get("ri:filename") or ""
            if filename:
                outbound.append(
                    OutLink(
                        kind="attachment",
                        target_id=filename,
                        anchor_text=anchor_text or filename,
                        in_section=in_section,
                    )
                )

    for anchor in soup.find_all("a"):
        href = anchor.get("href") or ""
        if not href or href.startswith("#"):
            continue
        outbound.append(
            OutLink(
                kind="url",
                target_id=href,
                anchor_text=anchor.get_text(strip=True),
                in_section=_locate_section(anchor, sections),
            )
        )

    for macro in soup.find_all("ac:structured-macro"):
        if (macro.get("ac:name") or "").lower() != _JIRA_MACRO_NAME:
            continue
        key = _get_macro_param(macro, "key")
        if not key:
            continue
        in_section = _locate_section(macro, sections)
        outbound.append(
            OutLink(
                kind="jira",
                target_id=key,
                anchor_text=key,
                in_section=in_section,
            )
        )
        mentions.append(Mention(kind="jira", ref=key))

    return outbound, mentions


# ---------------------------------------------------------------------------
# Code blocks
# ---------------------------------------------------------------------------


def _extract_code_blocks(
    soup: BeautifulSoup, sections: list[Section]
) -> list[CodeBlock]:
    """Confluence ``code``/``noformat`` 매크로와 표준 ``<pre><code>``를 추출한다."""
    blocks: list[CodeBlock] = []

    for macro in soup.find_all("ac:structured-macro"):
        name = (macro.get("ac:name") or "").lower()
        if name not in _CODE_MACRO_NAMES:
            continue
        language = _get_macro_param(macro, "language")
        body = macro.find("ac:plain-text-body") or macro.find("ac:rich-text-body")
        content = body.get_text() if body else macro.get_text()
        if not content.strip():
            continue
        blocks.append(
            CodeBlock(
                language=language,
                content=content,
                in_section=_locate_section(macro, sections),
            )
        )

    for pre in soup.find_all("pre"):
        if pre.find_parent("ac:structured-macro") is not None:
            continue
        code = pre.find("code")
        if code is None:
            continue
        language = _language_from_class(code.get("class"))
        content = code.get_text()
        if not content.strip():
            continue
        blocks.append(
            CodeBlock(
                language=language,
                content=content,
                in_section=_locate_section(pre, sections),
            )
        )

    return blocks


def _language_from_class(classes: list[str] | None) -> str:
    """``<code class="language-python">``의 language 토큰을 추출한다."""
    if not classes:
        return ""
    for cls in classes:
        if cls.startswith("language-"):
            return cls[len("language-") :]
    return ""


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _extract_tables(
    soup: BeautifulSoup, sections: list[Section]
) -> list[Table]:
    """HTML 테이블을 헤더/행 구조로 추출한다."""
    tables: list[Table] = []
    for table in soup.find_all("table"):
        headers: list[str] = []
        rows: list[list[str]] = []

        for tr in table.find_all("tr"):
            cells = tr.find_all(["th", "td"])
            if not cells:
                continue
            texts = [cell.get_text(strip=True) for cell in cells]
            all_th = all(cell.name == "th" for cell in cells)
            if all_th and not headers:
                headers = texts
            else:
                rows.append(texts)

        if not headers and not rows:
            continue
        tables.append(
            Table(
                headers=headers,
                rows=rows,
                in_section=_locate_section(table, sections),
            )
        )
    return tables


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _locate_section(tag: Tag, sections: list[Section]) -> list[str]:
    """DOM상에서 ``tag``보다 앞에 나온 가장 가까운 헤딩의 섹션 경로를 반환한다."""
    if not sections:
        return []
    heading = _find_preceding_heading(tag)
    if heading is None:
        return []
    target_title = heading.get_text(strip=True)
    target_level = int(heading.name[1])
    for section in reversed(sections):
        if section.title == target_title and section.level == target_level:
            return section.path
    return []


def _find_preceding_heading(tag: Tag) -> Tag | None:
    """DOM 순회 순서상 ``tag`` 직전에 등장한 헤딩 태그를 찾는다."""
    for prev in tag.find_all_previous(_HEADING_TAGS):
        return prev
    return None


def _get_macro_param(macro: Tag, param_name: str) -> str:
    """``ac:parameter[ac:name=...]`` 값을 반환한다."""
    for param in macro.find_all("ac:parameter"):
        if param.get("ac:name") == param_name:
            return param.get_text(strip=True)
    return ""
