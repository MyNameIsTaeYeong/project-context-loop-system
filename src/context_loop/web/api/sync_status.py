"""Sync 상태 API 엔드포인트.

Confluence 증분 동기화 결과를 영속화한 ``sync_runs`` 테이블을 조회하여
대시보드에 "최근 sync 시각 / 생성·변경·오류 건수"를 노출한다.
SSOT 신뢰의 핵심 신호(이 시스템이 Confluence만큼 최신인가?)를 사용자가
직접 확인할 수 있게 한다.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request

from context_loop.storage.metadata_store import MetadataStore
from context_loop.web.dependencies import get_meta_store, get_templates

router = APIRouter()


def _summarize(run: dict[str, Any]) -> dict[str, Any]:
    """sync_runs 행을 API 응답 형식으로 정규화한다."""
    errors_raw = run.get("errors")
    errors: list[Any] = []
    if errors_raw:
        try:
            errors = json.loads(errors_raw)
        except (ValueError, TypeError):
            errors = []
    return {
        "id": run["id"],
        "source_type": run["source_type"],
        "space_id": run.get("space_id"),
        "started_at": run.get("started_at"),
        "completed_at": run.get("completed_at"),
        "status": run.get("status"),
        "created_count": run.get("created_count", 0),
        "updated_count": run.get("updated_count", 0),
        "unchanged_count": run.get("unchanged_count", 0),
        "error_count": run.get("error_count", 0),
        "errors": errors,
    }


@router.get("/api/sync/status")
async def sync_status(
    limit: int = 20,
    source_type: str | None = None,
    meta_store: MetadataStore = Depends(get_meta_store),
) -> dict[str, Any]:
    """최근 sync 실행 이력과 소스별 마지막 sync 요약을 반환한다."""
    recent = await meta_store.get_recent_sync_runs(
        limit=limit, source_type=source_type,
    )
    last_confluence = await meta_store.get_last_sync_run("confluence")
    return {
        "last_confluence": _summarize(last_confluence) if last_confluence else None,
        "recent": [_summarize(r) for r in recent],
    }


@router.get("/partials/sync-status")
async def sync_status_partial(
    request: Request,
    meta_store: MetadataStore = Depends(get_meta_store),
):
    """대시보드에 표시할 sync 상태 HTML 파셜."""
    templates = get_templates(request)
    last_confluence = await meta_store.get_last_sync_run("confluence")
    summary = _summarize(last_confluence) if last_confluence else None
    return templates.TemplateResponse("partials/sync_status.html", {
        "request": request,
        "last_confluence": summary,
    })
