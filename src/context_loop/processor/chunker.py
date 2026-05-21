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
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from context_loop.ingestion.confluence_extractor import ExtractedDocument

logger = logging.getLogger(__name__)

# 폴백 토큰화 정책: tiktoken 이 없을 때 1 char = 1 token 으로 처리한다.
# 이 정책을 ``count_tokens`` 와 ``_chunk_blocks`` 의 encode/decode 가 일관되게
# 사용해야 한다 (이전에는 count_tokens 가 chars/4, _chunk_blocks 가
# range(len) 으로 일관성이 깨져 폴백 환경에서 overlap_tokens 가 의도의 25%
# 만 적용되고 decode 가 텍스트를 무시하는 버그가 있었다).
#
# 의미: 영문 텍스트에서 chunk_size=512 가 폴백 시 character 512 가 되어 청크가
# 작아질 수 있으나, 동작이 결정적이고 round-trip 정확하여 운영 환경에서 안전한
# fallback 으로 더 적절하다 (tiktoken 의존성을 가진 prod 환경에서는 영향 없음).
_FALLBACK_CHARS_PER_TOKEN = 1

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


@lru_cache(maxsize=8)
def _get_tokenizer(model: str = "cl100k_base") -> object | None:
    """tiktoken 인코더를 반환한다. 없거나 로드 실패 시 None.

    같은 모델명에 대해 첫 호출 후 결과를 캐시한다 — tiktoken 의 ``read_file``
    이 환경에 따라 매 호출마다 vocabulary 를 네트워크/디스크에서 다시 읽어
    상당한 지연(수십 ms × 호출 수)이 누적되는 케이스가 있다.
    """
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
        토큰 수. tiktoken 이 없으면 1 char = 1 token 으로 폴백 (decode 와의
        round-trip 정확성을 위해; ``_FALLBACK_CHARS_PER_TOKEN`` 참조).
    """
    enc = _get_tokenizer(model)
    if enc is not None:
        return len(enc.encode(text))  # type: ignore[union-attr]
    return len(text) // _FALLBACK_CHARS_PER_TOKEN


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


def chunk_extracted_document_doclevel(
    extracted: ExtractedDocument,
    *,
    max_tokens: int = 8000,
    model: str = "cl100k_base",
) -> list[Chunk]:
    """문서 단위 인덱싱용 청킹 — 가능하면 1 청크, 한도 초과만 섹션 폴백.

    R3 — 임베딩 청크 단위를 "문서 단위"로 통일하는 핵심 함수. 검색 결과의
    입자도는 검색 단계의 dedup 으로 문서 단위로 만들고, 인덱싱은 다음 정책을
    따른다:

    1. **작은 문서 (전체 토큰 <= max_tokens)**: 문서 전체를 1 청크로.
       ``section_path=""``, ``section_anchor=""``, ``section_index=None``.
    2. **큰 문서**: 섹션 단위 폴백. 각 ``Section`` 의 헤딩 + md_content 를
       하나의 청크로. 단일 섹션이 ``max_tokens`` 초과면 그 섹션만 토큰
       기준 추가 분할 (penses 코드/표는 atomic 보호).
    3. **sections 가 없는 문서**: plain_text 를 1 청크 (한도 이하) 또는 토큰
       단위 분할.

    기존 ``chunk_extracted_document`` (512 토큰 임의 분할) 와 달리 의미 단위
    (문서/섹션) 를 보존하여 가상 질문 인덱싱 (R3 ``question_generator``) 의
    source 와 자연스럽게 정렬된다.

    Args:
        extracted: Confluence 추출 결과.
        max_tokens: 단일 청크 토큰 한도 (사내 임베딩 모델 컨텍스트 윈도우).
        model: 토큰화 모델 이름.

    Returns:
        Chunk 목록. 빈 문서면 빈 리스트.
    """
    plain = (extracted.plain_text or "").strip()
    if not plain and not extracted.sections:
        return []

    # 케이스 1+3: sections 가 없거나 (있어도) 전체가 작으면 1 청크 우선
    if not extracted.sections:
        return _chunk_plain_with_fallback(
            plain, max_tokens=max_tokens, model=model,
        )

    # 문서 전체 합본 토큰이 한도 이하면 1 청크
    full_body_parts: list[str] = []
    for section in extracted.sections:
        heading_line = "#" * max(section.level, 1) + " " + section.title
        body = section.md_content.strip()
        full_body_parts.append(
            heading_line + "\n\n" + body if body else heading_line,
        )
    full_body = "\n\n".join(full_body_parts)
    full_token_count = count_tokens(full_body, model)

    if full_token_count <= max_tokens:
        return [Chunk(
            index=0,
            content=full_body,
            token_count=full_token_count,
            section_path="",
            section_anchor="",
            section_index=None,
        )]

    # 케이스 2: 섹션 단위 폴백
    return _chunk_by_section(extracted, max_tokens=max_tokens, model=model)


def _chunk_plain_with_fallback(
    text: str, *, max_tokens: int, model: str,
) -> list[Chunk]:
    """sections 가 없는 문서를 1 청크 (한도 이하) 또는 토큰 분할."""
    text = text.strip()
    if not text:
        return []
    token_count = count_tokens(text, model)
    if token_count <= max_tokens:
        return [Chunk(
            index=0,
            content=text,
            token_count=token_count,
            section_path="",
            section_anchor="",
            section_index=None,
        )]
    # 한도 초과 sections-less 거대 문서 — 단락 경계 기반 분할로 폴백.
    # 운영상 드문 경계이지만 안전을 위해 처리.
    return chunk_text(
        text,
        chunk_size=max_tokens,
        chunk_overlap=min(max_tokens // 10, 200),
        model=model,
    )


def _chunk_by_section(
    extracted: ExtractedDocument, *, max_tokens: int, model: str,
) -> list[Chunk]:
    """섹션 단위로 청크를 만든다. 거대 단일 섹션은 추가 분할."""
    chunks: list[Chunk] = []
    for section_idx, section in enumerate(extracted.sections):
        heading_line = "#" * max(section.level, 1) + " " + section.title
        body = section.md_content.strip()
        section_text = heading_line + "\n\n" + body if body else heading_line
        section_path = " > ".join(section.path) if section.path else section.title

        section_token_count = count_tokens(section_text, model)
        if section_token_count <= max_tokens:
            chunks.append(Chunk(
                index=len(chunks),
                content=section_text,
                token_count=section_token_count,
                section_path=section_path,
                section_anchor=section.anchor,
                section_index=section_idx,
            ))
            continue

        # 거대 섹션 — atomic 보호 + 토큰 분할
        blocks = _split_markdown_blocks(section_text)
        sub_chunks = _chunk_blocks(
            blocks,
            chunk_size=max_tokens,
            chunk_overlap=min(max_tokens // 10, 200),
            model=model,
        )
        for sub in sub_chunks:
            sub.index = len(chunks)
            sub.section_path = section_path
            sub.section_anchor = section.anchor
            sub.section_index = section_idx
            chunks.append(sub)

    return chunks


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
        # 폴백: 1 char = 1 token. ord/chr round-trip 정확성으로 overlap 이
        # 실제로 텍스트에 반영되도록 한다 (이전 구현은 range(len) 으로 토큰을
        # 만들어 decode 단계에서 fallback 텍스트로 대체되어 overlap 이 사라졌다).
        return [ord(c) for c in s]

    def decode(tokens: list[int], fallback: str) -> str:
        if enc is not None:
            return enc.decode(tokens)  # type: ignore[union-attr]
        return "".join(chr(t) for t in tokens)

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
                    # 폴백 시 1 char = 1 token 이므로 토큰 인덱스를 그대로
                    # character 인덱스로 사용한다 (decode 와 일관).
                    sub_fallback = block.content[
                        start * _FALLBACK_CHARS_PER_TOKEN
                        : end * _FALLBACK_CHARS_PER_TOKEN
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
