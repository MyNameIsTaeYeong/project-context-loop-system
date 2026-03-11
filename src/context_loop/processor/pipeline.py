"""문서 처리 파이프라인 통합 모듈.

LLM Classifier → 청킹/임베딩/그래프추출 → 저장의 전체 흐름을 조율한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from context_loop.processor.chunker import chunk_text
from context_loop.processor.classifier import StorageMethod, classify_document
from context_loop.processor.embedder import EmbeddingClient
from context_loop.processor.graph_extractor import extract_graph
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
    embedding_client: EmbeddingClient,
    config: PipelineConfig | None = None,
) -> dict[str, Any]:
    """단일 문서를 전체 파이프라인으로 처리한다.

    처리 순서:
    1. 문서 로드 및 재처리 시작 (파생 데이터 삭제 + status='processing')
    2. LLM Classifier로 저장 방식 판정
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

    Returns:
        처리 결과 dict:
          - document_id: int
          - storage_method: str
          - chunk_count: int
          - node_count: int
          - edge_count: int
    """
    cfg = config or PipelineConfig()

    doc = await meta_store.get_document(document_id)
    if doc is None:
        raise ValueError(f"문서를 찾을 수 없습니다: document_id={document_id}")

    history_id = await start_reprocessing(meta_store, document_id)

    try:
        title = doc["title"]
        content = doc["original_content"] or ""

        # 1. LLM 분류
        storage_method, reason = await classify_document(llm_client, title, content)
        logger.info(
            "분류 결과 — document_id=%d, method=%s, reason=%s",
            document_id,
            storage_method,
            reason,
        )

        chunk_count = 0
        node_count = 0
        edge_count = 0

        # 2. 청크 처리 (chunk or hybrid)
        if storage_method in ("chunk", "hybrid"):
            chunks = chunk_text(
                content,
                chunk_size=cfg.chunk_size,
                chunk_overlap=cfg.chunk_overlap,
                model=cfg.embedding_model,
            )
            if chunks:
                texts = [c.content for c in chunks]
                embeddings = await embedding_client.embed(texts)

                # 벡터DB 저장
                chunk_ids = [c.id for c in chunks]
                metadatas = [
                    {
                        "document_id": document_id,
                        "chunk_index": c.index,
                        "title": title,
                    }
                    for c in chunks
                ]
                # 기존 청크 삭제 후 추가 (reprocessor에서 이미 삭제했지만 안전하게)
                vector_store.delete_by_document(document_id)
                vector_store.add_chunks(chunk_ids, embeddings, texts, metadatas)

                # SQLite 청크 메타데이터 저장
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

        # 3. 그래프 처리 (graph or hybrid)
        if storage_method in ("graph", "hybrid"):
            graph_data = await extract_graph(llm_client, title, content)
            if graph_data.entities:
                result = await graph_store.save_graph_data(document_id, graph_data)
                node_count = result["nodes"]
                edge_count = result["edges"]

        # 4. 완료
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
