"""전용 리랭커 모델 기반 청크 재정렬 테스트."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from context_loop.processor.reranker import rerank


def _make_reranker_client(scores: list[float]) -> AsyncMock:
    """Mock 리랭커 클라이언트를 생성한다 (입력 순서대로 점수 반환)."""
    mock = AsyncMock()
    mock.rerank = AsyncMock(return_value=scores)
    return mock


def _make_chunks(n: int) -> list[dict]:
    """테스트용 청크 리스트를 생성한다."""
    return [
        {
            "id": f"chunk_{i}",
            "document": f"청크 {i} 내용입니다.",
            "metadata": {"document_id": i, "chunk_index": 0},
            "distance": 0.3 + i * 0.1,
        }
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_rerank_sorts_by_score() -> None:
    """리랭커가 점수 기준으로 청크를 재정렬한다."""
    client = _make_reranker_client([0.3, 0.9, 0.6])
    chunks = _make_chunks(3)

    result = await rerank("테스트 질의", chunks, client)

    assert len(result) == 3
    assert result[0]["rerank_score"] == 0.9
    assert result[1]["rerank_score"] == 0.6
    assert result[2]["rerank_score"] == 0.3
    assert result[0]["id"] == "chunk_1"


@pytest.mark.asyncio
async def test_rerank_top_k() -> None:
    """top_k 파라미터가 결과 수를 제한한다."""
    client = _make_reranker_client([0.8, 0.5, 0.9])
    chunks = _make_chunks(3)

    result = await rerank("질의", chunks, client, top_k=2)

    assert len(result) == 2
    assert result[0]["rerank_score"] == 0.9
    assert result[1]["rerank_score"] == 0.8


@pytest.mark.asyncio
async def test_rerank_empty_chunks() -> None:
    """빈 청크 리스트는 리랭커 호출 없이 빈 리스트를 반환한다."""
    client = _make_reranker_client([])
    result = await rerank("질의", [], client)
    assert result == []
    client.rerank.assert_not_called()


@pytest.mark.asyncio
async def test_rerank_single_chunk_skips_call() -> None:
    """단일 청크는 리랭커 호출 없이 만점을 부여한다."""
    client = _make_reranker_client([])
    chunks = _make_chunks(1)

    result = await rerank("질의", chunks, client)

    assert len(result) == 1
    assert result[0]["rerank_score"] == 1.0
    client.rerank.assert_not_called()


@pytest.mark.asyncio
async def test_rerank_failure_graceful() -> None:
    """리랭커 호출 실패 시 원본 순서를 유지한다."""
    client = AsyncMock()
    client.rerank = AsyncMock(side_effect=Exception("리랭커 서버 다운"))
    chunks = _make_chunks(3)

    result = await rerank("질의", chunks, client)

    assert len(result) == 3
    assert result[0]["id"] == "chunk_0"
    assert result[1]["id"] == "chunk_1"
    assert result[2]["id"] == "chunk_2"


@pytest.mark.asyncio
async def test_rerank_failure_with_top_k() -> None:
    """리랭커 실패 시에도 top_k가 적용된다."""
    client = AsyncMock()
    client.rerank = AsyncMock(side_effect=Exception("timeout"))
    chunks = _make_chunks(5)

    result = await rerank("질의", chunks, client, top_k=2)

    assert len(result) == 2


@pytest.mark.asyncio
async def test_rerank_sends_query_and_documents() -> None:
    """리랭커 호출 시 query 와 documents (각 청크의 document) 가 전달된다."""
    client = _make_reranker_client([0.7, 0.4])
    chunks = _make_chunks(2)

    await rerank("배포 절차", chunks, client)

    client.rerank.assert_awaited_once()
    call_args = client.rerank.call_args
    assert call_args.args[0] == "배포 절차"
    assert call_args.args[1] == ["청크 0 내용입니다.", "청크 1 내용입니다."]


@pytest.mark.asyncio
async def test_rerank_long_text_truncated() -> None:
    """긴 텍스트는 잘려서 리랭커에 전달된다."""
    client = _make_reranker_client([0.7, 0.5])
    chunks = [
        {"id": "long1", "document": "가" * 5000, "metadata": {}, "distance": 0.2},
        {"id": "long2", "document": "나" * 100, "metadata": {}, "distance": 0.3},
    ]

    await rerank("질의", chunks, client)

    documents = client.rerank.call_args.args[1]
    assert len(documents[0]) <= 2000
    assert len(documents[1]) == 100


@pytest.mark.asyncio
async def test_rerank_score_short_response_pads_zero() -> None:
    """리랭커 응답 길이가 부족하면 누락 인덱스에 0.0이 채워진다."""
    client = _make_reranker_client([0.8])  # chunks 는 2개인데 점수는 1개
    chunks = _make_chunks(2)

    result = await rerank("질의", chunks, client)

    assert len(result) == 2
    scores_by_id = {c["id"]: c["rerank_score"] for c in result}
    assert scores_by_id["chunk_0"] == 0.8
    assert scores_by_id["chunk_1"] == 0.0
