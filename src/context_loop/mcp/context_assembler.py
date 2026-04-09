"""컨텍스트 검색·조립 모듈.

벡터 유사도 검색과 그래프 탐색 결과를 병합하여
LLM에 제공할 컨텍스트를 조립한다.
LLM 기반 그래프 탐색 플래너로 질의 의도에 맞는 그래프 영역을 탐색한다.
유사도 threshold로 무관한 청크를 제외하고,
LLM 기반 리랭커로 검색 결과의 정밀도를 높인다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from context_loop.processor.graph_search_planner import (
    GraphSearchResult,
    execute_graph_search,
    plan_graph_search,
)
from context_loop.processor.query_expander import expand_query_embedding
from context_loop.processor.reranker import rerank
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
    similarity_threshold: float = 0.0,
    rerank_enabled: bool = False,
    rerank_top_k: int | None = None,
    rerank_score_threshold: float = 0.0,
    hyde_enabled: bool = False,
    include_source_code: bool = False,
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
        llm_client: LLM 클라이언트 (그래프 탐색 계획용 + 리랭킹용). None이면 스킵.
        max_chunks: 반환할 최대 청크 수.
        include_graph: 그래프 컨텍스트 포함 여부.
        similarity_threshold: 최소 코사인 유사도 (이 값 미만 제외, 0이면 필터링 없음).
        rerank_enabled: LLM 기반 리랭커 사용 여부.
        rerank_top_k: 리랭킹 후 반환할 최대 청크 수.
        rerank_score_threshold: 리랭크 점수 최소값 (0~10, 이 값 미만 제외).
        hyde_enabled: HyDE (Hypothetical Document Embedding) 사용 여부.
        include_source_code: code_doc/code_summary의 원본 git_code 소스를 첨부할지 여부.

    Returns:
        조립된 컨텍스트 텍스트.
    """
    sections: list[str] = []

    # 쿼리 임베딩 생성 (HyDE 활성화 시 가상 문서 임베딩과 평균)
    if hyde_enabled and llm_client:
        query_embedding = await expand_query_embedding(query, llm_client, embedding_client)
    else:
        query_embedding = await _embed_query(query, embedding_client)

    # 1. 벡터 유사도 검색 + threshold 필터링
    chunk_results = await _search_chunks(
        query_embedding, vector_store, max_chunks,
        similarity_threshold=similarity_threshold,
    )

    # 2. LLM 기반 리랭킹
    if chunk_results and rerank_enabled and llm_client:
        chunk_results = await rerank(
            query, chunk_results, llm_client, top_k=rerank_top_k,
        )
        if rerank_score_threshold > 0:
            chunk_results = [
                c for c in chunk_results
                if c.get("rerank_score", 0) >= rerank_score_threshold
            ]

    if chunk_results:
        chunk_section = _format_chunk_results(chunk_results, meta_store)
        sections.append(await chunk_section)

    # 3. LLM 기반 그래프 탐색 (쿼리 임베딩으로 관련 스키마 생성)
    if include_graph and llm_client:
        graph_result = await _search_graph_with_llm(
            query, graph_store, llm_client,
            query_embedding=query_embedding,
            embedding_client=embedding_client,
        )
        if graph_result:
            sections.append(graph_result.text)

    # 4. Phase 9.7: 원본 소스 코드 첨부
    if include_source_code and chunk_results:
        doc_ids = _extract_doc_ids(chunk_results)
        source_section = await _fetch_and_format_source_code(doc_ids, meta_store)
        if source_section:
            sections.append(source_section)

    if not sections:
        return "관련 컨텍스트를 찾을 수 없습니다."

    return "\n\n---\n\n".join(sections)


async def _embed_query(
    query: str,
    embedding_client: Any,
) -> list[float] | None:
    """쿼리 임베딩을 생성한다. 실패 시 None."""
    try:
        return await embedding_client.aembed_query(query)
    except Exception:
        logger.warning("쿼리 임베딩 생성 실패", exc_info=True)
        return None


async def _search_chunks(
    query_embedding: list[float] | None,
    vector_store: VectorStore,
    max_chunks: int,
    *,
    similarity_threshold: float = 0.0,
) -> list[dict[str, Any]]:
    """벡터 유사도 검색을 수행하고 threshold 이하 결과를 제외한다.

    Args:
        query_embedding: 쿼리 임베딩 벡터.
        vector_store: 벡터 저장소.
        max_chunks: 반환할 최대 청크 수.
        similarity_threshold: 최소 코사인 유사도 (1 - distance).
            이 값 미만인 청크는 제외된다. 0이면 필터링 없음.
    """
    try:
        if vector_store.count() == 0 or query_embedding is None:
            return []
        results = vector_store.search(query_embedding, n_results=max_chunks)
        if similarity_threshold > 0:
            results = [
                r for r in results
                if (1 - r.get("distance", 1.0)) >= similarity_threshold
            ]
        return results
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
        section_path = r.get("metadata", {}).get("section_path", "")
        header = f"\n### [{title}] (유사도: {1 - distance:.2f})"
        if section_path:
            header += f"\n_섹션: {section_path}_"
        lines.append(header)
        lines.append(content)

    return "\n".join(lines)


async def _search_graph_with_llm(
    query: str,
    graph_store: GraphStore,
    llm_client: Any,
    *,
    query_embedding: list[float] | None = None,
    embedding_client: Any = None,
) -> GraphSearchResult | None:
    """LLM 기반 플래너로 그래프를 탐색한다.

    1. 엔티티 임베딩이 없으면 자동으로 구축한다.
    2. LLM에게 쿼리 관련 스키마 + 질의를 보여주고 탐색 계획을 받는다.
    3. 계획에 따라 GraphStore에서 실제 탐색을 수행한다.
    4. LLM이 탐색 불필요로 판단하면 None을 반환한다.
    """
    try:
        # 엔티티 임베딩 자동 구축 (최초 1회만 비용 발생)
        if embedding_client and graph_store.entity_embedding_count == 0:
            await graph_store.build_entity_embeddings(embedding_client)

        plan = await plan_graph_search(
            query, graph_store, llm_client,
            query_embedding=query_embedding,
        )
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
    similarity_threshold: float = 0.0,
    rerank_enabled: bool = False,
    rerank_top_k: int | None = None,
    rerank_score_threshold: float = 0.0,
    hyde_enabled: bool = False,
    include_source_code: bool = False,
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
        llm_client: LLM 클라이언트 (그래프 탐색 계획용 + 리랭킹용). None이면 스킵.
        max_chunks: 반환할 최대 청크 수.
        include_graph: 그래프 컨텍스트 포함 여부.
        similarity_threshold: 최소 코사인 유사도 (이 값 미만 제외, 0이면 필터링 없음).
        rerank_enabled: LLM 기반 리랭커 사용 여부.
        rerank_top_k: 리랭킹 후 반환할 최대 청크 수.
        rerank_score_threshold: 리랭크 점수 최소값 (0~10, 이 값 미만 제외).
        hyde_enabled: HyDE (Hypothetical Document Embedding) 사용 여부.
        include_source_code: code_doc/code_summary의 원본 git_code 소스를 첨부할지 여부.

    Returns:
        컨텍스트 텍스트와 출처 정보를 담은 AssembledContext.
    """
    sections: list[str] = []
    sources: list[Source] = []
    doc_cache: dict[int, dict[str, Any]] = {}

    # 쿼리 임베딩 생성 (HyDE 활성화 시 가상 문서 임베딩과 평균)
    if hyde_enabled and llm_client:
        query_embedding = await expand_query_embedding(query, llm_client, embedding_client)
    else:
        query_embedding = await _embed_query(query, embedding_client)

    # 1. 벡터 유사도 검색 + threshold 필터링
    chunk_results = await _search_chunks(
        query_embedding, vector_store, max_chunks,
        similarity_threshold=similarity_threshold,
    )

    # 2. LLM 기반 리랭킹
    if chunk_results and rerank_enabled and llm_client:
        chunk_results = await rerank(
            query, chunk_results, llm_client, top_k=rerank_top_k,
        )
        if rerank_score_threshold > 0:
            chunk_results = [
                c for c in chunk_results
                if c.get("rerank_score", 0) >= rerank_score_threshold
            ]

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
            section_path = r.get("metadata", {}).get("section_path", "")
            source_label = f"[출처: {title}]"
            if section_path:
                source_label += f" (섹션: {section_path})"
            lines.append(f"{source_label}\n{r.get('document', '')}")
            if doc_id and doc_id not in {s.document_id for s in sources}:
                sources.append(Source(document_id=doc_id, title=title, similarity=similarity))
        sections.append("\n\n".join(lines))

    # 2. LLM 기반 그래프 탐색 (쿼리 임베딩으로 관련 스키마 생성)
    if include_graph and llm_client:
        graph_result = await _search_graph_with_llm(
            query, graph_store, llm_client,
            query_embedding=query_embedding,
            embedding_client=embedding_client,
        )
        if graph_result:
            sections.append(graph_result.text)
            # 그래프 탐색 결과에서 출처 추출
            existing_doc_ids = {s.document_id for s in sources}
            for doc_id in graph_result.document_ids:
                if doc_id not in existing_doc_ids:
                    if doc_id not in doc_cache:
                        doc = await meta_store.get_document(doc_id)
                        doc_cache[doc_id] = doc if doc else {"title": f"문서 #{doc_id}"}
                    title = doc_cache[doc_id]["title"]
                    sources.append(Source(document_id=doc_id, title=title, similarity=0.0))

    # Phase 9.7: 원본 소스 코드 첨부
    if include_source_code and chunk_results:
        hit_doc_ids = _extract_doc_ids(chunk_results)
        source_section = await _fetch_and_format_source_code(
            hit_doc_ids, meta_store,
        )
        if source_section:
            sections.append(source_section)

    context_text = "\n\n---\n\n".join(sections) if sections else ""
    sources.sort(key=lambda s: s.similarity, reverse=True)
    return AssembledContext(context_text=context_text, sources=sources)


# ---------------------------------------------------------------------------
# Phase 9.7: 원본 소스 코드 첨부 헬퍼
# ---------------------------------------------------------------------------


def _extract_doc_ids(chunk_results: list[dict[str, Any]]) -> set[int]:
    """청크 검색 결과에서 고유 문서 ID를 추출한다."""
    ids: set[int] = set()
    for r in chunk_results:
        doc_id = r.get("metadata", {}).get("document_id")
        if doc_id is not None:
            ids.add(doc_id)
    return ids


async def _fetch_and_format_source_code(
    doc_ids: set[int],
    meta_store: MetadataStore,
) -> str | None:
    """code_doc/code_summary 문서의 원본 git_code 소스를 조회하여 포맷팅한다.

    document_sources 테이블을 통해 연결된 git_code 문서의 원본 코드를
    검증용 섹션으로 조립한다.

    Returns:
        포맷팅된 소스 코드 섹션 문자열. 소스가 없으면 None.
    """
    source_parts: list[str] = []
    seen_source_ids: set[int] = set()

    for doc_id in doc_ids:
        doc = await meta_store.get_document(doc_id)
        if not doc or doc["source_type"] not in ("code_doc", "code_summary"):
            continue

        sources = await meta_store.get_document_sources(doc_id)
        for src in sources:
            src_id = src["source_doc_id"]
            if src_id in seen_source_ids:
                continue
            seen_source_ids.add(src_id)

            src_doc = await meta_store.get_document(src_id)
            if not src_doc or not src_doc.get("original_content"):
                continue

            file_path = src.get("file_path") or src_doc.get("source_id", "")
            title = src_doc.get("title", file_path)
            content = src_doc["original_content"]

            # 파일 확장자에서 언어 힌트 추출
            ext = ""
            if "." in file_path:
                ext = file_path.rsplit(".", 1)[-1]

            source_parts.append(
                f"### {title} ({file_path})\n```{ext}\n{content}\n```"
            )

    if not source_parts:
        return None

    return "## 원본 소스 코드 (검증용)\n\n" + "\n\n".join(source_parts)
