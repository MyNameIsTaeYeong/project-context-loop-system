"""Git 동기화 페이지 및 API 엔드포인트."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from langchain_core.embeddings import Embeddings

from context_loop.config import Config
from context_loop.ingestion.git_config import GitSourceConfig, load_git_source_config
from context_loop.ingestion.git_repository import purge_synced_results
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


def _validate_repo_url(git_config: GitSourceConfig, repo_url: str) -> None:
    """요청된 repo_url이 설정된 레포 중 하나인지 검증한다."""
    if not any(repo.url == repo_url for repo in git_config.repositories):
        raise HTTPException(
            404, f"설정에 등록되지 않은 레포지토리입니다: {repo_url}",
        )


def _validate_product(
    git_config: GitSourceConfig, repo_url: str, product: str,
) -> None:
    """요청된 product가 해당 레포에 정의되어 있는지 검증한다."""
    repo = next(
        (r for r in git_config.repositories if r.url == repo_url), None,
    )
    if repo is None:
        raise HTTPException(
            404, f"설정에 등록되지 않은 레포지토리입니다: {repo_url}",
        )
    if product not in repo.products:
        raise HTTPException(
            404,
            f"레포지토리 '{repo_url}'에 정의되지 않은 product 입니다: {product}",
        )


@router.delete("/api/git-sync/repositories")
async def purge_repository(
    url: str = Query(..., description="삭제 대상 레포지토리 URL"),
    config: Config = Depends(get_config),
    meta_store: MetadataStore = Depends(get_meta_store),
    vector_store: VectorStore = Depends(get_vector_store),
    graph_store: GraphStore = Depends(get_graph_store),
):
    """레포 단위로 싱크된 모든 결과(파생 문서·로컬 clone 포함)를 삭제한다.

    싱크가 진행 중이면 충돌을 피하기 위해 409를 반환한다.
    """
    if _sync_status.state == "running":
        raise HTTPException(409, "동기화가 진행 중일 때는 삭제할 수 없습니다.")

    git_config = load_git_source_config(config)
    _validate_repo_url(git_config, url)

    result = await purge_synced_results(
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
        repo_url=url,
        data_dir=config.data_dir,
    )
    return result.to_dict()


@router.delete("/api/git-sync/repositories/products")
async def purge_repository_product(
    url: str = Query(..., description="대상 레포지토리 URL"),
    product: str = Query(..., description="삭제 대상 product 이름"),
    config: Config = Depends(get_config),
    meta_store: MetadataStore = Depends(get_meta_store),
    vector_store: VectorStore = Depends(get_vector_store),
    graph_store: GraphStore = Depends(get_graph_store),
):
    """레포 안의 단일 product 싱크 결과만 삭제한다.

    로컬 clone 디렉토리는 유지한다 (같은 레포의 다른 product 가 사용 중).
    싱크가 진행 중이면 409.
    """
    if _sync_status.state == "running":
        raise HTTPException(409, "동기화가 진행 중일 때는 삭제할 수 없습니다.")

    git_config = load_git_source_config(config)
    _validate_product(git_config, url, product)

    result = await purge_synced_results(
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
        repo_url=url,
        product=product,
    )
    return result.to_dict()


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
