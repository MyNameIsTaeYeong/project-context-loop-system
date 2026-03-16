"""컨텍스트 검색·조립 모듈.

벡터 유사도 검색과 그래프 탐색 결과를 병합하여
LLM에 제공할 컨텍스트를 조립한다.
LLM 기반 그래프 탐색 플래너로 질의 의도에 맞는 그래프 영역을 탐색한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from context_loop.processor.graph_search_planner import (
    execute_graph_search,
    plan_graph_search,
)
from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore

logger = logging.getLogger(__name__)


@dataclass
class Source:
    """출처 정보."""

    document_id: int
    title: str
    similarity: float = 0.0


@dataclass
class AssembledContext:
    """조립된 컨텍스트와 출처 정보."""

    context_text: str
    sources: list[Source] = field(default_factory=list)


async def assemble_context(
    query: str,
    *,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
    embedding_client: Any,
    llm_client: Any = None,
    max_chunks: int = 10,
    include_graph: bool = True,
) -> str:
    """질의에 대해 벡터 검색 + LLM 기반 그래프 탐색으로 컨텍스트를 조립한다.

    LLM에게 그래프 스키마를 보여주고 사용자 질의에 맞는 탐색 계획을
    세운 뒤 해당 영역만 탐색한다. llm_client가 없으면 그래프 탐색을 스킵한다.

    Args:
        query: 검색 질의 문자열.
        meta_store: 메타데이터 저장소.
        vector_store: 벡터 저장소.
        graph_store: 그래프 저장소.
        embedding_client: 임베딩 클라이언트 (Embeddings 인터페이스).
        llm_client: LLM 클라이언트 (그래프 탐색 계획용). None이면 그래프 탐색 스킵.
        max_chunks: 반환할 최대 청크 수.
        include_graph: 그래프 컨텍스트 포함 여부.

    Returns:
        조립된 컨텍스트 텍스트.
    """
    sections: list[str] = []

    # 1. 벡터 유사도 검색
    chunk_results = await _search_chunks(
        query, vector_store, embedding_client, max_chunks,
    )
    if chunk_results:
        chunk_section = _format_chunk_results(chunk_results, meta_store)
        sections.append(await chunk_section)

    # 2. LLM 기반 그래프 탐색
    if include_graph and llm_client:
        graph_section = await _search_graph_with_llm(
            query, graph_store, llm_client,
        )
        if graph_section:
            sections.append(graph_section)

    if not sections:
        return "관련 컨텍스트를 찾을 수 없습니다."

    return "\n\n---\n\n".join(sections)


async def _search_chunks(
    query: str,
    vector_store: VectorStore,
    embedding_client: Any,
    max_chunks: int,
) -> list[dict[str, Any]]:
    """벡터 유사도 검색을 수행한다."""
    try:
        if vector_store.count() == 0:
            return []
        query_embedding = await embedding_client.aembed_query(query)
        return vector_store.search(query_embedding, n_results=max_chunks)
    except Exception:
        logger.warning("벡터 검색 실패", exc_info=True)
        return []


async def _format_chunk_results(
    results: list[dict[str, Any]],
    meta_store: MetadataStore,
) -> str:
    """청크 검색 결과를 텍스트로 포맷팅한다."""
    lines = ["## 관련 문서 청크"]
    doc_cache: dict[int, str] = {}

    for r in results:
        doc_id = r.get("metadata", {}).get("document_id")
        if doc_id and doc_id not in doc_cache:
            doc = await meta_store.get_document(doc_id)
            doc_cache[doc_id] = doc["title"] if doc else f"문서 #{doc_id}"

        title = doc_cache.get(doc_id, "알 수 없음") if doc_id else "알 수 없음"
        content = r.get("document", "")
        distance = r.get("distance", 0)
        lines.append(f"\n### [{title}] (유사도: {1 - distance:.2f})")
        lines.append(content)

    return "\n".join(lines)


async def _search_graph_with_llm(
    query: str,
    graph_store: GraphStore,
    llm_client: Any,
) -> str | None:
    """LLM 기반 플래너로 그래프를 탐색한다.

    1. LLM에게 그래프 스키마 + 질의를 보여주고 탐색 계획을 받는다.
    2. 계획에 따라 GraphStore에서 실제 탐색을 수행한다.
    3. LLM이 탐색 불필요로 판단하면 None을 반환한다.
    """
    try:
        plan = await plan_graph_search(query, graph_store, llm_client)
        if not plan.should_search:
            logger.debug("LLM 판단: 그래프 탐색 불필요 — %s", plan.reasoning)
            return None
        return await execute_graph_search(plan, graph_store)
    except Exception:
        logger.warning("LLM 기반 그래프 탐색 실패", exc_info=True)
        return None


async def assemble_context_with_sources(
    query: str,
    *,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
    embedding_client: Any,
    llm_client: Any = None,
    max_chunks: int = 10,
    include_graph: bool = True,
) -> AssembledContext:
    """컨텍스트를 조립하고 출처 정보를 함께 반환한다.

    LLM에게 그래프 스키마를 보여주고 사용자 질의에 맞는 탐색 계획을
    세운 뒤 해당 영역만 탐색한다.

    Args:
        query: 검색 질의 문자열.
        meta_store: 메타데이터 저장소.
        vector_store: 벡터 저장소.
        graph_store: 그래프 저장소.
        embedding_client: 임베딩 클라이언트.
        llm_client: LLM 클라이언트 (그래프 탐색 계획용). None이면 그래프 탐색 스킵.
        max_chunks: 반환할 최대 청크 수.
        include_graph: 그래프 컨텍스트 포함 여부.

    Returns:
        컨텍스트 텍스트와 출처 정보를 담은 AssembledContext.
    """
    sections: list[str] = []
    sources: list[Source] = []
    doc_cache: dict[int, dict[str, Any]] = {}

    # 1. 벡터 유사도 검색
    chunk_results = await _search_chunks(query, vector_store, embedding_client, max_chunks)
    if chunk_results:
        lines = []
        for r in chunk_results:
            doc_id = r.get("metadata", {}).get("document_id")
            if doc_id and doc_id not in doc_cache:
                doc = await meta_store.get_document(doc_id)
                doc_cache[doc_id] = doc if doc else {"title": f"문서 #{doc_id}"}
            title = doc_cache[doc_id]["title"] if doc_id and doc_id in doc_cache else "알 수 없음"
            distance = r.get("distance", 1.0)
            similarity = 1 - distance
            lines.append(f"[출처: {title}]\n{r.get('document', '')}")
            if doc_id and doc_id not in {s.document_id for s in sources}:
                sources.append(Source(document_id=doc_id, title=title, similarity=similarity))
        sections.append("\n\n".join(lines))

    # 2. LLM 기반 그래프 탐색
    if include_graph and llm_client:
        graph_section = await _search_graph_with_llm(
            query, graph_store, llm_client,
        )
        if graph_section:
            sections.append(graph_section)

    context_text = "\n\n---\n\n".join(sections) if sections else ""
    sources.sort(key=lambda s: s.similarity, reverse=True)
    return AssembledContext(context_text=context_text, sources=sources)
