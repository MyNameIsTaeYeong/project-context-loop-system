"""Cross-encoder 스타일 LLM 기반 리랭커.

벡터 검색(bi-encoder) 결과를 LLM으로 재평가하여
질의와의 관련도를 더 정밀하게 측정한다.
단일 LLM 호출로 모든 청크를 한꺼번에 평가하여 비용을 최소화한다.
"""

from __future__ import annotations

import logging
from typing import Any

from context_loop.processor.llm_client import LLMClient, extract_json

logger = logging.getLogger(__name__)

_RERANK_SYSTEM_PROMPT = """\
당신은 검색 결과 관련도 평가 전문가입니다.
사용자 질의와 각 검색 결과(청크)의 관련도를 0~10 점수로 평가해주세요.

평가 기준:
- 10: 질의에 대한 직접적이고 완전한 답변
- 7-9: 질의와 매우 관련 있으며 유용한 정보 포함
- 4-6: 부분적으로 관련 있음
- 1-3: 간접적으로만 관련 있음
- 0: 전혀 관련 없음

반드시 JSON 배열로만 응답하세요. 각 항목은 {"index": 청크번호, "score": 점수} 형태입니다.
"""


async def rerank(
    query: str,
    chunks: list[dict[str, Any]],
    llm_client: LLMClient,
    *,
    top_k: int | None = None,
) -> list[dict[str, Any]]:
    """LLM 기반으로 청크를 재순위화한다.

    Args:
        query: 사용자 질의.
        chunks: 벡터 검색 결과 리스트 (각 항목에 document, metadata, distance 포함).
        llm_client: LLM 클라이언트.
        top_k: 반환할 최대 청크 수. None이면 전체 반환.

    Returns:
        관련도 점수 기준 내림차순 정렬된 청크 리스트.
        각 청크에 rerank_score 필드가 추가된다.
    """
    if not chunks:
        return []

    if len(chunks) == 1:
        chunks[0]["rerank_score"] = 10.0
        return chunks

    # 프롬프트 구성
    chunk_descriptions = []
    for i, chunk in enumerate(chunks):
        doc_text = chunk.get("document", "")
        # 너무 긴 텍스트는 잘라서 LLM 토큰 절약
        if len(doc_text) > 500:
            doc_text = doc_text[:500] + "..."
        chunk_descriptions.append(f"[청크 {i}]\n{doc_text}")

    chunks_text = "\n\n".join(chunk_descriptions)
    prompt = f"## 사용자 질의\n{query}\n\n## 검색 결과\n{chunks_text}\n\n위 청크들의 관련도를 평가하세요."

    try:
        response = await llm_client.complete(
            prompt,
            system=_RERANK_SYSTEM_PROMPT,
            max_tokens=512,
            temperature=0.0,
        )
        scores = _parse_scores(response, len(chunks))
    except Exception:
        logger.warning("리랭커 LLM 호출 실패, 원본 순서 유지", exc_info=True)
        # 실패 시 원본 순서 유지 (graceful degradation)
        for i, chunk in enumerate(chunks):
            chunk["rerank_score"] = 10.0 - i * 0.1
        result = chunks
        if top_k is not None:
            result = result[:top_k]
        return result

    # 점수 할당 및 정렬
    for i, chunk in enumerate(chunks):
        chunk["rerank_score"] = scores.get(i, 0.0)

    sorted_chunks = sorted(chunks, key=lambda c: c["rerank_score"], reverse=True)

    if top_k is not None:
        sorted_chunks = sorted_chunks[:top_k]

    return sorted_chunks


def _parse_scores(response: str, n_chunks: int) -> dict[int, float]:
    """LLM 응답에서 점수를 파싱한다.

    Returns:
        {청크_인덱스: 점수} 딕셔너리.
    """
    try:
        data = extract_json(response)
    except ValueError:
        logger.warning("리랭커 응답 JSON 파싱 실패: %s", response[:200])
        return {}

    scores: dict[int, float] = {}
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "index" in item and "score" in item:
                idx = int(item["index"])
                score = float(item["score"])
                if 0 <= idx < n_chunks:
                    scores[idx] = min(max(score, 0.0), 10.0)
    return scores
