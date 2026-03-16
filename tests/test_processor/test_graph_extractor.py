"""그래프 추출 모듈 테스트."""

from __future__ import annotations

import json

import pytest

from context_loop.processor.graph_extractor import GraphData, extract_graph
from context_loop.processor.llm_client import LLMClient, extract_json


class MockLLMClient(LLMClient):
    def __init__(self, response: str) -> None:
        self._response = response

    async def complete(self, prompt: str, *, system: str | None = None,
                       max_tokens: int = 1024, temperature: float = 0.0) -> str:
        return self._response


_VALID_RESPONSE = json.dumps({
    "entities": [
        {"name": "Auth Service", "type": "service", "description": "Authentication service"},
        {"name": "User DB", "type": "system", "description": "User database"},
        {"name": "API Gateway", "type": "component", "description": "API gateway"},
    ],
    "relations": [
        {"source": "API Gateway", "target": "Auth Service", "type": "depends_on", "label": "authenticates via"},
        {"source": "Auth Service", "target": "User DB", "type": "uses", "label": "queries"},
    ],
})


@pytest.mark.asyncio
async def test_extract_graph_success() -> None:
    """정상적인 응답에서 엔티티와 관계를 추출한다."""
    client = MockLLMClient(_VALID_RESPONSE)
    result = await extract_graph(client, "Architecture", "System architecture doc")
    assert isinstance(result, GraphData)
    assert len(result.entities) == 3
    assert len(result.relations) == 2

    entity_names = [e.name for e in result.entities]
    assert "Auth Service" in entity_names
    assert "User DB" in entity_names


@pytest.mark.asyncio
async def test_extract_graph_entity_types() -> None:
    """엔티티 유형이 올바르게 파싱된다."""
    client = MockLLMClient(_VALID_RESPONSE)
    result = await extract_graph(client, "Title", "content")
    auth = next(e for e in result.entities if e.name == "Auth Service")
    assert auth.entity_type == "service"
    assert auth.description == "Authentication service"


@pytest.mark.asyncio
async def test_extract_graph_relations() -> None:
    """관계가 올바르게 파싱된다."""
    client = MockLLMClient(_VALID_RESPONSE)
    result = await extract_graph(client, "Title", "content")
    depends = next(r for r in result.relations if r.relation_type == "depends_on")
    assert depends.source == "API Gateway"
    assert depends.target == "Auth Service"
    assert depends.label == "authenticates via"


@pytest.mark.asyncio
async def test_extract_graph_fallback_on_invalid() -> None:
    """파싱 실패 시 빈 GraphData를 반환한다."""
    client = MockLLMClient("I cannot parse this")
    result = await extract_graph(client, "Title", "content")
    assert result.entities == []
    assert result.relations == []


@pytest.mark.asyncio
async def test_extract_graph_skips_missing_entities_in_relations() -> None:
    """relation의 source/target 엔티티가 없으면 relation이 무시되지 않는다 (추출 단계에서는 모두 포함)."""
    response = json.dumps({
        "entities": [{"name": "A", "type": "system"}],
        "relations": [
            {"source": "A", "target": "B", "type": "depends_on"},  # B는 엔티티 없음
        ],
    })
    client = MockLLMClient(response)
    result = await extract_graph(client, "Title", "content")
    # 추출 자체는 그대로 반환 (저장 시 필터링됨)
    assert len(result.relations) == 1


@pytest.mark.asyncio
async def test_extract_graph_truncated_json() -> None:
    """max_tokens 제한으로 잘린 JSON 응답에서도 완전한 항목을 추출한다."""
    truncated = (
        '{"entities": ['
        '{"name": "Auth Service", "type": "service", "description": "auth"},'
        '{"name": "User DB", "type": "system", "description": "db"}'
        '], "relations": ['
        '{"source": "Auth Service", "target": "User DB", "type": "uses", "label": "queries"},'
        '{"source": "Auth Service", "target": "Incom'  # 잘린 부분
    )
    client = MockLLMClient(truncated)
    result = await extract_graph(client, "Title", "content")
    assert len(result.entities) == 2
    assert len(result.relations) == 1
    assert result.relations[0].source == "Auth Service"


def test_extract_json_truncated_repair() -> None:
    """잘린 JSON 문자열을 복구하여 파싱한다."""
    truncated = '{"entities": [{"name": "A"}, {"name": "B"}], "relations": [{"source": "A", "targ'
    data = extract_json(truncated)
    assert len(data["entities"]) == 2
    # relations 키 자체가 잘려서 포함되지 않거나 빈 배열
    assert len(data.get("relations", [])) <= 1


def test_extract_json_truncated_code_block() -> None:
    """코드 블록 안의 잘린 JSON도 복구한다."""
    truncated = '```json\n{"entities": [{"name": "X"}], "relations": [{"sourc'
    data = extract_json(truncated)
    assert len(data["entities"]) == 1
