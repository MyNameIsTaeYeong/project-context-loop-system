"""전용 리랭커 모델 기반 청크 재정렬.

벡터 검색(bi-encoder) 결과를 dedicated cross-encoder 리랭커 모델로
재평가하여 질의와의 관련도를 더 정밀하게 측정한다.
"""

from __future__ import annotations

import logging
from typing import Any

from context_loop.processor.reranker_client import RerankerClient

logger = logging.getLogger(__name__)


async def rerank(
    query: str,
    chunks: list[dict[str, Any]],
    reranker_client: RerankerClient,
    *,
    top_k: int | None = None,
) -> list[dict[str, Any]]:
    """리랭커 모델로 청크를 재순위화한다.

    Args:
        query: 사용자 질의.
        chunks: 벡터 검색 결과 리스트 (각 항목에 document, metadata, distance 포함).
        reranker_client: 리랭커 클라이언트.
        top_k: 반환할 최대 청크 수. None이면 전체 반환.

    Returns:
        리랭커 점수 기준 내림차순 정렬된 청크 리스트.
        각 청크에 ``rerank_score`` 필드가 추가된다 (모델 의존, 보통 0~1).
    """
    if not chunks:
        return []

    if len(chunks) == 1:
        chunks[0]["rerank_score"] = 1.0
        return chunks

    documents = [_truncate(c.get("document", "")) for c in chunks]

    try:
        scores = await reranker_client.rerank(query, documents)
    except Exception:
        logger.warning("리랭커 호출 실패, 원본 순서 유지", exc_info=True)
        # 실패 시 원본 순서 유지 (graceful degradation)
        for i, chunk in enumerate(chunks):
            chunk["rerank_score"] = 1.0 - i * 0.001
        result = chunks
        if top_k is not None:
            result = result[:top_k]
        return result

    for i, chunk in enumerate(chunks):
        chunk["rerank_score"] = scores[i] if i < len(scores) else 0.0

    sorted_chunks = sorted(chunks, key=lambda c: c["rerank_score"], reverse=True)

    if top_k is not None:
        sorted_chunks = sorted_chunks[:top_k]

    return sorted_chunks


def _truncate(text: str, limit: int = 2000) -> str:
    """리랭커에 보낼 텍스트 길이를 제한한다.

    cross-encoder 모델은 보통 512~8192 토큰 컨텍스트를 가지므로
    너무 긴 청크는 잘라서 보낸다.
    """
    if len(text) <= limit:
        return text
    return text[:limit]
