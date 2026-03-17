"""GraphStore 테스트."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from context_loop.processor.graph_extractor import Entity, GraphData, Relation
from context_loop.storage.graph_store import GraphStore, _cosine_similarity
from context_loop.storage.metadata_store import MetadataStore


@pytest.fixture
async def meta_store(tmp_path: Path) -> MetadataStore:  # type: ignore[misc]
    s = MetadataStore(tmp_path / "test.db")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
async def graph_store(meta_store: MetadataStore) -> GraphStore:  # type: ignore[misc]
    return GraphStore(meta_store)


async def _create_doc(store: MetadataStore) -> int:
    return await store.create_document(
        source_type="manual",
        title="Test",
        original_content="content",
        content_hash="abc",
    )


@pytest.mark.asyncio
async def test_save_graph_data(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """엔티티와 관계를 저장한다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[
            Entity(name="Auth Service", entity_type="service"),
            Entity(name="User DB", entity_type="system"),
        ],
        relations=[
            Relation(source="Auth Service", target="User DB", relation_type="uses"),
        ],
    )
    result = await graph_store.save_graph_data(doc_id, data)
    assert result["nodes"] == 2
    assert result["edges"] == 1

    nodes = await meta_store.get_graph_nodes_by_document(doc_id)
    assert len(nodes) == 2
    edges = await meta_store.get_graph_edges_by_document(doc_id)
    assert len(edges) == 1


@pytest.mark.asyncio
async def test_save_graph_data_skips_missing_entity_in_relation(
    graph_store: GraphStore, meta_store: MetadataStore
) -> None:
    """relation의 source/target 엔티티가 없으면 해당 edge는 저장하지 않는다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[Entity(name="A", entity_type="system")],
        relations=[
            Relation(source="A", target="NonExistent", relation_type="depends_on"),
        ],
    )
    result = await graph_store.save_graph_data(doc_id, data)
    assert result["nodes"] == 1
    assert result["edges"] == 0  # B가 없어서 스킵


@pytest.mark.asyncio
async def test_delete_document_graph(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """문서 그래프 삭제 후 노드/엣지가 사라진다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[Entity(name="X"), Entity(name="Y")],
        relations=[Relation(source="X", target="Y", relation_type="related_to")],
    )
    await graph_store.save_graph_data(doc_id, data)
    await graph_store.delete_document_graph(doc_id)

    nodes = await meta_store.get_graph_nodes_by_document(doc_id)
    edges = await meta_store.get_graph_edges_by_document(doc_id)
    assert nodes == []
    assert edges == []
    assert graph_store.stats()["nodes"] == 0


@pytest.mark.asyncio
async def test_get_neighbors(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """엔티티 이름으로 주변 노드를 탐색한다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[
            Entity(name="API Gateway"),
            Entity(name="Auth Service"),
            Entity(name="User DB"),
        ],
        relations=[
            Relation(source="API Gateway", target="Auth Service", relation_type="depends_on"),
            Relation(source="Auth Service", target="User DB", relation_type="uses"),
        ],
    )
    await graph_store.save_graph_data(doc_id, data)

    neighbors = graph_store.get_neighbors("API Gateway", depth=1)
    names = [n["entity_name"] for n in neighbors]
    assert "API Gateway" in names
    assert "Auth Service" in names

    # depth=2면 User DB도 포함
    neighbors2 = graph_store.get_neighbors("API Gateway", depth=2)
    names2 = [n["entity_name"] for n in neighbors2]
    assert "User DB" in names2


@pytest.mark.asyncio
async def test_get_neighbors_nonexistent(graph_store: GraphStore) -> None:
    """존재하지 않는 엔티티는 빈 목록을 반환한다."""
    result = graph_store.get_neighbors("Nonexistent Entity")
    assert result == []


@pytest.mark.asyncio
async def test_load_from_db(meta_store: MetadataStore, tmp_path: Path) -> None:
    """DB에서 그래프를 로드하여 재구성한다."""
    store1 = GraphStore(meta_store)
    doc_id = await _create_doc(meta_store)
    data = GraphData(entities=[Entity(name="Node A"), Entity(name="Node B")])
    await store1.save_graph_data(doc_id, data)

    # 새 GraphStore 인스턴스로 로드
    store2 = GraphStore(meta_store)
    await store2.load_from_db()
    assert store2.stats()["nodes"] == 2


# --- 엔티티 임베딩 캐시 테스트 ---


def test_cosine_similarity_identical() -> None:
    """동일 벡터의 코사인 유사도는 1.0이다."""
    v = [1.0, 0.0, 0.0]
    assert abs(_cosine_similarity(v, v) - 1.0) < 1e-6


def test_cosine_similarity_orthogonal() -> None:
    """직교 벡터의 코사인 유사도는 0.0이다."""
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert abs(_cosine_similarity(a, b)) < 1e-6


def test_cosine_similarity_zero_vector() -> None:
    """영벡터와의 유사도는 0.0이다."""
    assert _cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


@pytest.mark.asyncio
async def test_build_entity_embeddings(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """엔티티 임베딩을 빌드하면 캐시에 저장된다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[Entity(name="Gateway"), Entity(name="AuthService")],
        relations=[],
    )
    await graph_store.save_graph_data(doc_id, data)

    mock_embed = AsyncMock()
    mock_embed.aembed_documents = AsyncMock(return_value=[[1.0, 0.0], [0.0, 1.0]])

    count = await graph_store.build_entity_embeddings(mock_embed)
    assert count == 2
    assert graph_store.entity_embedding_count == 2

    # 이미 캐시됨 → 다시 빌드해도 0
    count2 = await graph_store.build_entity_embeddings(mock_embed)
    assert count2 == 0


@pytest.mark.asyncio
async def test_search_entities_by_embedding(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """임베딩 유사도로 엔티티를 검색한다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[
            Entity(name="Gateway", entity_type="component"),
            Entity(name="AuthService", entity_type="service"),
            Entity(name="Database", entity_type="system"),
        ],
        relations=[],
    )
    await graph_store.save_graph_data(doc_id, data)

    mock_embed = AsyncMock()
    mock_embed.aembed_documents = AsyncMock(return_value=[
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ])
    await graph_store.build_entity_embeddings(mock_embed)

    # Gateway와 유사한 벡터로 검색
    results = graph_store.search_entities_by_embedding(
        [0.9, 0.1, 0.0], threshold=0.7, top_k=2,
    )
    assert len(results) >= 1
    assert results[0]["entity_name"] == "Gateway"

    # 아무것도 매칭 안 되는 벡터
    results_empty = graph_store.search_entities_by_embedding(
        [0.0, 0.0, 0.0], threshold=0.7,
    )
    assert results_empty == []


@pytest.mark.asyncio
async def test_delete_clears_embedding_cache(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """문서 삭제 시 임베딩 캐시도 삭제된다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(entities=[Entity(name="NodeA")], relations=[])
    await graph_store.save_graph_data(doc_id, data)

    mock_embed = AsyncMock()
    mock_embed.aembed_documents = AsyncMock(return_value=[[1.0, 0.0]])
    await graph_store.build_entity_embeddings(mock_embed)
    assert graph_store.entity_embedding_count == 1

    await graph_store.delete_document_graph(doc_id)
    assert graph_store.entity_embedding_count == 0


# --- 그래프 스키마 요약 테스트 ---


@pytest.mark.asyncio
async def test_get_schema_summary_empty(graph_store: GraphStore) -> None:
    """빈 그래프의 스키마 요약은 모든 값이 0/빈 값이다."""
    summary = graph_store.get_schema_summary()
    assert summary["total_nodes"] == 0
    assert summary["total_edges"] == 0
    assert summary["entity_types"] == {}
    assert summary["relation_types"] == {}


@pytest.mark.asyncio
async def test_get_schema_summary_with_data(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """그래프 데이터가 있으면 유형별 집계를 반환한다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[
            Entity(name="Gateway", entity_type="component"),
            Entity(name="AuthService", entity_type="service"),
            Entity(name="UserDB", entity_type="system"),
        ],
        relations=[
            Relation(source="Gateway", target="AuthService", relation_type="depends_on"),
            Relation(source="AuthService", target="UserDB", relation_type="uses"),
        ],
    )
    await graph_store.save_graph_data(doc_id, data)

    summary = graph_store.get_schema_summary()
    assert summary["total_nodes"] == 3
    assert summary["total_edges"] == 2
    assert "component" in summary["entity_types"]
    assert "service" in summary["entity_types"]
    assert "depends_on" in summary["relation_types"]
    assert "uses" in summary["relation_types"]
    assert "Gateway" in summary["entities_by_type"]["component"]
    assert len(summary["sample_relations"]) == 2


@pytest.mark.asyncio
async def test_format_schema_for_llm_empty(graph_store: GraphStore) -> None:
    """빈 그래프의 LLM 포맷은 비어있음을 알린다."""
    text = graph_store.format_schema_for_llm()
    assert "비어 있습니다" in text


@pytest.mark.asyncio
async def test_format_schema_for_llm_with_data(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """그래프 데이터가 있으면 LLM이 읽을 수 있는 텍스트를 생성한다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[
            Entity(name="Gateway", entity_type="component"),
            Entity(name="AuthService", entity_type="service"),
        ],
        relations=[
            Relation(source="Gateway", target="AuthService", relation_type="depends_on"),
        ],
    )
    await graph_store.save_graph_data(doc_id, data)

    text = graph_store.format_schema_for_llm()
    assert "지식 그래프 구조" in text
    assert "Gateway" in text
    assert "AuthService" in text
    assert "depends_on" in text
    assert "component" in text
    assert "service" in text


@pytest.mark.asyncio
async def test_schema_cache_invalidation(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """그래프 변경 시 스키마 캐시가 무효화된다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[Entity(name="NodeA", entity_type="service")],
        relations=[],
    )
    await graph_store.save_graph_data(doc_id, data)

    # 첫 호출: 캐시 생성
    summary1 = graph_store.get_schema_summary()
    assert summary1["total_nodes"] == 1

    # 같은 호출: 캐시 반환 (동일 객체)
    summary2 = graph_store.get_schema_summary()
    assert summary1 is summary2

    # 새 노드 추가 → 캐시 무효화
    doc_id2 = await meta_store.create_document(
        source_type="manual", title="T2", original_content="c", content_hash="h2",
    )
    await graph_store.save_graph_data(doc_id2, GraphData(
        entities=[Entity(name="NodeB", entity_type="component")],
        relations=[],
    ))

    summary3 = graph_store.get_schema_summary()
    assert summary3["total_nodes"] == 2
    assert summary3 is not summary1

    # 삭제 → 캐시 무효화
    await graph_store.delete_document_graph(doc_id)
    summary4 = graph_store.get_schema_summary()
    assert summary4["total_nodes"] == 1
    assert summary4 is not summary3


@pytest.mark.asyncio
async def test_schema_total_entity_limit(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """max_total_entities로 LLM에 전달할 엔티티 총 수를 제한한다."""
    doc_id = await _create_doc(meta_store)
    # 유형 3개 × 각 5개 = 15개 엔티티
    entities = []
    for etype in ["service", "component", "system"]:
        for i in range(5):
            entities.append(Entity(name=f"{etype}_{i}", entity_type=etype))
    await graph_store.save_graph_data(doc_id, GraphData(entities=entities, relations=[]))

    # max_total_entities=6 → 유형 3개 × 쿼타 2개 = 최대 6개
    summary = graph_store.get_schema_summary(max_entities_per_type=10, max_total_entities=6)
    total_shown = sum(len(names) for names in summary["entities_by_type"].values())
    assert total_shown <= 6
    assert summary["total_nodes"] == 15  # 전체 수는 정확


@pytest.mark.asyncio
async def test_schema_relations_balanced_sampling(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """관계 예시가 유형별로 균등하게 샘플링된다."""
    doc_id = await _create_doc(meta_store)
    entities = [Entity(name=f"E{i}", entity_type="service") for i in range(6)]
    relations = [
        # depends_on 3건
        Relation(source="E0", target="E1", relation_type="depends_on"),
        Relation(source="E1", target="E2", relation_type="depends_on"),
        Relation(source="E2", target="E3", relation_type="depends_on"),
        # uses 3건
        Relation(source="E3", target="E4", relation_type="uses"),
        Relation(source="E4", target="E5", relation_type="uses"),
        Relation(source="E5", target="E0", relation_type="uses"),
    ]
    await graph_store.save_graph_data(doc_id, GraphData(entities=entities, relations=relations))

    summary = graph_store.get_schema_summary()
    sample_types = [r["type"] for r in summary["sample_relations"]]
    # 두 유형 모두 샘플에 포함되어야 함
    assert "depends_on" in sample_types
    assert "uses" in sample_types
