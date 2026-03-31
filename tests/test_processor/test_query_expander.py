"""쿼리 확장 (HyDE) 모듈 테스트."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from context_loop.processor.query_expander import (
    _average_embeddings,
    expand_query_embedding,
    generate_hypothetical_document,
)


def _make_llm_client(response: str) -> AsyncMock:
    mock = AsyncMock()
    mock.complete = AsyncMock(return_value=response)
    return mock


def _make_embedding_client(embedding: list[float]) -> AsyncMock:
    mock = AsyncMock()
    mock.aembed_query = AsyncMock(return_value=embedding)
    return mock


# --- generate_hypothetical_document ---


@pytest.mark.asyncio
async def test_generate_hypothetical_document_success() -> None:
    """LLM이 가상 문서를 생성한다."""
    llm = _make_llm_client("배포 프로세스는 CI/CD 파이프라인을 통해 자동화됩니다.")
    result = await generate_hypothetical_document("배포 절차", llm)
    assert result is not None
    assert "CI/CD" in result
    llm.complete.assert_called_once()


@pytest.mark.asyncio
async def test_generate_hypothetical_document_failure() -> None:
    """LLM 호출 실패 시 None을 반환한다."""
    llm = AsyncMock()
    llm.complete = AsyncMock(side_effect=Exception("timeout"))
    result = await generate_hypothetical_document("질의", llm)
    assert result is None


@pytest.mark.asyncio
async def test_generate_hypothetical_document_prompt_contains_query() -> None:
    """프롬프트에 원본 질의가 포함된다."""
    llm = _make_llm_client("가상 답변")
    await generate_hypothetical_document("API Gateway 설정", llm)

    call_args = llm.complete.call_args
    prompt = call_args[0][0]
    assert "API Gateway 설정" in prompt


# --- _average_embeddings ---


def test_average_embeddings() -> None:
    """두 임베딩의 평균을 계산한다."""
    result = _average_embeddings([1.0, 0.0, 0.4], [0.0, 1.0, 0.6])
    assert result == [0.5, 0.5, 0.5]


def test_average_embeddings_same() -> None:
    """동일한 임베딩의 평균은 원본과 같다."""
    emb = [0.3, 0.7, 0.1]
    result = _average_embeddings(emb, emb)
    assert result == emb


# --- expand_query_embedding ---


@pytest.mark.asyncio
async def test_expand_query_embedding_with_hyde() -> None:
    """HyDE로 원본 + 가상문서 임베딩 평균을 반환한다."""
    llm = _make_llm_client("가상 답변 문서입니다.")

    call_count = 0
    async def mock_embed(text: str) -> list[float]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [1.0, 0.0]  # 원본 쿼리
        return [0.0, 1.0]  # HyDE 문서

    embed_client = AsyncMock()
    embed_client.aembed_query = mock_embed

    result = await expand_query_embedding("배포 절차", llm, embed_client)

    assert result is not None
    assert len(result) == 2
    # 평균: [0.5, 0.5]
    assert result[0] == pytest.approx(0.5)
    assert result[1] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_expand_query_embedding_hyde_failure_fallback() -> None:
    """HyDE 실패 시 원본 쿼리 임베딩을 반환한다."""
    llm = AsyncMock()
    llm.complete = AsyncMock(side_effect=Exception("LLM 다운"))
    embed_client = _make_embedding_client([0.9, 0.1])

    result = await expand_query_embedding("질의", llm, embed_client)

    # HyDE 실패 → 원본 임베딩 반환
    assert result == [0.9, 0.1]


@pytest.mark.asyncio
async def test_expand_query_embedding_embed_failure() -> None:
    """쿼리 임베딩 자체 실패 시 None을 반환한다."""
    llm = _make_llm_client("가상 문서")
    embed_client = AsyncMock()
    embed_client.aembed_query = AsyncMock(side_effect=Exception("임베딩 서버 다운"))

    result = await expand_query_embedding("질의", llm, embed_client)
    assert result is None


@pytest.mark.asyncio
async def test_expand_query_embedding_hyde_embed_failure_fallback() -> None:
    """HyDE 문서 임베딩 실패 시 원본 쿼리 임베딩을 반환한다."""
    llm = _make_llm_client("가상 문서입니다")

    call_count = 0
    async def mock_embed(text: str) -> list[float]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return [0.8, 0.2]  # 원본 성공
        raise Exception("두 번째 임베딩 실패")

    embed_client = AsyncMock()
    embed_client.aembed_query = mock_embed

    result = await expand_query_embedding("질의", llm, embed_client)

    # HyDE 임베딩 실패 → 원본 반환
    assert result == [0.8, 0.2]
