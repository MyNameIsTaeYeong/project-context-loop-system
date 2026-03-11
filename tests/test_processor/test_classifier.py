"""LLM Classifier 테스트 — LLM 클라이언트 모킹."""

from __future__ import annotations

import pytest

from context_loop.processor.classifier import classify_document
from context_loop.processor.llm_client import LLMClient, extract_json


class MockLLMClient(LLMClient):
    """테스트용 LLM 클라이언트 — 미리 정의된 응답을 반환한다."""

    def __init__(self, response: str) -> None:
        self._response = response

    async def complete(self, prompt: str, *, system: str | None = None,
                       max_tokens: int = 1024, temperature: float = 0.0) -> str:
        return self._response


def test_extract_json_plain() -> None:
    """순수 JSON 객체를 파싱한다."""
    text = '{"method": "chunk", "reason": "narrative text"}'
    result = extract_json(text)
    assert result["method"] == "chunk"


def test_extract_json_code_block() -> None:
    """마크다운 코드 블록에서 JSON을 추출한다."""
    text = '```json\n{"method": "graph"}\n```'
    result = extract_json(text)
    assert result["method"] == "graph"


def test_extract_json_no_json_raises() -> None:
    """JSON이 없으면 ValueError를 발생시킨다."""
    with pytest.raises(ValueError):
        extract_json("No JSON here at all.")


@pytest.mark.asyncio
async def test_classify_chunk() -> None:
    """chunk 분류 응답을 올바르게 파싱한다."""
    client = MockLLMClient('{"method": "chunk", "reason": "narrative guide"}')
    method, reason = await classify_document(client, "Guide", "This is a guide...")
    assert method == "chunk"
    assert "narrative" in reason


@pytest.mark.asyncio
async def test_classify_graph() -> None:
    """graph 분류 응답을 올바르게 파싱한다."""
    client = MockLLMClient('{"method": "graph", "reason": "entity relationships"}')
    method, reason = await classify_document(client, "Arch", "System architecture...")
    assert method == "graph"


@pytest.mark.asyncio
async def test_classify_hybrid() -> None:
    """hybrid 분류 응답을 올바르게 파싱한다."""
    client = MockLLMClient('{"method": "hybrid", "reason": "mixed content"}')
    method, _ = await classify_document(client, "Spec", "Project spec...")
    assert method == "hybrid"


@pytest.mark.asyncio
async def test_classify_fallback_on_invalid_json() -> None:
    """응답이 파싱 불가능하면 'chunk'로 폴백한다."""
    client = MockLLMClient("I cannot determine the method.")
    method, _ = await classify_document(client, "Doc", "content")
    assert method == "chunk"


@pytest.mark.asyncio
async def test_classify_fallback_on_unknown_method() -> None:
    """알 수 없는 method 값이면 'chunk'로 폴백한다."""
    client = MockLLMClient('{"method": "unknown_type", "reason": "???"}')
    method, _ = await classify_document(client, "Doc", "content")
    assert method == "chunk"
