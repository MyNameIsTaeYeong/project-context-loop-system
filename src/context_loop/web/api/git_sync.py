"""Git 동기화 페이지 및 API 엔드포인트."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from langchain_core.embeddings import Embeddings

from context_loop.config import Config
from context_loop.ingestion.git_config import GitSourceConfig, load_git_source_config
from context_loop.processor.llm_client import LLMClient
from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore
from context_loop.web.dependencies import (
    get_config,
    get_embedding_client,
    get_graph_store,
    get_llm_client,
    get_meta_store,
    get_templates,
    get_vector_store,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# 동기화 상태 관리 (인메모리)
# ---------------------------------------------------------------------------


@dataclass
class SyncStatus:
    """Git 동기화 실행 상태."""

    state: str = "idle"  # idle / running / completed / failed
    phase: str = ""
    started_at: float = 0.0
    completed_at: float = 0.0
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""


_sync_status = SyncStatus()
_sync_lock = asyncio.Lock()


def _get_sync_status() -> dict[str, Any]:
    """현재 동기화 상태를 dict로 반환한다."""
    elapsed = 0.0
    if _sync_status.started_at:
        end = _sync_status.completed_at or time.time()
        elapsed = round(end - _sync_status.started_at, 1)

    return {
        "state": _sync_status.state,
        "phase": _sync_status.phase,
        "elapsed_seconds": elapsed,
        "result": _sync_status.result,
        "error": _sync_status.error,
    }


# ---------------------------------------------------------------------------
# 페이지 라우트
# ---------------------------------------------------------------------------


@router.get("/git-sync")
async def git_sync_page(
    request: Request,
    config: Config = Depends(get_config),
    meta_store: MetadataStore = Depends(get_meta_store),
):
    """Git 동기화 페이지."""
    templates = get_templates(request)

    # 설정 로드 및 검증
    git_config = load_git_source_config(config)
    issues = git_config.validate() if git_config.enabled else []

    # Git 관련 문서 수 조회
    stats = await meta_store.get_stats()

    return templates.TemplateResponse("git_sync.html", {
        "request": request,
        "git_config": git_config,
        "issues": issues,
        "stats": stats,
        "sync_status": _get_sync_status(),
    })


# ---------------------------------------------------------------------------
# HTMX 파셜 라우트
# ---------------------------------------------------------------------------


@router.get("/partials/git-sync/status")
async def sync_status_partial(
    request: Request,
):
    """동기화 상태 HTML 파셜 (폴링용)."""
    templates = get_templates(request)
    return templates.TemplateResponse("partials/git_sync_status.html", {
        "request": request,
        "sync_status": _get_sync_status(),
    })


@router.get("/partials/git-sync/documents")
async def sync_documents_partial(
    request: Request,
    meta_store: MetadataStore = Depends(get_meta_store),
):
    """Git 관련 문서 목록 파셜."""
    templates = get_templates(request)
    docs: list[dict[str, Any]] = []
    for source_type in ("git_code",):
        type_docs = await meta_store.list_documents(source_type=source_type)
        docs.extend(type_docs)
    # 최신순 정렬, 상위 50개
    docs.sort(key=lambda d: d.get("updated_at", ""), reverse=True)
    docs = docs[:50]

    return templates.TemplateResponse("partials/document_list.html", {
        "request": request,
        "documents": docs,
    })


# ---------------------------------------------------------------------------
# API 엔드포인트
# ---------------------------------------------------------------------------


@router.get("/api/git-sync/status")
async def sync_status_json():
    """동기화 상태를 JSON으로 반환한다."""
    return _get_sync_status()


@router.post("/api/git-sync/start")
async def start_sync(
    config: Config = Depends(get_config),
    meta_store: MetadataStore = Depends(get_meta_store),
    vector_store: VectorStore = Depends(get_vector_store),
    graph_store: GraphStore = Depends(get_graph_store),
    embedding_client: Embeddings = Depends(get_embedding_client),
    llm_client: LLMClient = Depends(get_llm_client),
):
    """Git 동기화를 백그라운드로 시작한다."""
    global _sync_status

    if _sync_status.state == "running":
        raise HTTPException(409, "동기화가 이미 실행 중입니다.")

    git_config = load_git_source_config(config)
    if not git_config.enabled:
        raise HTTPException(400, "Git 소스가 비활성화되어 있습니다. config.yaml에서 sources.git.enabled를 true로 설정하세요.")

    if not git_config.repositories:
        raise HTTPException(400, "설정된 레포지토리가 없습니다.")

    # 상태 초기화
    _sync_status = SyncStatus(
        state="running",
        phase="초기화 중...",
        started_at=time.time(),
    )

    # 백그라운드에서 실행 (Phase 9.8: 파이프라인 의존성 전달)
    asyncio.create_task(_run_sync(
        config, meta_store, git_config,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=embedding_client,
        llm_client=llm_client,
    ))

    return {"status": "started"}


async def _run_sync(
    config: Config,
    meta_store: MetadataStore,
    git_config: GitSourceConfig,
    *,
    vector_store: VectorStore | None = None,
    graph_store: GraphStore | None = None,
    embedding_client: Embeddings | None = None,
    llm_client: LLMClient | None = None,
) -> None:
    """백그라운드에서 Git 동기화 파이프라인을 실행한다."""
    global _sync_status

    try:
        from context_loop.ingestion.coordinator import CoordinatorAgent

        coordinator = CoordinatorAgent(
            store=meta_store,
            config=config,
            git_config=git_config,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            llm_client=llm_client,
        )

        _sync_status.phase = "Git 레포지토리 동기화 중..."
        result = await coordinator.run_and_store()

        _sync_status.state = "completed"
        _sync_status.completed_at = time.time()
        _sync_status.phase = "완료"
        _sync_status.result = {
            "products": len(result.product_results),
            "files_processed": result.total_files_processed,
            "errors": len(result.errors),
            "error_details": result.errors[:10],  # 상위 10개만
        }

        logger.info(
            "Git 동기화 완료: 상품=%d, 파일=%d, 오류=%d",
            len(result.product_results),
            result.total_files_processed,
            len(result.errors),
        )

    except Exception as exc:
        logger.exception("Git 동기화 실패")
        _sync_status.state = "failed"
        _sync_status.completed_at = time.time()
        _sync_status.phase = "실패"
        _sync_status.error = str(exc)
