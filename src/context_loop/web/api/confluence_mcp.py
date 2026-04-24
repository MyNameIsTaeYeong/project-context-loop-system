"""Confluence MCP Client 연동 API 엔드포인트."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from langchain_core.embeddings import Embeddings

from context_loop.auth import get_token, store_token
from context_loop.config import Config
from context_loop.ingestion.mcp_confluence import (
    MCPConnectionError,
    connect_mcp,
    estimate_space_page_count,
    format_breadcrumb,
    get_all_spaces,
    get_child_pages,
    get_page_with_ancestors,
    get_space_info,
    get_user_contributed_pages,
    import_page_via_mcp,
    list_available_tools,
    search_content,
    search_content_envelope,
)
from context_loop.processor.pipeline import PipelineConfig
from context_loop.storage.cascade import delete_document_cascade
from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore
from context_loop.sync.mcp_sync import execute_sync_target
from context_loop.web.dependencies import (
    get_config,
    get_embedding_client,
    get_graph_store,
    get_meta_store,
    get_templates,
    get_vector_store,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# 동시성/진행 상태 관리 (인메모리, target_id 단위)
# ---------------------------------------------------------------------------

_target_locks: dict[int, asyncio.Lock] = {}
_target_status: dict[int, dict[str, Any]] = {}


def _get_target_lock(target_id: int) -> asyncio.Lock:
    lock = _target_locks.get(target_id)
    if lock is None:
        lock = asyncio.Lock()
        _target_locks[target_id] = lock
    return lock


def _get_target_status(target_id: int) -> dict[str, Any]:
    """인메모리에 기록된 진행 상태를 반환한다. 없으면 idle."""
    return _target_status.get(target_id, {"state": "idle"})


def _get_server_url(config: Config) -> str:
    """설정에서 MCP 서버 URL을 가져온다."""
    url = config.get("sources.confluence_mcp.server_url", "")
    if not url:
        raise HTTPException(400, "Confluence MCP 서버가 설정되지 않았습니다.")
    return url


def _get_token() -> str | None:
    """keyring에서 MCP 서버 토큰을 가져온다."""
    return get_token("confluence_mcp", "token")


def _get_transport(config: Config) -> str:
    """설정에서 MCP 전송 방식을 가져온다."""
    return config.get("sources.confluence_mcp.transport", "http")


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
    transport: str = Form("http"),
    config: Config = Depends(get_config),
):
    """MCP 서버 연결을 테스트하고 설정을 저장한다."""
    pat = token.strip() or None
    try:
        async with connect_mcp(server_url, token=pat, transport=transport) as session:
            tools = await list_available_tools(session)
    except MCPConnectionError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        raise HTTPException(400, f"연결 실패: {exc}")

    config.set("sources.confluence_mcp.server_url", server_url)
    config.set("sources.confluence_mcp.transport", transport)
    config.set("sources.confluence_mcp.enabled", True)
    config.save()

    if pat:
        store_token("confluence_mcp", "token", pat)

    return {"status": "connected", "tools": tools}


@router.get("/api/confluence-mcp/health")
async def health_check(config: Config = Depends(get_config)):
    """MCP 서버 연결 상태를 진단한다.

    설정값, 토큰 존재 여부, 실제 연결 테스트 결과를 반환한다.
    """
    server_url = config.get("sources.confluence_mcp.server_url", "")
    transport = _get_transport(config)
    token = _get_token()

    info: dict[str, Any] = {
        "server_url": server_url,
        "transport": transport,
        "token_configured": token is not None,
        "enabled": config.get("sources.confluence_mcp.enabled", False),
    }

    if not server_url:
        info["status"] = "not_configured"
        info["message"] = "server_url이 설정되지 않았습니다."
        return info

    try:
        async with connect_mcp(server_url, token=token, transport=transport) as session:
            tools = await list_available_tools(session)
        info["status"] = "ok"
        info["tools_count"] = len(tools)
        info["tools"] = tools
    except MCPConnectionError as exc:
        info["status"] = "error"
        info["message"] = str(exc)
    except Exception as exc:
        info["status"] = "error"
        info["message"] = f"{type(exc).__name__}: {exc}"

    return info


@router.get("/api/confluence-mcp/tools")
async def get_tools(config: Config = Depends(get_config)):
    """MCP 서버에서 사용 가능한 도구 목록을 반환한다."""
    server_url = _get_server_url(config)
    try:
        async with connect_mcp(server_url, token=_get_token(), transport=_get_transport(config)) as session:
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
        async with connect_mcp(server_url, token=_get_token(), transport=_get_transport(config)) as session:
            results = await search_content(session, query)
    except MCPConnectionError as exc:
        raise HTTPException(502, str(exc))
    return {"results": results}


@router.get("/api/confluence-mcp/spaces")
async def list_spaces(config: Config = Depends(get_config)):
    """MCP 서버를 통해 Confluence 스페이스 목록을 반환한다."""
    server_url = _get_server_url(config)
    try:
        async with connect_mcp(server_url, token=_get_token(), transport=_get_transport(config)) as session:
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
        async with connect_mcp(server_url, token=_get_token(), transport=_get_transport(config)) as session:
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
        async with connect_mcp(server_url, token=_get_token(), transport=_get_transport(config)) as session:
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
        async with connect_mcp(server_url, token=_get_token(), transport=_get_transport(config)) as session:
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


# ---------------------------------------------------------------------------
# 3-scope 싱크 지원: 검색 / 예상치 / 대상 등록·조회·재싱크·해제
# ---------------------------------------------------------------------------


@router.get("/api/confluence-mcp/search")
async def search_merged(
    q: str = "",
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """공간과 페이지를 한 번에 검색한다.

    결과:
        ``{"spaces": [...], "pages": [...], "total_pages": int | None}``

    ``spaces`` 는 ``getSpaceInfoAll`` 응답을 클라이언트 검색어로 필터한 결과.
    ``pages`` 는 ``searchContent`` 결과. ``total_pages`` 는 CQL 매치 전체 건수
    (envelope.totalSize) 로 공간 전체 싱크 UX 에는 쓰이지 않지만 페이지 결과의
    맥락을 파악하는 데 유용하다.

    쿼리가 비어 있으면 공간 전체 목록 + 빈 페이지 결과를 반환한다 ("공간 둘러보기" 용도).
    """
    server_url = _get_server_url(config)
    try:
        async with connect_mcp(
            server_url, token=_get_token(), transport=_get_transport(config),
        ) as session:
            spaces_task = get_all_spaces(session)
            if q.strip():
                env_task = search_content_envelope(session, q, limit=25)
                all_spaces, env = await asyncio.gather(spaces_task, env_task)
            else:
                all_spaces = await spaces_task
                env = None
    except MCPConnectionError as exc:
        raise HTTPException(502, str(exc))

    q_lower = q.strip().lower()
    if q_lower:
        matched_spaces = [
            s for s in all_spaces
            if q_lower in str(s.get("key", "")).lower()
            or q_lower in str(s.get("name", "")).lower()
        ]
    else:
        matched_spaces = all_spaces

    return {
        "spaces": matched_spaces,
        "pages": env.results if env else [],
        "total_pages": env.total_size if env else None,
    }


@router.get("/api/confluence-mcp/spaces/{space_key}/estimate")
async def estimate_space(
    space_key: str,
    config: Config = Depends(get_config),
) -> dict[str, Any]:
    """공간 전체 싱크 확인 다이얼로그용 예상 페이지 수."""
    server_url = _get_server_url(config)
    try:
        async with connect_mcp(
            server_url, token=_get_token(), transport=_get_transport(config),
        ) as session:
            count = await estimate_space_page_count(session, space_key)
    except MCPConnectionError as exc:
        raise HTTPException(502, str(exc))
    return {"space_key": space_key, "estimated_pages": count}


@router.post("/api/confluence-mcp/sync-targets")
async def create_sync_target(
    request: Request,
    background_tasks: BackgroundTasks,
    config: Config = Depends(get_config),
    meta_store: MetadataStore = Depends(get_meta_store),
    vector_store: VectorStore = Depends(get_vector_store),
    graph_store: GraphStore = Depends(get_graph_store),
    embedding_client: Embeddings = Depends(get_embedding_client),
) -> dict[str, Any]:
    """싱크 대상을 등록하고 첫 싱크를 백그라운드로 예약한다.

    Body: ``{"scope": "page"|"subtree"|"space", "space_key"?, "page_id"?}``

    - page/subtree: ``page_id`` 필수. ``space_key`` 는 페이지의 space 정보로
      자동 해석되므로 body에 없어도 된다.
    - space: ``space_key`` 필수.
    """
    body = await request.json()
    scope = body.get("scope")
    if scope not in ("page", "subtree", "space"):
        raise HTTPException(400, "scope must be one of page/subtree/space")

    page_id = body.get("page_id")
    space_key = body.get("space_key")

    if scope in ("page", "subtree") and not page_id:
        raise HTTPException(400, "page_id is required for page/subtree scope")
    if scope == "space" and not space_key:
        raise HTTPException(400, "space_key is required for space scope")

    # MCP 호출로 display name 해석
    server_url = _get_server_url(config)
    try:
        async with connect_mcp(
            server_url, token=_get_token(), transport=_get_transport(config),
        ) as session:
            if scope in ("page", "subtree"):
                page = await get_page_with_ancestors(session, str(page_id))
                resolved_space_key = (
                    page.get("space", {}).get("key") if isinstance(page.get("space"), dict)
                    else None
                ) or space_key
                if not resolved_space_key:
                    raise HTTPException(
                        400, "space_key could not be resolved from page",
                    )
                space_key = resolved_space_key
                name = format_breadcrumb(page) or f"Page {page_id}"
            else:
                space = await get_space_info(session, str(space_key))
                name = space.get("name") or str(space_key)
    except MCPConnectionError as exc:
        raise HTTPException(502, str(exc))

    target = await meta_store.upsert_sync_target(
        scope=scope,
        space_key=str(space_key),
        page_id=str(page_id) if page_id else None,
        name=name,
    )

    background_tasks.add_task(
        _run_sync_in_background,
        target["id"], config, meta_store, vector_store, graph_store,
        embedding_client,
    )

    return {
        "target": target,
        "status": _get_target_status(target["id"]) | {"state": "queued"},
    }


@router.get("/api/confluence-mcp/sync-targets")
async def list_sync_targets(
    meta_store: MetadataStore = Depends(get_meta_store),
) -> dict[str, Any]:
    """등록된 싱크 대상 목록 + 각각의 진행 상태."""
    targets = await meta_store.list_sync_targets()
    enriched = [
        {**t, "status": _get_target_status(t["id"])} for t in targets
    ]
    return {"targets": enriched}


@router.get("/api/confluence-mcp/sync-targets/{target_id}")
async def get_sync_target_detail(
    target_id: int,
    meta_store: MetadataStore = Depends(get_meta_store),
) -> dict[str, Any]:
    """단건 조회 + 진행 상태 (폴링용)."""
    target = await meta_store.get_sync_target(target_id)
    if target is None:
        raise HTTPException(404, "sync target not found")
    return {**target, "status": _get_target_status(target_id)}


@router.post("/api/confluence-mcp/sync-targets/{target_id}/sync")
async def trigger_sync_target(
    target_id: int,
    background_tasks: BackgroundTasks,
    config: Config = Depends(get_config),
    meta_store: MetadataStore = Depends(get_meta_store),
    vector_store: VectorStore = Depends(get_vector_store),
    graph_store: GraphStore = Depends(get_graph_store),
    embedding_client: Embeddings = Depends(get_embedding_client),
) -> dict[str, Any]:
    """등록된 대상을 다시 싱크한다 (동일 target 중복 실행 시 409)."""
    target = await meta_store.get_sync_target(target_id)
    if target is None:
        raise HTTPException(404, "sync target not found")

    lock = _get_target_lock(target_id)
    if lock.locked():
        raise HTTPException(409, "sync already in progress")

    background_tasks.add_task(
        _run_sync_in_background,
        target_id, config, meta_store, vector_store, graph_store,
        embedding_client,
    )
    return {"status": {"state": "queued"}}


@router.delete("/api/confluence-mcp/sync-targets/{target_id}")
async def delete_sync_target_endpoint(
    target_id: int,
    meta_store: MetadataStore = Depends(get_meta_store),
    vector_store: VectorStore = Depends(get_vector_store),
    graph_store: GraphStore = Depends(get_graph_store),
) -> dict[str, Any]:
    """싱크 대상을 해제하고 고아 문서를 cascade 삭제한다."""
    deleted, orphan_doc_ids = await meta_store.delete_sync_target(target_id)
    if not deleted:
        raise HTTPException(404, "sync target not found")

    deleted_docs = 0
    for doc_id in orphan_doc_ids:
        if await delete_document_cascade(
            doc_id,
            meta_store=meta_store,
            vector_store=vector_store,
            graph_store=graph_store,
        ):
            deleted_docs += 1

    _target_locks.pop(target_id, None)
    _target_status.pop(target_id, None)

    return {"deleted": True, "deleted_documents": deleted_docs}


# ---------------------------------------------------------------------------
# 백그라운드 싱크 실행
# ---------------------------------------------------------------------------


def _build_pipeline_config(config: Config) -> PipelineConfig:
    """Phase 2 인덱싱에 쓸 :class:`PipelineConfig` 를 앱 설정으로부터 만든다."""
    return PipelineConfig(
        chunk_size=config.get("processor.chunk_size", 512),
        chunk_overlap=config.get("processor.chunk_overlap", 50),
        embedding_model=config.get(
            "processor.embedding_model", "text-embedding-3-small",
        ),
    )


async def _run_sync_in_background(
    target_id: int,
    config: Config,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
    embedding_client: Embeddings,
) -> None:
    """BackgroundTasks 로 호출되는 실제 싱크 러너.

    Phase 1 (임포트) + Phase 2 (인덱싱) 를 한 BackgroundTask 안에서 순차
    실행한다. ``embedding_client`` 가 주입되면 Phase 2 가 자동 수행되고,
    실패 시에는 Phase 1 결과에 영향 없이 ``processing_errors`` 만 채워진다.
    """
    lock = _get_target_lock(target_id)
    if lock.locked():
        logger.info("sync 건너뜀 — 이미 진행 중 target_id=%d", target_id)
        return

    async with lock:
        _target_status[target_id] = {
            "state": "running",
            "started_at": time.time(),
        }
        try:
            target = await meta_store.get_sync_target(target_id)
            if target is None:
                logger.warning("target_id=%d 이 사라짐, sync 중단", target_id)
                _target_status[target_id] = {"state": "missing"}
                return

            pipeline_config = _build_pipeline_config(config)
            phase2_concurrency = int(
                config.get("processor.phase2_concurrency", 5),
            )
            async with connect_mcp(
                _get_server_url(config),
                token=_get_token(),
                transport=_get_transport(config),
            ) as session:
                result = await execute_sync_target(
                    session, target,
                    meta_store=meta_store,
                    vector_store=vector_store,
                    graph_store=graph_store,
                    embedding_client=embedding_client,
                    pipeline_config=pipeline_config,
                    phase2_concurrency=phase2_concurrency,
                )

            await meta_store.update_sync_result(
                target_id, json.dumps(result.to_dict()),
            )
            _target_status[target_id] = {
                "state": "completed",
                "completed_at": time.time(),
                "result": result.to_dict(),
            }
            logger.info(
                "sync 완료 target_id=%d summary=%s",
                target_id, result.to_dict()["summary"],
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("sync 실패 target_id=%d", target_id)
            _target_status[target_id] = {
                "state": "failed",
                "error": str(exc),
                "completed_at": time.time(),
            }
