"""컨텍스트 검색·조립 모듈.

벡터 유사도 검색과 그래프 탐색 결과를 병합하여
LLM에 제공할 컨텍스트를 조립한다.
임베딩 기반 엔티티 매칭으로 그래프 탐색 여부를 자동 결정한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore

logger = logging.getLogger(__name__)

# 엔티티 임베딩 유사도 임계값
_ENTITY_SIMILARITY_THRESHOLD = 0.7
_ENTITY_TOP_K = 5


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
    max_chunks: int = 10,
    include_graph: bool = True,
) -> str:
    """질의에 대해 벡터 검색 + 그래프 탐색으로 컨텍스트를 조립한다.

    그래프 탐색은 임베딩 기반으로 매칭되는 엔티티가 있을 때만 실행된다.

    Args:
        query: 검색 질의 문자열.
        meta_store: 메타데이터 저장소.
        vector_store: 벡터 저장소.
        graph_store: 그래프 저장소.
        embedding_client: 임베딩 클라이언트 (Embeddings 인터페이스).
        max_chunks: 반환할 최대 청크 수.
        include_graph: 그래프 컨텍스트 포함 여부.

    Returns:
        조립된 컨텍스트 텍스트.
    """
    sections: list[str] = []

    # 질의 임베딩 생성 (벡터 검색 + 엔티티 매칭 공용)
    query_embedding = await _get_query_embedding(query, embedding_client)

    # 1. 벡터 유사도 검색
    chunk_results = await _search_chunks_with_embedding(
        query_embedding, vector_store, max_chunks,
    )
    if chunk_results:
        chunk_section = _format_chunk_results(chunk_results, meta_store)
        sections.append(await chunk_section)

    # 2. 임베딩 기반 그래프 탐색 (매칭 엔티티가 있을 때만)
    if include_graph and query_embedding:
        graph_section = await _search_graph_by_embedding(
            query_embedding, graph_store, embedding_client,
        )
        if graph_section:
            sections.append(graph_section)

    if not sections:
        return "관련 컨텍스트를 찾을 수 없습니다."

    return "\n\n---\n\n".join(sections)


async def _get_query_embedding(query: str, embedding_client: Any) -> list[float]:
    """질의 임베딩을 생성한다."""
    try:
        return await embedding_client.aembed_query(query)
    except Exception:
        logger.warning("질의 임베딩 생성 실패", exc_info=True)
        return []


async def _search_chunks(
    query: str,
    vector_store: VectorStore,
    embedding_client: Any,
    max_chunks: int,
) -> list[dict[str, Any]]:
    """벡터 유사도 검색을 수행한다 (하위 호환용)."""
    try:
        if vector_store.count() == 0:
            return []
        query_embedding = await embedding_client.aembed_query(query)
        return vector_store.search(query_embedding, n_results=max_chunks)
    except Exception:
        logger.warning("벡터 검색 실패", exc_info=True)
        return []


async def _search_chunks_with_embedding(
    query_embedding: list[float],
    vector_store: VectorStore,
    max_chunks: int,
) -> list[dict[str, Any]]:
    """이미 생성된 질의 임베딩으로 벡터 검색을 수행한다."""
    try:
        if not query_embedding or vector_store.count() == 0:
            return []
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


async def _search_graph_by_embedding(
    query_embedding: list[float],
    graph_store: GraphStore,
    embedding_client: Any,
    threshold: float = _ENTITY_SIMILARITY_THRESHOLD,
    top_k: int = _ENTITY_TOP_K,
) -> str | None:
    """임베딩 유사도로 관련 엔티티를 찾고 그래프를 탐색한다.

    1. 엔티티 임베딩 캐시가 없으면 빌드한다.
    2. 질의 임베딩과 유사한 엔티티를 검색한다.
    3. 매칭된 엔티티를 중심으로 이웃 노드를 탐색한다.
    4. 매칭 엔티티가 없으면 None을 반환 (그래프 탐색 스킵).
    """
    if graph_store.stats()["nodes"] == 0:
        return None

    # 엔티티 임베딩 캐시 빌드 (없는 것만 추가)
    if graph_store.entity_embedding_count < graph_store.stats()["nodes"]:
        await graph_store.build_entity_embeddings(embedding_client)

    # 임베딩 유사도로 엔티티 검색
    matched_entities = graph_store.search_entities_by_embedding(
        query_embedding, threshold=threshold, top_k=top_k,
    )

    if not matched_entities:
        return None

    # 매칭된 엔티티 수에 따라 depth 동적 결정
    depth = 1 if len(matched_entities) >= 3 else 2

    # 매칭된 엔티티 중심으로 이웃 탐색
    all_nodes: list[dict[str, Any]] = []
    all_node_ids: set[int] = set()

    for entity in matched_entities:
        entity_name = entity["entity_name"]
        neighbors = graph_store.get_neighbors(entity_name, depth=depth)
        for n in neighbors:
            nid = n.get("id")
            if nid and nid not in all_node_ids:
                all_node_ids.add(nid)
                all_nodes.append(n)

    if not all_nodes:
        return None

    edges = graph_store.get_edges_between(list(all_node_ids))

    # 포맷팅
    matched_names = {e["entity_name"] for e in matched_entities}
    lines = ["## 관련 그래프 컨텍스트"]
    lines.append("\n**엔티티:**")
    for node in all_nodes:
        name = node.get("entity_name", "")
        etype = node.get("entity_type", "")
        marker = " *" if name in matched_names else ""
        lines.append(f"- {name} ({etype}){marker}")

    if edges:
        lines.append("\n**관계:**")
        id_to_name = {n["id"]: n.get("entity_name", "") for n in all_nodes}
        for edge in edges:
            src = id_to_name.get(edge.get("source"), "?")
            tgt = id_to_name.get(edge.get("target"), "?")
            rel = edge.get("relation_type", "관련")
            lines.append(f"- {src} --[{rel}]--> {tgt}")

    return "\n".join(lines)


# 하위 호환: 키워드 기반 그래프 탐색 (MCP tools에서 사용 가능)
def _search_graph(query: str, graph_store: GraphStore) -> str | None:
    """질의에서 키워드를 추출하여 그래프를 탐색한다 (레거시)."""
    keywords = query.split()
    all_nodes: list[dict[str, Any]] = []
    all_node_ids: set[int] = set()

    for keyword in keywords:
        if len(keyword) < 2:
            continue
        neighbors = graph_store.get_neighbors(keyword, depth=1)
        for n in neighbors:
            nid = n.get("id")
            if nid and nid not in all_node_ids:
                all_node_ids.add(nid)
                all_nodes.append(n)

    if not all_nodes:
        return None

    edges = graph_store.get_edges_between(list(all_node_ids))

    lines = ["## 관련 그래프 컨텍스트"]
    lines.append("\n**엔티티:**")
    for node in all_nodes:
        name = node.get("entity_name", "")
        etype = node.get("entity_type", "")
        lines.append(f"- {name} ({etype})")

    if edges:
        lines.append("\n**관계:**")
        id_to_name = {n["id"]: n.get("entity_name", "") for n in all_nodes}
        for edge in edges:
            src = id_to_name.get(edge.get("source"), "?")
            tgt = id_to_name.get(edge.get("target"), "?")
            rel = edge.get("relation_type", "관련")
            lines.append(f"- {src} --[{rel}]--> {tgt}")

    return "\n".join(lines)


async def assemble_context_with_sources(
    query: str,
    *,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
    embedding_client: Any,
    max_chunks: int = 10,
    include_graph: bool = True,
) -> AssembledContext:
    """컨텍스트를 조립하고 출처 정보를 함께 반환한다.

    그래프 탐색은 임베딩 기반으로 매칭되는 엔티티가 있을 때만 실행된다.

    Args:
        query: 검색 질의 문자열.
        meta_store: 메타데이터 저장소.
        vector_store: 벡터 저장소.
        graph_store: 그래프 저장소.
        embedding_client: 임베딩 클라이언트.
        max_chunks: 반환할 최대 청크 수.
        include_graph: 그래프 컨텍스트 포함 여부.

    Returns:
        컨텍스트 텍스트와 출처 정보를 담은 AssembledContext.
    """
    sections: list[str] = []
    sources: list[Source] = []
    doc_cache: dict[int, dict[str, Any]] = {}

    # 질의 임베딩 생성 (벡터 검색 + 엔티티 매칭 공용)
    query_embedding = await _get_query_embedding(query, embedding_client)

    # 1. 벡터 유사도 검색
    chunk_results = await _search_chunks_with_embedding(
        query_embedding, vector_store, max_chunks,
    )
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

    # 2. 임베딩 기반 그래프 탐색 (매칭 엔티티가 있을 때만)
    if include_graph and query_embedding:
        graph_section = await _search_graph_by_embedding(
            query_embedding, graph_store, embedding_client,
        )
        if graph_section:
            sections.append(graph_section)

    context_text = "\n\n---\n\n".join(sections) if sections else ""
    sources.sort(key=lambda s: s.similarity, reverse=True)
    return AssembledContext(context_text=context_text, sources=sources)
