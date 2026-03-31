"""LLM 기반 리랭커 테스트."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from context_loop.processor.reranker import _parse_scores, rerank


def _make_llm_client(response: str) -> AsyncMock:
    """Mock LLM 클라이언트를 생성한다."""
    mock = AsyncMock()
    mock.complete = AsyncMock(return_value=response)
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
    """리랭커가 LLM 점수 기준으로 청크를 재정렬한다."""
    scores = json.dumps([
        {"index": 0, "score": 3},
        {"index": 1, "score": 9},
        {"index": 2, "score": 6},
    ])
    llm = _make_llm_client(scores)
    chunks = _make_chunks(3)

    result = await rerank("테스트 질의", chunks, llm)

    assert len(result) == 3
    assert result[0]["rerank_score"] == 9.0
    assert result[1]["rerank_score"] == 6.0
    assert result[2]["rerank_score"] == 3.0
    assert result[0]["id"] == "chunk_1"


@pytest.mark.asyncio
async def test_rerank_top_k() -> None:
    """top_k 파라미터가 결과 수를 제한한다."""
    scores = json.dumps([
        {"index": 0, "score": 8},
        {"index": 1, "score": 5},
        {"index": 2, "score": 9},
    ])
    llm = _make_llm_client(scores)
    chunks = _make_chunks(3)

    result = await rerank("질의", chunks, llm, top_k=2)

    assert len(result) == 2
    assert result[0]["rerank_score"] == 9.0
    assert result[1]["rerank_score"] == 8.0


@pytest.mark.asyncio
async def test_rerank_empty_chunks() -> None:
    """빈 청크 리스트를 처리한다."""
    llm = _make_llm_client("[]")
    result = await rerank("질의", [], llm)
    assert result == []
    llm.complete.assert_not_called()


@pytest.mark.asyncio
async def test_rerank_single_chunk() -> None:
    """단일 청크는 LLM 호출 없이 만점을 부여한다."""
    llm = _make_llm_client("[]")
    chunks = _make_chunks(1)

    result = await rerank("질의", chunks, llm)

    assert len(result) == 1
    assert result[0]["rerank_score"] == 10.0
    llm.complete.assert_not_called()


@pytest.mark.asyncio
async def test_rerank_llm_failure_graceful() -> None:
    """LLM 호출 실패 시 원본 순서를 유지한다."""
    llm = AsyncMock()
    llm.complete = AsyncMock(side_effect=Exception("LLM 서버 다운"))
    chunks = _make_chunks(3)

    result = await rerank("질의", chunks, llm)

    assert len(result) == 3
    # 원본 순서 유지 (점수는 내림차순으로 부여)
    assert result[0]["id"] == "chunk_0"
    assert result[1]["id"] == "chunk_1"
    assert result[2]["id"] == "chunk_2"


@pytest.mark.asyncio
async def test_rerank_llm_failure_with_top_k() -> None:
    """LLM 실패 시에도 top_k가 적용된다."""
    llm = AsyncMock()
    llm.complete = AsyncMock(side_effect=Exception("timeout"))
    chunks = _make_chunks(5)

    result = await rerank("질의", chunks, llm, top_k=2)

    assert len(result) == 2


@pytest.mark.asyncio
async def test_rerank_markdown_wrapped_json() -> None:
    """마크다운 코드 블록으로 감싼 JSON도 파싱한다."""
    scores = '```json\n[{"index": 0, "score": 7}, {"index": 1, "score": 4}]\n```'
    llm = _make_llm_client(scores)
    chunks = _make_chunks(2)

    result = await rerank("질의", chunks, llm)

    assert result[0]["rerank_score"] == 7.0
    assert result[1]["rerank_score"] == 4.0


@pytest.mark.asyncio
async def test_rerank_long_text_truncated() -> None:
    """500자 초과 텍스트가 잘려서 LLM에 전달된다."""
    scores = json.dumps([{"index": 0, "score": 8}])
    llm = _make_llm_client(scores)
    chunks = [{"id": "long", "document": "가" * 1000, "metadata": {}, "distance": 0.2}]

    await rerank("질의", chunks, llm)

    # 단일 청크이므로 LLM 호출 안 됨 — 2개로 테스트
    chunks2 = [
        {"id": "long1", "document": "가" * 1000, "metadata": {}, "distance": 0.2},
        {"id": "long2", "document": "나" * 100, "metadata": {}, "distance": 0.3},
    ]
    scores2 = json.dumps([{"index": 0, "score": 7}, {"index": 1, "score": 5}])
    llm2 = _make_llm_client(scores2)
    await rerank("질의", chunks2, llm2)

    call_args = llm2.complete.call_args
    prompt = call_args[0][0]
    # 1000자 텍스트가 500자 + "..."로 잘림
    assert "..." in prompt


# --- _parse_scores 단위 테스트 ---


def test_parse_scores_valid() -> None:
    """정상적인 JSON 배열을 파싱한다."""
    response = json.dumps([
        {"index": 0, "score": 8},
        {"index": 1, "score": 5},
    ])
    scores = _parse_scores(response, 2)
    assert scores == {0: 8.0, 1: 5.0}


def test_parse_scores_clamps_range() -> None:
    """점수가 0~10 범위로 클램핑된다."""
    response = json.dumps([
        {"index": 0, "score": 15},
        {"index": 1, "score": -3},
    ])
    scores = _parse_scores(response, 2)
    assert scores[0] == 10.0
    assert scores[1] == 0.0


def test_parse_scores_ignores_out_of_range_index() -> None:
    """범위 밖 인덱스는 무시된다."""
    response = json.dumps([
        {"index": 0, "score": 8},
        {"index": 5, "score": 9},  # 범위 밖
    ])
    scores = _parse_scores(response, 3)
    assert 5 not in scores
    assert scores == {0: 8.0}


def test_parse_scores_invalid_json() -> None:
    """파싱 불가능한 응답은 빈 딕셔너리를 반환한다."""
    scores = _parse_scores("이건 JSON이 아닙니다", 3)
    assert scores == {}
