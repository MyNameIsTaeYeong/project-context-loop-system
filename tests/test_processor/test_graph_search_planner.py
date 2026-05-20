"""graph_search_planner 테스트."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from context_loop.processor.graph_extractor import Entity, GraphData, Relation
from context_loop.processor.graph_search_planner import (
    GraphSearchPlan,
    GraphSearchResult,
    SearchStep,
    TargetEntity,
    TargetRelation,
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
    assert isinstance(result, GraphSearchResult)
    assert "Gateway" in result.text
    assert "AuthService" in result.text
    assert "depends_on" in result.text
    assert "게이트웨이 구조 파악" in result.text
    assert doc_id in result.document_ids


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
    assert "calls" in result.text
    # uses는 필터링되어 표시되지 않아야 함
    assert "uses" not in result.text


# ---------------------------------------------------------------------------
# 시스템 프롬프트 어휘 노출 (PR-5)
# ---------------------------------------------------------------------------


def test_system_prompt_exposes_full_vocabulary() -> None:
    """LLM 시스템 프롬프트에 graph_vocabulary 의 모든 entity/relation 어휘가 노출된다.

    이 회귀 방지가 없으면 추출기는 새 관계(예: depends_on)를 그래프에 쌓아도
    탐색 플래너가 이를 모르고 활용 못한다.
    """
    from context_loop.processor.graph_search_planner import _render_system_prompt
    from context_loop.processor.graph_vocabulary import (
        ENTITY_TYPES,
        RELATION_TYPES,
    )

    prompt = _render_system_prompt()
    for entry in ENTITY_TYPES:
        assert entry.name in prompt, f"entity_type '{entry.name}' 누락"
    for entry in RELATION_TYPES:
        assert entry.name in prompt, f"relation_type '{entry.name}' 누락"


@pytest.mark.asyncio
async def test_execute_search_seeds_from_query_embedding_when_steps_miss(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """R1 F-SRCH-03: LLM 추측 entity_name 이 모두 인덱스에 없을 때
    ``query_embedding`` 으로 시드 노드를 보강한다 — 그래프 메트릭 0% 의
    핵심 funnel 손실 완화."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name="AuthService", entity_type="system"),
            Entity(name="OrderService", entity_type="system"),
        ],
        relations=[
            Relation(source="AuthService", target="OrderService", relation_type="depends_on"),
        ],
    ))

    mock_embed = AsyncMock()
    mock_embed.aembed_documents = AsyncMock(return_value=[[1.0, 0.0], [0.0, 1.0]])
    await graph_store.build_entity_embeddings(mock_embed)

    # LLM 이 잘못된 entity_name 을 답함 — 표면 매칭 전혀 안 됨
    plan = GraphSearchPlan(
        should_search=True,
        search_steps=[SearchStep(entity_name="DoesNotExistInIndex", depth=1)],
    )
    # query_embedding 으로 AuthService 와 가까운 의미 벡터 제공
    result = await execute_graph_search(
        plan, graph_store, query_embedding=[0.95, 0.05],
    )
    # 시드 fallback 으로 AuthService 가 잡혀 결과가 비어있지 않다.
    assert result is not None
    names = {e.name for e in result.entities}
    assert "AuthService" in names


@pytest.mark.asyncio
async def test_execute_search_fills_description_fallback_for_retrieved(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """R1 F-SRCH-06: retrieved GraphEntityRef 의 description 이 비어 있으면
    자연어 fallback 으로 채워준다 — 평가 측 T4 임베딩 매칭에서 짧은 이름
    임베딩의 비특이성을 완화."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        # description 을 비워둔 entity
        entities=[Entity(name="ShortName", entity_type="system", description="")],
        relations=[],
    ))
    plan = GraphSearchPlan(
        should_search=True,
        search_steps=[SearchStep(entity_name="ShortName", depth=1)],
    )
    result = await execute_graph_search(plan, graph_store)
    assert result is not None and len(result.entities) == 1
    entity = result.entities[0]
    assert entity.name == "ShortName"
    # 비어있지 않고 자연어 fallback 텍스트가 들어감
    assert entity.description
    assert "ShortName" in entity.description
    assert "system" in entity.description


@pytest.mark.asyncio
async def test_execute_search_seeds_augment_always_on(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """R2 (F-SRCH-R2-03): search_steps 가 일부 성공해도 query_embedding 으로
    top-k 시드를 always-on 합집합 보강한다.

    LLM 이 질의에 명시된 sink 이웃을 search step 으로 답하면 retrieved 가
    sink 자신만 담겨 gold seed 누락 — 양방향 traversal 보강 위에 의미
    유사도 보강을 추가한다.
    """
    doc_id = await _create_doc(meta_store)
    # 그래프: AuthService (gold) → KakaoPay (sink)
    # 시나리오: LLM 이 KakaoPay 를 시드로 답해도 query embedding 으로
    # AuthService 가 추가 시드로 보강되어 retrieved 에 포함되어야 한다.
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name="AuthService", entity_type="service"),
            Entity(name="KakaoPay", entity_type="team"),
        ],
        relations=[
            Relation(source="AuthService", target="KakaoPay", relation_type="depends_on"),
        ],
    ))
    mock_embed = AsyncMock()
    # AuthService 가 query embedding 과 더 비슷하게 임베딩
    mock_embed.aembed_documents = AsyncMock(return_value=[[0.95, 0.05], [0.1, 0.9]])
    await graph_store.build_entity_embeddings(mock_embed)

    plan = GraphSearchPlan(
        should_search=True,
        search_steps=[SearchStep(entity_name="KakaoPay", depth=1)],
    )
    # query embedding 이 AuthService 와 cosine ~0.95 (threshold 0.6 통과)
    result = await execute_graph_search(
        plan, graph_store, query_embedding=[1.0, 0.0],
    )
    assert result is not None
    names = {e.name for e in result.entities}
    # KakaoPay 는 step 이 잡았고, 양방향 traversal 로 AuthService 도 잡아야 한다.
    # 양방향이 작동 안 해도 always-on 보강이 AuthService 를 추가로 잡아야 한다.
    assert "KakaoPay" in names
    assert "AuthService" in names


@pytest.mark.asyncio
async def test_execute_search_description_uses_relation_summary(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """R2 (F-METRIC-R2-01): description 이 비어있을 때 1-hop 관계 요약으로
    채운다 — 평가 측 T4 임베딩 매칭의 의미적 분별력을 강화."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        # description 비어있음 — R1 fallback path 발동
        entities=[
            Entity(name="Hub", entity_type="service", description=""),
            Entity(name="Down", entity_type="component", description=""),
        ],
        relations=[
            Relation(source="Hub", target="Down", relation_type="uses"),
        ],
    ))
    plan = GraphSearchPlan(
        should_search=True,
        search_steps=[SearchStep(entity_name="Hub", depth=1)],
    )
    result = await execute_graph_search(plan, graph_store)
    assert result is not None
    by_name = {e.name: e for e in result.entities}
    # Hub 의 description 은 관계 요약 — "Down" 과 "uses" 가 포함
    hub_desc = by_name["Hub"].description
    assert "Down" in hub_desc, f"Hub description should mention neighbor: {hub_desc!r}"
    assert "uses" in hub_desc, f"Hub description should mention relation: {hub_desc!r}"


# --- R3: target_entities / target_relations 정렬 테스트 ---


def test_parse_plan_r3_target_entities_and_relations() -> None:
    """R3: LLM 이 인덱싱과 정렬된 형태 (target_entities + target_relations) 로
    응답하면 파싱된다."""
    data = {
        "should_search": True,
        "reasoning": "Order Service 의 결제 의존성 조회",
        "target_entities": [
            {"name": "Order Service", "type": "service"},
            {"name": "KakaoPay", "type": "team"},
        ],
        "target_relations": [
            {"source": "Order Service", "target": "KakaoPay", "relation_type": "depends_on"},
        ],
    }
    plan = _parse_plan(data)
    assert plan.should_search
    assert plan.has_targets
    assert len(plan.target_entities) == 2
    assert plan.target_entities[0].name == "Order Service"
    assert plan.target_entities[0].entity_type == "service"
    assert len(plan.target_relations) == 1
    rel = plan.target_relations[0]
    assert rel.source == "Order Service"
    assert rel.target == "KakaoPay"
    assert rel.relation_type == "depends_on"


def test_parse_plan_r3_relation_with_empty_endpoint() -> None:
    """R3: 정답이 미상이면 끝점을 빈 문자열로 두어도 파싱된다 (시스템이 fuzzy
    매칭으로 채움)."""
    data = {
        "should_search": True,
        "target_relations": [
            {"source": "", "target": "Elasticsearch", "relation_type": "uses"},
        ],
    }
    plan = _parse_plan(data)
    assert plan.has_targets
    assert plan.target_relations[0].source == ""
    assert plan.target_relations[0].target == "Elasticsearch"


def test_parse_plan_r3_falls_back_to_search_steps() -> None:
    """R3: LLM 이 target_* 없이 구식 search_steps 만 응답하면 후방 호환으로
    SearchStep 으로 파싱된다."""
    data = {
        "should_search": True,
        "search_steps": [
            {"entity_name": "Foo", "depth": 1, "focus_relations": ["uses"]},
        ],
    }
    plan = _parse_plan(data)
    assert plan.should_search
    assert not plan.has_targets
    assert len(plan.search_steps) == 1
    assert plan.search_steps[0].entity_name == "Foo"


@pytest.mark.asyncio
async def test_execute_search_with_target_entities_prioritizes_seed(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """R3: target_entities 로 명시된 노드는 retrieved 의 rank-1 priority 로
    배치된다 — MRR/NDCG 회복의 핵심."""
    doc_id = await _create_doc(meta_store)
    # 그래프: A (정답) → B, C  (B, C 는 이웃)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name="GoldSeed", entity_type="service"),
            Entity(name="Neighbor1", entity_type="component"),
            Entity(name="Neighbor2", entity_type="component"),
        ],
        relations=[
            Relation(source="GoldSeed", target="Neighbor1", relation_type="uses"),
            Relation(source="GoldSeed", target="Neighbor2", relation_type="uses"),
        ],
    ))
    plan = GraphSearchPlan(
        should_search=True,
        target_entities=[TargetEntity(name="GoldSeed", entity_type="service")],
    )
    result = await execute_graph_search(plan, graph_store)
    assert result is not None and result.entities
    # GoldSeed 가 rank-1
    assert result.entities[0].name == "GoldSeed"


@pytest.mark.asyncio
async def test_execute_search_with_target_relations_includes_both_endpoints(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """R3: target_relations 의 source 와 target 양쪽이 모두 retrieved 의 priority
    노드로 포함된다."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name="OrderService", entity_type="service"),
            Entity(name="KakaoPay", entity_type="team"),
            Entity(name="Unrelated", entity_type="service"),
        ],
        relations=[
            Relation(
                source="OrderService", target="KakaoPay",
                relation_type="depends_on",
            ),
        ],
    ))
    plan = GraphSearchPlan(
        should_search=True,
        target_relations=[TargetRelation(
            source="OrderService", target="KakaoPay", relation_type="depends_on",
        )],
    )
    result = await execute_graph_search(plan, graph_store)
    assert result is not None
    names = [e.name for e in result.entities]
    assert "OrderService" in names[:2], f"source endpoint should be priority: {names}"
    assert "KakaoPay" in names[:2], f"target endpoint should be priority: {names}"
    assert "Unrelated" not in names


@pytest.mark.asyncio
async def test_execute_search_target_entity_uses_embedding_fallback(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """R3: target_entity 의 표면 매칭이 실패해도 embedding fallback 으로 회복."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[Entity(name="Auth Service", entity_type="service")],
        relations=[],
    ))
    mock_embed = AsyncMock()
    # "Auth Service" 와 "AuthService" 의 표기 차이 — 임베딩으로 매칭
    mock_embed.aembed_documents = AsyncMock(return_value=[[1.0, 0.0]])
    mock_embed.aembed_query = AsyncMock(return_value=[0.95, 0.05])
    await graph_store.build_entity_embeddings(mock_embed)

    plan = GraphSearchPlan(
        should_search=True,
        target_entities=[TargetEntity(name="AuthService", entity_type="service")],
    )
    result = await execute_graph_search(
        plan, graph_store, embedding_client=mock_embed,
    )
    assert result is not None
    names = {e.name for e in result.entities}
    assert "Auth Service" in names


def test_system_prompt_enforces_exact_entity_name_copy() -> None:
    """R1 F-SRCH-04: 시스템 프롬프트가 'entity_name 을 글자 단위로 정확 복사' 를
    명시적으로 강제한다 — LLM 이 공백/케이스/하이픈을 임의로 바꿔서 매칭이
    실패하는 funnel 손실을 줄인다."""
    from context_loop.processor.graph_search_planner import _render_system_prompt

    prompt = _render_system_prompt()
    # 핵심 키워드 — 표현이 약간 달라도 의도가 살아있는지 확인
    assert "글자 단위" in prompt or "정확히" in prompt
    # 표기 변형 예시가 안내되어 LLM 이 의도를 이해
    assert "공백" in prompt


def test_system_prompt_includes_intent_mapping() -> None:
    """질의 의도 → 관계 매핑 가이드가 시스템 프롬프트에 포함된다."""
    from context_loop.processor.graph_search_planner import _render_system_prompt

    prompt = _render_system_prompt()
    # 핵심 의미 관계가 가이드에 포함되어 있어야 함
    assert "depends_on" in prompt
    assert "owned_by" in prompt
    assert "implements" in prompt
    # R3 — JSON 응답 스키마는 인덱싱 측과 정렬된 target_entities / target_relations
    assert "should_search" in prompt
    assert "target_entities" in prompt
    assert "target_relations" in prompt
