"""토큰 기반 텍스트 청킹 모듈.

tiktoken을 사용하여 텍스트를 토큰 기준으로 분할한다.
마크다운 헤딩 구조를 인식하여 섹션별로 분할하고,
각 청크에 상위 헤딩 경로(section_path)를 첨부한다.
tiktoken을 사용할 수 없는 경우 문자 기반 폴백을 사용한다.

``chunk_extracted_document`` 는 ``confluence_extractor.ExtractedDocument`` 를
입력으로 받아 이미 구조화된 섹션을 그대로 활용한다(헤딩 재파싱 없음).
동시에 펜스 코드블록(```)과 마크다운 테이블을 원자 단위로 보호하여
청크 경계에서 잘리지 않도록 한다.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from context_loop.ingestion.confluence_extractor import ExtractedDocument

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
        section_anchor: 해당 섹션 헤딩의 URL fragment용 앵커.
            Confluence 구조화 추출 경로에서만 채워지고, 그 외에는 빈 문자열.
        section_index: 청크가 유래한 ``ExtractedDocument.sections`` 인덱스.
            Confluence 구조화 추출 경로에서만 채워지며, 일반 마크다운/AST 추출
            경로에서는 ``None`` 이다. ExtractionUnit 의 ``section_ids``
            (``f"{document_id}:{section_index}"``)와 조인할 때 사용된다.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    index: int = 0
    content: str = ""
    token_count: int = 0
    section_path: str = ""
    section_anchor: str = ""
    section_index: int | None = None


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
# 원자 블록 분리 (코드블록/테이블 보호)
# ---------------------------------------------------------------------------


@dataclass
class _Block:
    """청킹 단위 블록.

    ``atomic=True`` 이면 chunk_size를 초과해도 내부에서 자르지 않고 단독 청크로
    방출한다. 펜스 코드블록과 마크다운 테이블이 여기 해당한다.
    """

    content: str
    atomic: bool


_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$")


def _split_markdown_blocks(text: str) -> list[_Block]:
    """마크다운 텍스트를 블록 목록으로 분리한다.

    - 펜스 코드블록(```) : 시작~종료 펜스를 통째로 ``atomic`` 블록으로.
    - 마크다운 테이블     : 헤더 + 구분자(``|---|``) 행을 포함한 연속 파이프
      행을 통째로 ``atomic`` 블록으로.
    - 그 외               : 빈 줄 기준으로 단락을 나누어 ``atomic=False`` 블록으로.
    """
    lines = text.split("\n")
    blocks: list[_Block] = []
    buf: list[str] = []

    def flush_regular() -> None:
        if not buf:
            return
        combined = "\n".join(buf).strip()
        buf.clear()
        if not combined:
            return
        for para in combined.split("\n\n"):
            stripped = para.strip()
            if stripped:
                blocks.append(_Block(content=stripped, atomic=False))

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.lstrip()

        # 펜스 코드블록
        if stripped.startswith("```"):
            flush_regular()
            fence_lines = [line]
            i += 1
            while i < len(lines):
                fence_lines.append(lines[i])
                if lines[i].lstrip().startswith("```"):
                    i += 1
                    break
                i += 1
            blocks.append(_Block(content="\n".join(fence_lines), atomic=True))
            continue

        # 마크다운 테이블: 현재 줄에 "|"가 있고 다음 줄이 구분자
        if (
            "|" in line
            and i + 1 < len(lines)
            and _TABLE_SEPARATOR_RE.match(lines[i + 1])
        ):
            flush_regular()
            table_lines = [line, lines[i + 1]]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                table_lines.append(lines[i])
                i += 1
            blocks.append(_Block(content="\n".join(table_lines), atomic=True))
            continue

        buf.append(line)
        i += 1

    flush_regular()
    return blocks


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
        blocks = _split_markdown_blocks(section.content)
        section_chunks = _chunk_blocks(
            blocks,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            model=model,
        )
        for chunk in section_chunks:
            chunk.index = len(all_chunks)
            chunk.section_path = section_path
            all_chunks.append(chunk)

    return all_chunks


def chunk_extracted_document(
    extracted: ExtractedDocument,
    *,
    chunk_size: int = 512,
    chunk_overlap: int = 50,
    model: str = "cl100k_base",
) -> list[Chunk]:
    """``ExtractedDocument`` 를 구조 기반으로 청크로 분할한다.

    ``chunk_text`` 와 달리 마크다운을 헤딩 정규식으로 재파싱하지 않고
    추출기가 이미 만들어둔 ``extracted.sections`` 를 그대로 소비한다.
    각 섹션 내부에서는 펜스 코드블록과 마크다운 테이블이 청크 경계에서
    잘리지 않도록 원자 단위로 보호한다. 청크에는 섹션 제목/경로에 더해
    ``section_anchor`` 가 첨부되어 벡터 검색 결과에서 Confluence 내부
    deep-link를 구성할 수 있다.

    Args:
        extracted: Confluence 추출기 결과.
        chunk_size: 청크당 최대 토큰 수.
        chunk_overlap: 인접 청크 간 겹치는 토큰 수.
        model: 토큰화에 사용할 모델/인코딩 이름.

    Returns:
        Chunk 목록. ``extracted.sections`` 가 비어 있으면 ``plain_text`` 에
        대해 ``chunk_text`` 를 적용한 결과를 반환한다.
    """
    if not extracted.sections:
        return chunk_text(
            extracted.plain_text,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            model=model,
        )

    all_chunks: list[Chunk] = []
    for section_idx, section in enumerate(extracted.sections):
        heading_line = "#" * section.level + " " + section.title
        body = section.md_content.strip()
        section_text = heading_line + "\n\n" + body if body else heading_line
        section_path = " > ".join(section.path) if section.path else section.title

        blocks = _split_markdown_blocks(section_text)
        section_chunks = _chunk_blocks(
            blocks,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            model=model,
        )
        for chunk in section_chunks:
            chunk.index = len(all_chunks)
            chunk.section_path = section_path
            chunk.section_anchor = section.anchor
            chunk.section_index = section_idx
            all_chunks.append(chunk)

    return all_chunks


def _chunk_blocks(
    blocks: list[_Block],
    *,
    chunk_size: int,
    chunk_overlap: int,
    model: str,
) -> list[Chunk]:
    """블록 목록을 토큰 기반으로 청크로 합친다.

    - 일반 블록: chunk_size를 초과하면 강제 분할.
    - atomic 블록: chunk_size를 초과하면 자르지 않고 단독 청크로 방출.
    """
    if not blocks:
        return []

    enc = _get_tokenizer(model)

    def encode(s: str) -> list[int]:
        if enc is not None:
            return list(enc.encode(s))  # type: ignore[union-attr]
        return list(range(len(s)))

    def decode(tokens: list[int], fallback: str) -> str:
        if enc is not None:
            return enc.decode(tokens)  # type: ignore[union-attr]
        return fallback

    chunks: list[Chunk] = []
    current_tokens: list[int] = []
    current_text_parts: list[str] = []

    def flush(overlap_tokens: list[int]) -> None:
        nonlocal current_tokens, current_text_parts
        if not current_tokens:
            return
        content = decode(current_tokens, "\n\n".join(current_text_parts))
        chunks.append(
            Chunk(
                index=0,
                content=content.strip(),
                token_count=len(current_tokens),
            )
        )
        current_tokens = overlap_tokens[:]
        current_text_parts = []

    for block in blocks:
        block_tokens = encode(block.content)
        block_token_count = len(block_tokens)

        if block_token_count > chunk_size:
            if current_tokens:
                overlap = current_tokens[-chunk_overlap:] if chunk_overlap else []
                flush(overlap)

            if block.atomic:
                # atomic은 내부에서 자르지 않고 통째로 1 청크 (oversized 허용)
                chunks.append(
                    Chunk(
                        index=0,
                        content=block.content.strip(),
                        token_count=block_token_count,
                    )
                )
            else:
                start = 0
                while start < len(block_tokens):
                    end = min(start + chunk_size, len(block_tokens))
                    sub_tokens = block_tokens[start:end]
                    sub_fallback = block.content[
                        start * _CHARS_PER_TOKEN : end * _CHARS_PER_TOKEN
                    ]
                    sub_text = decode(sub_tokens, sub_fallback)
                    chunks.append(
                        Chunk(
                            index=0,
                            content=sub_text.strip(),
                            token_count=len(sub_tokens),
                        )
                    )
                    start += chunk_size - chunk_overlap
            continue

        if len(current_tokens) + block_token_count > chunk_size:
            overlap = current_tokens[-chunk_overlap:] if chunk_overlap else []
            flush(overlap)

        current_tokens.extend(block_tokens)
        current_text_parts.append(block.content)

    if current_tokens:
        content = decode(current_tokens, "\n\n".join(current_text_parts))
        if content.strip():
            chunks.append(
                Chunk(
                    index=0,
                    content=content.strip(),
                    token_count=len(current_tokens),
                )
            )

    return chunks
