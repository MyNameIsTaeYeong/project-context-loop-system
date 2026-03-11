"""통계 API 엔드포인트."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from context_loop.storage.metadata_store import MetadataStore
from context_loop.web.dependencies import get_meta_store, get_templates

router = APIRouter()


@router.get("/api/stats")
async def stats_json(
    meta_store: MetadataStore = Depends(get_meta_store),
) -> dict[str, int]:
    """대시보드 통계를 JSON으로 반환한다."""
    return await meta_store.get_stats()


@router.get("/partials/stats")
async def stats_partial(
    request: Request,
    meta_store: MetadataStore = Depends(get_meta_store),
):
    """통계 카드 HTML 파셜."""
    templates = get_templates(request)
    stats = await meta_store.get_stats()
    return templates.TemplateResponse("partials/document_stats.html", {
        "request": request,
        "stats": stats,
    })
