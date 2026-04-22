"""MCP Tool 정의 모듈.

search_context, list_documents, get_document, get_graph_context 도구를 등록한다.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)


def register_tools(mcp: FastMCP) -> None:
    """MCP 서버에 도구를 등록한다."""

    @mcp.tool()
    async def search_context(
        query: str,
        max_chunks: int = 10,
        include_graph: bool = True,
    ) -> str:
        """질의 문자열로 관련 사내 지식 컨텍스트를 검색·조립하여 반환한다.

        벡터 유사도 검색과 그래프 탐색을 결합하여 관련 컨텍스트를 조립한다.
        어떤 문서가 인용되는지 ``search_logs``/``search_citations`` 테이블에
        관측 신호로 기록한다.

        Args:
            query: 검색 질의 문자열.
            max_chunks: 반환할 최대 청크 수.
            include_graph: 그래프 컨텍스트 포함 여부.
        """
        from context_loop.mcp.context_assembler import assemble_context_with_sources
        from context_loop.mcp.server import _config, _embedding_client, _get_stores, _llm_client

        meta_store, vector_store, graph_store = _get_stores()
        started = time.perf_counter()
        assembled = await assemble_context_with_sources(
            query=query,
            meta_store=meta_store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=_embedding_client,
            llm_client=_llm_client,
            max_chunks=max_chunks,
            include_graph=include_graph,
            similarity_threshold=_config.get("search.similarity_threshold", 0.0) if _config else 0.0,
            rerank_enabled=_config.get("search.reranker_enabled", False) if _config else False,
            rerank_top_k=_config.get("search.reranker_top_k", None) if _config else None,
            rerank_score_threshold=_config.get("search.reranker_score_threshold", 0.0) if _config else 0.0,
            hyde_enabled=_config.get("search.hyde_enabled", False) if _config else False,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)

        try:
            citations = [
                {
                    "document_id": s.document_id,
                    "rank": idx,
                    "similarity": s.similarity,
                    "retrieval": "vector" if s.similarity > 0 else "graph",
                }
                for idx, s in enumerate(assembled.sources)
            ]
            await meta_store.log_search(
                query=query,
                source="mcp",
                result_count=len(assembled.sources),
                latency_ms=latency_ms,
                citations=citations,
            )
        except Exception:
            logger.warning("검색 로그 기록 실패", exc_info=True)

        return assembled.context_text or "관련 컨텍스트를 찾을 수 없습니다."

    @mcp.tool()
    async def list_documents(
        source_type: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """등록된 문서 목록을 조회한다.

        각 항목에 ``source_updated_at`` (원본 시스템의 마지막 수정 시각)과
        ``staleness`` (``fresh``/``aging``/``stale``/``unknown`` 버킷 및
        ``age_days``)를 포함하여 소비 측에서 오래된 정보 여부를 판단할 수
        있도록 한다.

        Args:
            source_type: 소스 유형으로 필터링 ("confluence", "upload", "manual").
            status: 상태로 필터링 ("pending", "processing", "completed", "failed").
        """
        from context_loop.mcp.server import _get_stores
        from context_loop.storage.metadata_store import classify_staleness

        meta_store, _, _ = _get_stores()
        docs = await meta_store.list_documents(
            source_type=source_type,
            status=status,
        )
        return [
            {
                "id": doc["id"],
                "title": doc["title"],
                "source_type": doc["source_type"],
                "status": doc["status"],
                "storage_method": doc.get("storage_method"),
                "updated_at": doc.get("updated_at"),
                "source_updated_at": doc.get("source_updated_at"),
                "staleness": classify_staleness(doc.get("source_updated_at")),
                "author": doc.get("author"),
                "owner_id": doc.get("owner_id"),
            }
            for doc in docs
        ]

    @mcp.tool()
    async def get_document(
        document_id: int,
        format: str = "original",
    ) -> dict[str, Any]:
        """특정 문서의 원본 또는 처리된 데이터를 조회한다.

        Args:
            document_id: 문서 ID.
            format: 반환 형식 ("original", "chunks", "graph").
        """
        from context_loop.mcp.server import _get_stores

        meta_store, _, graph_store = _get_stores()
        doc = await meta_store.get_document(document_id)
        if not doc:
            return {"error": f"문서를 찾을 수 없습니다: {document_id}"}

        if format == "original":
            return {
                "id": doc["id"],
                "title": doc["title"],
                "content": doc.get("original_content", ""),
                "source_type": doc["source_type"],
                "status": doc["status"],
            }

        if format == "chunks":
            chunks = await meta_store.get_chunks_by_document(document_id)
            return {
                "id": doc["id"],
                "title": doc["title"],
                "chunks": [
                    {"index": c["chunk_index"], "content": c["content"]}
                    for c in chunks
                ],
            }

        if format == "graph":
            nodes = await meta_store.get_graph_nodes_by_document(document_id)
            edges = await meta_store.get_graph_edges_by_document(document_id)
            return {
                "id": doc["id"],
                "title": doc["title"],
                "nodes": [
                    {"name": n["entity_name"], "type": n.get("entity_type")}
                    for n in nodes
                ],
                "edges": [
                    {
                        "source": e["source_node_id"],
                        "target": e["target_node_id"],
                        "type": e.get("relation_type"),
                    }
                    for e in edges
                ],
            }

        return {"error": f"지원하지 않는 형식입니다: {format}"}

    @mcp.tool()
    async def get_graph_context(
        entity_name: str,
        depth: int = 1,
    ) -> dict[str, Any]:
        """특정 엔티티 중심으로 그래프 관계를 탐색하여 컨텍스트를 반환한다.

        Args:
            entity_name: 탐색 중심 엔티티 이름.
            depth: 탐색 깊이 (1 = 직접 연결만).
        """
        from context_loop.mcp.server import _get_stores

        _, _, graph_store = _get_stores()
        neighbors = graph_store.get_neighbors(entity_name, depth=depth)
        if not neighbors:
            return {"entity": entity_name, "nodes": [], "edges": [], "message": "엔티티를 찾을 수 없습니다."}

        node_ids = [n["id"] for n in neighbors]
        edges = graph_store.get_edges_between(node_ids)

        return {
            "entity": entity_name,
            "depth": depth,
            "nodes": [
                {
                    "name": n.get("entity_name", ""),
                    "type": n.get("entity_type", "other"),
                }
                for n in neighbors
            ],
            "edges": [
                {
                    "source": e.get("source"),
                    "target": e.get("target"),
                    "type": e.get("relation_type", ""),
                }
                for e in edges
            ],
        }
