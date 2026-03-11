"""ManualEditor 테스트."""

from pathlib import Path

import pytest

from context_loop.ingestion.editor import ManualEditor
from context_loop.storage.metadata_store import MetadataStore


@pytest.fixture
async def store(tmp_path: Path) -> MetadataStore:
    s = MetadataStore(tmp_path / "test.db")
    await s.initialize()
    yield s  # type: ignore[misc]
    await s.close()


@pytest.fixture
def editor(store: MetadataStore) -> ManualEditor:
    return ManualEditor(store)


async def test_create_document(editor: ManualEditor, store: MetadataStore) -> None:
    doc_id = await editor.create_document("# My Note\n\nSome content")
    doc = await store.get_document(doc_id)

    assert doc is not None
    assert doc["title"] == "My Note"
    assert doc["source_type"] == "manual"
    assert doc["status"] == "pending"


async def test_create_document_custom_title(editor: ManualEditor, store: MetadataStore) -> None:
    doc_id = await editor.create_document("Content", title="Custom")
    doc = await store.get_document(doc_id)

    assert doc is not None
    assert doc["title"] == "Custom"


async def test_create_document_empty_raises(editor: ManualEditor) -> None:
    with pytest.raises(ValueError, match="비어있습니다"):
        await editor.create_document("")

    with pytest.raises(ValueError, match="비어있습니다"):
        await editor.create_document("   ")


async def test_update_document_changed(editor: ManualEditor, store: MetadataStore) -> None:
    doc_id = await editor.create_document("Original content")
    changed = await editor.update_document(doc_id, "Updated content")

    assert changed is True
    doc = await store.get_document(doc_id)
    assert doc is not None
    assert doc["original_content"] == "Updated content"
    assert doc["version"] == 2
    assert doc["status"] == "processing"


async def test_update_document_no_change(editor: ManualEditor) -> None:
    doc_id = await editor.create_document("Same content")
    changed = await editor.update_document(doc_id, "Same content")
    assert changed is False


async def test_update_document_not_found(editor: ManualEditor) -> None:
    with pytest.raises(ValueError, match="찾을 수 없습니다"):
        await editor.update_document(999, "content")
