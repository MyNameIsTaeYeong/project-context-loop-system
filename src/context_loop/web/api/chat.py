"""채팅 인터페이스 API 엔드포인트.

RAG 파이프라인을 활용하여 사내 지식 기반 질의응답을 제공한다.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import asdict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
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
    include_source_code: bool = True


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
    """RAG 파이프라인으로 질의응답을 수행하며 NDJSON 스트림으로 응답한다.

    1. 질의를 임베딩하여 관련 컨텍스트를 검색·조립한다.
    2. 컨텍스트와 함께 LLM에 질의하여 답변 토큰을 스트리밍 한다.
    3. 답변 완료 직후 출처(sources) 이벤트를 보내고 done 으로 종료한다.

    이벤트 스키마(한 줄당 한 JSON)::

        {"type": "delta", "content": "토큰..."}
        {"type": "delta", "content": "토큰..."}
        {"type": "sources", "sources": [...]}
        {"type": "done"}
        # 오류 시:
        {"type": "error", "content": "..."}
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

    sources_payload = [asdict(s) for s in assembled.sources]

    async def event_stream() -> AsyncIterator[str]:
        if not assembled.context_text:
            yield _ndjson({
                "type": "delta",
                "content": "등록된 문서에서 관련 정보를 찾을 수 없습니다. "
                           "문서를 먼저 등록하고 처리해 주세요.",
            })
            yield _ndjson({"type": "sources", "sources": sources_payload})
            yield _ndjson({"type": "done"})
            return

        prompt = f"## 컨텍스트\n\n{assembled.context_text}\n\n## 질문\n\n{body.query}"
        try:
            async for chunk in llm_client.stream(
                prompt,
                system=_SYSTEM_PROMPT,
                max_tokens=8192,
                reasoning_mode="high",
            ):
                yield _ndjson({"type": "delta", "content": chunk})
        except Exception:
            logger.exception("LLM 호출 실패")
            yield _ndjson({
                "type": "error",
                "content": "LLM 호출 중 오류가 발생했습니다. 설정을 확인해 주세요.",
            })
            return
        # 답변 완료 후 출처 표시
        yield _ndjson({"type": "sources", "sources": sources_payload})
        yield _ndjson({"type": "done"})

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


def _ndjson(payload: dict) -> str:
    """딕셔너리를 NDJSON 한 줄(끝에 ``\\n``)로 직렬화한다."""
    return json.dumps(payload, ensure_ascii=False) + "\n"
