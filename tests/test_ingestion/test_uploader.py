"""FileUploader 테스트."""

from pathlib import Path

import pytest

from context_loop.ingestion.uploader import FileUploader
from context_loop.storage.metadata_store import MetadataStore


@pytest.fixture
async def store(tmp_path: Path) -> MetadataStore:
    s = MetadataStore(tmp_path / "test.db")
    await s.initialize()
    yield s  # type: ignore[misc]
    await s.close()


@pytest.fixture
def uploader(store: MetadataStore) -> FileUploader:
    return FileUploader(store)


async def test_upload_md_file(uploader: FileUploader, store: MetadataStore, tmp_path: Path) -> None:
    md_file = tmp_path / "test.md"
    md_file.write_text("# Test Document\n\nHello world", encoding="utf-8")

    doc_id = await uploader.upload_file(md_file)
    doc = await store.get_document(doc_id)

    assert doc is not None
    assert doc["title"] == "Test Document"
    assert doc["source_type"] == "upload"
    assert "Hello world" in doc["original_content"]


async def test_upload_html_file(uploader: FileUploader, store: MetadataStore, tmp_path: Path) -> None:
    html_file = tmp_path / "test.html"
    html_file.write_text("<h1>HTML Doc</h1><p>Content</p>", encoding="utf-8")

    doc_id = await uploader.upload_file(html_file)
    doc = await store.get_document(doc_id)

    assert doc is not None
    assert doc["title"] == "HTML Doc"
    assert "<h1>" not in doc["original_content"]


async def test_upload_txt_file(uploader: FileUploader, store: MetadataStore, tmp_path: Path) -> None:
    txt_file = tmp_path / "test.txt"
    txt_file.write_text("Plain text content", encoding="utf-8")

    doc_id = await uploader.upload_file(txt_file)
    doc = await store.get_document(doc_id)

    assert doc is not None
    assert doc["original_content"] == "Plain text content"


async def test_upload_unsupported_format(uploader: FileUploader, tmp_path: Path) -> None:
    pdf_file = tmp_path / "test.pdf"
    pdf_file.write_text("fake pdf", encoding="utf-8")

    with pytest.raises(ValueError, match="지원하지 않는 파일 형식"):
        await uploader.upload_file(pdf_file)


async def test_upload_file_not_found(uploader: FileUploader, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await uploader.upload_file(tmp_path / "nonexistent.md")


async def test_upload_with_custom_title(uploader: FileUploader, store: MetadataStore, tmp_path: Path) -> None:
    md_file = tmp_path / "test.md"
    md_file.write_text("Some content", encoding="utf-8")

    doc_id = await uploader.upload_file(md_file, title="Custom Title")
    doc = await store.get_document(doc_id)

    assert doc is not None
    assert doc["title"] == "Custom Title"


async def test_upload_content_directly(uploader: FileUploader, store: MetadataStore) -> None:
    doc_id = await uploader.upload_content(
        content="# Direct Upload\n\nContent here",
        filename="test.md",
    )
    doc = await store.get_document(doc_id)

    assert doc is not None
    assert doc["title"] == "Direct Upload"
    assert doc["source_type"] == "upload"
