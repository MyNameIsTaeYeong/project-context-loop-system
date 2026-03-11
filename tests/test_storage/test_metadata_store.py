"""MetadataStore 테스트."""

from pathlib import Path

import pytest

from context_loop.storage.metadata_store import MetadataStore


@pytest.fixture
async def store(tmp_path: Path) -> MetadataStore:
    s = MetadataStore(tmp_path / "test.db")
    await s.initialize()
    yield s  # type: ignore[misc]
    await s.close()


async def test_create_and_get_document(store: MetadataStore) -> None:
    doc_id = await store.create_document(
        source_type="manual",
        title="테스트 문서",
        original_content="# Hello\n테스트 내용",
        content_hash="abc123",
    )
    assert doc_id is not None

    doc = await store.get_document(doc_id)
    assert doc is not None
    assert doc["title"] == "테스트 문서"
    assert doc["source_type"] == "manual"
    assert doc["status"] == "pending"


async def test_list_documents_filter(store: MetadataStore) -> None:
    await store.create_document(
        source_type="manual", title="문서1", original_content="a", content_hash="h1"
    )
    await store.create_document(
        source_type="upload", title="문서2", original_content="b", content_hash="h2"
    )

    all_docs = await store.list_documents()
    assert len(all_docs) == 2

    manual_docs = await store.list_documents(source_type="manual")
    assert len(manual_docs) == 1
    assert manual_docs[0]["title"] == "문서1"


async def test_update_document_status(store: MetadataStore) -> None:
    doc_id = await store.create_document(
        source_type="manual", title="문서", original_content="x", content_hash="h"
    )
    await store.update_document_status(doc_id, "completed", storage_method="chunk")

    doc = await store.get_document(doc_id)
    assert doc is not None
    assert doc["status"] == "completed"
    assert doc["storage_method"] == "chunk"


async def test_update_document_content(store: MetadataStore) -> None:
    doc_id = await store.create_document(
        source_type="manual", title="문서", original_content="old", content_hash="h1"
    )
    await store.update_document_content(doc_id, "new content", "h2")

    doc = await store.get_document(doc_id)
    assert doc is not None
    assert doc["original_content"] == "new content"
    assert doc["content_hash"] == "h2"
    assert doc["version"] == 2
    assert doc["status"] == "processing"


async def test_delete_document_cascades(store: MetadataStore) -> None:
    doc_id = await store.create_document(
        source_type="manual", title="문서", original_content="x", content_hash="h"
    )
    await store.create_chunk(
        chunk_id="c1", document_id=doc_id, chunk_index=0, content="chunk", token_count=5
    )
    node_id = await store.create_graph_node(
        document_id=doc_id, entity_name="Entity1", entity_type="concept"
    )

    await store.delete_document(doc_id)

    assert await store.get_document(doc_id) is None
    assert await store.get_chunks_by_document(doc_id) == []
    assert await store.get_graph_nodes_by_document(doc_id) == []


async def test_chunks_crud(store: MetadataStore) -> None:
    doc_id = await store.create_document(
        source_type="manual", title="문서", original_content="x", content_hash="h"
    )
    await store.create_chunk(
        chunk_id="c1", document_id=doc_id, chunk_index=0, content="첫 번째 청크", token_count=10
    )
    await store.create_chunk(
        chunk_id="c2", document_id=doc_id, chunk_index=1, content="두 번째 청크", token_count=8
    )

    chunks = await store.get_chunks_by_document(doc_id)
    assert len(chunks) == 2
    assert chunks[0]["chunk_index"] == 0
    assert chunks[1]["chunk_index"] == 1

    await store.delete_chunks_by_document(doc_id)
    assert await store.get_chunks_by_document(doc_id) == []


async def test_graph_nodes_and_edges(store: MetadataStore) -> None:
    doc_id = await store.create_document(
        source_type="manual", title="문서", original_content="x", content_hash="h"
    )
    node1 = await store.create_graph_node(
        document_id=doc_id, entity_name="서비스A", entity_type="system"
    )
    node2 = await store.create_graph_node(
        document_id=doc_id, entity_name="서비스B", entity_type="system"
    )
    edge_id = await store.create_graph_edge(
        document_id=doc_id,
        source_node_id=node1,
        target_node_id=node2,
        relation_type="depends_on",
    )

    nodes = await store.get_graph_nodes_by_document(doc_id)
    assert len(nodes) == 2

    edges = await store.get_graph_edges_by_document(doc_id)
    assert len(edges) == 1
    assert edges[0]["relation_type"] == "depends_on"

    await store.delete_graph_data_by_document(doc_id)
    assert await store.get_graph_nodes_by_document(doc_id) == []
    assert await store.get_graph_edges_by_document(doc_id) == []


async def test_processing_history(store: MetadataStore) -> None:
    doc_id = await store.create_document(
        source_type="manual", title="문서", original_content="x", content_hash="h"
    )
    history_id = await store.add_processing_history(
        document_id=doc_id, action="created", new_storage_method="chunk"
    )
    await store.complete_processing_history(history_id, status="completed")

    history = await store.get_processing_history(doc_id)
    assert len(history) == 1
    assert history[0]["action"] == "created"
    assert history[0]["status"] == "completed"
    assert history[0]["completed_at"] is not None


async def test_get_stats(store: MetadataStore) -> None:
    doc_id = await store.create_document(
        source_type="manual", title="문서", original_content="x", content_hash="h"
    )
    await store.create_chunk(
        chunk_id="c1", document_id=doc_id, chunk_index=0, content="chunk", token_count=5
    )
    await store.create_graph_node(
        document_id=doc_id, entity_name="E1", entity_type="concept"
    )

    stats = await store.get_stats()
    assert stats["document_count"] == 1
    assert stats["chunk_count"] == 1
    assert stats["node_count"] == 1
    assert stats["edge_count"] == 0
