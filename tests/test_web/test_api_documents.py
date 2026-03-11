"""문서 API 테스트."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_create_document(client, stores):
    """에디터에서 문서를 생성하면 HX-Redirect가 반환된다."""
    resp = await client.post(
        "/api/documents",
        data={"title": "Test Doc", "content": "Hello World"},
    )
    assert resp.status_code == 204
    assert "/documents/" in resp.headers.get("hx-redirect", "")


@pytest.mark.asyncio
async def test_create_and_view_document(client, stores):
    """문서를 생성하고 상세 페이지를 조회한다."""
    meta_store = stores[0]
    doc_id = await meta_store.create_document(
        source_type="manual",
        title="Test",
        original_content="# Hello",
        content_hash="abc123",
    )

    resp = await client.get(f"/documents/{doc_id}")
    assert resp.status_code == 200
    assert "Test" in resp.text


@pytest.mark.asyncio
async def test_document_original_tab(client, stores):
    """원본 탭 파셜이 문서 내용을 반환한다."""
    meta_store = stores[0]
    doc_id = await meta_store.create_document(
        source_type="manual",
        title="Tab Test",
        original_content="Content here",
        content_hash="h1",
    )

    resp = await client.get(f"/partials/document/{doc_id}/original")
    assert resp.status_code == 200
    assert "Content here" in resp.text


@pytest.mark.asyncio
async def test_document_chunks_tab_empty(client, stores):
    """청크가 없는 문서의 청크 탭은 안내 메시지를 표시한다."""
    meta_store = stores[0]
    doc_id = await meta_store.create_document(
        source_type="manual",
        title="No Chunks",
        original_content="Text",
        content_hash="h2",
    )

    resp = await client.get(f"/partials/document/{doc_id}/chunks")
    assert resp.status_code == 200
    assert "No chunks" in resp.text


@pytest.mark.asyncio
async def test_document_graph_tab_empty(client, stores):
    """그래프가 없는 문서의 그래프 탭은 안내 메시지를 표시한다."""
    meta_store = stores[0]
    doc_id = await meta_store.create_document(
        source_type="manual",
        title="No Graph",
        original_content="Text",
        content_hash="h3",
    )

    resp = await client.get(f"/partials/document/{doc_id}/graph")
    assert resp.status_code == 200
    assert "No graph data" in resp.text


@pytest.mark.asyncio
async def test_document_metadata_tab(client, stores):
    """메타데이터 탭이 문서 정보를 표시한다."""
    meta_store = stores[0]
    doc_id = await meta_store.create_document(
        source_type="upload",
        title="Meta Test",
        original_content="Content",
        content_hash="h4",
    )

    resp = await client.get(f"/partials/document/{doc_id}/metadata")
    assert resp.status_code == 200
    assert "upload" in resp.text
    assert "Meta Test" in resp.text


@pytest.mark.asyncio
async def test_document_status_api(client, stores):
    """문서 상태 API가 정상 동작한다."""
    meta_store = stores[0]
    doc_id = await meta_store.create_document(
        source_type="manual",
        title="Status",
        original_content="C",
        content_hash="h5",
    )

    resp = await client.get(f"/api/documents/{doc_id}/status")
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"


@pytest.mark.asyncio
async def test_delete_document(client, stores):
    """문서 삭제 API가 정상 동작한다."""
    meta_store = stores[0]
    doc_id = await meta_store.create_document(
        source_type="manual",
        title="To Delete",
        original_content="Del",
        content_hash="h6",
    )

    resp = await client.delete(f"/api/documents/{doc_id}")
    assert resp.status_code == 204

    doc = await meta_store.get_document(doc_id)
    assert doc is None


@pytest.mark.asyncio
async def test_document_list_with_filter(client, stores):
    """소스 타입 필터가 동작한다."""
    meta_store = stores[0]
    await meta_store.create_document(
        source_type="manual", title="M1", original_content="A", content_hash="f1",
    )
    await meta_store.create_document(
        source_type="upload", title="U1", original_content="B", content_hash="f2",
    )

    resp = await client.get("/partials/document-list?source_type=manual")
    assert resp.status_code == 200
    assert "M1" in resp.text
    assert "U1" not in resp.text


@pytest.mark.asyncio
async def test_update_document(client, stores):
    """문서 수정 API가 정상 동작한다."""
    meta_store = stores[0]
    doc_id = await meta_store.create_document(
        source_type="manual",
        title="Original",
        original_content="Old content",
        content_hash="old",
    )

    resp = await client.put(
        f"/api/documents/{doc_id}",
        data={"title": "Updated", "content": "New content"},
    )
    assert resp.status_code == 204

    doc = await meta_store.get_document(doc_id)
    assert doc is not None
    assert doc["title"] == "Updated"
