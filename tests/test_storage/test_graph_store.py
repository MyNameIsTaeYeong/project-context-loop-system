"""GraphStore 테스트."""

from __future__ import annotations

from pathlib import Path

import pytest

from context_loop.processor.graph_extractor import Entity, GraphData, Relation
from context_loop.storage.graph_store import GraphStore
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
