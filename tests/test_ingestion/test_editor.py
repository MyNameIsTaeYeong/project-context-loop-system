"""마크다운 직접 작성 저장 테스트."""

from __future__ import annotations

from pathlib import Path

import pytest

from context_loop.ingestion.editor import save_document
from context_loop.storage.metadata_store import MetadataStore


@pytest.fixture
async def store(tmp_path: Path) -> MetadataStore:  # type: ignore[misc]
    s = MetadataStore(tmp_path / "test.db")
    await s.initialize()
    yield s
    await s.close()


@pytest.mark.asyncio
async def test_save_creates_new_document(store: MetadataStore) -> None:
    """신규 문서를 생성한다."""
    result = await save_document(store, "My Doc", "# Hello\nWorld")
    assert result["created"] is True
    assert result["changed"] is True
    assert result["source_type"] == "manual"
    assert result["title"] == "My Doc"
    assert result["original_content"] == "# Hello\nWorld"


@pytest.mark.asyncio
async def test_save_no_change(store: MetadataStore) -> None:
    """같은 내용으로 저장하면 변경 없음을 반환한다."""
    first = await save_document(store, "Doc", "content")
    doc_id = first["id"]
    result = await save_document(store, "Doc", "content", document_id=doc_id)
    assert result["created"] is False
    assert result["changed"] is False


@pytest.mark.asyncio
async def test_save_updates_existing(store: MetadataStore) -> None:
    """내용이 변경되면 기존 문서를 갱신한다."""
    first = await save_document(store, "Doc", "original")
    doc_id = first["id"]
    result = await save_document(store, "Doc", "updated content", document_id=doc_id)
    assert result["created"] is False
    assert result["changed"] is True
    assert result["status"] == "changed"
    assert result["original_content"] == "updated content"


@pytest.mark.asyncio
async def test_save_invalid_document_id(store: MetadataStore) -> None:
    """존재하지 않는 document_id면 ValueError를 발생시킨다."""
    with pytest.raises(ValueError, match="문서를 찾을 수 없습니다"):
        await save_document(store, "Doc", "content", document_id=999)
