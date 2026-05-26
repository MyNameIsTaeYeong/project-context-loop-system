"""문서 처리 파이프라인 통합 모듈.

결정론적 파이프라인: 청킹/임베딩 + 구조 기반 그래프(AST 코드 심볼,
Confluence outbound_links)까지 저장한다. LLM 호출은 없다.

소스 타입별 처리
    - ``git_code``           : AST 기반 청크 + 멀티뷰 임베딩(body+meta) + import 그래프
    - ``confluence``         : 구조화 추출 + plain_text 청크(멀티뷰) + 링크 그래프
    - ``confluence_mcp``     : 위와 동일
    - 그 외 (``upload`` 등)   : 청크(멀티뷰)만

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
from context_loop.processor.body_extractor import extract_body_graph
from context_loop.processor.chunker import (
    chunk_extracted_document_doclevel,
    chunk_text,
)
from context_loop.processor.extraction_unit import build_extraction_units
from context_loop.processor.link_graph_builder import build_link_graph
from context_loop.processor.llm_body_extractor import (
    InputTooLargeError,
    OutputTruncatedError,
    extract_llm_body_graph,
    extract_llm_body_graph_for_document,
)
from context_loop.processor.question_generator import (
    InputTooLargeError as QuestionInputTooLargeError,
    QuestionGenConfig,
    generate_questions_for_document,
)
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
    # LLM 본문 추출은 그래프의 도메인 의미 관계(depends_on/implements/owned_by 등)를
    # 보강하여 검색 추론 가치를 끌어올린다. 비용이 발생하지만 운영 기본을 ON 으로
    # 두어 그래프 품질을 기본 보장. LLM 호출을 끄려면 호출자가 명시적으로 False.
    enable_llm_body_extraction: bool = True
    # R3 — 문서 단위 멀티 벡터 인덱싱.
    # max_embedding_tokens: 단일 청크가 가질 수 있는 최대 토큰 수. 사내 임베딩
    #   모델 컨텍스트 윈도우(8K 가정) 이하로 설정한다. 문서가 이 한도 이하면
    #   1 청크로, 초과면 섹션 단위로 자연 폴백한다.
    # enable_question_indexing: 인덱싱 시 LLM 으로 섹션별 가상 질문을 생성하여
    #   별도 임베딩 view 로 등록한다. query 와 동일한 자연 질의 형태라 검색
    #   정밀도가 향상된다 (proposition / question-based indexing). 비용 추가
    #   발생 (문서당 LLM 호출 +1) 이지만 운영 기본 ON.
    max_embedding_tokens: int = 8000
    enable_question_indexing: bool = True


async def process_document(
    document_id: int,
    *,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
    embedding_client: Embeddings,
    config: PipelineConfig | None = None,
    llm_client: Any = None,
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
            # --- git_code: AST 기반 정적 추출 + 멀티뷰 임베딩 ---
            logger.info(
                "AST 코드 추출 시작 — document_id=%d, title=%s",
                document_id, title,
            )

            extraction = extract_code_symbols(content, title)

            # 심볼 → 청크 → 벡터DB
            chunks, meta_texts = to_chunks(extraction, title)
            if chunks:
                # 멀티뷰 임베딩 (D-042 git_code 일반화 / I-046):
                #   body 뷰: 코드 본문(chunk.content) — 자연어 도메인 용어·주석·구현
                #   meta 뷰: 식별자 요약(file+parent+name+signature+docstring)
                #             — 시그니처/타입 친화 질의
                # 두 뷰는 같은 본문(documents=chunk.content)을 가리키고
                # logical_chunk_id 를 공유하므로 _search_chunks dedup 으로 흡수된다.
                # meta 텍스트는 항상 비어있지 않다 (file_title + name + signature
                # 가 최소 보장). embed_text SQLite 컬럼은 meta 뷰 입력을 그대로
                # 영속화한다.
                body_texts = [c.content for c in chunks]
                to_embed = body_texts + meta_texts
                embeddings = await embedding_client.aembed_documents(to_embed)
                body_embeddings = embeddings[: len(chunks)]
                meta_embeddings = embeddings[len(chunks):]

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

                    vec_ids.append(f"{chunk.id}#meta")
                    vec_embeddings.append(meta_embeddings[i])
                    vec_documents.append(chunk.content)
                    vec_metadatas.append({**base_meta, "view": "meta"})

                vector_store.delete_by_document(document_id)
                vector_store.add_chunks(
                    vec_ids, vec_embeddings, vec_documents, vec_metadatas,
                )

                await meta_store.delete_chunks_by_document(document_id)
                for chunk, meta_text in zip(chunks, meta_texts):
                    await meta_store.create_chunk(
                        chunk_id=chunk.id,
                        document_id=document_id,
                        chunk_index=chunk.index,
                        content=chunk.content,
                        token_count=chunk.token_count,
                        section_path=chunk.section_path,
                        section_anchor=chunk.section_anchor,
                        embed_text=meta_text,
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

            # R3 — 문서 단위 청킹 (작은 문서 = 1 청크, 큰 문서 = 섹션 폴백).
            # 임베딩 모델 컨텍스트 한도(8K 가정)를 max_embedding_tokens 로 가드.
            if extracted is not None:
                chunks = chunk_extracted_document_doclevel(
                    extracted,
                    max_tokens=cfg.max_embedding_tokens,
                    model=cfg.embedding_model,
                )
            else:
                chunks = chunk_text(
                    content,
                    chunk_size=cfg.max_embedding_tokens,
                    chunk_overlap=min(cfg.max_embedding_tokens // 10, 200),
                    model=cfg.embedding_model,
                )
            if chunks:
                # R3 — 가상 질문 생성 (옵션 — extracted 가 있고 enable_question_indexing
                # 일 때만). LLM 호출 1회로 모든 섹션의 자연 질의를 한 번에 추출.
                # 결과는 section_index → [questions] 매핑.
                question_map: dict[int, list[str]] = {}
                if (
                    extracted is not None
                    and cfg.enable_question_indexing
                    and llm_client is not None
                ):
                    try:
                        question_map, q_stats = (
                            await generate_questions_for_document(
                                doc_title=title,
                                extracted=extracted,
                                llm_client=llm_client,
                            )
                        )
                        logger.info(
                            "가상 질문 — doc_id=%d, sections=%d→%d, questions=%d, "
                            "input_tokens≈%d",
                            document_id,
                            q_stats.sections_total,
                            q_stats.sections_with_questions,
                            q_stats.final_questions,
                            q_stats.input_tokens_estimate,
                        )
                    except QuestionInputTooLargeError:
                        logger.info(
                            "가상 질문 스킵 — 문서 본문 입력 한도 초과 "
                            "(doc_id=%d)", document_id,
                        )

                # 멀티뷰 임베딩: body(본문) + meta(title + section_path) + 가상 질문.
                # body/meta 는 같은 청크를 가리키며 logical_chunk_id 로 dedup.
                # 가상 질문 view 는 같은 source 청크에 연결되어 검색 매칭 시
                # document_id 단위로 그루핑된다.
                body_texts = [c.content for c in chunks]
                meta_texts = [
                    build_meta_view_text(title, c.section_path) for c in chunks
                ]
                meta_mask = [bool(t) for t in meta_texts]

                # 각 청크에 매핑할 가상 질문 목록.
                # - 1청크 문서: 모든 가상 질문이 그 1청크에 연결
                #   (section_index None → question_map 키와 매칭되지 않으면 다 묶음)
                # - 다청크 (섹션 폴백): chunk.section_index 와 question_map 키 매칭
                question_lists: list[list[str]] = []
                for chunk in chunks:
                    if chunk.section_index is None:
                        # 단일 청크 — 문서의 모든 가상 질문을 묶음
                        all_q: list[str] = []
                        for qs in question_map.values():
                            all_q.extend(qs)
                        question_lists.append(all_q)
                    else:
                        question_lists.append(
                            list(question_map.get(chunk.section_index, [])),
                        )

                to_embed = (
                    body_texts
                    + [t for t, keep in zip(meta_texts, meta_mask) if keep]
                    + [q for qs in question_lists for q in qs]
                )
                embeddings = await embedding_client.aembed_documents(to_embed)
                body_embeddings = embeddings[: len(body_texts)]
                meta_count = sum(1 for keep in meta_mask if keep)
                meta_embeddings_iter = iter(
                    embeddings[len(body_texts): len(body_texts) + meta_count],
                )
                question_embeddings_iter = iter(
                    embeddings[len(body_texts) + meta_count:],
                )

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

                    # 가상 질문 view — query 와 동일한 자연 질의 형태로 검색
                    # 정밀도를 끌어올린다. document 컬럼에는 source 청크 본문을
                    # 그대로 저장하여 답변 컨텍스트 조립이 본문을 받게 한다.
                    for q_idx, q_text in enumerate(question_lists[i]):
                        vec_ids.append(f"{chunk.id}#q{q_idx}")
                        vec_embeddings.append(next(question_embeddings_iter))
                        vec_documents.append(chunk.content)
                        vec_metadatas.append({
                            **base_meta,
                            "view": "question",
                            "question_text": q_text,
                        })

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

            # 본문 그래프 (결정론적, LLM 호출 없음)
            # ExtractionUnit 단위로 굵게/API/표 헤더/Jira 키를 엔티티로 추출하고
            # self-document 와 연결한다. 링크 그래프와 같은 ``document`` 노드로
            # GraphStore 의 정규 노드 병합을 통해 자연 수렴한다.
            if extracted is not None and extracted.sections:
                units = build_extraction_units(
                    extracted, document_id=document_id, doc_title=title,
                )
                if units:
                    body_graph = extract_body_graph(units, doc_title=title)
                    if body_graph.entities:
                        body_result = await graph_store.save_graph_data(
                            document_id, body_graph,
                        )
                        node_count += body_result["nodes"]
                        edge_count += body_result["edges"]
                        logger.info(
                            "본문 그래프 저장 — doc_id=%d, nodes=%d, edges=%d, "
                            "merged=%d, units=%d",
                            document_id,
                            body_result["nodes"],
                            body_result["edges"],
                            body_result.get("merged", 0),
                            len(units),
                        )

                    # LLM 의미 관계 추출 (opt-in, 비용 발생)
                    # 결정론 본문 그래프와 같은 ``document`` 노드로 수렴하지는
                    # 않지만, 도메인 엔티티 간 의미 관계 (depends_on, implements,
                    # owned_by 등) 를 추가하여 그래프의 추론 가치를 끌어올린다.
                    #
                    # 호출 단위: 문서 단위 1회 호출이 기본 (256K 컨텍스트 모델
                    # 가정). cross-section entity 통합·중복 제거가 LLM 자체에서
                    # 해소되어 그래프 품질이 향상되고 호출 비용이 N→1 로 준다.
                    # 본문 토큰이 입력 한도 초과인 거대 문서는 자동으로 기존
                    # unit 단위 폴백으로 전환된다.
                    if cfg.enable_llm_body_extraction and llm_client is not None:
                        doc_body = _assemble_document_body(extracted)
                        try:
                            llm_graph, llm_stats = (
                                await extract_llm_body_graph_for_document(
                                    doc_title=title,
                                    body=doc_body,
                                    llm_client=llm_client,
                                )
                            )
                            logger.info(
                                "LLM 본문 그래프(문서 단위) — doc_id=%d, "
                                "input_chars=%d, raw_entities=%d, raw_relations=%d",
                                document_id,
                                len(doc_body),
                                llm_stats.raw_entities,
                                llm_stats.raw_relations,
                            )
                        except (InputTooLargeError, OutputTruncatedError) as exc:
                            # F-CG2-02/04: 입력 한도 초과뿐 아니라 출력 잘림
                            # (JSON 파싱 실패) 도 unit 기반 폴백으로 라우팅.
                            # 두 예외 모두 unit 분할로 입력·출력 규모를 줄이면
                            # 회복 가능성이 높다.
                            logger.info(
                                "문서 단위 LLM 추출 폴백 — doc_id=%d, units=%d, "
                                "reason=%s",
                                document_id,
                                len(units),
                                type(exc).__name__,
                            )
                            llm_graph, llm_stats = await extract_llm_body_graph(
                                units, doc_title=title, llm_client=llm_client,
                            )
                        if llm_graph.entities:
                            llm_result = await graph_store.save_graph_data(
                                document_id, llm_graph,
                            )
                            node_count += llm_result["nodes"]
                            edge_count += llm_result["edges"]
                            logger.info(
                                "LLM 본문 그래프 저장 — doc_id=%d, nodes=%d, "
                                "edges=%d, merged=%d, units_called=%d/%d",
                                document_id,
                                llm_result["nodes"],
                                llm_result["edges"],
                                llm_result.get("merged", 0),
                                llm_stats.units_called,
                                llm_stats.units_total,
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


def _assemble_document_body(extracted: ExtractedDocument) -> str:
    """문서 단위 LLM 본문 추출 입력으로 사용할 전체 본문을 조립한다.

    ``ExtractedDocument.sections`` 가 있으면 헤딩 + md_content 를 트리 순서대로
    이어붙여 LLM 에게 섹션 경계가 보이게 한다. 섹션이 없으면 ``plain_text`` 를
    그대로 사용한다.

    breadcrumb (문서 제목, 위치, lead paragraph) 은 추가하지 않는다 — 문서
    전체를 넣는 경로에서 doc_title 은 user prompt 의 별도 필드(``# 문서 제목``)
    로 노출되므로 중복이며, 위치 정보는 본문 헤딩 자체에 이미 들어 있다.
    """
    if extracted.sections:
        parts: list[str] = []
        for section in extracted.sections:
            heading_line = "#" * max(section.level, 1) + " " + section.title
            body = section.md_content.strip()
            if body:
                parts.append(heading_line + "\n\n" + body)
            else:
                parts.append(heading_line)
        return "\n\n".join(parts)
    return extracted.plain_text


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
