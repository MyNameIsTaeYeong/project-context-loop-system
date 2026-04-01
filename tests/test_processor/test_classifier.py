"""LLM Classifier 테스트 — LLM 클라이언트 모킹."""

from __future__ import annotations

import pytest

from context_loop.processor.classifier import _sample_content, classify_document
from context_loop.processor.llm_client import LLMClient, extract_json


class MockLLMClient(LLMClient):
    """테스트용 LLM 클라이언트 — 미리 정의된 응답을 반환한다."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.last_prompt: str = ""

    async def complete(self, prompt: str, *, system: str | None = None,
                       max_tokens: int = 1024, temperature: float = 0.0) -> str:
        self.last_prompt = prompt
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


# --- _sample_content 단위 테스트 ---


def test_sample_content_short_document() -> None:
    """짧은 문서는 전문을 반환한다."""
    content = "짧은 문서입니다."
    sampled, label = _sample_content(content)
    assert sampled == content
    assert label == "Content"


def test_sample_content_exact_boundary() -> None:
    """정확히 MAX_TOTAL_CHARS인 문서는 전문을 반환한다."""
    content = "가" * 4000
    sampled, label = _sample_content(content)
    assert sampled == content
    assert label == "Content"


def test_sample_content_long_document_three_sections() -> None:
    """긴 문서는 시작/중간/끝 3구간으로 샘플링한다."""
    content = "A" * 3000 + "B" * 3000 + "C" * 3000  # 9000자
    sampled, label = _sample_content(content)
    assert "[Beginning]" in sampled
    assert "[Middle]" in sampled
    assert "[End]" in sampled
    assert "sampled" in label
    assert "9000" in label


def test_sample_content_beginning_has_start() -> None:
    """시작 구간에 문서 앞부분이 포함된다."""
    content = "START_MARKER " + "x" * 10000
    sampled, _ = _sample_content(content)
    assert "START_MARKER" in sampled


def test_sample_content_end_has_end() -> None:
    """끝 구간에 문서 뒷부분이 포함된다."""
    content = "x" * 10000 + " END_MARKER"
    sampled, _ = _sample_content(content)
    assert "END_MARKER" in sampled


def test_sample_content_middle_has_center() -> None:
    """중간 구간에 문서 중앙부가 포함된다."""
    content = "x" * 4000 + "MIDDLE_MARKER" + "x" * 4000
    sampled, _ = _sample_content(content)
    assert "MIDDLE_MARKER" in sampled


# --- 통합 테스트: classify_document에서 샘플링이 적용되는지 ---


@pytest.mark.asyncio
async def test_classify_long_document_uses_sampling() -> None:
    """긴 문서 분류 시 프롬프트에 샘플링 레이블이 포함된다."""
    client = MockLLMClient('{"method": "hybrid", "reason": "mixed content"}')
    content = "서술형 텍스트. " * 500 + "엔티티 관계 설명. " * 500
    method, _ = await classify_document(client, "LongDoc", content)
    assert method == "hybrid"
    assert "sampled" in client.last_prompt


@pytest.mark.asyncio
async def test_classify_short_document_uses_full_content() -> None:
    """짧은 문서 분류 시 전문이 프롬프트에 포함된다."""
    client = MockLLMClient('{"method": "chunk", "reason": "short narrative"}')
    content = "짧은 가이드 문서입니다."
    await classify_document(client, "Short", content)
    assert content in client.last_prompt
    assert "sampled" not in client.last_prompt
