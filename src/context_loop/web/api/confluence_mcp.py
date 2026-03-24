"""Confluence MCP Client 연동 API 엔드포인트."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request

from context_loop.auth import get_token, store_token
from context_loop.config import Config
from context_loop.ingestion.mcp_confluence import (
    MCPConnectionError,
    connect_mcp,
    get_all_spaces,
    get_child_pages,
    get_user_contributed_pages,
    import_page_via_mcp,
    list_available_tools,
    search_content,
)
from context_loop.storage.metadata_store import MetadataStore
from context_loop.web.dependencies import get_config, get_meta_store, get_templates

logger = logging.getLogger(__name__)

router = APIRouter()


def _get_server_url(config: Config) -> str:
    """설정에서 MCP 서버 URL을 가져온다."""
    url = config.get("sources.confluence_mcp.server_url", "")
    if not url:
        raise HTTPException(400, "Confluence MCP 서버가 설정되지 않았습니다.")
    return url


def _get_token() -> str | None:
    """keyring에서 MCP 서버 토큰을 가져온다."""
    return get_token("confluence_mcp", "token")


@router.get("/confluence-mcp")
async def confluence_mcp_page(
    request: Request,
    config: Config = Depends(get_config),
):
    """Confluence MCP 임포트 페이지."""
    templates = get_templates(request)
    connected = bool(config.get("sources.confluence_mcp.server_url"))
    return templates.TemplateResponse("confluence_mcp.html", {
        "request": request,
        "connected": connected,
        "server_url": config.get("sources.confluence_mcp.server_url", ""),
    })


@router.post("/api/confluence-mcp/connect")
async def connect_confluence_mcp(
    server_url: str = Form(...),
    token: str = Form(""),
    config: Config = Depends(get_config),
):
    """MCP 서버 연결을 테스트하고 설정을 저장한다."""
    pat = token.strip() or None
    try:
        async with connect_mcp(server_url, token=pat) as session:
            tools = await list_available_tools(session)
    except MCPConnectionError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(400, f"연결 실패: {exc}")

    config.set("sources.confluence_mcp.server_url", server_url)
    config.set("sources.confluence_mcp.enabled", True)
    config.save()

    if pat:
        store_token("confluence_mcp", "token", pat)

    return {"status": "connected", "tools": tools}


@router.get("/api/confluence-mcp/tools")
async def get_tools(config: Config = Depends(get_config)):
    """MCP 서버에서 사용 가능한 도구 목록을 반환한다."""
    server_url = _get_server_url(config)
    try:
        async with connect_mcp(server_url, token=_get_token()) as session:
            tools = await list_available_tools(session)
    except MCPConnectionError as exc:
        raise HTTPException(502, str(exc))
    return {"tools": tools}


@router.post("/api/confluence-mcp/search")
async def search(
    request: Request,
    config: Config = Depends(get_config),
):
    """MCP 서버를 통해 Confluence 콘텐츠를 검색한다."""
    body = await request.json()
    query = body.get("query", "").strip()
    if not query:
        raise HTTPException(400, "query is required")

    server_url = _get_server_url(config)
    try:
        async with connect_mcp(server_url, token=_get_token()) as session:
            results = await search_content(session, query)
    except MCPConnectionError as exc:
        raise HTTPException(502, str(exc))
    return {"results": results}


@router.get("/api/confluence-mcp/spaces")
async def list_spaces(config: Config = Depends(get_config)):
    """MCP 서버를 통해 Confluence 스페이스 목록을 반환한다."""
    server_url = _get_server_url(config)
    try:
        async with connect_mcp(server_url, token=_get_token()) as session:
            spaces = await get_all_spaces(session)
    except MCPConnectionError as exc:
        raise HTTPException(502, str(exc))
    return {"spaces": spaces}


@router.get("/api/confluence-mcp/pages/{page_id}/children")
async def list_children(
    page_id: str,
    config: Config = Depends(get_config),
):
    """MCP 서버를 통해 하위 페이지 목록을 반환한다."""
    server_url = _get_server_url(config)
    try:
        async with connect_mcp(server_url, token=_get_token()) as session:
            children = await get_child_pages(session, page_id)
    except MCPConnectionError as exc:
        raise HTTPException(502, str(exc))
    return {"pages": children}


@router.get("/api/confluence-mcp/user-pages")
async def user_pages(
    user_id: str,
    config: Config = Depends(get_config),
):
    """MCP 서버를 통해 사용자 기여 페이지를 반환한다."""
    server_url = _get_server_url(config)
    try:
        async with connect_mcp(server_url, token=_get_token()) as session:
            pages = await get_user_contributed_pages(session, user_id)
    except MCPConnectionError as exc:
        raise HTTPException(502, str(exc))
    return {"pages": pages}


@router.post("/api/confluence-mcp/import")
async def import_pages(
    request: Request,
    config: Config = Depends(get_config),
    meta_store: MetadataStore = Depends(get_meta_store),
):
    """선택한 Confluence 페이지를 MCP를 통해 임포트한다."""
    body = await request.json()
    page_ids: list[str] = body.get("page_ids", [])
    if not page_ids:
        raise HTTPException(400, "page_ids is required")

    server_url = _get_server_url(config)
    results = []
    try:
        async with connect_mcp(server_url, token=_get_token()) as session:
            for pid in page_ids:
                try:
                    result = await import_page_via_mcp(session, meta_store, str(pid))
                    results.append({
                        "page_id": pid,
                        "doc_id": result["id"],
                        "title": result.get("title", ""),
                        "created": result["created"],
                        "changed": result["changed"],
                    })
                except Exception as exc:
                    logger.warning("페이지 임포트 실패: %s — %s", pid, exc)
                    results.append({"page_id": pid, "error": str(exc)})
    except MCPConnectionError as exc:
        raise HTTPException(502, str(exc))

    return {"results": results}
