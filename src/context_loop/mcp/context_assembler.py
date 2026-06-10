"""컨텍스트 검색·조립 모듈.

벡터 유사도 검색과 그래프 탐색 결과를 병합하여
LLM에 제공할 컨텍스트를 조립한다.
LLM 기반 그래프 탐색 플래너로 질의 의도에 맞는 그래프 영역을 탐색한다.
유사도 threshold로 무관한 청크를 제외하고,
LLM 기반 리랭커로 검색 결과의 정밀도를 높인다.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from context_loop.eval.gold_set import GraphEntityRef, GraphRelationRef
from context_loop.processor.chunker import count_tokens
from context_loop.processor.graph_search_planner import (
    GraphSearchResult,
    execute_graph_search,
    plan_graph_search,
)
from context_loop.processor.query_expander import expand_query_embedding
from context_loop.processor.reranker import rerank
from context_loop.processor.reranker_client import RerankerClient
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
    # parent-document retrieval 로 섹션 청크 대신 문서 전문이 첨부되었는지 여부.
    full_document: bool = False


@dataclass
class AssembledContext:
    """조립된 컨텍스트와 출처 정보.

    ``retrieved_graph_entities`` 는 검색된 그래프 노드의 ``GraphEntityRef``
    리스트 (2차 — description 포함). ``retrieved_graph_relations`` 는
    ``--score-relations`` 평가용으로 노출되는 1-hop 엣지 정보 (2차).
    """

    context_text: str
    sources: list[Source] = field(default_factory=list)
    retrieved_graph_entities: list[GraphEntityRef] = field(default_factory=list)
    retrieved_graph_relations: list[GraphRelationRef] = field(default_factory=list)


async def assemble_context(
    query: str,
    *,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
    embedding_client: Any,
    llm_client: Any = None,
    reranker_client: RerankerClient | None = None,
    max_chunks: int = 10,
    include_graph: bool = True,
    similarity_threshold: float = 0.0,
    rerank_enabled: bool = False,
    rerank_top_k: int | None = None,
    rerank_score_threshold: float = 0.0,
    hyde_enabled: bool = False,
    include_source_code: bool = False,
    max_graph_docs: int = 3,
    max_graph_tokens: int = 6000,
    parent_doc_enabled: bool = False,
    parent_doc_max_doc_tokens: int = 32000,
    parent_doc_total_tokens: int = 96000,
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
        reranker_client: 전용 리랭커 모델 클라이언트. None이면 리랭킹 스킵.
        max_chunks: 반환할 최대 청크 수.
        include_graph: 그래프 컨텍스트 포함 여부.
        similarity_threshold: 최소 코사인 유사도 (이 값 미만 제외, 0이면 필터링 없음).
        rerank_enabled: 전용 리랭커 사용 여부.
        rerank_top_k: 리랭킹 후 반환할 최대 청크 수.
        rerank_score_threshold: 리랭크 점수 최소값 (모델 의존, 보통 0~1).
        hyde_enabled: HyDE (Hypothetical Document Embedding) 사용 여부.
        include_source_code: code_doc/code_summary의 원본 git_code 소스를 첨부할지 여부.
        parent_doc_enabled: 섹션 폴백 청크 적중 시 문서 전문 치환
            (parent-document retrieval) 여부.
        parent_doc_max_doc_tokens: 전문 치환의 문서당 토큰 한도.
        parent_doc_total_tokens: 전문 치환 총합 토큰 예산 (벡터+그래프 합산).

    Returns:
        조립된 컨텍스트 텍스트.
    """
    sections: list[str] = []
    parent_doc_budget = parent_doc_total_tokens
    parent_substituted: set[int] = set()

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

    # 2. 리랭킹과 그래프 탐색을 병렬 실행 (모델 호출 두 건을 동시에 처리).
    chunk_results, graph_result = await _rerank_and_search_graph(
        query, chunk_results,
        graph_store=graph_store,
        llm_client=llm_client,
        reranker_client=reranker_client,
        embedding_client=embedding_client,
        query_embedding=query_embedding,
        rerank_enabled=rerank_enabled,
        rerank_top_k=rerank_top_k,
        rerank_score_threshold=rerank_score_threshold,
        include_graph=include_graph,
    )

    if chunk_results:
        if parent_doc_enabled:
            parent_doc_budget -= await _apply_parent_documents(
                chunk_results, meta_store,
                max_doc_tokens=parent_doc_max_doc_tokens,
                remaining_budget=parent_doc_budget,
                substituted_doc_ids=parent_substituted,
            )
        chunk_section = _format_chunk_results(chunk_results, meta_store)
        sections.append(await chunk_section)

    # 3. 그래프 탐색 결과 처리 — 엔티티/관계 요약 + 연결 문서 본문 첨부 (설계 A)
    if graph_result:
        sections.append(graph_result.text)
        graph_chunks = await _search_graph_sourced_chunks(
            query_embedding, vector_store,
            graph_result.document_ids, _extract_doc_ids(chunk_results),
            max_graph_docs=max_graph_docs,
            max_graph_tokens=max_graph_tokens,
        )
        if graph_chunks:
            if parent_doc_enabled:
                parent_doc_budget -= await _apply_parent_documents(
                    graph_chunks, meta_store,
                    max_doc_tokens=parent_doc_max_doc_tokens,
                    remaining_budget=parent_doc_budget,
                    substituted_doc_ids=parent_substituted,
                )
            sections.append(
                await _format_graph_chunk_results(graph_chunks, meta_store)
            )

    # 4. Phase 9.7: 원본 소스 코드 첨부
    if include_source_code and chunk_results:
        doc_ids = _extract_doc_ids(chunk_results)
        source_section = await _fetch_and_format_source_code(doc_ids, meta_store)
        if source_section:
            sections.append(source_section)

    if not sections:
        logger.info(
            "Assembled context | query=%s | chars=0 | text=<empty>",
            query,
        )
        return "관련 컨텍스트를 찾을 수 없습니다."

    context_text = "\n\n---\n\n".join(sections)
    logger.info(
        "Assembled context | query=%s | chars=%d | text=%s",
        query, len(context_text), context_text,
    )
    return context_text


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

    R3 멀티 벡터 인덱싱(body/meta/question 3 view) 을 사용하므로 한 문서가
    여러 view 로 임베딩되어 있다. 과잉 인출(over-fetch) 후 **``document_id``
    단위로 dedup** 하여 같은 문서가 결과를 점유하지 않게 한다 (사용자 의도:
    "리턴은 문서 단위"). 거리 오름차순으로 도착하므로 먼저 등장하는 항목이
    그 문서의 최소 distance 이며, 매칭된 view 의 metadata(``view``,
    ``section_path``, ``question_text``)가 출처 라벨로 보존된다.

    Args:
        query_embedding: 쿼리 임베딩 벡터.
        vector_store: 벡터 저장소.
        max_chunks: 반환할 최대 문서 수.
        similarity_threshold: 최소 코사인 유사도 (1 - distance).
            이 값 미만인 결과는 제외된다. 0이면 필터링 없음.
    """
    try:
        if vector_store.count() == 0 or query_embedding is None:
            return []
        # 멀티 벡터 (body/meta/가상 질문) 고려해 over-fetch 후 doc 단위 dedup.
        # 가상 질문은 섹션당 3~5개 추가될 수 있어 over-fetch 배수를 늘림.
        raw = vector_store.search(query_embedding, n_results=max_chunks * 6)
        seen: set[Any] = set()
        deduped: list[dict[str, Any]] = []
        for r in raw:
            meta = r.get("metadata") or {}
            # R3: document_id 단위 dedup (같은 문서의 여러 view 가 매칭되면
            # 최소 distance 항목만 결과로 반환). document_id 가 없는 외부
            # 데이터는 logical_chunk_id 로 폴백.
            key = meta.get("document_id") or meta.get("logical_chunk_id") or r.get("id")
            if key in seen:
                continue
            seen.add(key)
            deduped.append(r)
            if len(deduped) >= max_chunks:
                break
        if similarity_threshold > 0:
            deduped = [
                r for r in deduped
                if (1 - r.get("distance", 1.0)) >= similarity_threshold
            ]
        return deduped
    except Exception:
        logger.warning("벡터 검색 실패", exc_info=True)
        return []


async def _format_chunk_results(
    results: list[dict[str, Any]],
    meta_store: MetadataStore,
) -> str:
    """청크 검색 결과를 텍스트로 포맷팅한다."""
    lines = ["## 관련 문서"]
    doc_cache: dict[int, str] = {}

    for r in results:
        meta = r.get("metadata") or {}
        doc_id = meta.get("document_id")
        if doc_id and doc_id not in doc_cache:
            doc = await meta_store.get_document(doc_id)
            doc_cache[doc_id] = doc["title"] if doc else f"문서 #{doc_id}"

        title = doc_cache.get(doc_id, "알 수 없음") if doc_id else "알 수 없음"
        content = r.get("document", "")
        distance = r.get("distance", 0)
        section_path = meta.get("section_path", "")
        view = meta.get("view", "")
        question_text = meta.get("question_text", "")

        header = f"\n### [{title}] (유사도: {1 - distance:.2f})"
        if r.get("parent_document"):
            label = "전문 첨부"
            if section_path:
                label += f" (매칭 섹션: {section_path})"
            header += f"\n_{label}_"
        elif section_path:
            header += f"\n_섹션: {section_path}_"
        if view == "question" and question_text:
            header += f"\n_매칭 질문: {question_text}_"
        lines.append(header)
        lines.append(content)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Parent-document retrieval (적중 섹션 → 문서 전문 치환)
# ---------------------------------------------------------------------------
#
# doc-level 청킹은 문서가 max_embedding_tokens 를 초과하면 섹션 단위로 폴백
# 분할한다. 검색이 그런 섹션 청크를 적중하면 LLM 에게 문서의 일부만 전달되어
# 답변 컨텍스트가 부족해진다. 이 헬퍼는 적중 청크가 섹션 폴백 청크인 문서에
# 한해 결과의 ``document`` 를 ``documents.original_content`` 전문으로 치환한다.
# 임베딩/검색 단위는 그대로 두고 답변 컨텍스트만 확장하는 것이 목적.

# parent-document 치환에서 제외할 소스 타입. git_code 는 심볼 단위 청킹이라
# "섹션 폴백" 개념이 다르고, 원본 소스 첨부는 include_source_code 경로
# (_fetch_and_format_source_code) 가 이미 담당하므로 중복을 피한다.
_PARENT_DOC_EXCLUDED_SOURCES = frozenset({"git_code"})


async def _apply_parent_documents(
    results: list[dict[str, Any]],
    meta_store: MetadataStore,
    *,
    max_doc_tokens: int,
    remaining_budget: int,
    substituted_doc_ids: set[int],
) -> int:
    """섹션 폴백 청크 적중 문서의 본문을 원문 전문으로 치환한다.

    결과 dict 를 in-place 변형한다: ``document`` 를 전문으로 바꾸고
    ``parent_document=True`` 마커를 추가한다. ``metadata.section_path`` 는
    보존되어 포맷터가 "매칭 섹션" 라벨로 노출한다 (provenance 유지).

    치환 조건 (하나라도 어긋나면 기존 섹션 청크 유지 — 안전 폴백):
      1. 문서가 존재하고 source_type 이 제외 목록에 없음
      2. ``original_content`` 가 비어있지 않음
      3. 문서의 청크가 2개 이상 (= 섹션 폴백 발생; 1청크면 이미 전문)
      4. 전문 토큰이 ``max_doc_tokens`` 이하
      5. 전문 토큰이 ``remaining_budget`` 이하

    Args:
        results: 검색 결과 리스트 (벡터 또는 그래프 첨부).
        meta_store: 메타데이터 저장소.
        max_doc_tokens: 문서당 전문 토큰 한도.
        remaining_budget: 전문 첨부 총합 잔여 예산.
        substituted_doc_ids: 이미 전문으로 치환된 문서 ID (호출 간 공유 —
            벡터/그래프 결과 사이의 중복 치환 방지).

    Returns:
        이번 호출에서 소비한 토큰 수 (호출측이 잔여 예산에서 차감).
    """
    consumed = 0
    for r in results:
        meta = r.get("metadata") or {}
        doc_id = meta.get("document_id")
        if not doc_id or doc_id in substituted_doc_ids:
            continue
        try:
            doc = await meta_store.get_document(doc_id)
            if not doc:
                continue
            if doc.get("source_type") in _PARENT_DOC_EXCLUDED_SOURCES:
                continue
            original_content = doc.get("original_content") or ""
            if not original_content.strip():
                continue
            chunk_rows = await meta_store.get_chunks_by_document(doc_id)
            if len(chunk_rows) <= 1:
                continue  # 섹션 폴백 없음 — 적중 청크가 이미 문서 전문
            doc_tokens = count_tokens(original_content)
            if doc_tokens > max_doc_tokens:
                continue
            if doc_tokens > remaining_budget - consumed:
                continue
            r["document"] = original_content
            r["parent_document"] = True
            substituted_doc_ids.add(doc_id)
            consumed += doc_tokens
        except Exception:
            logger.warning(
                "parent-document 치환 실패 | document_id=%s", doc_id,
                exc_info=True,
            )
    return consumed


# ---------------------------------------------------------------------------
# 그래프 연결 문서 첨부 (설계 A)
# ---------------------------------------------------------------------------
#
# 그래프 탐색은 ``GraphSearchResult.document_ids`` 로 "관계로 도달한 문서" 를
# 알지만, 기존 조립기는 엔티티/관계 텍스트만 컨텍스트에 넣고 그 문서 본문은
# 넣지 않았다. 이 헬퍼는 그래프가 도달한 문서 중 **벡터 검색이 못 찾은 것**의
# 가장 관련된 청크를 인출하여 별도 섹션으로 첨부한다 — 임베딩 유사도로는
# 닿지 않지만 관계로 연결된 문서를 LLM 에게 실제 산문으로 전달하는 것이 목적.
#
# 청크가 문서 단위(작은 문서=1청크=문서 전체, 큰 문서=섹션)임을 전제로,
# document_id 단위 dedup + 개수 상한(max_graph_docs) + 토큰 상한
# (max_graph_tokens) 을 함께 적용해 컨텍스트 예산을 보호한다.

# ``$in`` 필터에 넣을 doc_id 최대 개수 — 멀티홉으로 너무 많은 문서를 한 번에
# 필터링하지 않도록 가드한다. 쿼리 유사도로 어차피 상위만 추리므로 충분.
_MAX_GRAPH_DOC_FILTER = 50


async def _search_graph_sourced_chunks(
    query_embedding: list[float] | None,
    vector_store: VectorStore,
    graph_doc_ids: set[int],
    existing_doc_ids: set[int],
    *,
    max_graph_docs: int,
    max_graph_tokens: int,
) -> list[dict[str, Any]]:
    """그래프가 도달한 문서 중 벡터가 못 찾은 것들의 가장 관련된 청크를 인출한다.

    벡터 결과와 겹치는 문서는 제외하여 **순수 추가분**만 첨부한다. 청크는
    문서 단위라 작은 문서는 문서 전체가 1청크이므로, 개수(``max_graph_docs``)와
    토큰(``max_graph_tokens``) 상한을 함께 적용한다.

    Args:
        query_embedding: 쿼리 임베딩. None 이면 빈 리스트.
        vector_store: 벡터 저장소.
        graph_doc_ids: 그래프 탐색이 도달한 문서 ID 집합.
        existing_doc_ids: 벡터 검색이 이미 찾은 문서 ID 집합 (제외 대상).
        max_graph_docs: 첨부할 최대 문서 수. 0 이면 기능 off.
        max_graph_tokens: 첨부 청크의 토큰 합 상한.

    Returns:
        document_id 단위로 dedup 된 청크 결과 리스트 (distance 오름차순).
    """
    if query_embedding is None or max_graph_docs <= 0:
        return []
    new_doc_ids = [d for d in graph_doc_ids if d not in existing_doc_ids]
    if not new_doc_ids:
        return []
    # 멀티홉으로 doc_id 가 폭증해도 필터 크기를 가드 (쿼리 유사도가 상위를 추림)
    new_doc_ids = new_doc_ids[:_MAX_GRAPH_DOC_FILTER]

    try:
        # 멀티뷰(body/meta/question) 고려 over-fetch + document_id 단위 dedup.
        raw = vector_store.search(
            query_embedding,
            n_results=len(new_doc_ids) * 6,
            where={"document_id": {"$in": new_doc_ids}},
        )
    except Exception:
        logger.warning("그래프 문서 청크 검색 실패", exc_info=True)
        return []

    seen: set[Any] = set()
    selected: list[dict[str, Any]] = []
    token_total = 0
    for r in raw:  # distance 오름차순 → 문서당 첫 항목이 최소 distance
        meta = r.get("metadata") or {}
        key = meta.get("document_id") or meta.get("logical_chunk_id") or r.get("id")
        if key in seen:
            continue
        seen.add(key)
        # 토큰 예산 가드 (doc-level 청크는 문서 전체일 수 있어 무거움)
        chunk_tokens = count_tokens(r.get("document", ""))
        if selected and token_total + chunk_tokens > max_graph_tokens:
            break
        selected.append(r)
        token_total += chunk_tokens
        if len(selected) >= max_graph_docs:
            break
    return selected


async def _format_graph_chunk_results(
    results: list[dict[str, Any]],
    meta_store: MetadataStore,
) -> str:
    """그래프 연결 문서 청크를 텍스트로 포맷팅한다.

    벡터 검색 섹션('## 관련 문서')과 구분되도록 별도 헤더를 쓴다.
    """
    lines = ["## 그래프 연결 문서"]
    doc_cache: dict[int, str] = {}

    for r in results:
        meta = r.get("metadata") or {}
        doc_id = meta.get("document_id")
        if doc_id and doc_id not in doc_cache:
            doc = await meta_store.get_document(doc_id)
            doc_cache[doc_id] = doc["title"] if doc else f"문서 #{doc_id}"

        title = doc_cache.get(doc_id, "알 수 없음") if doc_id else "알 수 없음"
        content = r.get("document", "")
        section_path = meta.get("section_path", "")

        header = f"\n### [{title}] (그래프 경로로 도달)"
        if r.get("parent_document"):
            label = "전문 첨부"
            if section_path:
                label += f" (매칭 섹션: {section_path})"
            header += f"\n_{label}_"
        elif section_path:
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
    graph_planner_seed: int | None = None,
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
            seed=graph_planner_seed,
        )
        if not plan.should_search:
            logger.debug("LLM 판단: 그래프 탐색 불필요 — %s", plan.reasoning)
            return None
        # query_embedding + embedding_client 전달 — execute_graph_search 가
        # LLM 추측 entity_name 의 표면 매칭 실패 시 임베딩 fallback 으로 시드
        # 노드를 보강할 수 있게 한다 (그래프 메트릭 0% 의 핵심 funnel 손실 완화).
        return await execute_graph_search(
            plan, graph_store,
            query_embedding=query_embedding,
            embedding_client=embedding_client,
        )
    except Exception:
        logger.warning("LLM 기반 그래프 탐색 실패", exc_info=True)
        return None


async def _rerank_and_search_graph(
    query: str,
    chunk_results: list[dict[str, Any]],
    *,
    graph_store: GraphStore,
    llm_client: Any,
    reranker_client: RerankerClient | None,
    embedding_client: Any,
    query_embedding: list[float] | None,
    rerank_enabled: bool,
    rerank_top_k: int | None,
    rerank_score_threshold: float,
    include_graph: bool,
    graph_planner_seed: int | None = None,
) -> tuple[list[dict[str, Any]], GraphSearchResult | None]:
    """리랭킹과 그래프 탐색을 병렬 실행한다.

    리랭킹은 chunk_results, 그래프 계획은 query_embedding 에만 의존하므로
    서로 무관한 두 외부 호출을 동시에 보내 응답 지연을 줄인다.
    """
    async def _maybe_rerank() -> list[dict[str, Any]]:
        if not (chunk_results and rerank_enabled and reranker_client):
            return chunk_results
        reranked = await rerank(
            query, chunk_results, reranker_client, top_k=rerank_top_k,
        )
        if rerank_score_threshold > 0:
            reranked = [
                c for c in reranked
                if c.get("rerank_score", 0) >= rerank_score_threshold
            ]
        return reranked

    async def _maybe_graph() -> GraphSearchResult | None:
        if not (include_graph and llm_client):
            return None
        return await _search_graph_with_llm(
            query, graph_store, llm_client,
            query_embedding=query_embedding,
            embedding_client=embedding_client,
            graph_planner_seed=graph_planner_seed,
        )

    return await asyncio.gather(_maybe_rerank(), _maybe_graph())


async def assemble_context_with_sources(
    query: str,
    *,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
    embedding_client: Any,
    llm_client: Any = None,
    reranker_client: RerankerClient | None = None,
    max_chunks: int = 10,
    include_graph: bool = True,
    similarity_threshold: float = 0.0,
    rerank_enabled: bool = False,
    rerank_top_k: int | None = None,
    rerank_score_threshold: float = 0.0,
    hyde_enabled: bool = False,
    include_source_code: bool = False,
    max_graph_docs: int = 3,
    max_graph_tokens: int = 6000,
    parent_doc_enabled: bool = False,
    parent_doc_max_doc_tokens: int = 32000,
    parent_doc_total_tokens: int = 96000,
    graph_planner_seed: int | None = None,
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
        reranker_client: 전용 리랭커 모델 클라이언트. None이면 리랭킹 스킵.
        max_chunks: 반환할 최대 청크 수.
        include_graph: 그래프 컨텍스트 포함 여부.
        similarity_threshold: 최소 코사인 유사도 (이 값 미만 제외, 0이면 필터링 없음).
        rerank_enabled: 전용 리랭커 사용 여부.
        rerank_top_k: 리랭킹 후 반환할 최대 청크 수.
        rerank_score_threshold: 리랭크 점수 최소값 (모델 의존, 보통 0~1).
        hyde_enabled: HyDE (Hypothetical Document Embedding) 사용 여부.
        include_source_code: code_doc/code_summary의 원본 git_code 소스를 첨부할지 여부.
        parent_doc_enabled: 섹션 폴백 청크 적중 시 문서 전문 치환
            (parent-document retrieval) 여부.
        parent_doc_max_doc_tokens: 전문 치환의 문서당 토큰 한도.
        parent_doc_total_tokens: 전문 치환 총합 토큰 예산 (벡터+그래프 합산).
        graph_planner_seed: 그래프 탐색 플래너 LLM 호출 seed. None(기본)이면
            미전달 — 실서비스 동작은 변경되지 않는다. 평가 경로에서만 쿼리 기반
            결정적 seed 를 주입해 그래프 탐색 계획(→ 청크 recall)의 재현성을
            확보한다.

    Returns:
        컨텍스트 텍스트와 출처 정보를 담은 AssembledContext.
    """
    sections: list[str] = []
    sources: list[Source] = []
    doc_cache: dict[int, dict[str, Any]] = {}
    parent_doc_budget = parent_doc_total_tokens
    parent_substituted: set[int] = set()

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

    # 2. 리랭킹과 그래프 탐색을 병렬 실행 (모델 호출 두 건을 동시에 처리).
    chunk_results, graph_result = await _rerank_and_search_graph(
        query, chunk_results,
        graph_store=graph_store,
        llm_client=llm_client,
        reranker_client=reranker_client,
        embedding_client=embedding_client,
        query_embedding=query_embedding,
        rerank_enabled=rerank_enabled,
        rerank_top_k=rerank_top_k,
        rerank_score_threshold=rerank_score_threshold,
        include_graph=include_graph,
        graph_planner_seed=graph_planner_seed,
    )

    if chunk_results:
        if parent_doc_enabled:
            parent_doc_budget -= await _apply_parent_documents(
                chunk_results, meta_store,
                max_doc_tokens=parent_doc_max_doc_tokens,
                remaining_budget=parent_doc_budget,
                substituted_doc_ids=parent_substituted,
            )
        lines = []
        for r in chunk_results:
            meta = r.get("metadata") or {}
            doc_id = meta.get("document_id")
            if doc_id and doc_id not in doc_cache:
                doc = await meta_store.get_document(doc_id)
                doc_cache[doc_id] = doc if doc else {"title": f"문서 #{doc_id}"}
            title = doc_cache[doc_id]["title"] if doc_id and doc_id in doc_cache else "알 수 없음"
            distance = r.get("distance", 1.0)
            similarity = 1 - distance
            section_path = meta.get("section_path", "")
            view = meta.get("view", "")
            question_text = meta.get("question_text", "")
            is_parent_doc = bool(r.get("parent_document"))

            source_label = f"[출처: {title}]"
            if is_parent_doc:
                source_label += " (전문 첨부"
                if section_path:
                    source_label += f", 매칭 섹션: {section_path}"
                source_label += ")"
            elif section_path:
                source_label += f" (섹션: {section_path})"
            if view == "question" and question_text:
                source_label += f" (매칭 질문: {question_text})"
            lines.append(f"{source_label}\n{r.get('document', '')}")
            if doc_id and doc_id not in {s.document_id for s in sources}:
                sources.append(Source(
                    document_id=doc_id, title=title, similarity=similarity,
                    full_document=is_parent_doc,
                ))
        sections.append("\n\n".join(lines))

    # 3. 그래프 탐색 결과 처리 — 엔티티/관계 요약 + 연결 문서 본문 첨부 (설계 A)
    if graph_result:
        sections.append(graph_result.text)

        # 연결 문서 본문 첨부 (벡터가 못 찾은 그래프 도달 문서)
        graph_chunks = await _search_graph_sourced_chunks(
            query_embedding, vector_store,
            graph_result.document_ids, _extract_doc_ids(chunk_results),
            max_graph_docs=max_graph_docs,
            max_graph_tokens=max_graph_tokens,
        )
        fetched_sim: dict[int, float] = {}
        fetched_parent: set[int] = set()
        if graph_chunks:
            if parent_doc_enabled:
                parent_doc_budget -= await _apply_parent_documents(
                    graph_chunks, meta_store,
                    max_doc_tokens=parent_doc_max_doc_tokens,
                    remaining_budget=parent_doc_budget,
                    substituted_doc_ids=parent_substituted,
                )
            sections.append(
                await _format_graph_chunk_results(graph_chunks, meta_store)
            )
            for r in graph_chunks:
                did = (r.get("metadata") or {}).get("document_id")
                if did is not None:
                    fetched_sim[did] = 1 - r.get("distance", 1.0)
                    if r.get("parent_document"):
                        fetched_parent.add(did)

        # 그래프 탐색 결과에서 출처 추출 — 본문이 실제로 컨텍스트에 첨부된
        # 문서만 포함한다. graph_result.document_ids 전체(탐색된 모든 노드의
        # 문서 합집합, 개수 무제한)가 아니라, _search_graph_sourced_chunks 가
        # 개수(max_graph_docs)/토큰(max_graph_tokens) 상한 안에서 본문을 인출한
        # 문서(fetched_sim 의 키)만 노출하여, 출처 목록을 실제 컨텍스트에 들어간
        # 최종 문서와 일치시킨다.
        existing_doc_ids = {s.document_id for s in sources}
        for doc_id, similarity in fetched_sim.items():
            if doc_id not in existing_doc_ids:
                if doc_id not in doc_cache:
                    doc = await meta_store.get_document(doc_id)
                    doc_cache[doc_id] = doc if doc else {"title": f"문서 #{doc_id}"}
                title = doc_cache[doc_id]["title"]
                sources.append(Source(
                    document_id=doc_id, title=title,
                    similarity=similarity,
                    full_document=doc_id in fetched_parent,
                ))

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
    retrieved_entities = list(graph_result.entities) if graph_result else []
    retrieved_relations = list(graph_result.relations) if graph_result else []
    logger.info(
        "Assembled context | query=%s | chars=%d | sources=%d | text=%s",
        query, len(context_text), len(sources),
        context_text if context_text else "<empty>",
    )
    return AssembledContext(
        context_text=context_text,
        sources=sources,
        retrieved_graph_entities=retrieved_entities,
        retrieved_graph_relations=retrieved_relations,
    )


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
