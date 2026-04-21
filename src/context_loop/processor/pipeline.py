"""문서 처리 파이프라인 통합 모듈.

결정론적 파이프라인: 청킹/임베딩 + 구조 기반 그래프(AST 코드 심볼,
Confluence outbound_links)까지 저장한다. LLM 호출은 없다.

소스 타입별 처리
    - ``git_code``           : AST 기반 청크 + import 그래프
    - ``confluence``         : 구조화 추출 + plain_text 청크 + 링크 그래프
    - ``confluence_mcp``     : 위와 동일
    - 그 외 (``upload`` 등)   : 청크만

``storage_method``
    과거에는 LLM classifier가 결정했으나 현재는 처리 결과에서 파생한다:
    ``chunks`` 와 ``graph data`` 가 모두 생기면 ``hybrid``,
    한쪽만 있으면 각 ``chunk``/``graph``, 아무것도 없으면 ``chunk``(기본).
    스키마·UI 표시 용도로만 의미를 갖는다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from langchain_core.embeddings import Embeddings

from context_loop.ingestion.confluence_extractor import (
    ExtractedDocument,
    extract as extract_confluence,
)
from context_loop.processor.ast_code_extractor import (
    extract_code_symbols,
    to_chunks,
    to_graph_data,
)
from context_loop.processor.chunker import chunk_text
from context_loop.processor.link_graph_builder import build_link_graph
from context_loop.processor.reprocessor import (
    complete_reprocessing,
    start_reprocessing,
)
from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore

logger = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    """파이프라인 설정.

    Attributes:
        chunk_size: 청크당 최대 토큰 수.
        chunk_overlap: 인접 청크 간 겹치는 토큰 수.
        embedding_model: 토큰화/임베딩 모델 이름.
    """

    chunk_size: int = 512
    chunk_overlap: int = 50
    embedding_model: str = "text-embedding-3-small"


async def process_document(
    document_id: int,
    *,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
    embedding_client: Embeddings,
    config: PipelineConfig | None = None,
) -> dict[str, Any]:
    """단일 문서를 전체 파이프라인으로 처리한다.

    처리 순서:
        1. 문서 로드 및 재처리 시작 (파생 데이터 삭제 + status='processing')
        2. 소스별 추출: git_code=AST, confluence=구조화 HTML, 그 외=원문
        3. 청킹 + 임베딩 + 벡터DB 저장 (빈 본문이 아니면 항상)
        4. 그래프 저장: git_code=AST import, confluence=링크 그래프
        5. storage_method 파생 + 처리 완료 기록

    Args:
        document_id: 처리할 문서 ID.
        meta_store: MetadataStore 인스턴스.
        vector_store: VectorStore 인스턴스 (초기화됨).
        graph_store: GraphStore 인스턴스.
        embedding_client: EmbeddingClient 인스턴스.
        config: PipelineConfig. None이면 기본값 사용.

    Returns:
        처리 결과 dict:
          - document_id: int
          - storage_method: str ("chunk" | "graph" | "hybrid") — 결과에서 파생
          - chunk_count: int
          - node_count: int
          - edge_count: int
          - link_node_count: int — 링크 그래프로 생성된 노드 수
          - link_edge_count: int — 링크 그래프로 생성된 엣지 수
          - extraction: Confluence 소스에서 ``raw_content``가 있을 때만 dict,
            아니면 ``None``. 키: sections, outbound_links, code_blocks,
            tables, mentions (각 개수).
    """
    cfg = config or PipelineConfig()

    doc = await meta_store.get_document(document_id)
    if doc is None:
        raise ValueError(f"문서를 찾을 수 없습니다: document_id={document_id}")

    history_id = await start_reprocessing(meta_store, document_id)

    try:
        title = doc["title"]
        content = doc["original_content"] or ""
        source_type = doc.get("source_type")

        chunk_count = 0
        node_count = 0
        edge_count = 0
        link_node_count = 0
        link_edge_count = 0
        extracted: ExtractedDocument | None = None  # Confluence 경로에서만 채워짐

        if source_type == "git_code":
            # --- git_code: AST 기반 정적 추출 ---
            logger.info(
                "AST 코드 추출 시작 — document_id=%d, title=%s",
                document_id, title,
            )

            extraction = extract_code_symbols(content, title)

            # 심볼 → 청크 → 벡터DB
            chunks, embed_texts = to_chunks(extraction, title)
            if chunks:
                # 임베딩: 이름+시그니처+docstring (검색 정확도)
                # 저장: 전체 코드 (반환 내용)
                embeddings = await embedding_client.aembed_documents(embed_texts)
                documents = [c.content for c in chunks]

                chunk_ids = [c.id for c in chunks]
                metadatas = [
                    {
                        "document_id": document_id,
                        "chunk_index": c.index,
                        "title": title,
                        "section_path": c.section_path,
                    }
                    for c in chunks
                ]
                vector_store.delete_by_document(document_id)
                vector_store.add_chunks(chunk_ids, embeddings, documents, metadatas)

                await meta_store.delete_chunks_by_document(document_id)
                for chunk, cid in zip(chunks, chunk_ids):
                    await meta_store.create_chunk(
                        chunk_id=cid,
                        document_id=document_id,
                        chunk_index=chunk.index,
                        content=chunk.content,
                        token_count=chunk.token_count,
                    )
                chunk_count = len(chunks)

            # import 관계 → GraphStore
            graph_data = to_graph_data(extraction, title)
            if graph_data.entities:
                result = await graph_store.save_graph_data(document_id, graph_data)
                node_count = result["nodes"]
                edge_count = result["edges"]

        else:
            # --- 일반 문서: 청크 + (Confluence일 때) 링크 그래프 ---
            # Confluence 소스면 원본 HTML에서 구조화 추출.
            # 청커는 plain_text(마크다운)를 소비하므로 content를 교체하고,
            # extracted는 링크 그래프 / 반환 메트릭 용도로 보존한다.
            raw_html = doc.get("raw_content")
            if source_type in ("confluence", "confluence_mcp") and raw_html:
                extracted = extract_confluence(raw_html)
                content = extracted.plain_text
                logger.info(
                    "Confluence 추출 — doc_id=%d, sections=%d, links=%d, "
                    "code=%d, tables=%d, mentions=%d",
                    document_id,
                    len(extracted.sections),
                    len(extracted.outbound_links),
                    len(extracted.code_blocks),
                    len(extracted.tables),
                    len(extracted.mentions),
                )

            # 청크 (항상 실행; 본문이 비어 있으면 chunk_text가 빈 리스트 반환)
            chunks = chunk_text(
                content,
                chunk_size=cfg.chunk_size,
                chunk_overlap=cfg.chunk_overlap,
                model=cfg.embedding_model,
            )
            if chunks:
                texts = [c.content for c in chunks]
                embeddings = await embedding_client.aembed_documents(texts)

                chunk_ids = [c.id for c in chunks]
                metadatas = [
                    {
                        "document_id": document_id,
                        "chunk_index": c.index,
                        "title": title,
                        "section_path": c.section_path,
                    }
                    for c in chunks
                ]
                vector_store.delete_by_document(document_id)
                vector_store.add_chunks(chunk_ids, embeddings, texts, metadatas)

                await meta_store.delete_chunks_by_document(document_id)
                for chunk, cid in zip(chunks, chunk_ids):
                    await meta_store.create_chunk(
                        chunk_id=cid,
                        document_id=document_id,
                        chunk_index=chunk.index,
                        content=chunk.content,
                        token_count=chunk.token_count,
                    )
                chunk_count = len(chunks)

            # Confluence 링크 그래프 (LLM 호출 없음)
            if extracted is not None and extracted.outbound_links:
                link_graph = build_link_graph(extracted, doc_title=title)
                if link_graph.entities:
                    link_result = await graph_store.save_graph_data(
                        document_id, link_graph,
                    )
                    link_node_count = link_result["nodes"]
                    link_edge_count = link_result["edges"]
                    node_count += link_node_count
                    edge_count += link_edge_count
                    logger.info(
                        "링크 그래프 저장 — doc_id=%d, nodes=%d, edges=%d, merged=%d",
                        document_id,
                        link_node_count,
                        link_edge_count,
                        link_result.get("merged", 0),
                    )

        storage_method = _derive_storage_method(
            has_chunks=chunk_count > 0,
            has_graph=node_count > 0,
        )

        await complete_reprocessing(
            meta_store,
            document_id,
            history_id,
            storage_method,
        )

        return {
            "document_id": document_id,
            "storage_method": storage_method,
            "chunk_count": chunk_count,
            "node_count": node_count,
            "edge_count": edge_count,
            "link_node_count": link_node_count,
            "link_edge_count": link_edge_count,
            "extraction": (
                {
                    "sections": len(extracted.sections),
                    "outbound_links": len(extracted.outbound_links),
                    "code_blocks": len(extracted.code_blocks),
                    "tables": len(extracted.tables),
                    "mentions": len(extracted.mentions),
                }
                if extracted is not None
                else None
            ),
        }

    except Exception as exc:
        await complete_reprocessing(
            meta_store,
            document_id,
            history_id,
            "chunk",
            error_message=str(exc),
        )
        raise


def _derive_storage_method(*, has_chunks: bool, has_graph: bool) -> str:
    """실제 저장된 산출물로부터 ``storage_method`` 레이블을 파생한다."""
    if has_chunks and has_graph:
        return "hybrid"
    if has_graph:
        return "graph"
    return "chunk"
