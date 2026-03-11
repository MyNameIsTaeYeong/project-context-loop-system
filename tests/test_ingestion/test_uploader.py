"""파일 업로드 처리 테스트."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from context_loop.ingestion.uploader import (
    UnsupportedFileTypeError,
    compute_content_hash,
    read_file,
    upload_file,
)
from context_loop.storage.metadata_store import MetadataStore


@pytest.fixture
async def store(tmp_path: Path) -> MetadataStore:  # type: ignore[misc]
    s = MetadataStore(tmp_path / "test.db")
    await s.initialize()
    yield s
    await s.close()


def test_compute_content_hash_deterministic() -> None:
    """동일 내용은 항상 같은 해시를 반환한다."""
    content = "Hello, World!"
    assert compute_content_hash(content) == compute_content_hash(content)


def test_compute_content_hash_different() -> None:
    """다른 내용은 다른 해시를 반환한다."""
    assert compute_content_hash("abc") != compute_content_hash("def")


def test_read_file_supported_extensions(tmp_path: Path) -> None:
    """지원하는 확장자 파일을 정상적으로 읽는다."""
    for ext in (".md", ".txt", ".html"):
        f = tmp_path / f"test{ext}"
        f.write_text("content", encoding="utf-8")
        assert read_file(f) == "content"


def test_read_file_unsupported_extension(tmp_path: Path) -> None:
    """지원하지 않는 확장자는 UnsupportedFileTypeError를 발생시킨다."""
    f = tmp_path / "test.pdf"
    f.write_text("content", encoding="utf-8")
    with pytest.raises(UnsupportedFileTypeError):
        read_file(f)


def test_read_file_not_found(tmp_path: Path) -> None:
    """존재하지 않는 파일은 FileNotFoundError를 발생시킨다."""
    with pytest.raises(FileNotFoundError):
        read_file(tmp_path / "nonexistent.md")


@pytest.mark.asyncio
async def test_upload_file_creates_document(store: MetadataStore, tmp_path: Path) -> None:
    """파일 업로드 시 새 문서를 생성한다."""
    f = tmp_path / "guide.md"
    f.write_text("# Guide\nHello", encoding="utf-8")
    result = await upload_file(store, f, author="user@example.com")
    assert result["created"] is True
    assert result["changed"] is True
    assert result["source_type"] == "upload"
    assert result["source_id"] == "guide.md"
    assert result["title"] == "guide"
    assert result["author"] == "user@example.com"


@pytest.mark.asyncio
async def test_upload_file_no_change(store: MetadataStore, tmp_path: Path) -> None:
    """동일 파일 재업로드 시 변경 없음을 반환한다."""
    f = tmp_path / "doc.md"
    f.write_text("Same content", encoding="utf-8")
    await upload_file(store, f)
    result = await upload_file(store, f)
    assert result["created"] is False
    assert result["changed"] is False


@pytest.mark.asyncio
async def test_upload_file_detects_change(store: MetadataStore, tmp_path: Path) -> None:
    """내용이 변경된 파일 재업로드 시 변경 감지."""
    f = tmp_path / "doc.md"
    f.write_text("Original content", encoding="utf-8")
    await upload_file(store, f)

    f.write_text("Updated content", encoding="utf-8")
    result = await upload_file(store, f)
    assert result["created"] is False
    assert result["changed"] is True
    assert result["status"] == "changed"


@pytest.mark.asyncio
async def test_upload_file_custom_title(store: MetadataStore, tmp_path: Path) -> None:
    """title 파라미터가 주어지면 해당 제목으로 생성한다."""
    f = tmp_path / "doc.md"
    f.write_text("content", encoding="utf-8")
    result = await upload_file(store, f, title="Custom Title")
    assert result["title"] == "Custom Title"
