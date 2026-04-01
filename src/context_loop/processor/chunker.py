"""토큰 기반 텍스트 청킹 모듈.

tiktoken을 사용하여 텍스트를 토큰 기준으로 분할한다.
마크다운 헤딩 구조를 인식하여 섹션별로 분할하고,
각 청크에 상위 헤딩 경로(section_path)를 첨부한다.
tiktoken을 사용할 수 없는 경우 문자 기반 폴백을 사용한다.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 토큰 추정을 위한 문자 비율 (폴백용)
_CHARS_PER_TOKEN = 4

# 마크다운 헤딩 패턴 (# ~ ######)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)


@dataclass
class Chunk:
    """텍스트 청크.

    Attributes:
        id: 청크 고유 ID (UUID).
        index: 문서 내 순서 (0-based).
        content: 청크 텍스트.
        token_count: 청크의 토큰 수.
        section_path: 상위 헤딩 경로 (예: "프로젝트 개요 > 아키텍처 > 백엔드").
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    index: int = 0
    content: str = ""
    token_count: int = 0
    section_path: str = ""


def _get_tokenizer(model: str = "cl100k_base") -> object | None:
    """tiktoken 인코더를 반환한다. 없거나 로드 실패 시 None."""
    try:
        import tiktoken  # noqa: PLC0415
        try:
            return tiktoken.encoding_for_model(model)
        except KeyError:
            pass
        try:
            return tiktoken.get_encoding("cl100k_base")
        except Exception:  # noqa: BLE001
            pass
        return None
    except (ImportError, Exception):  # noqa: BLE001
        logger.warning("tiktoken을 로드할 수 없습니다. 문자 기반 폴백을 사용합니다.")
        return None


def count_tokens(text: str, model: str = "cl100k_base") -> int:
    """텍스트의 토큰 수를 반환한다.

    Args:
        text: 토큰을 셀 텍스트.
        model: tiktoken 인코딩 이름 또는 모델 이름.

    Returns:
        토큰 수.
    """
    enc = _get_tokenizer(model)
    if enc is not None:
        return len(enc.encode(text))  # type: ignore[union-attr]
    return len(text) // _CHARS_PER_TOKEN


# ---------------------------------------------------------------------------
# 섹션 분리 (마크다운 헤딩 기반)
# ---------------------------------------------------------------------------


@dataclass
class _Section:
    """마크다운 헤딩으로 구분된 문서 섹션."""

    heading_level: int  # 0 = 헤딩 없는 최상위 텍스트
    heading_text: str  # 헤딩 제목
    content: str  # 해당 섹션의 본문 텍스트
    path: list[str]  # 상위 헤딩 경로 (자기 자신 포함)


def _split_into_sections(text: str) -> list[_Section]:
    """마크다운 텍스트를 헤딩 기반으로 섹션으로 분리한다.

    각 섹션에 상위 헤딩 경로를 계산하여 첨부한다.
    헤딩이 없는 텍스트는 최상위 섹션(level=0)으로 처리한다.

    Args:
        text: 마크다운 텍스트.

    Returns:
        섹션 목록.
    """
    # 헤딩 위치 찾기
    headings: list[tuple[int, int, str, int]] = []  # (start, level, title, end_of_heading_line)
    for m in _HEADING_RE.finditer(text):
        level = len(m.group(1))
        title = m.group(2).strip()
        headings.append((m.start(), level, title, m.end()))

    if not headings:
        # 헤딩이 없으면 전체를 하나의 섹션으로
        return [_Section(heading_level=0, heading_text="", content=text, path=[])]

    sections: list[_Section] = []

    # 첫 번째 헤딩 이전의 텍스트
    pre_text = text[: headings[0][0]].strip()
    if pre_text:
        sections.append(
            _Section(heading_level=0, heading_text="", content=pre_text, path=[])
        )

    # 현재 헤딩 스택: [(level, title), ...]
    heading_stack: list[tuple[int, str]] = []

    for i, (start, level, title, heading_end) in enumerate(headings):
        # 이 섹션의 본문 범위
        if i + 1 < len(headings):
            body = text[heading_end: headings[i + 1][0]]
        else:
            body = text[heading_end:]

        # 스택 갱신: 현재 레벨 이상의 항목 제거
        while heading_stack and heading_stack[-1][0] >= level:
            heading_stack.pop()
        heading_stack.append((level, title))

        path = [t for _, t in heading_stack]
        content_text = body.strip()

        # 헤딩 자체를 본문 앞에 포함 (검색 시 컨텍스트 보존)
        heading_line = "#" * level + " " + title
        full_content = heading_line + "\n\n" + content_text if content_text else heading_line

        sections.append(
            _Section(
                heading_level=level,
                heading_text=title,
                content=full_content,
                path=path,
            )
        )

    return sections


# ---------------------------------------------------------------------------
# 메인 청킹 함수
# ---------------------------------------------------------------------------


def chunk_text(
    text: str,
    *,
    chunk_size: int = 512,
    chunk_overlap: int = 50,
    model: str = "cl100k_base",
) -> list[Chunk]:
    """텍스트를 마크다운 헤딩 구조를 인식하여 청크로 분할한다.

    1. 마크다운 헤딩(#~######)으로 섹션을 분리한다.
    2. 각 섹션 내에서 토큰 기준으로 청크를 분할한다.
    3. 각 청크에 상위 헤딩 경로(section_path)를 첨부한다.

    헤딩이 없는 문서는 기존과 동일하게 단락 경계 기반으로 분할한다.

    Args:
        text: 분할할 텍스트.
        chunk_size: 청크당 최대 토큰 수.
        chunk_overlap: 인접 청크 간 겹치는 토큰 수.
        model: 토큰화에 사용할 모델/인코딩 이름.

    Returns:
        Chunk 목록.
    """
    if not text.strip():
        return []

    sections = _split_into_sections(text)

    all_chunks: list[Chunk] = []
    for section in sections:
        section_path = " > ".join(section.path) if section.path else ""
        section_chunks = _chunk_section(
            section.content,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            model=model,
        )
        for chunk in section_chunks:
            chunk.index = len(all_chunks)
            chunk.section_path = section_path
            all_chunks.append(chunk)

    return all_chunks


def _chunk_section(
    text: str,
    *,
    chunk_size: int = 512,
    chunk_overlap: int = 50,
    model: str = "cl100k_base",
) -> list[Chunk]:
    """단일 섹션을 토큰 기반으로 청크로 분할한다.

    단락(\\n\\n) 경계를 우선하여 자연스럽게 분할하고,
    chunk_size를 초과하면 강제로 분할한다.
    """
    if not text.strip():
        return []

    enc = _get_tokenizer(model)

    def encode(s: str) -> list[int]:
        if enc is not None:
            return list(enc.encode(s))  # type: ignore[union-attr]
        return list(range(len(s)))

    # 단락으로 먼저 분리
    paragraphs = text.split("\n\n")

    chunks: list[Chunk] = []
    current_tokens: list[int] = []
    current_text_parts: list[str] = []

    def flush(overlap_tokens: list[int]) -> None:
        nonlocal current_tokens, current_text_parts
        if not current_tokens:
            return
        if enc is not None:
            content = enc.decode(current_tokens)  # type: ignore[union-attr]
        else:
            content = "\n\n".join(current_text_parts)
        chunks.append(
            Chunk(
                index=0,  # caller가 재설정함
                content=content.strip(),
                token_count=len(current_tokens),
            )
        )
        current_tokens = overlap_tokens[:]
        current_text_parts = []

    for para in paragraphs:
        if not para.strip():
            continue
        para_tokens = encode(para)
        para_token_count = len(para_tokens)

        # 단락 자체가 chunk_size를 초과하면 강제 분할
        if para_token_count > chunk_size:
            if current_tokens:
                overlap = current_tokens[-chunk_overlap:] if chunk_overlap else []
                flush(overlap)
            start = 0
            while start < len(para_tokens):
                end = min(start + chunk_size, len(para_tokens))
                sub_tokens = para_tokens[start:end]
                if enc is not None:
                    sub_text = enc.decode(sub_tokens)  # type: ignore[union-attr]
                else:
                    sub_text = para[start * _CHARS_PER_TOKEN : end * _CHARS_PER_TOKEN]
                chunks.append(
                    Chunk(
                        index=0,
                        content=sub_text.strip(),
                        token_count=len(sub_tokens),
                    )
                )
                start += chunk_size - chunk_overlap
            continue

        if len(current_tokens) + para_token_count > chunk_size:
            overlap = current_tokens[-chunk_overlap:] if chunk_overlap else []
            flush(overlap)

        current_tokens.extend(para_tokens)
        current_text_parts.append(para)

    # 남은 버퍼 처리
    if current_tokens:
        if enc is not None:
            content = enc.decode(current_tokens)  # type: ignore[union-attr]
        else:
            content = "\n\n".join(current_text_parts)
        if content.strip():
            chunks.append(
                Chunk(
                    index=0,
                    content=content.strip(),
                    token_count=len(current_tokens),
                )
            )

    return chunks
