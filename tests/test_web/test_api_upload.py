"""파일 업로드 API 테스트."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_upload_md_file(client, stores):
    """마크다운 파일 업로드가 정상 동작한다."""
    resp = await client.post(
        "/api/upload",
        files={"file": ("test.md", b"# Hello\n\nWorld", "text/markdown")},
    )
    assert resp.status_code == 204
    assert "/documents/" in resp.headers.get("hx-redirect", "")


@pytest.mark.asyncio
async def test_upload_txt_file(client, stores):
    """텍스트 파일 업로드가 정상 동작한다."""
    resp = await client.post(
        "/api/upload",
        files={"file": ("note.txt", b"Plain text content", "text/plain")},
    )
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_upload_unsupported_type(client, stores):
    """지원하지 않는 파일 형식은 400을 반환한다."""
    resp = await client.post(
        "/api/upload",
        files={"file": ("image.png", b"\x89PNG", "image/png")},
    )
    assert resp.status_code == 400
