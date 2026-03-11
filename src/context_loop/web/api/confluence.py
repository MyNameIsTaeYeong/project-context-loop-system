"""Confluence 연동 API 엔드포인트."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from context_loop.auth import get_token, store_token
from context_loop.config import Config
from context_loop.ingestion.confluence import (
    ConfluenceAuthError,
    ConfluenceClient,
    import_page,
    import_space,
)
from context_loop.storage.metadata_store import MetadataStore
from context_loop.web.dependencies import get_config, get_meta_store, get_templates

logger = logging.getLogger(__name__)

router = APIRouter()


def _create_client(config: Config) -> ConfluenceClient:
    """설정에서 ConfluenceClient를 생성한다."""
    base_url = config.get("sources.confluence.base_url", "")
    email = config.get("sources.confluence.email", "")
    token = get_token("confluence", email) or ""
    if not base_url or not email or not token:
        raise HTTPException(400, "Confluence 연결이 설정되지 않았습니다.")
    return ConfluenceClient(base_url, email, token)


@router.get("/confluence")
async def confluence_page(
    request: Request,
    config: Config = Depends(get_config),
):
    """Confluence 임포트 페이지."""
    templates = get_templates(request)
    connected = bool(
        config.get("sources.confluence.base_url")
        and config.get("sources.confluence.email")
    )
    return templates.TemplateResponse("confluence.html", {
        "request": request,
        "connected": connected,
        "base_url": config.get("sources.confluence.base_url", ""),
        "email": config.get("sources.confluence.email", ""),
    })


@router.post("/api/confluence/connect")
async def connect_confluence(
    base_url: str = Form(...),
    email: str = Form(...),
    token: str = Form(...),
    config: Config = Depends(get_config),
):
    """Confluence 연결을 설정하고 테스트한다."""
    client = ConfluenceClient(base_url, email, token)
    try:
        await client.list_spaces(limit=1)
    except ConfluenceAuthError as exc:
        raise HTTPException(401, str(exc))
    except Exception as exc:
        raise HTTPException(400, f"Connection failed: {exc}")

    store_token("confluence", email, token)
    config.set("sources.confluence.base_url", base_url)
    config.set("sources.confluence.email", email)
    config.save()

    return {"status": "connected"}


@router.get("/api/confluence/spaces")
async def list_spaces(config: Config = Depends(get_config)):
    """Confluence 스페이스 목록을 반환한다."""
    client = _create_client(config)
    try:
        spaces = await client.list_spaces()
    except ConfluenceAuthError as exc:
        raise HTTPException(401, str(exc))
    return {"spaces": spaces}


@router.get("/api/confluence/spaces/{space_id}/pages")
async def list_pages(
    space_id: str,
    config: Config = Depends(get_config),
):
    """스페이스의 페이지 목록을 반환한다."""
    client = _create_client(config)
    pages = await client.list_pages(space_id)
    return {"pages": pages}


@router.post("/api/confluence/import")
async def import_pages(
    request: Request,
    config: Config = Depends(get_config),
    meta_store: MetadataStore = Depends(get_meta_store),
):
    """선택한 Confluence 페이지를 임포트한다."""
    body = await request.json()
    page_ids: list[str] = body.get("page_ids", [])
    if not page_ids:
        raise HTTPException(400, "page_ids is required")

    client = _create_client(config)
    base_url = config.get("sources.confluence.base_url", "")
    results = []
    for pid in page_ids:
        try:
            result = await import_page(client, meta_store, pid, base_url)
            results.append({"page_id": pid, "doc_id": result["id"], "created": result["created"]})
        except Exception as exc:
            results.append({"page_id": pid, "error": str(exc)})

    return {"results": results}
