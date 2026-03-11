"""ProcessingPipeline 테스트."""

from pathlib import Path

import pytest

from context_loop.processor.pipeline import ProcessingPipeline
from context_loop.storage.metadata_store import MetadataStore


@pytest.fixture
async def store(tmp_path: Path) -> MetadataStore:
    s = MetadataStore(tmp_path / "test.db")
    await s.initialize()
    yield s  # type: ignore[misc]
    await s.close()


@pytest.fixture
def pipeline(store: MetadataStore) -> ProcessingPipeline:
    return ProcessingPipeline(store)


async def test_process_document(pipeline: ProcessingPipeline, store: MetadataStore) -> None:
    doc_id = await store.create_document(
        source_type="manual", title="테스트", original_content="content", content_hash="h1"
    )
    await pipeline.process_document(doc_id)

    doc = await store.get_document(doc_id)
    assert doc is not None
    assert doc["status"] == "completed"

    history = await store.get_processing_history(doc_id)
    assert len(history) == 1
    assert history[0]["action"] == "created"
    assert history[0]["status"] == "completed"


async def test_process_document_not_found(pipeline: ProcessingPipeline) -> None:
    with pytest.raises(ValueError, match="찾을 수 없습니다"):
        await pipeline.process_document(999)


async def test_reprocess_document(pipeline: ProcessingPipeline, store: MetadataStore) -> None:
    doc_id = await store.create_document(
        source_type="manual", title="테스트", original_content="v1", content_hash="h1"
    )
    # 초기 처리
    await pipeline.process_document(doc_id)

    # 청크/그래프 데이터 추가 (재처리 시 삭제되어야 함)
    await store.create_chunk(
        chunk_id="c1", document_id=doc_id, chunk_index=0, content="chunk", token_count=5
    )
    node_id = await store.create_graph_node(
        document_id=doc_id, entity_name="E1", entity_type="concept"
    )

    # 콘텐츠 갱신 후 재처리
    await store.update_document_content(doc_id, "v2", "h2")
    await pipeline.reprocess_document(doc_id)

    # 기존 파생 데이터가 삭제되었는지 확인
    chunks = await store.get_chunks_by_document(doc_id)
    assert len(chunks) == 0

    nodes = await store.get_graph_nodes_by_document(doc_id)
    assert len(nodes) == 0

    doc = await store.get_document(doc_id)
    assert doc is not None
    assert doc["status"] == "completed"


async def test_check_and_reprocess_changed(pipeline: ProcessingPipeline, store: MetadataStore) -> None:
    doc_id = await store.create_document(
        source_type="manual", title="테스트", original_content="old", content_hash="h1"
    )
    changed = await pipeline.check_and_reprocess(doc_id, "new content")
    assert changed is True

    doc = await store.get_document(doc_id)
    assert doc is not None
    assert doc["original_content"] == "new content"


async def test_check_and_reprocess_unchanged(pipeline: ProcessingPipeline, store: MetadataStore) -> None:
    from context_loop.processor.parser import compute_content_hash

    content = "same content"
    h = compute_content_hash(content)
    doc_id = await store.create_document(
        source_type="manual", title="테스트", original_content=content, content_hash=h
    )
    changed = await pipeline.check_and_reprocess(doc_id, content)
    assert changed is False
