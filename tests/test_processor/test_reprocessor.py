"""재처리 파이프라인 테스트."""

from __future__ import annotations

from pathlib import Path

import pytest

from context_loop.processor.reprocessor import (
    DocumentNotFoundError,
    check_and_mark_changed,
    complete_reprocessing,
    delete_derived_data,
    get_pending_documents,
    start_reprocessing,
)
from context_loop.storage.metadata_store import MetadataStore


@pytest.fixture
async def store(tmp_path: Path) -> MetadataStore:  # type: ignore[misc]
    s = MetadataStore(tmp_path / "test.db")
    await s.initialize()
    yield s
    await s.close()


async def _create_doc(store: MetadataStore, content: str = "original") -> int:
    return await store.create_document(
        source_type="manual",
        title="Test Doc",
        original_content=content,
        content_hash=__import__("hashlib").sha256(content.encode()).hexdigest(),
    )


@pytest.mark.asyncio
async def test_check_no_change(store: MetadataStore) -> None:
    """동일 내용이면 False를 반환한다."""
    doc_id = await _create_doc(store, "same content")
    changed = await check_and_mark_changed(store, doc_id, "same content")
    assert changed is False


@pytest.mark.asyncio
async def test_check_detects_change(store: MetadataStore) -> None:
    """내용이 다르면 True를 반환하고 status를 'changed'로 갱신한다."""
    doc_id = await _create_doc(store, "old content")
    changed = await check_and_mark_changed(store, doc_id, "new content")
    assert changed is True
    doc = await store.get_document(doc_id)
    assert doc is not None
    assert doc["status"] == "changed"
    assert doc["original_content"] == "new content"


@pytest.mark.asyncio
async def test_check_document_not_found(store: MetadataStore) -> None:
    """없는 문서는 DocumentNotFoundError를 발생시킨다."""
    with pytest.raises(DocumentNotFoundError):
        await check_and_mark_changed(store, 999, "content")


@pytest.mark.asyncio
async def test_delete_derived_data(store: MetadataStore) -> None:
    """청크와 그래프 데이터를 삭제한다."""
    doc_id = await _create_doc(store)
    await store.create_chunk(
        chunk_id="c1",
        document_id=doc_id,
        chunk_index=0,
        content="chunk text",
        token_count=5,
    )
    await store.create_graph_node(document_id=doc_id, entity_name="Entity A")
    await delete_derived_data(store, doc_id)
    chunks = await store.get_chunks_by_document(doc_id)
    nodes = await store.get_graph_nodes_by_document(doc_id)
    assert chunks == []
    assert nodes == []


@pytest.mark.asyncio
async def test_start_reprocessing(store: MetadataStore) -> None:
    """재처리 시작 시 status가 processing으로 변경되고 이력이 생성된다."""
    doc_id = await _create_doc(store)
    history_id = await start_reprocessing(store, doc_id)
    doc = await store.get_document(doc_id)
    assert doc is not None
    assert doc["status"] == "processing"
    history = await store.get_processing_history(doc_id)
    assert any(h["id"] == history_id for h in history)


@pytest.mark.asyncio
async def test_complete_reprocessing_success(store: MetadataStore) -> None:
    """재처리 완료 후 status가 completed로 변경된다."""
    doc_id = await _create_doc(store)
    history_id = await start_reprocessing(store, doc_id)
    await complete_reprocessing(store, doc_id, history_id, "chunk")
    doc = await store.get_document(doc_id)
    assert doc is not None
    assert doc["status"] == "completed"
    assert doc["storage_method"] == "chunk"


@pytest.mark.asyncio
async def test_complete_reprocessing_failure(store: MetadataStore) -> None:
    """오류 발생 시 status가 failed로 변경된다."""
    doc_id = await _create_doc(store)
    history_id = await start_reprocessing(store, doc_id)
    await complete_reprocessing(store, doc_id, history_id, "chunk", error_message="LLM 오류")
    doc = await store.get_document(doc_id)
    assert doc is not None
    assert doc["status"] == "failed"


@pytest.mark.asyncio
async def test_get_pending_documents(store: MetadataStore) -> None:
    """pending/changed 상태 문서를 반환한다."""
    doc_id = await _create_doc(store)
    pending = await get_pending_documents(store)
    assert any(d["id"] == doc_id for d in pending)

    # completed 상태는 반환하지 않는다
    await store.update_document_status(doc_id, status="completed")
    pending = await get_pending_documents(store)
    assert not any(d["id"] == doc_id for d in pending)
