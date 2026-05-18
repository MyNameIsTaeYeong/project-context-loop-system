"""delete_document_cascade 유틸 테스트."""

from __future__ import annotations

from pathlib import Path

import pytest

from context_loop.processor.graph_extractor import Entity, GraphData, Relation
from context_loop.storage.cascade import delete_document_cascade
from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore


@pytest.fixture
async def stores(tmp_path: Path):  # type: ignore[misc]
    meta = MetadataStore(tmp_path / "meta.db")
    await meta.initialize()
    vec = VectorStore(tmp_path)
    vec.initialize()
    graph = GraphStore(meta)
    yield meta, vec, graph
    await meta.close()


async def _seed_document(
    meta: MetadataStore,
    vec: VectorStore,
    graph: GraphStore,
    *,
    title: str,
    entity_names: tuple[str, str],
) -> int:
    """테스트용 문서 + 청크 + 벡터 + 그래프 데이터를 한 번에 생성."""
    doc_id = await meta.create_document(
        source_type="manual",
        title=title,
        original_content="hello",
        content_hash=f"hash-{title}",
    )
    chunk_id = f"c-{doc_id}-0"
    await meta.create_chunk(
        chunk_id=chunk_id,
        document_id=doc_id,
        chunk_index=0,
        content="chunk",
        token_count=5,
    )
    vec.add_chunks(
        chunk_ids=[chunk_id],
        embeddings=[[0.1, 0.2, 0.3]],
        documents=["chunk"],
        metadatas=[{"document_id": doc_id, "chunk_index": 0}],
    )
    e1, e2 = entity_names
    await graph.save_graph_data(
        doc_id,
        GraphData(
            entities=[Entity(name=e1), Entity(name=e2)],
            relations=[Relation(source=e1, target=e2, relation_type="related_to")],
        ),
    )
    return doc_id


async def test_cascade_deletes_all(stores) -> None:
    """벡터·그래프·메타데이터 세 저장소에서 모두 제거된다."""
    meta, vec, graph = stores
    doc_id = await _seed_document(
        meta, vec, graph, title="Doc", entity_names=("X", "Y"),
    )

    assert vec.count() == 1
    assert graph.stats()["nodes"] == 2
    assert await meta.get_document(doc_id) is not None
    assert await meta.get_chunks_by_document(doc_id) != []

    result = await delete_document_cascade(
        doc_id,
        meta_store=meta,
        vector_store=vec,
        graph_store=graph,
    )

    assert result is True
    assert vec.count() == 0
    assert graph.stats()["nodes"] == 0
    assert await meta.get_document(doc_id) is None
    assert await meta.get_chunks_by_document(doc_id) == []
    assert await meta.get_graph_nodes_by_document(doc_id) == []


async def test_cascade_returns_false_for_missing_document(stores) -> None:
    """존재하지 않는 문서 ID는 False를 반환한다."""
    meta, vec, graph = stores
    result = await delete_document_cascade(
        9999,
        meta_store=meta,
        vector_store=vec,
        graph_store=graph,
    )
    assert result is False


async def test_cascade_is_idempotent(stores) -> None:
    """두 번 호출해도 오류 없이 두 번째는 False를 반환한다."""
    meta, vec, graph = stores
    doc_id = await _seed_document(
        meta, vec, graph, title="Doc", entity_names=("X", "Y"),
    )

    first = await delete_document_cascade(
        doc_id, meta_store=meta, vector_store=vec, graph_store=graph,
    )
    second = await delete_document_cascade(
        doc_id, meta_store=meta, vector_store=vec, graph_store=graph,
    )

    assert first is True
    assert second is False


async def test_cascade_preserves_other_documents(stores) -> None:
    """다른 문서의 청크·벡터·그래프는 영향받지 않는다."""
    meta, vec, graph = stores
    doc1 = await _seed_document(
        meta, vec, graph, title="Doc1", entity_names=("A1", "B1"),
    )
    doc2 = await _seed_document(
        meta, vec, graph, title="Doc2", entity_names=("A2", "B2"),
    )

    assert vec.count() == 2
    assert graph.stats()["nodes"] == 4

    deleted = await delete_document_cascade(
        doc1, meta_store=meta, vector_store=vec, graph_store=graph,
    )
    assert deleted is True

    assert await meta.get_document(doc1) is None
    assert await meta.get_document(doc2) is not None
    assert await meta.get_chunks_by_document(doc2) != []
    assert await meta.get_graph_nodes_by_document(doc2) != []
    assert vec.count() == 1
    assert graph.stats()["nodes"] == 2
