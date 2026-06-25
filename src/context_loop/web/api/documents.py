"""문서 관련 페이지 및 API 엔드포인트."""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request, Response
from langchain_core.embeddings import Embeddings

from context_loop.config import Config
from context_loop.ingestion.editor import save_document
from context_loop.ingestion.html_converter import confluence_storage_to_html
from context_loop.processor.llm_client import LLMClient
from context_loop.processor.pipeline import build_meta_view_text
from context_loop.storage.cascade import delete_document_cascade
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
    # git_code일 때 source_id에서 확장자 기반 언어 힌트 추출
    lang_hint = ""
    if doc.get("source_type") == "git_code" and doc.get("source_id"):
        lang_hint = _guess_language(doc["source_id"])
    # 마크다운 본문(original_content)이 비었지만 원본 HTML(raw_content)이 있는
    # 폴백 케이스(큰/중첩 깊은 Confluence 문서의 변환 실패): Storage Format 을
    # 브라우저 표시용 표준 HTML 로 전처리해 템플릿에 넘긴다. 클라이언트가
    # DOMPurify 로 sanitize 후 렌더하므로 XSS 안전.
    raw_html_view = ""
    if not doc.get("original_content") and doc.get("raw_content"):
        raw_html_view = confluence_storage_to_html(doc["raw_content"])
    templates = get_templates(request)
    return templates.TemplateResponse("partials/tab_original.html", {
        "request": request,
        "doc": doc,
        "lang_hint": lang_hint,
        "raw_html_view": raw_html_view,
    })


@router.get("/partials/document/{document_id}/chunks")
async def tab_chunks(
    request: Request,
    document_id: int,
    meta_store: MetadataStore = Depends(get_meta_store),
    vector_store: VectorStore = Depends(get_vector_store),
):
    """청크 탭 HTML 파셜.

    모든 소스 타입이 멀티뷰(body + meta) 임베딩을 사용하지만, meta 뷰의
    입력 텍스트 정의가 소스 타입에 따라 다르므로 합성 경로가 다르다:

      - ``git_code``: meta 뷰 입력은 ``embed_text`` 컬럼에 영속화된
        식별자 요약(이름+시그니처+docstring). body 뷰는 ``content``
        (전체 코드)이며 둘 다 ChromaDB 엔트리로 저장된다 (I-046).
        레거시 청크(멀티뷰 적용 이전 처리)는 ``embed_text`` 가 비어 있다.
      - 그 외(Confluence/upload/manual): D-042 멀티뷰. body=``content``,
        meta=``build_meta_view_text(title, section_path)``.

    R3 — 가상 질문 임베딩(view='question') 도 vector_store 에서 조회하여
    각 청크에 묶어 표시한다. SQLite chunks 테이블에는 가상 질문이 영속화
    되지 않으므로 (검색 키 용도) 표시 경로는 vector_store 가 단일 진실의
    원천. 청크 ID 별로 그룹핑되어 ``chunk.questions`` 리스트로 전달된다.

    템플릿이 ``source_type`` 으로 분기하여 운영자가 실제 임베딩된 텍스트를
    오인하지 않도록 표시한다.
    """
    chunks = await meta_store.get_chunks_by_document(document_id)
    doc = await meta_store.get_document(document_id)
    title = doc["title"] if doc else ""
    source_type = doc["source_type"] if doc else ""

    # R3 — vector_store 에서 가상 질문 엔트리 조회 후 청크별로 그룹핑.
    # logical_chunk_id 가 SQLite chunks.id 와 일치하므로 그것으로 조인한다.
    questions_by_chunk: dict[str, list[str]] = {}
    for entry in vector_store.list_by_document(document_id, view="question"):
        meta = entry.get("metadata") or {}
        chunk_id = meta.get("logical_chunk_id")
        q_text = meta.get("question_text", "")
        if not chunk_id or not q_text:
            continue
        questions_by_chunk.setdefault(chunk_id, []).append(q_text)

    enriched = []
    for chunk in chunks:
        chunk_id = chunk.get("id", "")
        questions = questions_by_chunk.get(chunk_id, [])
        if source_type == "git_code":
            # git_code 의 meta 뷰 입력은 파이프라인이 SQLite ``embed_text`` 에
            # 영속화한다 (D-042 후속). 재구성 없이 그대로 노출.
            enriched.append({
                **chunk,
                "meta_text": chunk.get("embed_text", ""),
                "questions": questions,
            })
        else:
            meta_text = build_meta_view_text(
                title, chunk.get("section_path", ""),
            )
            enriched.append({
                **chunk,
                "meta_text": meta_text,
                "questions": questions,
            })

    templates = get_templates(request)
    return templates.TemplateResponse("partials/tab_chunks.html", {
        "request": request,
        "chunks": enriched,
        "source_type": source_type,
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


@router.get("/partials/document/{document_id}/sources")
async def tab_sources(
    request: Request,
    document_id: int,
    meta_store: MetadataStore = Depends(get_meta_store),
):
    """소스 연결 탭 HTML 파셜."""
    doc = await meta_store.get_document(document_id)
    if not doc:
        raise HTTPException(404)
    sources = await meta_store.get_document_sources(document_id)
    reverse_refs = await meta_store.get_documents_by_source(document_id)
    templates = get_templates(request)
    return templates.TemplateResponse("partials/tab_sources.html", {
        "request": request,
        "doc": doc,
        "sources": sources,
        "reverse_refs": reverse_refs,
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
    detail_raw = doc.get("llm_degraded_detail")
    llm_degradation: dict[str, Any] | None = None
    if detail_raw:
        try:
            llm_degradation = json.loads(detail_raw)
        except (ValueError, TypeError):
            llm_degradation = None
    return {
        "status": doc["status"],
        "storage_method": doc.get("storage_method"),
        # 검색 품질 결손(생성형 LLM 단계 실패) 여부. status='completed' 라도
        # True 일 수 있다 — 청크는 있으나 그래프·질문 view 가 누락된 상태.
        "llm_degraded": bool(doc.get("llm_degraded")),
        "llm_degradation": llm_degradation,
    }


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
    deleted = await delete_document_cascade(
        document_id,
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
    )
    if not deleted:
        raise HTTPException(404)

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
    embedding_client: Embeddings = Depends(get_embedding_client),
    llm_client: LLMClient = Depends(get_llm_client),
):
    """문서 처리를 백그라운드로 실행한다."""
    doc = await meta_store.get_document(document_id)
    if not doc:
        raise HTTPException(404)

    await meta_store.update_document_status(document_id, "processing")
    background_tasks.add_task(
        _run_pipeline,
        document_id, meta_store, vector_store, graph_store, config,
        embedding_client, llm_client,
    )
    return {"status": "processing", "document_id": document_id}


async def _run_pipeline(
    document_id: int,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
    config: Config,
    embedding_client: Embeddings,
    llm_client: LLMClient | None = None,
) -> None:
    """백그라운드에서 파이프라인을 실행한다."""
    try:
        from context_loop.processor.pipeline import PipelineConfig, process_document

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
            embedding_client=embedding_client,
            config=pipeline_config,
            llm_client=llm_client,
        )
    except Exception:
        logger.exception("문서 %d 파이프라인 실행 실패", document_id)
        await meta_store.update_document_status(document_id, "failed")


# --- Helpers ---

_EXT_LANG_MAP: dict[str, str] = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "jsx",
    ".java": "java", ".kt": "kotlin", ".scala": "scala",
    ".go": "go", ".rs": "rust", ".c": "c", ".cpp": "cpp", ".h": "cpp",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".sql": "sql", ".html": "html", ".css": "css", ".scss": "scss",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".xml": "xml", ".md": "markdown", ".txt": "plaintext",
    ".dockerfile": "dockerfile", ".gradle": "gradle",
}


def _guess_language(source_id: str) -> str:
    """source_id (repo_url:relative_path 형식)에서 파일 확장자 기반 언어를 추측한다."""
    path = source_id.rsplit(":", 1)[-1] if ":" in source_id else source_id
    # Dockerfile 등 확장자 없는 파일 처리
    basename = path.rsplit("/", 1)[-1].lower()
    if basename == "dockerfile":
        return "dockerfile"
    if basename == "makefile":
        return "makefile"
    dot_idx = path.rfind(".")
    if dot_idx == -1:
        return ""
    ext = path[dot_idx:].lower()
    return _EXT_LANG_MAP.get(ext, "")
