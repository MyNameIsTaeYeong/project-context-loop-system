"""채팅 인터페이스 API 엔드포인트.

RAG 파이프라인을 활용하여 사내 지식 기반 질의응답을 제공한다.
"""

from __future__ import annotations

import logging
from dataclasses import asdict

from fastapi import APIRouter, Depends, Request
from langchain_core.embeddings import Embeddings
from pydantic import BaseModel

from context_loop.config import Config
from context_loop.mcp.context_assembler import assemble_context_with_sources
from context_loop.processor.llm_client import LLMClient
from context_loop.processor.reranker_client import RerankerClient
from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore
from context_loop.web.dependencies import (
    get_config,
    get_embedding_client,
    get_graph_store,
    get_llm_client,
    get_meta_store,
    get_reranker_client,
    get_templates,
    get_vector_store,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_SYSTEM_PROMPT = (
    "당신은 사내 지식 기반 어시스턴트입니다. "
    "아래 제공된 컨텍스트를 기반으로 사용자의 질문에 정확하게 답변하세요. "
    "컨텍스트에 없는 내용은 '제공된 문서에서 관련 정보를 찾을 수 없습니다'라고 답하세요. "
    "답변 시 어떤 문서에서 정보를 가져왔는지 자연스럽게 언급하세요."
)


class ChatRequest(BaseModel):
    """채팅 요청."""

    query: str
    max_chunks: int = 10
    include_graph: bool = True
    include_source_code: bool = False


@router.get("/chat")
async def chat_page(request: Request):
    """채팅 페이지."""
    templates = get_templates(request)
    return templates.TemplateResponse("chat.html", {"request": request})


@router.post("/api/chat")
async def chat_api(
    body: ChatRequest,
    meta_store: MetadataStore = Depends(get_meta_store),
    vector_store: VectorStore = Depends(get_vector_store),
    graph_store: GraphStore = Depends(get_graph_store),
    llm_client: LLMClient = Depends(get_llm_client),
    embedding_client: Embeddings = Depends(get_embedding_client),
    reranker_client: RerankerClient | None = Depends(get_reranker_client),
    config: Config = Depends(get_config),
):
    """RAG 파이프라인으로 질의응답을 수행한다.

    1. 질의를 임베딩하여 관련 컨텍스트를 검색·조립한다.
    2. 컨텍스트와 함께 LLM에 질의하여 답변을 생성한다.
    3. 답변과 출처 정보를 반환한다.
    """
    # 1. 컨텍스트 조립 + 출처 추출
    assembled = await assemble_context_with_sources(
        query=body.query,
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=embedding_client,
        llm_client=llm_client,
        reranker_client=reranker_client,
        max_chunks=body.max_chunks,
        include_graph=body.include_graph,
        include_source_code=body.include_source_code,
        similarity_threshold=config.get("search.similarity_threshold", 0.0),
        rerank_enabled=config.get("search.reranker_enabled", False),
        rerank_top_k=config.get("search.reranker_top_k", None),
        rerank_score_threshold=config.get("search.reranker_score_threshold", 0.0),
        hyde_enabled=config.get("search.hyde_enabled", False),
    )

    # 2. LLM에 질의
    if not assembled.context_text:
        answer = "등록된 문서에서 관련 정보를 찾을 수 없습니다. 문서를 먼저 등록하고 처리해 주세요."
    else:
        prompt = f"## 컨텍스트\n\n{assembled.context_text}\n\n## 질문\n\n{body.query}"
        try:
            answer = await llm_client.complete(prompt, system=_SYSTEM_PROMPT, max_tokens=2048)
        except Exception:
            logger.exception("LLM 호출 실패")
            answer = "LLM 호출 중 오류가 발생했습니다. 설정을 확인해 주세요."

    # 3. 응답
    return {
        "answer": answer,
        "sources": [asdict(s) for s in assembled.sources],
    }
