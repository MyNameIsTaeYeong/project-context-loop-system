"""문서 처리 파이프라인 통합 모듈.

LLM Classifier → 청킹/임베딩/그래프추출 → 저장의 전체 흐름을 조율한다.
git_code는 AST 기반 정적 추출로 처리한다 (LLM 호출 없음).
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
from context_loop.processor.classifier import StorageMethod, classify_document
from context_loop.processor.graph_extractor import extract_graph
from context_loop.processor.link_graph_builder import build_link_graph
from context_loop.processor.llm_client import LLMClient
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
    llm_client: LLMClient,
    embedding_client: Embeddings,
    config: PipelineConfig | None = None,
    storage_method_override: StorageMethod | None = None,
) -> dict[str, Any]:
    """단일 문서를 전체 파이프라인으로 처리한다.

    처리 순서:
    1. 문서 로드 및 재처리 시작 (파생 데이터 삭제 + status='processing')
    2. LLM Classifier로 저장 방식 판정 (storage_method_override 시 건너뜀)
    3. chunk 또는 hybrid → 청킹 + 임베딩 + 벡터DB 저장
    4. graph 또는 hybrid → 그래프 추출 + GraphStore 저장
    5. SQLite에 청크 메타데이터 저장
    6. 처리 완료 기록

    Args:
        document_id: 처리할 문서 ID.
        meta_store: MetadataStore 인스턴스.
        vector_store: VectorStore 인스턴스 (초기화됨).
        graph_store: GraphStore 인스턴스.
        llm_client: LLMClient 인스턴스.
        embedding_client: EmbeddingClient 인스턴스.
        config: PipelineConfig. None이면 기본값 사용.
        storage_method_override: 저장 방식을 직접 지정. 설정 시 LLM Classifier를 건너뛴다.

    Returns:
        처리 결과 dict:
          - document_id: int
          - storage_method: str
          - chunk_count: int
          - node_count: int (LLM 추출 + 링크 그래프 합계)
          - edge_count: int (LLM 추출 + 링크 그래프 합계)
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

        # --- git_code: AST 기반 정적 추출 (LLM 호출 없음) ---
        if source_type == "git_code":
            storage_method = "hybrid"
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

        # --- 일반 문서: 기존 LLM 기반 파이프라인 ---
        else:
            # Confluence 소스면 원본 HTML에서 구조화 추출.
            # 하류(청킹/분류/그래프)는 여전히 plain_text(마크다운)를 소비하므로
            # 동작 호환을 유지하고, extracted는 메트릭/추후 소비용으로 보존한다.
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

            if storage_method_override is not None:
                storage_method = storage_method_override
                reason = "storage_method_override"
            else:
                storage_method, reason = await classify_document(
                    llm_client, title, content,
                )
            logger.info(
                "분류 결과 — document_id=%d, method=%s, reason=%s",
                document_id, storage_method, reason,
            )

            # 청크 처리 (chunk or hybrid)
            if storage_method in ("chunk", "hybrid"):
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

            # 그래프 처리 (graph or hybrid)
            if storage_method in ("graph", "hybrid"):
                graph_data = await extract_graph(
                    llm_client, title, content, source_type=source_type,
                )
                if graph_data.entities:
                    result = await graph_store.save_graph_data(
                        document_id, graph_data,
                    )
                    node_count = result["nodes"]
                    edge_count = result["edges"]

            # Confluence 링크 그래프 (LLM 호출 없음): outbound_links 기반.
            # LLM classifier 결과(storage_method)와 무관하게 항상 실행한다.
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

        # 완료
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
