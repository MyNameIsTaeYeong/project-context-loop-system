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
from context_loop.processor.chunker import chunk_extracted_document, chunk_text
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
                        "section_anchor": c.section_anchor,
                    }
                    for c in chunks
                ]
                vector_store.delete_by_document(document_id)
                vector_store.add_chunks(chunk_ids, embeddings, documents, metadatas)

                await meta_store.delete_chunks_by_document(document_id)
                for chunk, cid, embed_text in zip(chunks, chunk_ids, embed_texts):
                    await meta_store.create_chunk(
                        chunk_id=cid,
                        document_id=document_id,
                        chunk_index=chunk.index,
                        content=chunk.content,
                        token_count=chunk.token_count,
                        section_path=chunk.section_path,
                        section_anchor=chunk.section_anchor,
                        embed_text=embed_text,
                        section_index=chunk.section_index,
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
            # 청커는 extracted의 sections/anchor를 그대로 소비하고,
            # 코드블록/테이블이 중간에서 잘리지 않도록 원자 단위로 보호한다.
            # extracted는 링크 그래프 / 반환 메트릭 용도로도 보존한다.
            raw_html = doc.get("raw_content")
            if source_type in ("confluence", "confluence_mcp") and raw_html:
                extracted = extract_confluence(raw_html)
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

            # 청크 (항상 실행; 본문이 비어 있으면 청커가 빈 리스트 반환)
            if extracted is not None:
                chunks = chunk_extracted_document(
                    extracted,
                    chunk_size=cfg.chunk_size,
                    chunk_overlap=cfg.chunk_overlap,
                    model=cfg.embedding_model,
                )
            else:
                chunks = chunk_text(
                    content,
                    chunk_size=cfg.chunk_size,
                    chunk_overlap=cfg.chunk_overlap,
                    model=cfg.embedding_model,
                )
            if chunks:
                # 멀티뷰 임베딩: body(본문) + meta(title + section_path).
                # 두 뷰는 같은 본문(document)을 가리키며 ChromaDB에는 별도 엔트리로
                # 저장된다. 검색 단계에서 logical_chunk_id로 dedup한다.
                body_texts = [c.content for c in chunks]
                meta_texts = [
                    build_meta_view_text(title, c.section_path) for c in chunks
                ]
                meta_mask = [bool(t) for t in meta_texts]

                to_embed = body_texts + [t for t, keep in zip(meta_texts, meta_mask) if keep]
                embeddings = await embedding_client.aembed_documents(to_embed)
                body_embeddings = embeddings[: len(body_texts)]
                meta_embeddings_iter = iter(embeddings[len(body_texts):])

                vec_ids: list[str] = []
                vec_embeddings: list[list[float]] = []
                vec_documents: list[str] = []
                vec_metadatas: list[dict[str, Any]] = []

                for i, chunk in enumerate(chunks):
                    base_meta = {
                        "document_id": document_id,
                        "chunk_index": chunk.index,
                        "title": title,
                        "section_path": chunk.section_path,
                        "section_anchor": chunk.section_anchor,
                        "logical_chunk_id": chunk.id,
                    }
                    vec_ids.append(f"{chunk.id}#body")
                    vec_embeddings.append(body_embeddings[i])
                    vec_documents.append(chunk.content)
                    vec_metadatas.append({**base_meta, "view": "body"})

                    if meta_mask[i]:
                        vec_ids.append(f"{chunk.id}#meta")
                        vec_embeddings.append(next(meta_embeddings_iter))
                        vec_documents.append(chunk.content)
                        vec_metadatas.append({**base_meta, "view": "meta"})

                vector_store.delete_by_document(document_id)
                vector_store.add_chunks(
                    vec_ids, vec_embeddings, vec_documents, vec_metadatas,
                )

                await meta_store.delete_chunks_by_document(document_id)
                for chunk in chunks:
                    await meta_store.create_chunk(
                        chunk_id=chunk.id,
                        document_id=document_id,
                        chunk_index=chunk.index,
                        content=chunk.content,
                        token_count=chunk.token_count,
                        section_path=chunk.section_path,
                        section_anchor=chunk.section_anchor,
                        section_index=chunk.section_index,
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


def build_meta_view_text(title: str, section_path: str) -> str:
    """멀티뷰 임베딩의 meta 뷰 텍스트를 생성한다.

    제목과 ``section_path`` 로 구성되며, 둘 다 비어 있으면 빈 문자열을
    반환하여 meta 뷰 생성을 건너뛰게 한다. 본문(body)과 섹션 경로가
    언어적으로 이질적일 때 경로 키워드 질의의 리콜을 끌어올리는 것이
    목적이다 (D-042).

    파이프라인 저장 시점뿐 아니라 대시보드/CLI에서 "이 청크가 무엇으로
    임베딩되었는가"를 보여주기 위해 호출될 수 있어 결정론적 순수 함수로
    유지한다.
    """
    title_part = (title or "").strip()
    path_part = (section_path or "").strip()
    if not title_part and not path_part:
        return ""
    if title_part and path_part:
        return f"{title_part}\n{path_part}"
    return title_part or path_part
