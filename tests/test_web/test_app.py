"""웹 앱 기본 테스트."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_dashboard_returns_html(client):
    """대시보드 페이지가 HTML로 렌더링된다."""
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Context Loop" in resp.text


@pytest.mark.asyncio
async def test_stats_api_empty(client):
    """빈 DB에서 통계 API가 0값을 반환한다."""
    resp = await client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["document_count"] == 0
    assert data["chunk_count"] == 0


@pytest.mark.asyncio
async def test_stats_partial_returns_html(client):
    """통계 파셜이 HTML을 반환한다."""
    resp = await client.get("/partials/stats")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_document_list_partial_empty(client):
    """빈 DB에서 문서 목록 파셜이 빈 상태 메시지를 반환한다."""
    resp = await client.get("/partials/document-list")
    assert resp.status_code == 200
    assert "No documents found" in resp.text


@pytest.mark.asyncio
async def test_document_not_found(client):
    """존재하지 않는 문서 상세 페이지는 404를 반환한다."""
    resp = await client.get("/documents/999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_editor_new_page(client):
    """새 문서 에디터 페이지가 렌더링된다."""
    resp = await client.get("/editor")
    assert resp.status_code == 200
    assert "New Document" in resp.text
