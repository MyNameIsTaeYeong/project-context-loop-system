"""HTML → Markdown 변환 모듈.

Confluence HTML(Storage Format)을 정리된 마크다운으로 변환한다.
BeautifulSoup으로 Confluence 전용 매크로를 전처리한 뒤
markdownify로 최종 변환한다.

지원 항목:
  - 테이블 (``<table>``) → 마크다운 테이블
  - 중첩 목록 (``<ul>``/``<ol>`` 중첩)
  - Confluence 매크로:
    - info / warning / note / tip 패널 → blockquote
    - code / noformat 매크로 → 코드 블록
    - toc 매크로 → 제거
    - expand 매크로 → 접힌 텍스트 펼침
  - 이미지, 링크, 헤딩, 서식 등 기본 HTML 요소
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, NavigableString, Tag
from markdownify import MarkdownConverter, markdownify


def html_to_markdown(html: str) -> str:
    """HTML을 마크다운으로 변환한다.

    Confluence 매크로를 전처리한 뒤 markdownify로 변환하고
    결과를 정리하여 반환한다.

    Args:
        html: 변환할 HTML 문자열.

    Returns:
        마크다운 형식의 문자열.
    """
    if not html or not html.strip():
        return ""

    soup = BeautifulSoup(html, "html.parser")

    # 1. Confluence 매크로 전처리
    _preprocess_confluence_macros(soup)

    # 2. script/style 태그 제거 (markdownify에 strip과 convert를 동시에 넘길 수 없으므로)
    for tag_name in ("script", "style"):
        for tag in soup.find_all(tag_name):
            tag.decompose()

    # 3. markdownify 변환
    processed_html = str(soup)
    md = markdownify(
        processed_html,
        heading_style="ATX",
    )

    # 4. 후처리
    md = _postprocess(md)

    return md.strip()


# ---------------------------------------------------------------------------
# Confluence 매크로 전처리
# ---------------------------------------------------------------------------

_PANEL_MACRO_NAMES = {"info", "warning", "note", "tip", "panel"}
_CODE_MACRO_NAMES = {"code", "noformat"}
_REMOVE_MACRO_NAMES = {"toc", "toc-zone", "children", "recently-updated"}

_PANEL_EMOJI = {
    "info": "ℹ️",
    "warning": "⚠️",
    "note": "📝",
    "tip": "💡",
    "panel": "",
}


def _preprocess_confluence_macros(soup: BeautifulSoup) -> None:
    """Confluence ``ac:structured-macro`` 태그를 표준 HTML로 치환한다."""
    for macro in soup.find_all("ac:structured-macro"):
        macro_name = (macro.get("ac:name") or "").lower()

        if macro_name in _PANEL_MACRO_NAMES:
            _convert_panel_macro(soup, macro, macro_name)
        elif macro_name in _CODE_MACRO_NAMES:
            _convert_code_macro(soup, macro, macro_name)
        elif macro_name == "expand":
            _convert_expand_macro(soup, macro)
        elif macro_name in _REMOVE_MACRO_NAMES:
            macro.decompose()
        else:
            # 알 수 없는 매크로: 본문만 남김
            _unwrap_macro_body(macro)

    # ac:image → img 태그 변환
    for img_tag in soup.find_all("ac:image"):
        _convert_ac_image(soup, img_tag)

    # ac:link → a 태그 변환
    for link_tag in soup.find_all("ac:link"):
        _convert_ac_link(soup, link_tag)

    # ri:attachment 등 남은 Confluence 네임스페이스 태그 정리
    for tag in soup.find_all(re.compile(r"^(ac|ri):")):
        tag.unwrap()


def _get_macro_body(macro: Tag) -> str:
    """매크로의 ``ac:rich-text-body`` 또는 ``ac:plain-text-body`` 내용을 반환한다."""
    for body_tag_name in ("ac:rich-text-body", "ac:plain-text-body"):
        body = macro.find(body_tag_name)
        if body:
            return body.decode_contents()
    return macro.decode_contents()


def _get_macro_param(macro: Tag, param_name: str) -> str:
    """매크로 파라미터 값을 반환한다."""
    for param in macro.find_all("ac:parameter"):
        if param.get("ac:name") == param_name:
            return param.get_text(strip=True)
    return ""


def _convert_panel_macro(
    soup: BeautifulSoup, macro: Tag, macro_name: str,
) -> None:
    """패널 매크로(info/warning/note/tip)를 blockquote로 변환한다."""
    title = _get_macro_param(macro, "title")
    body_html = _get_macro_body(macro)
    emoji = _PANEL_EMOJI.get(macro_name, "")

    bq = soup.new_tag("blockquote")
    # 제목이 있으면 추가
    if title:
        header = soup.new_tag("strong")
        header.string = f"{emoji} {title}".strip()
        bq.append(header)
        bq.append(soup.new_tag("br"))
    elif emoji:
        label = soup.new_tag("strong")
        label.string = f"{emoji} {macro_name.upper()}"
        bq.append(label)
        bq.append(soup.new_tag("br"))

    body_soup = BeautifulSoup(body_html, "html.parser")
    for child in list(body_soup.children):
        bq.append(child)

    macro.replace_with(bq)


def _convert_code_macro(
    soup: BeautifulSoup, macro: Tag, macro_name: str,
) -> None:
    """code/noformat 매크로를 ``<pre><code>`` 블록으로 변환한다."""
    language = _get_macro_param(macro, "language") or ""
    body_tag = macro.find("ac:plain-text-body") or macro.find("ac:rich-text-body")
    code_text = body_tag.get_text() if body_tag else macro.get_text()

    pre = soup.new_tag("pre")
    code = soup.new_tag("code", attrs={"class": f"language-{language}"} if language else {})
    code.string = code_text
    pre.append(code)

    macro.replace_with(pre)


def _convert_expand_macro(soup: BeautifulSoup, macro: Tag) -> None:
    """expand 매크로의 본문을 펼쳐서 표시한다."""
    title = _get_macro_param(macro, "title") or _get_macro_param(macro, "")
    body_html = _get_macro_body(macro)

    wrapper = soup.new_tag("div")
    if title:
        summary = soup.new_tag("p")
        strong = soup.new_tag("strong")
        strong.string = title
        summary.append(strong)
        wrapper.append(summary)

    body_soup = BeautifulSoup(body_html, "html.parser")
    for child in list(body_soup.children):
        wrapper.append(child)

    macro.replace_with(wrapper)


def _unwrap_macro_body(macro: Tag) -> None:
    """알 수 없는 매크로의 본문을 꺼내고 매크로 태그를 제거한다."""
    body_html = _get_macro_body(macro)
    if body_html.strip():
        replacement = BeautifulSoup(body_html, "html.parser")
        macro.replace_with(replacement)
    else:
        macro.decompose()


def _convert_ac_image(soup: BeautifulSoup, img_tag: Tag) -> None:
    """``ac:image`` 태그를 표준 ``<img>``로 변환한다."""
    attachment = img_tag.find("ri:attachment")
    url_tag = img_tag.find("ri:url")

    src = ""
    alt = img_tag.get("ac:alt", "")

    if attachment:
        filename = attachment.get("ri:filename", "")
        src = filename
        if not alt:
            alt = filename
    elif url_tag:
        src = url_tag.get("ri:value", "")

    new_img = soup.new_tag("img", src=src, alt=alt)
    img_tag.replace_with(new_img)


def _convert_ac_link(soup: BeautifulSoup, link_tag: Tag) -> None:
    """``ac:link`` 태그를 표준 ``<a>``로 변환한다."""
    page_ref = link_tag.find("ri:page")
    attachment = link_tag.find("ri:attachment")
    link_body = link_tag.find("ac:link-body") or link_tag.find("ac:plain-text-link-body")

    text = link_body.get_text(strip=True) if link_body else ""
    href = ""

    if page_ref:
        title = page_ref.get("ri:content-title", "")
        href = title
        if not text:
            text = title
    elif attachment:
        filename = attachment.get("ri:filename", "")
        href = filename
        if not text:
            text = filename

    if not text:
        text = href or "link"

    new_a = soup.new_tag("a", href=href)
    new_a.string = text
    link_tag.replace_with(new_a)


# ---------------------------------------------------------------------------
# 후처리
# ---------------------------------------------------------------------------


def _postprocess(md: str) -> str:
    """변환 결과를 정리한다."""
    # 연속 빈 줄을 최대 2줄로 축소
    md = re.sub(r"\n{3,}", "\n\n", md)
    # 줄 끝 공백 제거
    md = re.sub(r"[ \t]+$", "", md, flags=re.MULTILINE)
    return md
