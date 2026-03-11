"""문서 파싱 및 정규화 모듈.

HTML, 마크다운, 텍스트 파일을 파싱하여 정규화된 마크다운 텍스트로 변환한다.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path


_SUPPORTED_EXTENSIONS = {".md", ".txt", ".html", ".htm"}


def is_supported_file(path: Path) -> bool:
    """지원되는 파일 형식인지 확인한다."""
    return path.suffix.lower() in _SUPPORTED_EXTENSIONS


def compute_content_hash(content: str) -> str:
    """콘텐츠의 SHA-256 해시를 반환한다."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def html_to_markdown(html: str) -> str:
    """간단한 HTML → 마크다운 변환.

    외부 라이브러리(markdownify 등) 없이 기본 태그를 변환한다.
    복잡한 HTML은 추후 markdownify 등으로 교체 가능.
    """
    text = html

    # <br> → 줄바꿈
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)

    # 헤딩
    for level in range(6, 0, -1):
        tag = f"h{level}"
        text = re.sub(
            rf"<{tag}[^>]*>(.*?)</{tag}>",
            lambda m, lv=level: f"\n{'#' * lv} {m.group(1).strip()}\n",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )

    # <p> → 줄바꿈
    text = re.sub(r"<p[^>]*>(.*?)</p>", r"\n\1\n", text, flags=re.IGNORECASE | re.DOTALL)

    # <strong>/<b> → **bold**
    text = re.sub(
        r"<(?:strong|b)[^>]*>(.*?)</(?:strong|b)>", r"**\1**", text, flags=re.IGNORECASE | re.DOTALL
    )

    # <em>/<i> → *italic*
    text = re.sub(
        r"<(?:em|i)[^>]*>(.*?)</(?:em|i)>", r"*\1*", text, flags=re.IGNORECASE | re.DOTALL
    )

    # <code> → `code`
    text = re.sub(r"<code[^>]*>(.*?)</code>", r"`\1`", text, flags=re.IGNORECASE | re.DOTALL)

    # <a href="...">text</a> → [text](url)
    text = re.sub(
        r'<a\s+[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
        r"[\2](\1)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    # <li> → - item
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"\n- \1", text, flags=re.IGNORECASE | re.DOTALL)

    # 나머지 HTML 태그 제거
    text = re.sub(r"<[^>]+>", "", text)

    # HTML 엔티티 디코딩 (기본)
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    text = text.replace("&#39;", "'")
    text = text.replace("&nbsp;", " ")

    # 연속 빈 줄 정리
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def normalize_content(content: str, source_format: str) -> str:
    """콘텐츠를 정규화된 마크다운으로 변환한다.

    Args:
        content: 원본 콘텐츠.
        source_format: 소스 형식 ("md", "txt", "html").

    Returns:
        정규화된 마크다운 텍스트.
    """
    if source_format in ("html", "htm"):
        return html_to_markdown(content)
    # md, txt는 그대로 반환
    return content.strip()


def extract_title_from_content(content: str) -> str:
    """마크다운 콘텐츠에서 제목을 추출한다.

    첫 번째 # 헤딩을 제목으로 사용한다. 없으면 첫 줄 사용.
    """
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    # 헤딩이 없으면 첫 번째 비어있지 않은 줄
    for line in content.split("\n"):
        stripped = line.strip()
        if stripped:
            return stripped[:100]
    return "Untitled"
