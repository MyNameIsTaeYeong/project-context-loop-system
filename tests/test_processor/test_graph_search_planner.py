"""graph_search_planner 테스트."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from context_loop.processor.graph_extractor import Entity, GraphData, Relation
from context_loop.processor.graph_search_planner import (
    GraphSearchPlan,
    SearchStep,
    _parse_plan,
    execute_graph_search,
    plan_graph_search,
)
from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore


@pytest.fixture
async def meta_store(tmp_path: Path) -> MetadataStore:
    s = MetadataStore(tmp_path / "test.db")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
async def graph_store(meta_store: MetadataStore) -> GraphStore:
    return GraphStore(meta_store)


async def _create_doc(store: MetadataStore) -> int:
    return await store.create_document(
        source_type="manual", title="Test", original_content="c", content_hash="abc",
    )


# --- _parse_plan 테스트 ---


def test_parse_plan_valid() -> None:
    """정상 JSON을 GraphSearchPlan으로 파싱한다."""
    data = {
        "should_search": True,
        "reasoning": "테스트 이유",
        "search_steps": [
            {"entity_name": "Gateway", "depth": 1, "focus_relations": ["depends_on"]},
            {"entity_name": "Auth", "depth": 2, "focus_relations": []},
        ],
    }
    plan = _parse_plan(data)
    assert plan.should_search is True
    assert plan.reasoning == "테스트 이유"
    assert len(plan.search_steps) == 2
    assert plan.search_steps[0].entity_name == "Gateway"
    assert plan.search_steps[0].depth == 1
    assert plan.search_steps[0].focus_relations == ["depends_on"]
    assert plan.search_steps[1].entity_name == "Auth"
    assert plan.search_steps[1].depth == 2


def test_parse_plan_should_not_search() -> None:
    """should_search=false면 search_steps가 비어있어도 된다."""
    plan = _parse_plan({"should_search": False, "reasoning": "무관", "search_steps": []})
    assert plan.should_search is False
    assert plan.search_steps == []


def test_parse_plan_max_steps() -> None:
    """search_steps가 3개를 초과하면 3개로 잘린다."""
    steps = [{"entity_name": f"E{i}", "depth": 1} for i in range(5)]
    plan = _parse_plan({"should_search": True, "search_steps": steps})
    assert len(plan.search_steps) == 3


def test_parse_plan_depth_clamped() -> None:
    """depth가 1~2 범위로 제한된다."""
    plan = _parse_plan({
        "should_search": True,
        "search_steps": [{"entity_name": "X", "depth": 5}],
    })
    assert plan.search_steps[0].depth == 2

    plan2 = _parse_plan({
        "should_search": True,
        "search_steps": [{"entity_name": "X", "depth": 0}],
    })
    assert plan2.search_steps[0].depth == 1


def test_parse_plan_invalid_data() -> None:
    """비정상 입력에 대해 should_search=false를 반환한다."""
    plan = _parse_plan("not a dict")
    assert plan.should_search is False

    plan2 = _parse_plan(None)
    assert plan2.should_search is False


def test_parse_plan_empty_entity_name() -> None:
    """entity_name이 비어있는 step은 무시한다."""
    plan = _parse_plan({
        "should_search": True,
        "search_steps": [
            {"entity_name": "", "depth": 1},
            {"entity_name": "Valid", "depth": 1},
        ],
    })
    assert len(plan.search_steps) == 1
    assert plan.search_steps[0].entity_name == "Valid"


# --- plan_graph_search 테스트 ---


@pytest.mark.asyncio
async def test_plan_graph_search_empty_graph(graph_store: GraphStore) -> None:
    """빈 그래프에서는 LLM 호출 없이 should_search=false를 반환한다."""
    mock_llm = AsyncMock()
    plan = await plan_graph_search("질의", graph_store, mock_llm)
    assert plan.should_search is False
    mock_llm.complete.assert_not_called()


@pytest.mark.asyncio
async def test_plan_graph_search_calls_llm(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """LLM에게 스키마와 질의를 전달하여 계획을 생성한다."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[Entity(name="Gateway", entity_type="component")],
        relations=[],
    ))

    plan_json = json.dumps({
        "should_search": True,
        "reasoning": "Gateway 관련 정보 필요",
        "search_steps": [{"entity_name": "Gateway", "depth": 1, "focus_relations": []}],
    })

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=plan_json)

    plan = await plan_graph_search("게이트웨이 구조", graph_store, mock_llm)
    assert plan.should_search is True
    assert len(plan.search_steps) == 1
    assert plan.search_steps[0].entity_name == "Gateway"

    # LLM에 전달된 프롬프트에 스키마 정보가 포함되어야 함
    call_args = mock_llm.complete.call_args
    prompt = call_args[0][0]
    assert "게이트웨이 구조" in prompt
    assert "Gateway" in prompt


@pytest.mark.asyncio
async def test_plan_graph_search_with_query_embedding(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """query_embedding이 제공되면 쿼리 관련 스키마를 LLM에 전달한다."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name="Gateway", entity_type="component"),
            Entity(name="Auth", entity_type="service"),
        ],
        relations=[
            Relation(source="Gateway", target="Auth", relation_type="depends_on"),
        ],
    ))

    # 엔티티 임베딩 구축
    mock_embed = AsyncMock()
    mock_embed.aembed_documents = AsyncMock(return_value=[[1.0, 0.0], [0.0, 1.0]])
    await graph_store.build_entity_embeddings(mock_embed)

    plan_json = json.dumps({
        "should_search": True,
        "reasoning": "Gateway 확인",
        "search_steps": [{"entity_name": "Gateway", "depth": 1, "focus_relations": []}],
    })

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=plan_json)

    plan = await plan_graph_search(
        "게이트웨이", graph_store, mock_llm,
        query_embedding=[0.9, 0.1],
    )
    assert plan.should_search is True

    # 프롬프트에 "쿼리 관련 핵심 엔티티" 섹션이 포함되어야 함
    prompt = mock_llm.complete.call_args[0][0]
    assert "쿼리 관련 핵심 엔티티" in prompt


@pytest.mark.asyncio
async def test_plan_graph_search_llm_failure(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """LLM 호출 실패 시 should_search=false를 반환한다."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[Entity(name="X")], relations=[],
    ))

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(side_effect=Exception("네트워크 오류"))

    plan = await plan_graph_search("질의", graph_store, mock_llm)
    assert plan.should_search is False


# --- execute_graph_search 테스트 ---


@pytest.mark.asyncio
async def test_execute_search_no_search(graph_store: GraphStore) -> None:
    """should_search=false면 None을 반환한다."""
    plan = GraphSearchPlan(should_search=False)
    result = await execute_graph_search(plan, graph_store)
    assert result is None


@pytest.mark.asyncio
async def test_execute_search_entity_not_found(graph_store: GraphStore) -> None:
    """존재하지 않는 엔티티를 탐색하면 None을 반환한다."""
    plan = GraphSearchPlan(
        should_search=True,
        search_steps=[SearchStep(entity_name="NonExistent", depth=1)],
    )
    result = await execute_graph_search(plan, graph_store)
    assert result is None


@pytest.mark.asyncio
async def test_execute_search_success(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """탐색 계획에 따라 그래프를 탐색하고 결과를 포맷팅한다."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name="Gateway", entity_type="component"),
            Entity(name="AuthService", entity_type="service"),
        ],
        relations=[
            Relation(source="Gateway", target="AuthService", relation_type="depends_on"),
        ],
    ))

    plan = GraphSearchPlan(
        should_search=True,
        reasoning="게이트웨이 구조 파악",
        search_steps=[SearchStep(entity_name="Gateway", depth=1, focus_relations=["depends_on"])],
    )
    result = await execute_graph_search(plan, graph_store)
    assert result is not None
    assert "Gateway" in result
    assert "AuthService" in result
    assert "depends_on" in result
    assert "게이트웨이 구조 파악" in result


@pytest.mark.asyncio
async def test_execute_search_focus_relations_filter(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """focus_relations로 표시할 관계를 필터링한다."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name="A", entity_type="service"),
            Entity(name="B", entity_type="service"),
            Entity(name="C", entity_type="system"),
        ],
        relations=[
            Relation(source="A", target="B", relation_type="calls"),
            Relation(source="A", target="C", relation_type="uses"),
        ],
    ))

    # focus_relations=["calls"]이면 uses는 필터링됨
    plan = GraphSearchPlan(
        should_search=True,
        search_steps=[SearchStep(entity_name="A", depth=1, focus_relations=["calls"])],
    )
    result = await execute_graph_search(plan, graph_store)
    assert result is not None
    assert "calls" in result
    # uses는 필터링되어 표시되지 않아야 함
    assert "uses" not in result
