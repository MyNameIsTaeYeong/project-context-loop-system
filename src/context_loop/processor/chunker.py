"""토큰 기반 텍스트 청킹 모듈.

tiktoken을 사용하여 텍스트를 토큰 기준으로 분할한다.
tiktoken을 사용할 수 없는 경우 문자 기반 폴백을 사용한다.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 토큰 추정을 위한 문자 비율 (폴백용)
_CHARS_PER_TOKEN = 4


@dataclass
class Chunk:
    """텍스트 청크.

    Attributes:
        id: 청크 고유 ID (UUID).
        index: 문서 내 순서 (0-based).
        content: 청크 텍스트.
        token_count: 청크의 토큰 수.
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    index: int = 0
    content: str = ""
    token_count: int = 0


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


def chunk_text(
    text: str,
    *,
    chunk_size: int = 512,
    chunk_overlap: int = 50,
    model: str = "cl100k_base",
) -> list[Chunk]:
    """텍스트를 토큰 기준으로 청크로 분할한다.

    단락(\\n\\n) 경계를 우선하여 자연스럽게 분할하고,
    chunk_size를 초과하면 강제로 분할한다.
    청크 간 chunk_overlap 토큰만큼 겹친다.

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

    enc = _get_tokenizer(model)

    def token_count(s: str) -> int:
        if enc is not None:
            return len(enc.encode(s))  # type: ignore[union-attr]
        return len(s) // _CHARS_PER_TOKEN

    def encode(s: str) -> list[int]:
        if enc is not None:
            return list(enc.encode(s))  # type: ignore[union-attr]
        # 폴백: 문자 단위
        return list(range(len(s)))

    def decode(tokens: list[int], original: str) -> str:
        if enc is not None:
            return enc.decode(tokens)  # type: ignore[union-attr]
        # 폴백: 문자 단위
        return original[tokens[0] : tokens[-1] + 1] if tokens else ""

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
                index=len(chunks),
                content=content.strip(),
                token_count=len(current_tokens),
            )
        )
        # 다음 청크는 overlap 만큼 이전 토큰으로 시작
        current_tokens = overlap_tokens[:]
        current_text_parts = []

    for para in paragraphs:
        if not para.strip():
            continue
        para_tokens = encode(para)
        para_token_count = len(para_tokens)

        # 단락 자체가 chunk_size를 초과하면 강제 분할
        if para_token_count > chunk_size:
            # 현재 버퍼 먼저 flush
            if current_tokens:
                overlap = current_tokens[-chunk_overlap:] if chunk_overlap else []
                flush(overlap)
            # 단락을 chunk_size 단위로 자름
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
                        index=len(chunks),
                        content=sub_text.strip(),
                        token_count=len(sub_tokens),
                    )
                )
                start += chunk_size - chunk_overlap
            continue

        # 현재 버퍼 + 단락이 chunk_size를 초과하면 flush
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
                    index=len(chunks),
                    content=content.strip(),
                    token_count=len(current_tokens),
                )
            )

    return chunks
