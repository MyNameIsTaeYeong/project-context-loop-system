"""문서 관련 페이지 및 API 엔드포인트."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, Response

from context_loop.config import Config
from context_loop.ingestion.editor import save_document
from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore
from context_loop.web.dependencies import (
    get_config,
    get_graph_store,
    get_meta_store,
    get_templates,
    get_vector_store,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# --- 페이지 라우트 ---


@router.get("/")
async def dashboard(
    request: Request,
    meta_store: MetadataStore = Depends(get_meta_store),
):
    """메인 대시보드 페이지."""
    templates = get_templates(request)
    return templates.TemplateResponse("dashboard.html", {"request": request})


@router.get("/documents/{document_id}")
async def document_detail(
    request: Request,
    document_id: int,
    meta_store: MetadataStore = Depends(get_meta_store),
):
    """문서 상세 페이지."""
    doc = await meta_store.get_document(document_id)
    if not doc:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    templates = get_templates(request)
    return templates.TemplateResponse("document_detail.html", {
        "request": request,
        "doc": doc,
    })


@router.get("/editor")
async def editor_new(request: Request):
    """새 문서 에디터 페이지."""
    templates = get_templates(request)
    return templates.TemplateResponse("editor.html", {
        "request": request,
        "doc": None,
    })


@router.get("/editor/{document_id}")
async def editor_edit(
    request: Request,
    document_id: int,
    meta_store: MetadataStore = Depends(get_meta_store),
):
    """기존 문서 에디터 페이지."""
    doc = await meta_store.get_document(document_id)
    if not doc:
        raise HTTPException(404, "문서를 찾을 수 없습니다.")
    templates = get_templates(request)
    return templates.TemplateResponse("editor.html", {
        "request": request,
        "doc": doc,
    })


# --- HTMX 파셜 라우트 ---


@router.get("/partials/document-list")
async def document_list_partial(
    request: Request,
    source_type: str | None = None,
    status: str | None = None,
    meta_store: MetadataStore = Depends(get_meta_store),
):
    """문서 목록 HTML 파셜."""
    docs = await meta_store.list_documents(
        source_type=source_type or None,
        status=status or None,
    )
    templates = get_templates(request)
    return templates.TemplateResponse("partials/document_list.html", {
        "request": request,
        "documents": docs,
    })


@router.get("/partials/document/{document_id}/original")
async def tab_original(
    request: Request,
    document_id: int,
    meta_store: MetadataStore = Depends(get_meta_store),
):
    """원본 탭 HTML 파셜."""
    doc = await meta_store.get_document(document_id)
    if not doc:
        raise HTTPException(404)
    templates = get_templates(request)
    return templates.TemplateResponse("partials/tab_original.html", {
        "request": request,
        "doc": doc,
    })


@router.get("/partials/document/{document_id}/chunks")
async def tab_chunks(
    request: Request,
    document_id: int,
    meta_store: MetadataStore = Depends(get_meta_store),
):
    """청크 탭 HTML 파셜."""
    chunks = await meta_store.get_chunks_by_document(document_id)
    templates = get_templates(request)
    return templates.TemplateResponse("partials/tab_chunks.html", {
        "request": request,
        "chunks": chunks,
    })


@router.get("/partials/document/{document_id}/graph")
async def tab_graph(
    request: Request,
    document_id: int,
    meta_store: MetadataStore = Depends(get_meta_store),
):
    """그래프 탭 HTML 파셜."""
    nodes = await meta_store.get_graph_nodes_by_document(document_id)
    edges = await meta_store.get_graph_edges_by_document(document_id)
    graph_data = {
        "nodes": [
            {"id": n["id"], "label": n["entity_name"], "group": n.get("entity_type", "other")}
            for n in nodes
        ],
        "edges": [
            {
                "from": e["source_node_id"],
                "to": e["target_node_id"],
                "label": e.get("relation_type", ""),
            }
            for e in edges
        ],
    }
    templates = get_templates(request)
    return templates.TemplateResponse("partials/tab_graph.html", {
        "request": request,
        "graph_data": json.dumps(graph_data, ensure_ascii=False),
        "has_graph": bool(nodes),
    })


@router.get("/partials/document/{document_id}/metadata")
async def tab_metadata(
    request: Request,
    document_id: int,
    meta_store: MetadataStore = Depends(get_meta_store),
):
    """메타데이터 탭 HTML 파셜."""
    doc = await meta_store.get_document(document_id)
    if not doc:
        raise HTTPException(404)
    history = await meta_store.get_processing_history(document_id)
    templates = get_templates(request)
    return templates.TemplateResponse("partials/tab_metadata.html", {
        "request": request,
        "doc": doc,
        "history": history,
    })


# --- 문서 API ---


@router.get("/api/documents/{document_id}/status")
async def document_status(
    document_id: int,
    meta_store: MetadataStore = Depends(get_meta_store),
):
    """문서 처리 상태를 JSON으로 반환한다."""
    doc = await meta_store.get_document(document_id)
    if not doc:
        raise HTTPException(404)
    return {"status": doc["status"], "storage_method": doc.get("storage_method")}


@router.post("/api/documents")
async def create_document_api(
    title: str = Form(...),
    content: str = Form(...),
    meta_store: MetadataStore = Depends(get_meta_store),
):
    """에디터에서 새 문서를 생성한다."""
    result = await save_document(meta_store, title=title, content=content)
    response = Response(status_code=204)
    response.headers["HX-Redirect"] = f"/documents/{result['id']}"
    return response


@router.put("/api/documents/{document_id}")
async def update_document_api(
    document_id: int,
    title: str = Form(...),
    content: str = Form(...),
    meta_store: MetadataStore = Depends(get_meta_store),
):
    """에디터에서 기존 문서를 수정한다."""
    result = await save_document(
        meta_store, title=title, content=content, document_id=document_id,
    )
    # save_document은 title을 업데이트하지 않으므로 별도 갱신
    await meta_store.db.execute(
        "UPDATE documents SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (title, document_id),
    )
    await meta_store.db.commit()
    response = Response(status_code=204)
    response.headers["HX-Redirect"] = f"/documents/{document_id}"
    return response


@router.delete("/api/documents/{document_id}")
async def delete_document_api(
    document_id: int,
    meta_store: MetadataStore = Depends(get_meta_store),
    vector_store: VectorStore = Depends(get_vector_store),
    graph_store: GraphStore = Depends(get_graph_store),
):
    """문서와 관련 데이터를 모두 삭제한다."""
    doc = await meta_store.get_document(document_id)
    if not doc:
        raise HTTPException(404)

    vector_store.delete_by_document(document_id)
    await graph_store.delete_document_graph(document_id)
    await meta_store.delete_document(document_id)

    response = Response(status_code=204)
    response.headers["HX-Redirect"] = "/"
    return response


@router.post("/api/documents/{document_id}/process")
async def trigger_processing(
    document_id: int,
    background_tasks: BackgroundTasks,
    meta_store: MetadataStore = Depends(get_meta_store),
    vector_store: VectorStore = Depends(get_vector_store),
    graph_store: GraphStore = Depends(get_graph_store),
    config: Config = Depends(get_config),
):
    """문서 처리를 백그라운드로 실행한다."""
    doc = await meta_store.get_document(document_id)
    if not doc:
        raise HTTPException(404)

    await meta_store.update_document_status(document_id, "processing")
    background_tasks.add_task(
        _run_pipeline, document_id, meta_store, vector_store, graph_store, config,
    )
    return {"status": "processing", "document_id": document_id}


async def _run_pipeline(
    document_id: int,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
    config: Config,
) -> None:
    """백그라운드에서 파이프라인을 실행한다."""
    try:
        from context_loop.auth import get_token
        from context_loop.processor.embedder import OpenAIEmbeddingClient
        from context_loop.processor.llm_client import AnthropicClient, OpenAIClient
        from context_loop.processor.pipeline import PipelineConfig, process_document

        provider = config.get("llm.provider", "openai")
        if provider == "anthropic":
            api_key = get_token("anthropic", "api_key")
            llm_client = AnthropicClient(api_key=api_key or "")
        else:
            api_key = get_token("openai", "api_key")
            llm_client = OpenAIClient(api_key=api_key or "")

        embed_key = get_token("openai", "api_key")
        embedding_client = OpenAIEmbeddingClient(api_key=embed_key or "")

        pipeline_config = PipelineConfig(
            chunk_size=config.get("processor.chunk_size", 512),
            chunk_overlap=config.get("processor.chunk_overlap", 50),
            embedding_model=config.get("processor.embedding_model", "text-embedding-3-small"),
        )

        await process_document(
            document_id,
            meta_store=meta_store,
            vector_store=vector_store,
            graph_store=graph_store,
            llm_client=llm_client,
            embedding_client=embedding_client,
            config=pipeline_config,
        )
    except Exception:
        logger.exception("문서 %d 파이프라인 실행 실패", document_id)
        await meta_store.update_document_status(document_id, "failed")
