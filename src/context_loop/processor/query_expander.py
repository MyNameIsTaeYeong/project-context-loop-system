"""쿼리 확장 모듈 — HyDE (Hypothetical Document Embedding).

사용자 질의를 LLM으로 가상 답변 문서로 변환한 뒤 임베딩하여
벡터 검색의 재현율(recall)을 높인다.

"배포 절차" 질의 → 가상 문서에 "릴리즈", "디플로이", "CI/CD" 등
동의어가 포함되어 의미적으로 유사한 청크를 더 많이 검색.
"""

from __future__ import annotations

import logging
from typing import Any

from context_loop.processor.llm_client import LLMClient

logger = logging.getLogger(__name__)

_HYDE_SYSTEM_PROMPT = """\
당신은 사내 기술 문서 작성 전문가입니다.
사용자의 질문에 대해 답변이 될 수 있는 가상의 문서 단락을 작성하세요.

규칙:
- 3~5문장으로 간결하게 작성
- 실제 사내 기술 문서에 있을 법한 내용과 용어를 사용
- 관련 동의어, 약어, 기술 용어를 자연스럽게 포함
- 구체적인 수치나 사실을 지어내지 말고, 일반적인 설명 위주로 작성
- 마크다운 서식 없이 순수 텍스트로 작성
"""

_HYDE_USER_TEMPLATE = "다음 질문에 대한 답변이 포함된 사내 기술 문서 단락을 작성하세요:\n\n{query}"


async def generate_hypothetical_document(
    query: str,
    llm_client: LLMClient,
) -> str | None:
    """사용자 질의에 대한 가상 답변 문서를 생성한다 (HyDE).

    Args:
        query: 사용자 질의.
        llm_client: LLM 클라이언트.

    Returns:
        가상 답변 문서 텍스트. 실패 시 None.
    """
    try:
        prompt = _HYDE_USER_TEMPLATE.format(query=query)
        return await llm_client.complete(
            prompt,
            system=_HYDE_SYSTEM_PROMPT,
            max_tokens=512,
            temperature=0.7,
            reasoning_mode="off",
            purpose="hyde_query_expansion",
        )
    except Exception:
        logger.warning("HyDE 가상 문서 생성 실패", exc_info=True)
        return None


async def expand_query_embedding(
    query: str,
    llm_client: LLMClient,
    embedding_client: Any,
) -> list[float] | None:
    """HyDE로 쿼리 임베딩을 확장한다.

    원본 쿼리 임베딩과 가상 문서 임베딩을 평균하여
    의미적으로 더 풍부한 검색 벡터를 생성한다.

    Args:
        query: 사용자 질의.
        llm_client: LLM 클라이언트.
        embedding_client: 임베딩 클라이언트.

    Returns:
        확장된 임베딩 벡터. 실패 시 None.
    """
    # 원본 쿼리 임베딩
    try:
        query_embedding = await embedding_client.aembed_query(query)
    except Exception:
        logger.warning("쿼리 임베딩 생성 실패", exc_info=True)
        return None

    # HyDE 가상 문서 생성 + 임베딩
    hypothetical_doc = await generate_hypothetical_document(query, llm_client)
    if not hypothetical_doc:
        return query_embedding

    try:
        hyde_embedding = await embedding_client.aembed_query(hypothetical_doc)
    except Exception:
        logger.warning("HyDE 임베딩 생성 실패, 원본 쿼리 임베딩 사용", exc_info=True)
        return query_embedding

    # 원본 + HyDE 평균 벡터
    return _average_embeddings(query_embedding, hyde_embedding)


def _average_embeddings(
    emb_a: list[float],
    emb_b: list[float],
) -> list[float]:
    """두 임베딩 벡터의 평균을 계산한다."""
    return [(a + b) / 2.0 for a, b in zip(emb_a, emb_b)]
