"""graph_search (임베딩 시딩 기반 그래프 검색) 테스트."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from context_loop.processor.graph_extractor import Entity, GraphData, Relation
from context_loop.processor.graph_search import GraphSearchResult, search_graph
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


def _embed_client(entity_embeddings: list[list[float]]) -> AsyncMock:
    """엔티티 임베딩 lazy 구축용 mock (aembed_documents 1회 응답)."""
    mock = AsyncMock()
    mock.aembed_documents = AsyncMock(return_value=entity_embeddings)
    return mock


@pytest.mark.asyncio
async def test_search_graph_none_query_embedding(graph_store: GraphStore) -> None:
    """query_embedding 이 None 이면 탐색 없이 None 을 반환한다."""
    assert await search_graph(None, graph_store) is None


@pytest.mark.asyncio
async def test_search_graph_empty_graph(graph_store: GraphStore) -> None:
    """빈 그래프에서는 None 을 반환한다."""
    assert await search_graph([1.0, 0.0], graph_store) is None


@pytest.mark.asyncio
async def test_search_graph_no_embeddings_no_client(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """엔티티 임베딩 캐시도 없고 embedding_client 도 없으면 시딩 불가 → None."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[Entity(name="Gateway", entity_type="component")],
        relations=[],
    ))
    assert await search_graph([1.0, 0.0], graph_store) is None


@pytest.mark.asyncio
async def test_search_graph_threshold_gating(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """threshold 를 넘는 시드가 없으면 그래프 섹션 생략 (None) —
    LLM should_search 게이팅의 대체."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[Entity(name="Gateway", entity_type="component")],
        relations=[],
    ))
    await graph_store.build_entity_embeddings(_embed_client([[1.0, 0.0]]))

    # 쿼리 임베딩이 모든 엔티티와 직교 → 유사도 0 < threshold
    assert await search_graph([0.0, 1.0], graph_store) is None


@pytest.mark.asyncio
async def test_search_graph_seed_and_one_hop(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """시드 엔티티와 1-hop 이웃·내부 엣지·document_ids 가 결과에 담긴다."""
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
    await graph_store.build_entity_embeddings(
        _embed_client([[1.0, 0.0], [0.0, 1.0]]),
    )

    result = await search_graph([0.95, 0.05], graph_store)
    assert result is not None
    assert isinstance(result, GraphSearchResult)
    assert "Gateway" in result.text
    assert "AuthService" in result.text
    assert "depends_on" in result.text
    assert doc_id in result.document_ids
    names = {e.name for e in result.entities}
    assert names == {"Gateway", "AuthService"}


@pytest.mark.asyncio
async def test_search_graph_seed_ranked_first(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """entities 순서는 시드(유사도 내림차순) 우선 — rank 민감 메트릭
    (MRR/NDCG) 이 쿼리와 가장 유사한 노드를 rank-1 로 본다."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name="Neighbor", entity_type="component"),
            Entity(name="GoldSeed", entity_type="service"),
        ],
        relations=[
            Relation(source="GoldSeed", target="Neighbor", relation_type="uses"),
        ],
    ))
    # Neighbor 는 쿼리와 직교, GoldSeed 는 거의 일치
    await graph_store.build_entity_embeddings(
        _embed_client([[0.0, 1.0], [1.0, 0.0]]),
    )

    result = await search_graph([1.0, 0.0], graph_store)
    assert result is not None and result.entities
    assert result.entities[0].name == "GoldSeed"


@pytest.mark.asyncio
async def test_search_graph_bidirectional_expansion(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """sink 노드가 시드여도 양방향 1-hop 으로 상류 노드가 포함된다."""
    doc_id = await _create_doc(meta_store)
    # AuthService --[depends_on]--> KakaoPay (sink). KakaoPay 를 시드로.
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name="AuthService", entity_type="service"),
            Entity(name="KakaoPay", entity_type="team"),
        ],
        relations=[
            Relation(source="AuthService", target="KakaoPay", relation_type="depends_on"),
        ],
    ))
    await graph_store.build_entity_embeddings(
        _embed_client([[0.0, 1.0], [1.0, 0.0]]),
    )

    result = await search_graph([1.0, 0.0], graph_store)
    assert result is not None
    names = {e.name for e in result.entities}
    assert "KakaoPay" in names
    assert "AuthService" in names


@pytest.mark.asyncio
async def test_search_graph_lazy_builds_entity_embeddings(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """엔티티 임베딩이 없으면 embedding_client 로 lazy 구축 후 시딩한다."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[Entity(name="Gateway", entity_type="component")],
        relations=[],
    ))
    client = _embed_client([[1.0, 0.0]])

    result = await search_graph([0.95, 0.05], graph_store, embedding_client=client)
    assert result is not None
    client.aembed_documents.assert_awaited_once()
    assert {e.name for e in result.entities} == {"Gateway"}


@pytest.mark.asyncio
async def test_search_graph_description_preserved(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """노드 properties 의 description 이 있으면 그대로 노출된다."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[Entity(
            name="Gateway", entity_type="component",
            description="모든 요청의 진입점",
        )],
        relations=[],
    ))
    await graph_store.build_entity_embeddings(_embed_client([[1.0, 0.0]]))

    result = await search_graph([1.0, 0.0], graph_store)
    assert result is not None
    assert result.entities[0].description == "모든 요청의 진입점"
    assert "모든 요청의 진입점" in result.text


@pytest.mark.asyncio
async def test_search_graph_description_fallback_relation_summary(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """description 이 비어 있으면 1-hop 관계 요약 자연어로 채운다 —
    평가 측 T4 임베딩 매칭의 의미적 분별력 유지."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name="Hub", entity_type="service", description=""),
            Entity(name="Down", entity_type="component", description=""),
        ],
        relations=[
            # uses 는 alias — 저장 시 depends_on 으로 정규화된다.
            Relation(source="Hub", target="Down", relation_type="uses"),
        ],
    ))
    await graph_store.build_entity_embeddings(
        _embed_client([[1.0, 0.0], [0.9, 0.1]]),
    )

    result = await search_graph([1.0, 0.0], graph_store)
    assert result is not None
    by_name = {e.name: e for e in result.entities}
    hub_desc = by_name["Hub"].description
    assert "Down" in hub_desc
    assert "depends_on" in hub_desc


@pytest.mark.asyncio
async def test_search_graph_description_fallback_isolated_node(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """관계가 없는 단독 노드의 description fallback 은 이름/타입을 포함한다."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[Entity(name="ShortName", entity_type="system", description="")],
        relations=[],
    ))
    await graph_store.build_entity_embeddings(_embed_client([[1.0, 0.0]]))

    result = await search_graph([1.0, 0.0], graph_store)
    assert result is not None and len(result.entities) == 1
    entity = result.entities[0]
    assert entity.description
    assert "ShortName" in entity.description
    assert "system" in entity.description


@pytest.mark.asyncio
async def test_search_graph_exposes_relations(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """--score-relations 평가용으로 검색된 엣지가 relations 에 노출된다."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name="OrderService", entity_type="service"),
            Entity(name="KakaoPay", entity_type="team"),
        ],
        relations=[
            Relation(source="OrderService", target="KakaoPay", relation_type="depends_on"),
        ],
    ))
    await graph_store.build_entity_embeddings(
        _embed_client([[1.0, 0.0], [0.9, 0.1]]),
    )

    result = await search_graph([1.0, 0.0], graph_store)
    assert result is not None and result.relations
    rel = result.relations[0]
    assert rel.source_name == "OrderService"
    assert rel.target_name == "KakaoPay"
    assert rel.relation_type == "depends_on"
    assert rel.description


@pytest.mark.asyncio
async def test_search_graph_deterministic(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """같은 인덱스 + 같은 쿼리 임베딩이면 결과가 항상 같다 (LLM 0회)."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name="A", entity_type="service"),
            Entity(name="B", entity_type="service"),
            Entity(name="C", entity_type="system"),
        ],
        relations=[
            Relation(source="A", target="B", relation_type="calls"),
            Relation(source="B", target="C", relation_type="uses"),
        ],
    ))
    await graph_store.build_entity_embeddings(
        _embed_client([[1.0, 0.0], [0.8, 0.2], [0.0, 1.0]]),
    )

    r1 = await search_graph([1.0, 0.0], graph_store)
    r2 = await search_graph([1.0, 0.0], graph_store)
    assert r1 is not None and r2 is not None
    assert r1.text == r2.text
    assert [e.name for e in r1.entities] == [e.name for e in r2.entities]
    assert r1.document_ids == r2.document_ids


@pytest.mark.asyncio
async def test_search_graph_top_k_limits_seeds(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """seed_top_k 로 시드 수를 제한한다 (이웃이 없는 독립 노드 기준)."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name=f"E{i}", entity_type="service") for i in range(4)
        ],
        relations=[],
    ))
    # 모두 쿼리와 동일 방향이나 유사도가 조금씩 다름
    await graph_store.build_entity_embeddings(_embed_client([
        [1.0, 0.0], [0.99, 0.01], [0.98, 0.02], [0.97, 0.03],
    ]))

    result = await search_graph([1.0, 0.0], graph_store, seed_top_k=2)
    assert result is not None
    # 독립 노드라 이웃 확장이 없으므로 시드 2개만 결과에 담긴다
    assert len(result.entities) == 2
