#!/usr/bin/env python3
"""LLM 으로 검색 평가용 골드셋을 자동 생성한다.

원리:
1. 인덱싱된 문서(통째) / 그래프 서브그래프에서 계층 샘플링 (source_type 별 균등)
2. Generator LLM 으로 후보당 N개 질문 생성 (역방향 생성)
3. Judge LLM 의 4단계 품질 게이트로 사기성/노이즈 질문 탈락
4. 통과한 (질문, 정답 문서ID / 정답 그래프 엔티티) 페어를 YAML 골드셋으로 저장

Generator 와 Judge 를 서로 다른 모델로 분리하면 자기 평가 편향이 줄어든다.

사용법
------

기본 (config 의 llm.* 를 Generator/Judge 양쪽에 사용)::

    python scripts/build_synthetic_gold_set.py \\
        --config ~/.context-loop/config.yaml \\
        --n-chunks 30 \\
        --questions-per-chunk 2 \\
        --max-doc-tokens 24000 \\
        --output eval/gold_set.yaml

문서 통째 입력 — 큰 문서는 앞부분 truncate (R2 한도 가드)::

    python scripts/build_synthetic_gold_set.py \\
        --n-chunks 50 --max-chars 200000 --max-doc-tokens 16000 \\
        --output eval/gold_set.yaml

Generator/Judge 분리 (편향 회피, 권장)::

    python scripts/build_synthetic_gold_set.py \\
        --generator-endpoint http://strong-model:8080/v1 \\
        --generator-model gpt-4o \\
        --judge-endpoint http://other-family:8080/v1 \\
        --judge-model claude-haiku \\
        --output eval/gold_set.yaml

source_type 제한, 시드 고정 (재현성)::

    python scripts/build_synthetic_gold_set.py \\
        --source-types git_code,confluence_mcp \\
        --seed 42 \\
        --output eval/gold_set.yaml

그래프 기반 질문 포함 (R1 — chunk + graph 평가)::

    python scripts/build_synthetic_gold_set.py \\
        --source-types confluence_mcp,git_code \\
        --include-graph-questions \\
        --n-graph-nodes 20 \\
        --output eval/gold_set.yaml

그래프 인덱싱 강건성 옵션 (2차 — evidence + alias + 관계 채점)::

    python scripts/build_synthetic_gold_set.py \\
        --include-graph-questions --embed-graph-evidence true \\
        --score-relations --graph-match-threshold 0.78 \\
        --output eval/gold_set.yaml

cross-document 질문 생성 (R2 — 그래프 성능 측정용)::

    python scripts/build_synthetic_gold_set.py \\
        --enable-cross-doc --source-types confluence_mcp git_code \\
        --cross-doc-max-seeds 50 \\
        --output eval/gold_set.yaml

변동성 측정용 다중 골드셋 (같은 source_type 으로 N개 빌드)::

    python scripts/build_synthetic_gold_set.py \\
        --source-types git_code \\
        --seed 42 --n-gold-sets 5 \\
        --output eval/gold_sets/git_code.yaml

    # → eval/gold_sets/git_code_001.yaml  (seed=42)
    #    eval/gold_sets/git_code_002.yaml  (seed=43)
    #    ... git_code_005.yaml             (seed=46)
    # 평가 시 --gold-set-glob "eval/gold_sets/git_code_*.yaml" 로 일괄 채점.

빠른 실험 (게이트 OFF — 디버그/탐색 전용)::

    python scripts/build_synthetic_gold_set.py --no-filter --n-chunks 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from context_loop.processor.llm_client import LLMClient

# 프로젝트 루트를 sys.path 에 추가
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from context_loop.config import Config  # noqa: E402
from context_loop.eval.gold_set import (  # noqa: E402
    GoldItem,
    GoldSet,
    GraphEntityRef,
    GraphRelationRef,
    save_gold_set,
)
from context_loop.eval.graph_match import (  # noqa: E402
    DEFAULT_GRAPH_MATCH_THRESHOLD,
    aembed_with_client,
)
from context_loop.eval.llm import build_eval_llm_client, role_is_configured  # noqa: E402
from context_loop.eval.synth import (  # noqa: E402
    GeneratedGraphQuestion,
    build_korean_stopwords_from_corpus,
    build_subgraph_snippet,
    filter_question,
    find_equivalent_documents,
    generate_cross_doc_questions,
    generate_graph_questions,
    generate_questions,
    make_text_anchor,
    sanitize_graph_aliases,
    sanitize_graph_evidence,
    stratified_sample,
    truncate_to_tokens,
)
from context_loop.storage.graph_store import GraphStore  # noqa: E402
from context_loop.storage.metadata_store import MetadataStore  # noqa: E402
from context_loop.storage.vector_store import VectorStore  # noqa: E402

logger = logging.getLogger("build_synthetic_gold_set")


# ---------------------------------------------------------------------------
# Document loading
# ---------------------------------------------------------------------------


# distractor 본문은 generic 게이트(is_answerable)에 입력되므로, 통째 문서를
# 넣으면 judge 토큰이 폭주한다(R2 일관). 앞부분 prefix 만으로도 "다른 문서로
# 답이 되는가" 판정에 충분 — 토큰 비용을 상수로 고정한다.
DISTRACTOR_EXCERPT_CHARS = 2000


# S1-3 (R6 — 단일 엔티티 0/1 채점의 소표본 민감도): 그래프 entity recall 은
# per-item 이 0/1 이라 표본이 작으면 신뢰구간이 넓어 A/B 판정력이 약하다.
# 그래프 골드 항목 수가 이 임계 미만이면 빌드 종료 시 경고하고 metadata 에
# ``graph_low_sample_warning`` 을 기록한다 (N≥150 권고).
GRAPH_LOW_SAMPLE_THRESHOLD = 150


def _distractor_excerpt(content: str) -> str:
    """distractor 문서 본문의 앞부분만 잘라 generic 게이트 비용을 제한한다."""
    return content[:DISTRACTOR_EXCERPT_CHARS]


async def load_candidate_documents(
    store: MetadataStore,
    *,
    source_types: list[str] | None,
    min_chars: int,
    max_chars: int,
) -> list[dict[str, Any]]:
    """metadata_store 에서 문서 후보를 로드한다 (R1 — chunks 테이블 비의존).

    각 항목 dict 형태::

        {
            "document_id": int,          # 정답 doc + 결정성 seed 키
            "source_type": str,          # stratified_sample / distractor 필터 키
            "content": str,              # original_content (Generator 입력 = 통째)
            "title": str,                # 디버그 메타 (source_section_path 대체용)
            "url": str,                  # 디버그 메타
        }

    original_content 길이(문자) 기준으로 min/max_chars 필터. NULL/빈 문서 제외.
    """
    documents = await store.list_documents()

    out: list[dict[str, Any]] = []
    for doc in documents:
        if source_types and doc.get("source_type") not in source_types:
            continue
        content: str = doc.get("original_content") or ""
        if not content.strip():
            continue
        if len(content) < min_chars or len(content) > max_chars:
            continue
        out.append({
            "document_id": doc["id"],
            "source_type": doc.get("source_type", ""),
            "content": content,
            "title": doc.get("title") or "",
            "url": doc.get("url") or "",
        })
    # 결정론적 순서 — document_id 오름차순 (1 doc = 1 후보, chunk_index 불필요)
    out.sort(key=lambda x: x["document_id"])
    logger.info("후보 문서 로드 완료 — total=%d", len(out))
    return out


# ---------------------------------------------------------------------------
# Graph subgraph loading
# ---------------------------------------------------------------------------


async def load_candidate_subgraphs(
    meta_store: MetadataStore,
    graph_store: GraphStore,
    *,
    source_types: list[str] | None,
    min_neighbors: int = 1,
) -> list[dict[str, Any]]:
    """그래프 후보를 로드한다.

    각 항목 dict 형태::

        {
            "entity_name": str,
            "entity_type": str,
            "entity_description": str,
            "document_ids": list[int],          # 노드 소유 문서들
            "primary_document_id": int,         # 출처 추적용 (document_ids[0])
            "source_type": str,                 # 소유 문서의 source_type
            "edges": list[dict],                # 1-hop 엣지 (source_name/target_name/relation_type)
            "subgraph_snippet": str,            # LLM 입력용 포맷팅
        }

    Args:
        meta_store: SQLite 메타스토어 (문서·노드 조회).
        graph_store: NetworkX 그래프 (1-hop neighbors / edges 조회).
        source_types: 화이트리스트. 빈 값/None 이면 전체.
        min_neighbors: 1-hop 이웃 최소 수 (W-2 — 미만이면 제외).
    """
    nodes = await meta_store.get_all_graph_nodes()
    documents = await meta_store.list_documents()
    doc_by_id = {d["id"]: d for d in documents}

    out: list[dict[str, Any]] = []
    for node in nodes:
        name = str(node.get("entity_name") or "")
        if not name:
            continue
        etype = str(node.get("entity_type") or "")

        # 노드 소유 문서들 — graph_node_documents 링크 테이블에서 조회
        node_id = node.get("id")
        if node_id is None:
            continue
        doc_ids = await meta_store.get_node_document_ids(int(node_id))
        if not doc_ids:
            continue

        # source_type 필터 — 소유 문서 중 하나라도 화이트리스트면 통과
        owning_types = {
            doc_by_id[d].get("source_type", "")
            for d in doc_ids
            if d in doc_by_id
        }
        if source_types and not (set(source_types) & owning_types):
            continue
        primary_doc_id = doc_ids[0]
        primary_source_type = doc_by_id.get(primary_doc_id, {}).get(
            "source_type", "",
        )

        # 1-hop 이웃 + 엣지
        neighbors = graph_store.get_neighbors(name, depth=1)
        if len(neighbors) < min_neighbors + 1:
            # neighbors 에는 자기 자신도 포함 (depth=0) — 1-hop 이웃이 0개면 제외
            continue

        neighbor_ids = [n["id"] for n in neighbors if n.get("id") is not None]
        raw_edges = graph_store.get_edges_between(neighbor_ids)
        id_to_name = {n["id"]: n.get("entity_name", "") for n in neighbors}
        edges: list[dict[str, Any]] = []
        for e in raw_edges:
            edges.append({
                "source_name": id_to_name.get(e.get("source"), "?"),
                "target_name": id_to_name.get(e.get("target"), "?"),
                "relation_type": e.get("relation_type", ""),
            })

        # entity_description — properties JSON 의 description 필드
        description = ""
        props_raw = node.get("properties")
        if props_raw:
            try:
                props = json.loads(props_raw) if isinstance(props_raw, str) else props_raw
                if isinstance(props, dict):
                    description = str(props.get("description") or "")
            except (json.JSONDecodeError, TypeError):
                description = ""

        snippet = build_subgraph_snippet(
            entity_name=name,
            entity_type=etype,
            entity_description=description,
            edges=edges,
        )

        out.append({
            "entity_name": name,
            "entity_type": etype,
            "entity_description": description,
            "document_ids": list(doc_ids),
            "primary_document_id": primary_doc_id,
            "source_type": primary_source_type,
            "edges": edges,
            "subgraph_snippet": snippet,
            "primary_document_content": (  # R3 — 소유 문서 원문 (추가 DB 호출 없음)
                doc_by_id.get(primary_doc_id, {}).get("original_content") or ""
            ),
        })

    # 결정론적 정렬
    out.sort(key=lambda x: (
        x["primary_document_id"],
        x["entity_name"],
        x["entity_type"],
    ))
    logger.info(
        "후보 subgraph 로드 완료 — total=%d, source_nodes=%d",
        len(out), len(nodes),
    )
    return out


async def load_cross_doc_seeds(
    meta_store: MetadataStore,
    graph_store: GraphStore,
    *,
    source_types: list[str] | None,
    max_seeds: int | None = None,
) -> list[dict[str, Any]]:
    """서로 다른 문서를 잇는 엣지에서 cross-doc 질의 씨앗을 결정론적으로 추출 (R2).

    각 씨앗 dict::

        {
            "source_entity": {"name": str, "type": str, "doc_id": int},
            "target_entity": {"name": str, "type": str, "doc_id": int},
            "relation_type": str,
            "document_ids": [src_doc_id, tgt_doc_id],   # AND 그룹의 재료
            "source_type": str,                          # 대표 source_type
        }

    'cross-doc' 정의: 엣지의 source 노드 소유 문서집합과 target 노드 소유 문서
    집합이 서로소(겹치지 않음)인 엣지. → 한 문서만 봐서는 양쪽 엔티티를 모두
    알 수 없음. 같은 입력 + 정렬키면 항상 같은 씨앗 리스트 (결정론).
    """
    documents = await meta_store.list_documents()
    doc_by_id = {d["id"]: d for d in documents}

    g = graph_store.graph
    out: list[dict[str, Any]] = []
    for u, v, data in g.edges(data=True):
        if not (g.has_node(u) and g.has_node(v)):
            continue
        docs_u = set(g.nodes[u].get("document_ids") or set())
        docs_v = set(g.nodes[v].get("document_ids") or set())
        if not (docs_u and docs_v and docs_u.isdisjoint(docs_v)):
            continue

        # source_type 필터 — 양쪽 소유 문서 중 하나라도 화이트리스트면 통과.
        owning_types = {
            doc_by_id[d].get("source_type", "")
            for d in (docs_u | docs_v)
            if d in doc_by_id
        }
        if source_types and not (set(source_types) & owning_types):
            continue

        src_doc = min(docs_u)
        tgt_doc = min(docs_v)
        src_name = str(g.nodes[u].get("entity_name") or "")
        tgt_name = str(g.nodes[v].get("entity_name") or "")
        if not (src_name and tgt_name):
            continue
        src_type = str(g.nodes[u].get("entity_type") or "")
        tgt_type = str(g.nodes[v].get("entity_type") or "")
        relation_type = str(data.get("relation_type") or "")
        primary_source_type = doc_by_id.get(src_doc, {}).get("source_type", "")

        out.append({
            "source_entity": {"name": src_name, "type": src_type, "doc_id": src_doc},
            "target_entity": {"name": tgt_name, "type": tgt_type, "doc_id": tgt_doc},
            "relation_type": relation_type,
            "document_ids": [src_doc, tgt_doc],
            "source_type": primary_source_type,
        })

    # 결정론적 정렬 — (src_doc, tgt_doc, source_name, target_name, relation_type)
    out.sort(key=lambda s: (
        s["source_entity"]["doc_id"],
        s["target_entity"]["doc_id"],
        s["source_entity"]["name"],
        s["target_entity"]["name"],
        s["relation_type"],
    ))
    if max_seeds is not None and max_seeds >= 0:
        out = out[:max_seeds]
    logger.info("cross-doc 씨앗 로드 완료 — total=%d", len(out))
    return out


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


async def build(
    *,
    config: Config,
    n_chunks: int,
    questions_per_chunk: int,
    output_path: Path,
    source_types: list[str] | None,
    seed: int | None,
    apply_filter: bool,
    n_distractors: int,
    generator: LLMClient,
    judge: LLMClient,
    reasoning_mode: str | None,
    min_chars: int,
    max_chars: int,
    max_doc_tokens: int = 0,
    enable_graph_mode: bool = False,
    enable_cross_doc: bool = False,
    cross_doc_max_seeds: int | None = None,
    n_graph_nodes: int = 0,
    min_graph_neighbors: int = 1,
    embed_graph_evidence: bool = True,
    score_relations: bool = False,
    graph_match_threshold: float = DEFAULT_GRAPH_MATCH_THRESHOLD,
    embedding_client: Any | None = None,
    embedding_model_id: str = "",
    concurrency: int = 1,
    generator_model: str = "",
    generator_endpoint: str = "",
    judge_model: str = "",
    judge_endpoint: str = "",
    generator_configured_separately: bool = False,
    judge_configured_separately: bool = False,
    self_evaluation_warning: bool = False,
    allow_self_eval: bool = False,
    generator_temperature: float = 0.0,
    generator_seed_base: int | None = None,
    equivalence_enabled: bool = False,
    equivalence_top_m: int = 3,
    equivalence_min_similarity: float = 0.6,
) -> GoldSet:
    """전체 파이프라인 실행.

    2차 추가 파라미터:
        embed_graph_evidence: ``True`` 면 graph 골드 항목의 description 들을
            모아 한 번에 임베딩하여 ``description_embedding`` 에 박는다.
            ``False`` 면 embedding 미보유 — 평가 시 lazy 계산.
        score_relations: ``True`` 면 generator 가 채운 관계 evidence 도
            ``GraphRelationRef`` 로 골드셋에 emit.
        graph_match_threshold: 골드셋 metadata 에 기록할 기본 τ (평가 시
            보고 비교용).
        embedding_client: graph 임베딩 계산용 클라이언트. ``None`` 이면
            임베딩 미계산.
        embedding_model_id: 골드셋 metadata 에 기록할 임베딩 모델 ID.

    3차 추가 파라미터:
        concurrency: 항목(chunk/subgraph) 단위 동시 처리 수. 1 이면 직렬.
            LLM endpoint 의 rate limit 에 맞춰 사용자가 명시 (보통 4~8).

    감사 보강 파라미터 (golden-set 추적성):
        generator_model: 실제 사용된 Generator 모델 ID (CLI > eval.generator >
            llm 우선순위로 호출자가 해석한 값). 골드셋 metadata 에 기록되어
            사후에 어떤 모델로 빌드되었는지 추적 가능.
        generator_endpoint: Generator endpoint URL (동일 우선순위).
        judge_model: 실제 사용된 Judge 모델 ID.
        judge_endpoint: Judge endpoint URL.
        generator_configured_separately: Generator 가 system LLM 과 분리되어
            구성됐는지 (CLI override 또는 ``config.eval.generator.*``).
        judge_configured_separately: Judge 동일.
        self_evaluation_warning: Generator/Judge 가 모두 system LLM 으로
            fall-through 되어 자기 평가 편향 위험이 있는 빌드인지 표시.
        allow_self_eval: 사용자가 ``--allow-self-eval`` 로 명시 허용했는지.
    """

    rng = random.Random(seed)

    store = MetadataStore(config.data_dir / "metadata.db")
    await store.initialize()

    graph_store: GraphStore | None = None
    if enable_graph_mode or enable_cross_doc:
        graph_store = GraphStore(store)
        await graph_store.load_from_db()

    try:
        candidates = await load_candidate_documents(
            store,
            source_types=source_types,
            min_chars=min_chars,
            max_chars=max_chars,
        )
        if not candidates:
            raise RuntimeError("후보 문서가 없습니다. 인덱싱된 문서가 있는지 확인하세요.")

        sampled = stratified_sample(
            candidates, n_total=n_chunks, key="source_type", rng=rng,
        )
        logger.info(
            "문서 샘플링 완료 — sampled=%d (요청 %d)", len(sampled), n_chunks,
        )

        # 일반성 게이트용 distractor 풀: 샘플과 다른 문서에서 무작위 추출
        sampled_doc_ids = {s["document_id"] for s in sampled}
        distractor_pool = [
            c for c in candidates if c["document_id"] not in sampled_doc_ids
        ]
        rng.shuffle(distractor_pool)

        # S3 — 한글 화이트리스트 자동 학습. 전체 후보 문서 코퍼스에서 빈도 ≥
        # 8 인 한글 stem 을 도메인 일반어로 간주, false positive 누설 검출 감소.
        # 문서 원문은 청크 합집합이라 빈도가 자연 증가 → 임계 8 로 선별 강도 유지.
        extra_korean_stopwords = build_korean_stopwords_from_corpus(
            [c["content"] for c in candidates],
            min_corpus_freq=8,
            max_stopwords=500,
        )
        logger.info(
            "한글 stopword 자동 학습 — %d 개 stem (도메인 일반어)",
            len(extra_korean_stopwords),
        )

        items: list[GoldItem] = []
        stats: dict[str, int] = {
            "generated": 0,
            "passed": 0,
            "fail_not_answerable": 0,
            "fail_leakage": 0,
            "fail_korean_leakage": 0,
            "fail_non_unique_source": 0,
            "fail_demonstrative": 0,
            "fail_generic": 0,
            "fail_parse": 0,
            "fail_runtime": 0,
            "truncated_too_large": 0,
            "graph_generated": 0,
            "graph_passed": 0,
            "cross_doc_generated": 0,
            "cross_doc_passed": 0,
            # Phase 3.5 — OR-동치 자동 검출 통계.
            "equivalence_groups_added": 0,
            "equivalence_member_total": 0,
            "non_unique_recovered": 0,
        }

        # Phase 3.5 — OR-동치 자동 검출용 벡터 스토어(코퍼스 전역 검색).
        # 활성 시에만 초기화한다. apply_filter 가 꺼져 있으면 게이트 자체가 없어
        # 동치 검출도 의미가 없으므로 비활성으로 둔다.
        equiv_vector_store: VectorStore | None = None
        equiv_active = equivalence_enabled and apply_filter
        if equiv_active:
            equiv_vector_store = VectorStore(config.data_dir)
            equiv_vector_store.initialize()

        effective_concurrency = max(1, concurrency)
        sem = asyncio.Semaphore(effective_concurrency)

        # next_id 는 chunk 모드 → graph 모드로 연속 부여한다 (D-3).
        next_id = 1
        next_id = await _run_chunk_mode(
            sampled=sampled,
            distractor_pool=distractor_pool,
            generator=generator,
            judge=judge,
            questions_per_chunk=questions_per_chunk,
            n_distractors=n_distractors,
            reasoning_mode=reasoning_mode,
            apply_filter=apply_filter,
            sem=sem,
            items=items,
            stats=stats,
            next_id=next_id,
            generator_temperature=generator_temperature,
            generator_seed_base=generator_seed_base,
            extra_korean_stopwords=extra_korean_stopwords,
            max_doc_tokens=max_doc_tokens,
            equivalence_enabled=equiv_active,
            equivalence_top_m=equivalence_top_m,
            equivalence_min_similarity=equivalence_min_similarity,
            vector_store=equiv_vector_store,
            embedding_client=embedding_client,
        )

        # 그래프 모드 실행 — chunk 모드 이후에 머지
        if enable_graph_mode and graph_store is not None:
            next_id = await _run_graph_mode(
                meta_store=store,
                graph_store=graph_store,
                source_types=source_types,
                min_neighbors=min_graph_neighbors,
                n_graph_nodes=n_graph_nodes,
                questions_per_chunk=questions_per_chunk,
                generator=generator,
                judge=judge,
                reasoning_mode=reasoning_mode,
                apply_filter=apply_filter,
                n_distractors=n_distractors,
                rng=rng,
                items=items,
                stats=stats,
                score_relations=score_relations,
                embed_evidence=embed_graph_evidence,
                embedding_client=embedding_client,
                sem=sem,
                next_id=next_id,
                generator_temperature=generator_temperature,
                generator_seed_base=generator_seed_base,
                extra_korean_stopwords=extra_korean_stopwords,
                max_doc_tokens=max_doc_tokens,
            )

        # cross-doc 모드 실행 — graph 모드 이후에 머지 (R2)
        if enable_cross_doc and graph_store is not None:
            next_id = await _run_cross_doc_mode(
                meta_store=store,
                graph_store=graph_store,
                source_types=source_types,
                max_seeds=cross_doc_max_seeds,
                questions_per_chunk=questions_per_chunk,
                generator=generator,
                judge=judge,
                reasoning_mode=reasoning_mode,
                apply_filter=apply_filter,
                items=items,
                stats=stats,
                sem=sem,
                next_id=next_id,
                generator_temperature=generator_temperature,
                generator_seed_base=generator_seed_base,
                extra_korean_stopwords=extra_korean_stopwords,
            )

        generation_modes = ["chunk"]
        if enable_graph_mode:
            generation_modes.append("graph")
        if enable_cross_doc:
            generation_modes.append("cross_doc")

        metadata: dict[str, Any] = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "n_chunks_sampled": len(sampled),
            "questions_per_chunk": questions_per_chunk,
            "filter_applied": apply_filter,
            "seed": seed,
            "source_types": source_types or [],
            "generation_modes": generation_modes,
            "concurrency": effective_concurrency,
            "stats": stats,
            "generator_model": generator_model,
            "generator_endpoint": generator_endpoint,
            "judge_model": judge_model,
            "judge_endpoint": judge_endpoint,
            "generator_configured_separately": generator_configured_separately,
            "judge_configured_separately": judge_configured_separately,
            "self_evaluation_warning": self_evaluation_warning,
            "allow_self_eval": allow_self_eval,
            "generator_temperature": generator_temperature,
            "generator_seed_base": generator_seed_base,
            # Phase 3.5 — OR-동치 자동 검출 메타(앵커링·재현성).
            "or_equivalence_detection": equiv_active,
            "equivalence_top_m": equivalence_top_m if equiv_active else 0,
            "equivalence_min_similarity": (
                equivalence_min_similarity if equiv_active else 0.0
            ),
            "equivalence_embedding_model": embedding_model_id or "" if equiv_active else "",
        }
        if enable_graph_mode:
            graph_meta = _build_graph_metadata(
                items=items,
                stats=stats,
                embedding_model_id=embedding_model_id,
                graph_match_threshold=graph_match_threshold,
                score_relations=score_relations,
                embed_graph_evidence=embed_graph_evidence,
            )
            metadata.update(graph_meta)
            if graph_meta["graph_low_sample_warning"]:
                logger.warning(
                    "그래프 골드 항목 수가 %d개로 권고치 %d 미만입니다 — "
                    "단일 엔티티 0/1 채점이라 그래프 메트릭 신뢰구간이 넓어 "
                    "A/B 판정력이 약합니다. N≥%d 를 권장합니다.",
                    graph_meta["graph_question_count"],
                    GRAPH_LOW_SAMPLE_THRESHOLD,
                    GRAPH_LOW_SAMPLE_THRESHOLD,
                )
        if enable_cross_doc:
            # cross-doc 생성 정책 추적성 (R2 — 결정론 씨앗 + LLM 문장화).
            metadata["cross_doc_enabled"] = True
            metadata["cross_doc_max_seeds"] = cross_doc_max_seeds
            metadata["cross_doc_generation"] = "deterministic_seed+llm_phrasing"

        gold = GoldSet(version=1, items=items, metadata=metadata)
        save_gold_set(gold, output_path)
        logger.info(
            "골드셋 저장 — path=%s, items=%d, stats=%s",
            output_path, len(items), stats,
        )
        return gold

    finally:
        await store.close()


async def _process_chunk_item(
    idx: int,
    chunk: dict[str, Any],
    *,
    distractor_pool: list[dict[str, Any]],
    generator: LLMClient,
    judge: LLMClient,
    questions_per_chunk: int,
    n_distractors: int,
    reasoning_mode: str | None,
    apply_filter: bool,
    sem: asyncio.Semaphore,
    total: int,
    generator_temperature: float = 0.0,
    generator_seed_base: int | None = None,
    extra_korean_stopwords: frozenset[str] | None = None,
    max_doc_tokens: int = 0,
    equivalence_enabled: bool = False,
    equivalence_top_m: int = 3,
    equivalence_min_similarity: float = 0.6,
    vector_store: Any | None = None,
    embedding_client: Any | None = None,
) -> tuple[list[GoldItem], dict[str, int]]:
    """문서 1건 처리 — LLM 생성·게이트를 수행하고 (items, local_stats) 반환.

    ``chunk`` 인자는 문서 후보 dict (``load_candidate_documents`` 산출물 —
    document_id/source_type/content/title/url). id 는 호출자가 gather 완료 후
    idx 순서로 부여하므로 여기서는 ``id=""``.
    distractor_pool 은 read-only 공유 (D-7) — 슬라이싱만 사용한다.
    """
    async with sem:
        logger.info(
            "[doc start %d/%d] doc=%d, source_type=%s",
            idx, total, chunk["document_id"], chunk["source_type"],
        )
        local_items: list[GoldItem] = []
        local_stats: dict[str, int] = {}

        # 문서별 결정성: seed_base + document_id 로 문서 단위 deterministic seed.
        item_seed = (
            generator_seed_base + int(chunk["document_id"])
            if generator_seed_base is not None
            else None
        )
        # R2 — generator 입력 한도 가드. 초과 시 앞부분 truncate (skip 아님).
        gen_content, truncated = truncate_to_tokens(chunk["content"], max_doc_tokens)
        if truncated:
            local_stats["truncated_too_large"] = (
                local_stats.get("truncated_too_large", 0) + 1
            )
        generated = await generate_questions(
            gen_content,
            n=questions_per_chunk,
            generator=generator,
            reasoning_mode=reasoning_mode,
            temperature=generator_temperature,
            seed=item_seed,
        )
        local_stats["generated"] = local_stats.get("generated", 0) + len(generated)

        if not generated:
            logger.warning("  → 생성 실패 (빈 응답)")
            local_stats["fail_parse"] = local_stats.get("fail_parse", 0) + 1
            logger.info("[doc done %d/%d]", idx, total)
            return local_items, local_stats

        # distractor 는 같은 source_type 내에서 우선 골라야 식별자 충돌이 적다.
        same_type_distractors = [
            c for c in distractor_pool
            if c["source_type"] == chunk["source_type"]
        ][:n_distractors]
        if len(same_type_distractors) < n_distractors:
            fill = [c for c in distractor_pool if c not in same_type_distractors]
            same_type_distractors += fill[: n_distractors - len(same_type_distractors)]

        anchor = make_text_anchor(chunk["content"])

        for j, gq in enumerate(generated):
            if not apply_filter:
                local_items.append(GoldItem(
                    id="",
                    query=gq.query,
                    relevant_doc_ids=[chunk["document_id"]],
                    source_type=chunk["source_type"],
                    source_document_id=chunk["document_id"],
                    source_text_anchor=anchor,
                    source_section_path=chunk["title"],
                    difficulty=gq.difficulty,
                    synthesized=True,
                ))
                local_stats["passed"] = local_stats.get("passed", 0) + 1
                continue

            # S3 — Judge 게이트도 결정성을 위해 seed 전달.
            # chunk 단위 base seed + 질문 인덱스 j 로 질문별 deterministic seed.
            judge_seed = (
                (item_seed + 10000 + j) if item_seed is not None else None
            )
            report = await filter_question(
                gq.query,
                chunk["content"],
                [_distractor_excerpt(d["content"]) for d in same_type_distractors],
                judge=judge,
                reasoning_mode=reasoning_mode,
                seed=judge_seed,
                extra_korean_stopwords=extra_korean_stopwords,
            )
            src_doc = int(chunk["document_id"])

            async def _detect_equivalents() -> list[int]:
                """OR-동치(경우 B) 후보 검출 — 비활성 시 빈 리스트."""
                if not equivalence_enabled:
                    return []
                eq_seed = (
                    (item_seed + 20000 + j) if item_seed is not None else None
                )
                return await find_equivalent_documents(
                    gq.query,
                    chunk["content"],
                    src_doc,
                    embedding_client=embedding_client,
                    vector_store=vector_store,
                    judge=judge,
                    top_m=equivalence_top_m,
                    min_similarity=equivalence_min_similarity,
                    reasoning_mode=reasoning_mode,
                    seed=eq_seed,
                )

            def _append_item(doc_ids: list[int]) -> None:
                groups = [doc_ids] if len(doc_ids) > 1 else []
                local_items.append(GoldItem(
                    id="",
                    query=gq.query,
                    relevant_doc_ids=doc_ids,
                    relevant_doc_groups=groups,
                    source_type=chunk["source_type"],
                    source_document_id=src_doc,
                    source_text_anchor=anchor,
                    source_section_path=chunk["title"],
                    difficulty=gq.difficulty,
                    synthesized=True,
                ))
                local_stats["passed"] = local_stats.get("passed", 0) + 1
                if len(doc_ids) > 1:
                    local_stats["equivalence_groups_added"] = (
                        local_stats.get("equivalence_groups_added", 0) + 1
                    )
                    local_stats["equivalence_member_total"] = (
                        local_stats.get("equivalence_member_total", 0)
                        + len(doc_ids)
                    )

            if not report.passed:
                # 경우 B 구제: uniqueness 게이트가 'non_unique_source' 로 막은
                # 질문이 실제로 코퍼스에 동등 문서를 가지면 폐기 대신 OR 그룹으로
                # 기록(recall 과소평가 해소). 동등 문서가 없으면(=일반 답변, 경우
                # A) 기존대로 폐기. 그 외 사유(leakage/demonstrative/...)는 폐기.
                recovered: list[int] = []
                if report.reason == "non_unique_source":
                    recovered = await _detect_equivalents()
                if not recovered:
                    key = f"fail_{report.reason}" if report.reason else "fail_parse"
                    local_stats[key] = local_stats.get(key, 0) + 1
                    logger.info(
                        "  q%d 탈락 — reason=%s, query=%s",
                        j + 1, report.reason, gq.query[:80],
                    )
                    continue
                _append_item(sorted({src_doc, *recovered}))
                local_stats["non_unique_recovered"] = (
                    local_stats.get("non_unique_recovered", 0) + 1
                )
                logger.info(
                    "  q%d 구제(동치 %d) — query=%s",
                    j + 1, len(recovered), gq.query[:80],
                )
                continue

            # 통과 — uniqueness LLM 이 'unique' 라 판단해도 게이트는 코퍼스 전역
            # 검색이 아니므로 진짜 동등 문서를 놓쳤을 수 있다. 한 번 더 확인하여
            # 있으면 OR 그룹으로 기록(놓친 동등 문서 → recall 과소평가 방지).
            equivalents = await _detect_equivalents()
            _append_item(sorted({src_doc, *equivalents}))
            logger.info(
                "  q%d 통과%s — query=%s",
                j + 1,
                f"(동치 {len(equivalents)})" if equivalents else "",
                gq.query[:80],
            )

        logger.info("[doc done %d/%d]", idx, total)
        return local_items, local_stats


async def _run_chunk_mode(
    *,
    sampled: list[dict[str, Any]],
    distractor_pool: list[dict[str, Any]],
    generator: LLMClient,
    judge: LLMClient,
    questions_per_chunk: int,
    n_distractors: int,
    reasoning_mode: str | None,
    apply_filter: bool,
    sem: asyncio.Semaphore,
    items: list[GoldItem],
    stats: dict[str, int],
    next_id: int,
    generator_temperature: float = 0.0,
    generator_seed_base: int | None = None,
    extra_korean_stopwords: frozenset[str] | None = None,
    max_doc_tokens: int = 0,
    equivalence_enabled: bool = False,
    equivalence_top_m: int = 3,
    equivalence_min_similarity: float = 0.6,
    vector_store: Any | None = None,
    embedding_client: Any | None = None,
) -> int:
    """sampled 문서들을 동시 처리하고 결과를 items 에 합친다.

    Returns: 다음 idx 에 사용될 ``next_id`` (graph 모드와 연속).
    """
    total = len(sampled)
    tasks = [
        _process_chunk_item(
            idx, chunk,
            distractor_pool=distractor_pool,
            generator=generator,
            judge=judge,
            questions_per_chunk=questions_per_chunk,
            n_distractors=n_distractors,
            reasoning_mode=reasoning_mode,
            apply_filter=apply_filter,
            sem=sem,
            total=total,
            generator_temperature=generator_temperature,
            generator_seed_base=generator_seed_base,
            extra_korean_stopwords=extra_korean_stopwords,
            max_doc_tokens=max_doc_tokens,
            equivalence_enabled=equivalence_enabled,
            equivalence_top_m=equivalence_top_m,
            equivalence_min_similarity=equivalence_min_similarity,
            vector_store=vector_store,
            embedding_client=embedding_client,
        )
        for idx, chunk in enumerate(sampled, start=1)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for idx, r in enumerate(results, start=1):
        if isinstance(r, BaseException):
            logger.exception(
                "[chunk fail %d/%d] %s", idx, total, r,
                exc_info=r if isinstance(r, Exception) else None,
            )
            stats["fail_runtime"] = stats.get("fail_runtime", 0) + 1
            continue
        local_items, local_stats = r
        for item in local_items:
            item.id = f"q{next_id:04d}"
            items.append(item)
            next_id += 1
        _merge_stats(stats, local_stats)
    return next_id


def _merge_stats(target: dict[str, int], local: dict[str, int]) -> None:
    """LocalStats dict 를 main stats 에 더한다 (동적 키 포함)."""
    for k, v in local.items():
        target[k] = target.get(k, 0) + v


async def _process_subgraph_item(
    idx: int,
    sg: dict[str, Any],
    *,
    distractor_pool: list[dict[str, Any]],
    skip_generic_gate: bool,
    generator: LLMClient,
    judge: LLMClient,
    questions_per_chunk: int,
    n_distractors: int,
    reasoning_mode: str | None,
    apply_filter: bool,
    score_relations: bool,
    sem: asyncio.Semaphore,
    total: int,
    generator_temperature: float = 0.0,
    generator_seed_base: int | None = None,
    extra_korean_stopwords: frozenset[str] | None = None,
    max_doc_tokens: int = 0,
) -> tuple[list[GoldItem], dict[str, int]]:
    """subgraph 1개 처리 — graph 질문 생성·게이트.

    id 는 호출자가 후처리에서 부여 — placeholder ``id=""``.
    """
    async with sem:
        logger.info(
            "[graph start %d/%d] entity=%s (%s), source_type=%s",
            idx, total, sg["entity_name"], sg["entity_type"],
            sg["source_type"],
        )
        local_items: list[GoldItem] = []
        local_stats: dict[str, int] = {}

        # subgraph 별 결정성: seed_base + idx (subgraph 정렬 위치) 로 결정적 seed.
        sg_seed = (
            generator_seed_base + idx if generator_seed_base is not None else None
        )
        generated = await generate_graph_questions(
            sg,
            n=questions_per_chunk,
            generator=generator,
            reasoning_mode=reasoning_mode,
            temperature=generator_temperature,
            seed=sg_seed,
            doc_max_tokens=max_doc_tokens,
        )
        local_stats["graph_generated"] = local_stats.get("graph_generated", 0) + len(generated)
        local_stats["generated"] = local_stats.get("generated", 0) + len(generated)

        if not generated:
            logger.warning("  → 그래프 생성 실패 (빈 응답)")
            local_stats["fail_parse"] = local_stats.get("fail_parse", 0) + 1
            logger.info("[graph done %d/%d]", idx, total)
            return local_items, local_stats

        same_type_distractors = [
            s for s in distractor_pool if s["source_type"] == sg["source_type"]
        ][:n_distractors]
        if len(same_type_distractors) < n_distractors:
            fill = [s for s in distractor_pool if s not in same_type_distractors]
            same_type_distractors += fill[
                : n_distractors - len(same_type_distractors)
            ]

        distractor_snippets = (
            [] if skip_generic_gate
            else [d["subgraph_snippet"] for d in same_type_distractors]
        )

        for j, gq in enumerate(generated):
            if not apply_filter:
                local_items.append(_make_graph_gold_item(
                    sg, gq, score_relations=score_relations,
                    source_text=sg["subgraph_snippet"],
                    extra_korean_stopwords=extra_korean_stopwords,
                    leak_stats=local_stats,
                ))
                local_stats["passed"] = local_stats.get("passed", 0) + 1
                local_stats["graph_passed"] = local_stats.get("graph_passed", 0) + 1
                continue

            # S3 — graph 모드에도 Judge seed 전파 (subgraph_seed + j).
            graph_judge_seed = (
                (sg_seed + 10000 + j) if sg_seed is not None else None
            )
            report = await filter_question(
                gq.query,
                sg["subgraph_snippet"],
                distractor_snippets,
                judge=judge,
                reasoning_mode=reasoning_mode,
                seed=graph_judge_seed,
                extra_korean_stopwords=extra_korean_stopwords,
            )
            if not report.passed:
                key = f"fail_{report.reason}" if report.reason else "fail_parse"
                local_stats[key] = local_stats.get(key, 0) + 1
                logger.info(
                    "  graph q%d 탈락 — reason=%s, query=%s",
                    j + 1, report.reason, gq.query[:80],
                )
                continue

            local_items.append(_make_graph_gold_item(
                sg, gq, score_relations=score_relations,
                source_text=sg["subgraph_snippet"],
                extra_korean_stopwords=extra_korean_stopwords,
                leak_stats=local_stats,
            ))
            local_stats["passed"] = local_stats.get("passed", 0) + 1
            local_stats["graph_passed"] = local_stats.get("graph_passed", 0) + 1
            logger.info(
                "  graph q%d 통과 — query=%s", j + 1, gq.query[:80],
            )

        logger.info("[graph done %d/%d]", idx, total)
        return local_items, local_stats


async def _run_graph_mode(
    *,
    meta_store: MetadataStore,
    graph_store: GraphStore,
    source_types: list[str] | None,
    min_neighbors: int,
    n_graph_nodes: int,
    questions_per_chunk: int,
    generator: LLMClient,
    judge: LLMClient,
    reasoning_mode: str | None,
    apply_filter: bool,
    n_distractors: int,
    rng: random.Random,
    items: list[GoldItem],
    stats: dict[str, int],
    score_relations: bool = False,
    embed_evidence: bool = True,
    embedding_client: Any | None = None,
    sem: asyncio.Semaphore | None = None,
    next_id: int = 1,
    generator_temperature: float = 0.0,
    generator_seed_base: int | None = None,
    extra_korean_stopwords: frozenset[str] | None = None,
    max_doc_tokens: int = 0,
) -> int:
    """graph 모드 — subgraph 샘플링 후 질문 생성·게이트 적용.

    Returns: 다음에 부여될 ``next_id`` (chunk + graph 연속 공간).
    """
    subgraphs = await load_candidate_subgraphs(
        meta_store,
        graph_store,
        source_types=source_types,
        min_neighbors=min_neighbors,
    )
    if not subgraphs:
        # W-2: 후보 0개면 silent skip + 경고
        logger.warning(
            "그래프 후보가 0개 — 인덱싱된 graph_nodes 가 없거나 source_type 필터에 "
            "매칭되지 않습니다. graph 모드를 건너뜁니다.",
        )
        return next_id

    sampled_sg = stratified_sample(
        subgraphs, n_total=n_graph_nodes, key="source_type", rng=rng,
    )
    logger.info(
        "subgraph 샘플링 완료 — sampled=%d (요청 %d)",
        len(sampled_sg), n_graph_nodes,
    )

    # graph distractor 풀
    sampled_keys = {
        (s["entity_name"].lower(), s["entity_type"]) for s in sampled_sg
    }
    distractor_pool = [
        s for s in subgraphs
        if (s["entity_name"].lower(), s["entity_type"]) not in sampled_keys
    ]
    rng.shuffle(distractor_pool)

    # W-9: distractor 풀 부족 (<5) 시 일반성 게이트 skip 경고
    skip_generic_gate = len(distractor_pool) < 5
    if skip_generic_gate:
        logger.warning(
            "graph distractor 풀이 5개 미만 — 일반성 게이트 신뢰도가 낮습니다.",
        )

    # sem 미지정 시 직렬 동작 (보호 — 단독 호출자 호환).
    if sem is None:
        sem = asyncio.Semaphore(1)

    total = len(sampled_sg)
    tasks = [
        _process_subgraph_item(
            idx, sg,
            distractor_pool=distractor_pool,
            skip_generic_gate=skip_generic_gate,
            generator=generator,
            judge=judge,
            questions_per_chunk=questions_per_chunk,
            n_distractors=n_distractors,
            reasoning_mode=reasoning_mode,
            apply_filter=apply_filter,
            score_relations=score_relations,
            sem=sem,
            total=total,
            generator_temperature=generator_temperature,
            generator_seed_base=generator_seed_base,
            extra_korean_stopwords=extra_korean_stopwords,
            max_doc_tokens=max_doc_tokens,
        )
        for idx, sg in enumerate(sampled_sg, start=1)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for idx, r in enumerate(results, start=1):
        if isinstance(r, BaseException):
            logger.exception(
                "[graph fail %d/%d] %s", idx, total, r,
                exc_info=r if isinstance(r, Exception) else None,
            )
            stats["fail_runtime"] = stats.get("fail_runtime", 0) + 1
            continue
        local_items, local_stats = r
        for item in local_items:
            item.id = f"q{next_id:04d}"
            items.append(item)
            next_id += 1
        _merge_stats(stats, local_stats)

    # 모든 graph 항목 생성 후 description 임베딩을 배치로 계산하여 채운다.
    if embed_evidence:
        await _embed_graph_item_descriptions(items, embedding_client)

    return next_id


def _cross_doc_seed_snippet(seed: dict[str, Any]) -> str:
    """cross-doc 씨앗을 judge 게이트 입력용 텍스트로 포맷팅 (양쪽 엔티티+관계)."""
    src = seed["source_entity"]
    tgt = seed["target_entity"]
    lines = [
        f"엔티티 A: {src['name']} ({src['type']})",
        f"엔티티 B: {tgt['name']} ({tgt['type']})",
        f"관계: {src['name']} --[{seed.get('relation_type') or '관련'}]--> {tgt['name']}",
    ]
    return "\n".join(lines)


async def _process_cross_doc_item(
    idx: int,
    seed: dict[str, Any],
    *,
    distractor_snippets: list[str],
    skip_generic_gate: bool,
    generator: LLMClient,
    judge: LLMClient,
    questions_per_chunk: int,
    reasoning_mode: str | None,
    apply_filter: bool,
    sem: asyncio.Semaphore,
    total: int,
    generator_temperature: float = 0.0,
    generator_seed_base: int | None = None,
    extra_korean_stopwords: frozenset[str] | None = None,
) -> tuple[list[GoldItem], dict[str, int]]:
    """cross-doc 씨앗 1개 처리 — 질문 생성·게이트.

    id 는 호출자가 후처리에서 부여 — placeholder ``id=""``.
    """
    async with sem:
        logger.info(
            "[cross-doc start %d/%d] %s -> %s (%s)",
            idx, total,
            seed["source_entity"]["name"], seed["target_entity"]["name"],
            seed["relation_type"],
        )
        local_items: list[GoldItem] = []
        local_stats: dict[str, int] = {}

        sg_seed = (
            generator_seed_base + idx if generator_seed_base is not None else None
        )
        generated = await generate_cross_doc_questions(
            seed,
            n=questions_per_chunk,
            generator=generator,
            reasoning_mode=reasoning_mode,
            temperature=generator_temperature,
            seed=sg_seed,
        )
        local_stats["cross_doc_generated"] = (
            local_stats.get("cross_doc_generated", 0) + len(generated)
        )
        local_stats["generated"] = local_stats.get("generated", 0) + len(generated)

        if not generated:
            logger.warning("  → cross-doc 생성 실패 (빈 응답)")
            local_stats["fail_parse"] = local_stats.get("fail_parse", 0) + 1
            logger.info("[cross-doc done %d/%d]", idx, total)
            return local_items, local_stats

        snippet = _cross_doc_seed_snippet(seed)
        gate_distractors = [] if skip_generic_gate else distractor_snippets

        for j, gq in enumerate(generated):
            if not apply_filter:
                local_items.append(_make_cross_doc_gold_item(seed, gq))
                local_stats["passed"] = local_stats.get("passed", 0) + 1
                local_stats["cross_doc_passed"] = (
                    local_stats.get("cross_doc_passed", 0) + 1
                )
                continue

            judge_seed = (sg_seed + 10000 + j) if sg_seed is not None else None
            report = await filter_question(
                gq.query,
                snippet,
                gate_distractors,
                judge=judge,
                reasoning_mode=reasoning_mode,
                seed=judge_seed,
                extra_korean_stopwords=extra_korean_stopwords,
            )
            if not report.passed:
                key = f"fail_{report.reason}" if report.reason else "fail_parse"
                local_stats[key] = local_stats.get(key, 0) + 1
                logger.info(
                    "  cross-doc q%d 탈락 — reason=%s, query=%s",
                    j + 1, report.reason, gq.query[:80],
                )
                continue

            local_items.append(_make_cross_doc_gold_item(seed, gq))
            local_stats["passed"] = local_stats.get("passed", 0) + 1
            local_stats["cross_doc_passed"] = (
                local_stats.get("cross_doc_passed", 0) + 1
            )
            logger.info("  cross-doc q%d 통과 — query=%s", j + 1, gq.query[:80])

        logger.info("[cross-doc done %d/%d]", idx, total)
        return local_items, local_stats


async def _run_cross_doc_mode(
    *,
    meta_store: MetadataStore,
    graph_store: GraphStore,
    source_types: list[str] | None,
    max_seeds: int | None,
    questions_per_chunk: int,
    generator: LLMClient,
    judge: LLMClient,
    reasoning_mode: str | None,
    apply_filter: bool,
    items: list[GoldItem],
    stats: dict[str, int],
    sem: asyncio.Semaphore | None = None,
    next_id: int = 1,
    generator_temperature: float = 0.0,
    generator_seed_base: int | None = None,
    extra_korean_stopwords: frozenset[str] | None = None,
) -> int:
    """cross-doc 모드 — 서로 다른 문서를 잇는 엣지 씨앗에서 질문 생성·게이트.

    Returns: 다음에 부여될 ``next_id`` (chunk + graph + cross-doc 연속 공간).
    """
    seeds = await load_cross_doc_seeds(
        meta_store,
        graph_store,
        source_types=source_types,
        max_seeds=max_seeds,
    )
    if not seeds:
        logger.warning(
            "cross-doc 씨앗이 0개 — 서로 다른 문서를 잇는 그래프 엣지가 없거나 "
            "source_type 필터에 매칭되지 않습니다. cross-doc 모드를 건너뜁니다.",
        )
        return next_id

    # 일반성 게이트용 distractor 풀: 씨앗 자체 snippet 들 (자기 자신 제외).
    all_snippets = [_cross_doc_seed_snippet(s) for s in seeds]
    skip_generic_gate = len(seeds) < 5
    if skip_generic_gate:
        logger.warning(
            "cross-doc 씨앗 풀이 5개 미만 — 일반성 게이트 신뢰도가 낮습니다.",
        )

    if sem is None:
        sem = asyncio.Semaphore(1)

    total = len(seeds)
    tasks = [
        _process_cross_doc_item(
            idx, seed,
            distractor_snippets=[
                s for k, s in enumerate(all_snippets) if k != idx - 1
            ][:5],
            skip_generic_gate=skip_generic_gate,
            generator=generator,
            judge=judge,
            questions_per_chunk=questions_per_chunk,
            reasoning_mode=reasoning_mode,
            apply_filter=apply_filter,
            sem=sem,
            total=total,
            generator_temperature=generator_temperature,
            generator_seed_base=generator_seed_base,
            extra_korean_stopwords=extra_korean_stopwords,
        )
        for idx, seed in enumerate(seeds, start=1)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for idx, r in enumerate(results, start=1):
        if isinstance(r, BaseException):
            logger.exception(
                "[cross-doc fail %d/%d] %s", idx, total, r,
                exc_info=r if isinstance(r, Exception) else None,
            )
            stats["fail_runtime"] = stats.get("fail_runtime", 0) + 1
            continue
        local_items, local_stats = r
        for item in local_items:
            item.id = f"q{next_id:04d}"
            items.append(item)
            next_id += 1
        _merge_stats(stats, local_stats)

    return next_id


def _build_graph_metadata(
    *,
    items: list[GoldItem],
    stats: dict[str, int],
    embedding_model_id: str,
    graph_match_threshold: float,
    score_relations: bool,
    embed_graph_evidence: bool,
) -> dict[str, Any]:
    """graph 모드 빌드 metadata 섹션을 구성한다 (S1-2/S1-3/S1-4).

    순수 함수 — ``build()`` 가 호출하며, 테스트가 직접 검증할 수 있게 분리했다.

    포함 키:
        - 2차 재현성: ``embedding_model`` / ``graph_match_threshold_default`` /
          ``score_relations`` / ``embed_graph_evidence``.
        - S1-2 (R5, Channel D — 누설 필터 카운트): ``alias_leakage_filtered`` /
          ``evidence_leakage_filtered``. ``_process_subgraph_item`` 이 stats 에
          누적한 값을 metadata 그래프 섹션으로 끌어올려 감사 가능하게 한다.
        - S1-3 (R6 — 소표본 경고): ``graph_question_count`` /
          ``graph_low_sample_warning`` / ``graph_low_sample_threshold``.
          순수 그래프(단일 엔티티) 항목만 세고 cross-doc(doc-level AND 채점)은
          제외한다.
        - S1-4 (R4 감사추적): 골드 evidence 임베딩 모델 ID 를
          ``graph_evidence_embedding_model`` 로 기록. 인덱싱(그래프 추출) LLM
          은 골드 빌더가 알 수 없으므로 ``extraction_llm_provenance: unrecorded``
          로 정직하게 미기록임을 명시한다.
    """
    graph_question_count = sum(
        1 for it in items
        if it.relevant_graph_entities and not it.cross_document
    )
    graph_low_sample_warning = graph_question_count < GRAPH_LOW_SAMPLE_THRESHOLD
    return {
        "embedding_model": embedding_model_id or "",
        "graph_match_threshold_default": graph_match_threshold,
        "score_relations": score_relations,
        "embed_graph_evidence": embed_graph_evidence,
        "alias_leakage_filtered": stats.get("alias_leakage_filtered", 0),
        "evidence_leakage_filtered": stats.get("evidence_leakage_filtered", 0),
        "graph_question_count": graph_question_count,
        "graph_low_sample_warning": graph_low_sample_warning,
        "graph_low_sample_threshold": GRAPH_LOW_SAMPLE_THRESHOLD,
        "graph_evidence_embedding_model": embedding_model_id or "",
        "extraction_llm_provenance": "unrecorded",
    }


def _make_graph_gold_item(
    sg: dict[str, Any],
    gq: GeneratedGraphQuestion,
    *,
    score_relations: bool = False,
    source_text: str | None = None,
    extra_korean_stopwords: frozenset[str] | None = None,
    leak_stats: dict[str, int] | None = None,
) -> GoldItem:
    """그래프 질문을 GoldItem 으로 직렬화한다.

    ``relevant_graph_entities`` 에는 핵심 노드 1개만 기록한다 (W-3 — 이웃
    포함 시 채점 후해짐). 2차 확장으로 generator 가 채워준
    ``evidence_description`` / ``entity_aliases`` 를 함께 저장하고,
    ``score_relations`` 가 True 면 ``relation`` 도 ``GraphRelationRef`` 로
    emit 한다. ``description_embedding`` 은 호출자가 배치 임베딩 후 채운다.

    3차 변경: ``id`` 는 placeholder ``""`` 로 두고, ``_run_chunk_mode`` /
    ``_run_graph_mode`` 가 gather 완료 후 idx 순으로 부여한다 (D-3).

    감사 보강: LLM 이 ``evidence_description`` 을 비우면 description 도 빈
    문자열로 둔다. graph_store 의 원본 ``entity_description`` 으로 폴백하지
    않는다 — 인덱싱 시점의 description 을 그대로 정답으로 사용하면 T4
    임베딩 cosine 이 trivially 1.0 으로 부풀려져 그래프 시스템의 표기 변형·
    패러프레이즈 강건성 측정이 무력화된다. description 이 비면 임베딩 단계
    (``_embed_graph_item_descriptions``) 가 자연 skip 한다.

    S1-2 (R5, Channel D — alias/evidence 누설 게이트): generator 가 작성한
    ``entity_aliases`` / ``evidence_description`` 이 출처 청크(``source_text``)
    의 고유 식별자·한국어 고유명사를 그대로 베껴 T2 alias / T4 임베딩 매칭을
    자명 통과시키는 것을 막는다. ``source_text`` 가 주어지면 누설 alias 는
    드롭하고, 누설 evidence 는 비워 T4 를 skip 한다. 정답 엔티티 이름의 표기
    변형(케이스/공백/하이픈/언더스코어)인 alias 는 정상으로 통과시켜 과교정을
    피한다. 드롭/필터 수는 ``leak_stats`` 에 누적해 빌드 metadata 의
    ``alias_leakage_filtered`` / ``evidence_leakage_filtered`` 로 노출한다.
    """
    aliases = list(gq.entity_aliases)
    # LLM 이 자연어로 풀어쓴 evidence 만 사용. 빈 문자열이면 T4 skip (감사 H6).
    description = gq.evidence_description

    if source_text is not None:
        aliases, dropped = sanitize_graph_aliases(
            aliases,
            sg["entity_name"],
            source_text,
            extra_korean_stopwords=extra_korean_stopwords,
        )
        description, evid_leaked = sanitize_graph_evidence(
            description,
            source_text,
            extra_korean_stopwords=extra_korean_stopwords,
        )
        if leak_stats is not None:
            if dropped:
                leak_stats["alias_leakage_filtered"] = (
                    leak_stats.get("alias_leakage_filtered", 0) + dropped
                )
            if evid_leaked:
                leak_stats["evidence_leakage_filtered"] = (
                    leak_stats.get("evidence_leakage_filtered", 0) + 1
                )

    entity_ref = GraphEntityRef(
        name=sg["entity_name"],
        type=sg["entity_type"],
        aliases=aliases,
        description=description,
        description_embedding=None,
    )

    relations: list[GraphRelationRef] = []
    if score_relations and gq.relation is not None:
        relations.append(GraphRelationRef(
            source_name=gq.relation.source_name,
            target_name=gq.relation.target_name,
            relation_type=gq.relation.relation_type,
            description=gq.relation.description,
            description_embedding=None,
        ))

    # 같은 엔티티가 N개 문서에 등장 = "그 중 어디서든 찾으면 OK" = 단일 OR 그룹
    # (R3). doc 가 1개뿐이면 그룹 불필요 (평탄 채점과 동일) → 빈 그룹으로 둠.
    doc_ids = sorted(set(int(d) for d in sg["document_ids"]))
    return GoldItem(
        id="",
        query=gq.query,
        relevant_doc_ids=doc_ids,  # 평탄 — 하위호환/CSV용
        relevant_doc_groups=[doc_ids] if len(doc_ids) > 1 else [],
        relevant_graph_entities=[entity_ref],
        relevant_graph_relations=relations,
        source_type=sg["source_type"],
        source_document_id=sg["primary_document_id"],
        source_text_anchor=None,
        difficulty=gq.difficulty,
        synthesized=True,
    )


def _make_cross_doc_gold_item(
    seed: dict[str, Any],
    gq: GeneratedGraphQuestion,
) -> GoldItem:
    """cross-doc 씨앗 + 생성 질문을 GoldItem 으로 직렬화한다 (R2).

    두 문서를 '모두' 봐야 답 가능 → AND 다중그룹 ``[[src_doc], [tgt_doc]]``.
    각 그룹은 단일 doc 라 OR 은 자명. ``cross_document=True`` 로 식별한다.
    id 는 호출자가 후처리에서 부여 — placeholder ``id=""``.
    """
    src = seed["source_entity"]
    tgt = seed["target_entity"]
    src_doc = int(src["doc_id"])
    tgt_doc = int(tgt["doc_id"])
    return GoldItem(
        id="",
        query=gq.query,
        relevant_doc_ids=sorted({src_doc, tgt_doc}),  # 평탄(하위호환)
        relevant_doc_groups=[[src_doc], [tgt_doc]],
        cross_document=True,
        relevant_graph_entities=[
            GraphEntityRef(
                name=src["name"], type=src["type"],
                aliases=list(gq.entity_aliases),
            ),
            GraphEntityRef(name=tgt["name"], type=tgt["type"]),
        ],
        source_type=seed["source_type"],
        source_document_id=src_doc,
        difficulty=gq.difficulty,
        synthesized=True,
        notes="cross_document",
    )


async def _embed_graph_item_descriptions(
    items: list[GoldItem],
    embedding_client: Any | None,
) -> None:
    """모든 graph 골드 항목의 description 임베딩을 한 번에 계산해 채운다.

    엔티티 description 과 (있다면) 관계 description 을 모두 모아 배치 임베딩
    호출 1회로 처리한다. ``embedding_client`` 가 ``None`` 이면 silent skip.
    """
    if embedding_client is None:
        return

    texts: list[str] = []
    targets: list[tuple[int, str, int]] = []  # (item_idx, "entity"|"relation", inner_idx)
    for idx, item in enumerate(items):
        for ei, entity in enumerate(item.relevant_graph_entities):
            if entity.description and entity.description_embedding is None:
                texts.append(entity.description)
                targets.append((idx, "entity", ei))
        for ri, rel in enumerate(item.relevant_graph_relations):
            if rel.description and rel.description_embedding is None:
                texts.append(rel.description)
                targets.append((idx, "relation", ri))

    if not texts:
        return

    embeddings = await aembed_with_client(embedding_client, texts)
    for (idx, kind, inner_idx), emb in zip(targets, embeddings):
        if emb is None:
            continue
        if kind == "entity":
            items[idx].relevant_graph_entities[inner_idx].description_embedding = list(emb)
        else:
            items[idx].relevant_graph_relations[inner_idx].description_embedding = list(emb)


def _numbered_output_path(base: Path, index: int, total: int) -> Path:
    """N>1 일 때 base 경로에 ``_NNN`` 접미사를 추가한다.

    ``eval/gold_sets/git_code.yaml`` + index=2 → ``eval/gold_sets/git_code_002.yaml``.
    N=1 이면 base 그대로 반환 (기존 단일-파일 동작 유지).
    width 는 total 자릿수와 최소 3 의 max — N=5 든 N=500 든 사전적 정렬이 안정적.
    """
    if total <= 1:
        return base
    width = max(3, len(str(total)))
    suffix = f"_{index:0{width}d}"
    return base.with_name(f"{base.stem}{suffix}{base.suffix}")


def _unfiltered_output_path(base: Path) -> Path:
    """``--no-filter`` 빌드 경로에 ``.UNFILTERED`` 접미사를 강제 부여한다.

    ``eval/gold_set.yaml`` → ``eval/gold_set.UNFILTERED.yaml``.
    이미 ``.UNFILTERED`` 가 포함된 stem 이면 그대로 반환 (이중 접미사 방지).
    ``_numbered_output_path`` 의 ``{stem}_NNN{suffix}`` 변환과 호환되며,
    UNFILTERED 가 stem 마지막에 위치하므로 인덱스 접미사가 그 뒤에 오는
    구조가 된다: ``gold_set.UNFILTERED_001.yaml``.

    디버그/탐색 빌드를 운영 골드셋과 시각적으로 분리하기 위한 안전장치.
    평가 스크립트는 파일명 또는 ``metadata.filter_applied`` 로 차단 가능.
    """
    stem = base.stem
    if stem.endswith(".UNFILTERED"):
        return base
    return base.with_name(f"{stem}.UNFILTERED{base.suffix}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _resolve_eval_role_identity(
    config: Config,
    role: str,
    *,
    endpoint_override: str,
    model_override: str,
) -> tuple[str, str]:
    """``(effective_model, effective_endpoint)`` 를 우선순위대로 해석한다.

    우선순위: CLI override > ``config.eval.{role}.{key}`` > ``config.llm.{key}``.
    ``build_eval_llm_client`` 가 따르는 동일 폴백 체인을 메타데이터 기록용으로
    재현한 헬퍼다. 어느 단계의 값도 비어 있을 수 있으므로 빈 문자열 가능.
    """
    role_path = f"eval.{role}"
    effective_model = (
        model_override
        or str(config.get(f"{role_path}.model") or "")
        or str(config.get("llm.model") or "")
    )
    effective_endpoint = (
        endpoint_override
        or str(config.get(f"{role_path}.endpoint") or "")
        or str(config.get("llm.endpoint") or "")
    )
    return effective_model, effective_endpoint


def _parse_optional_bool(v: str | None) -> bool:
    """``--embed-graph-evidence true/false`` CLI 입력 파서.

    문자열 ``"1" / "true" / "yes" / "on"`` 은 True, 그 외는 False.
    None 이면 기본값을 그대로 받으므로 호출자가 처리.
    """
    if v is None or isinstance(v, bool):
        return bool(v) if isinstance(v, bool) else True
    return v.lower() in ("1", "true", "yes", "on")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="검색 평가용 합성 골드셋 생성 (LLM 기반)",
    )
    parser.add_argument(
        "--config", "-c", default="",
        help="사용자 config 파일 경로 (미지정 시 ~/.context-loop/config.yaml)",
    )
    parser.add_argument(
        "--output", "-o", default="eval/gold_set.yaml",
        help="저장 경로 (기본: eval/gold_set.yaml)",
    )
    parser.add_argument(
        "--n-chunks", type=int, default=30,
        help="샘플링할 문서 수 (기본 30). 이름은 하위호환 위해 유지 — "
             "문서 기반 전환 후 의미는 '문서 수'.",
    )
    parser.add_argument(
        "--questions-per-chunk", type=int, default=2,
        help="청크당 생성 질문 수 (기본 2)",
    )
    parser.add_argument(
        "--source-types", default="",
        help="쉼표로 구분된 source_type 화이트리스트 (예: 'git_code,confluence_mcp'). 빈 값이면 전체.",
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="랜덤 시드 (재현성). 미지정 시 결정론적 정렬 순서로 샘플링. "
             "--n-gold-sets>1 일 때 i번째 골드셋은 seed+i-1 시드를 사용.",
    )
    parser.add_argument(
        "--n-gold-sets", type=int, default=1,
        help="생성할 골드셋 수 (기본 1). N>1 일 때 동일 파라미터로 시드만 바꿔 "
             "여러 골드셋을 빌드 — 평가 변동성/안정성 측정용. 출력 경로에는 "
             "'_NNN' 접미사가 자동 추가된다.",
    )
    parser.add_argument(
        "--no-filter", action="store_true",
        help="품질 게이트 비활성화 (디버그/탐색용 — 운영 골드셋에는 사용 금지). "
             "출력 경로에는 ``.UNFILTERED`` 접미사가 강제 추가된다.",
    )
    parser.add_argument(
        "--allow-self-eval", action="store_true",
        help="Generator/Judge 가 모두 system LLM (``llm.*``) 으로 fall-through "
             "되는 빌드를 허용한다. 명시 없으면 자기 평가 편향을 막기 위해 "
             "실행이 차단된다. 골드셋 metadata 에 ``allow_self_eval=True`` 가 "
             "기록되어 사후 추적된다.",
    )
    parser.add_argument(
        "--n-distractors", type=int, default=2,
        help="일반성 게이트의 무관 문서 수 (기본 2)",
    )
    parser.add_argument(
        "--min-chars", type=int, default=200,
        help="최소 문서 길이 (그 미만은 후보 제외, 기본 200자)",
    )
    parser.add_argument(
        "--max-chars", type=int, default=200000,
        help="최대 문서 길이 1차 필터 (그 초과는 후보 제외, 기본 200000자). "
             "통째 정책 — 큰 문서를 버리지 않고 토큰 가드로 truncate.",
    )
    parser.add_argument(
        "--max-doc-tokens", type=int, default=24000,
        help="generator 입력 문서 토큰 한도. 초과분은 앞부분 truncate. "
             "0=무제한 (기본 24000).",
    )
    parser.add_argument(
        "--reasoning-mode", default="off",
        help="LLM reasoning_mode 프로파일 (config.llm.reasoning_profiles 키, 기본 'off')",
    )
    # 그래프 기반 질문 생성 — R1 (chunk + graph context 평가).
    parser.add_argument(
        "--include-graph-questions", action="store_true",
        help="그래프 subgraph 기반 질문도 함께 생성한다 (R1). 기본 False — "
             "기존 chunk-only 동작 보존.",
    )
    parser.add_argument(
        "--n-graph-nodes", type=int, default=0,
        help="샘플링할 graph subgraph 후보 수. 0 이면 --n-chunks 와 동일 (W-8). "
             "--include-graph-questions 가 꺼져 있으면 무시. "
             "그래프 메트릭은 단일 엔티티 0/1 채점이라 소표본에 민감 — "
             "A/B 판정력 확보를 위해 그래프 골드 항목 N≥150 을 권장 (S1-3). "
             "임계 미만이면 빌드 종료 시 소표본 경고가 출력된다.",
    )
    parser.add_argument(
        "--min-graph-neighbors", type=int, default=1,
        help="graph 후보의 1-hop 이웃 최소 수 (W-2, 기본 1).",
    )
    # cross-document 질문 생성 — R2 (그래프 성능 측정용).
    parser.add_argument(
        "--enable-cross-doc", action="store_true",
        help="서로 다른 문서를 잇는 그래프 엣지에서 cross-document 질문을 "
             "생성한다 (R2). 결정론 씨앗 추출 + LLM 문장화. 기본 False.",
    )
    parser.add_argument(
        "--cross-doc-max-seeds", type=int, default=None,
        help="cross-doc 씨앗 상한 (None=무제한). 결정론적 정렬 후 head 절단. "
             "--enable-cross-doc 가 꺼져 있으면 무시.",
    )
    # 2차 — 그래프 인덱싱 강건성 (R1/R2/R3).
    parser.add_argument(
        "--embed-graph-evidence", type=lambda v: _parse_optional_bool(v),
        default=True,
        help="graph 골드 항목의 description 임베딩을 생성 시 1회 계산해 박을지 "
             "여부 (기본 True). False 면 평가 시 lazy 계산.",
    )
    parser.add_argument(
        "--score-relations", action="store_true",
        help="generator 가 채운 관계 evidence 를 GraphRelationRef 로 골드셋에 "
             "emit (관계 채점용). 기본 False — chunk/entity 만 평가.",
    )
    parser.add_argument(
        "--graph-match-threshold", type=float,
        default=DEFAULT_GRAPH_MATCH_THRESHOLD,
        help=f"평가 시 사용될 tiered matching τ 의 기본값. 골드셋 metadata 에 "
             f"기록되어 재현성을 보장한다 (기본 {DEFAULT_GRAPH_MATCH_THRESHOLD}).",
    )
    # Phase 3.5 — OR-동치 자동 검출. 질문이 다른 문서로도 정당하게 답되는
    # 경우(경우 B)를 폐기하지 않고 relevant_doc_groups OR 그룹으로 기록해 recall
    # 과소평가를 막는다. 코퍼스 전역 벡터 검색 + answer-containment 검증.
    parser.add_argument(
        "--equivalence-detection", action="store_true",
        help="OR-동치 자동 검출 활성화 (Phase 3.5). 통과/비유일 질문에 대해 "
             "코퍼스 전역에서 같은 답을 담은 동등 문서를 찾아 relevant_doc_groups "
             "에 OR 그룹으로 기록한다. recall 과소평가(가짜 false negative) 해소. "
             "--no-filter 와 함께면 무시(게이트 없음). 빌드 비용 증가 — "
             "--concurrency 로 상쇄.",
    )
    parser.add_argument(
        "--equivalence-top-m", type=int, default=3,
        help="동치 검출 시 answer-containment 검증할 최대 후보 문서 수 (기본 3).",
    )
    parser.add_argument(
        "--equivalence-min-similarity", type=float, default=0.6,
        help="동치 후보의 최소 cosine 유사도 하한 (기본 0.6). 무관·일반 문서 "
             "과탐 방지.",
    )
    # Generator/Judge 는 운영 디폴트를 config.eval.{generator,judge}.* 에 둔다.
    # 아래 CLI 인자는 일회성 실험용 override — 미지정 시 config 값 사용,
    # config 도 비어 있으면 상위 llm.* 로 폴백한다.
    parser.add_argument("--generator-endpoint", default="")
    parser.add_argument("--generator-model", default="")
    parser.add_argument("--generator-api-key", default="")
    parser.add_argument(
        "--generator-headers", default="",
        help="Generator 헤더 JSON (예: '{\"X-Org-Id\":\"abc\"}'). "
             "미지정 시 config.eval.generator.headers, 그것도 비면 llm.headers.",
    )
    parser.add_argument("--judge-endpoint", default="")
    parser.add_argument("--judge-model", default="")
    parser.add_argument("--judge-api-key", default="")
    parser.add_argument(
        "--judge-headers", default="",
        help="Judge 헤더 JSON. 미지정 시 config.eval.judge.headers, "
             "그것도 비면 llm.headers.",
    )

    # 3차 — 항목 단위 병렬 처리 (R1).
    parser.add_argument(
        "--concurrency", type=int, default=1,
        help="항목(chunk/subgraph) 단위 동시 처리 수 (기본 1, 직렬). "
             "LLM endpoint rate limit 에 맞춰 4~8 권장. metadata 에 기록.",
    )
    # S2 P13 — 재현성: Generator temperature 와 seed.
    parser.add_argument(
        "--generator-temperature", type=float, default=0.0,
        help="Generator LLM 의 sampling 온도 (기본 0.0 = 결정적). "
             "다양성 확대 시 0.3~0.7 권장 — 단 재현성 트레이드오프. "
             "metadata 에 기록되어 사후 재현 시 동일 값 사용 필수.",
    )
    parser.add_argument(
        "--generator-seed-base", type=int, default=None,
        help="Generator LLM 호출의 seed base. None 이면 seed 미전달 "
             "(endpoint 가 결정성 보장 안 함). 정수 명시 시 청크별 "
             "deterministic seed = seed_base + chunk_index 로 부여. "
             "OpenAI 호환 endpoint (vLLM/gpt-4 이상) 만 실제 효과. "
             "Anthropic 은 무시. metadata 에 기록.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    if args.concurrency > 32:
        logger.warning(
            "--concurrency=%d 는 endpoint rate limit 초과 위험. 4~8 권장.",
            args.concurrency,
        )

    config = Config(config_path=Path(args.config) if args.config else None)

    generator = build_eval_llm_client(
        config, "generator",
        endpoint_override=args.generator_endpoint,
        model_override=args.generator_model,
        api_key_override=args.generator_api_key,
        headers_override_json=args.generator_headers,
    )
    judge = build_eval_llm_client(
        config, "judge",
        endpoint_override=args.judge_endpoint,
        model_override=args.judge_model,
        api_key_override=args.judge_api_key,
        headers_override_json=args.judge_headers,
    )

    gen_configured = role_is_configured(
        config, "generator",
        endpoint_override=args.generator_endpoint,
        model_override=args.generator_model,
    )
    judge_configured = role_is_configured(
        config, "judge",
        endpoint_override=args.judge_endpoint,
        model_override=args.judge_model,
    )
    self_evaluation_warning = not (gen_configured or judge_configured)
    if self_evaluation_warning and not args.allow_self_eval:
        parser.error(
            "Generator/Judge 모두 system LLM (llm.*) 과 동일 — 자기 평가 편향이 "
            "차단되었습니다. config.yaml 의 eval.generator / eval.judge 에 별도 "
            "모델을 지정하거나 --generator-* / --judge-* 인자를 사용하세요. "
            "실험/디버그 용도로 의도적으로 진행하려면 --allow-self-eval 을 명시.",
        )
    if self_evaluation_warning:
        logger.warning(
            "Generator/Judge 모두 system LLM (llm.*) 과 동일 — 자기 평가 편향 가능. "
            "--allow-self-eval 이 명시되어 진행합니다. metadata 에 "
            "self_evaluation_warning=True 가 기록됩니다.",
        )

    effective_generator_model, effective_generator_endpoint = (
        _resolve_eval_role_identity(
            config, "generator",
            endpoint_override=args.generator_endpoint,
            model_override=args.generator_model,
        )
    )
    effective_judge_model, effective_judge_endpoint = (
        _resolve_eval_role_identity(
            config, "judge",
            endpoint_override=args.judge_endpoint,
            model_override=args.judge_model,
        )
    )

    source_types = [s.strip() for s in args.source_types.split(",") if s.strip()] or None

    if args.n_gold_sets < 1:
        parser.error("--n-gold-sets 는 1 이상이어야 합니다.")

    # W-8: --n-graph-nodes 기본값을 --n-chunks 와 동일하게 (직관적).
    effective_n_graph_nodes = args.n_graph_nodes or args.n_chunks

    base_output = Path(args.output)
    if args.no_filter:
        original_output = base_output
        base_output = _unfiltered_output_path(base_output)
        if base_output != original_output:
            logger.warning(
                "--no-filter 빌드 — 출력 경로를 %s 에서 %s 로 변환했습니다. "
                "운영 골드셋과 시각적으로 분리됩니다 (감사 H7).",
                original_output, base_output,
            )
    base_seed = args.seed

    # 2차 — graph evidence 임베딩 계산용 클라이언트.
    # Phase 3.5 — OR-동치 검출(--equivalence-detection)도 코퍼스 전역 벡터 검색에
    # 임베딩 클라이언트가 필요하므로, 그래프 모드가 꺼져 있어도 빌드한다.
    embedding_client: Any | None = None
    embedding_model_id = ""
    needs_embedding = (
        (args.include_graph_questions and args.embed_graph_evidence)
        or (args.equivalence_detection and not args.no_filter)
    )
    if needs_embedding:
        try:
            from context_loop.web.app import _build_embedding_client  # noqa: PLC0415
            embedding_client = _build_embedding_client(config)
            embedding_model_id = str(config.get("processor.embedding_model") or "")
        except Exception:
            logger.warning(
                "embedding 클라이언트 빌드 실패 — graph evidence 임베딩 / OR-동치 "
                "검출이 비활성화됩니다.",
                exc_info=True,
            )

    async def _run_all() -> None:
        for i in range(1, args.n_gold_sets + 1):
            seed_i = (base_seed + i - 1) if base_seed is not None else None
            out_i = _numbered_output_path(base_output, i, args.n_gold_sets)
            if args.n_gold_sets > 1:
                logger.info(
                    "=== 골드셋 %d/%d — seed=%s, output=%s ===",
                    i, args.n_gold_sets, seed_i, out_i,
                )
            await build(
                config=config,
                n_chunks=args.n_chunks,
                questions_per_chunk=args.questions_per_chunk,
                output_path=out_i,
                source_types=source_types,
                seed=seed_i,
                apply_filter=not args.no_filter,
                n_distractors=args.n_distractors,
                generator=generator,
                judge=judge,
                reasoning_mode=args.reasoning_mode,
                min_chars=args.min_chars,
                max_chars=args.max_chars,
                max_doc_tokens=args.max_doc_tokens,
                enable_graph_mode=args.include_graph_questions,
                enable_cross_doc=bool(args.enable_cross_doc),
                cross_doc_max_seeds=args.cross_doc_max_seeds,
                n_graph_nodes=effective_n_graph_nodes,
                min_graph_neighbors=args.min_graph_neighbors,
                embed_graph_evidence=bool(args.embed_graph_evidence),
                score_relations=bool(args.score_relations),
                graph_match_threshold=float(args.graph_match_threshold),
                embedding_client=embedding_client,
                embedding_model_id=embedding_model_id,
                concurrency=int(args.concurrency),
                generator_model=effective_generator_model,
                generator_endpoint=effective_generator_endpoint,
                judge_model=effective_judge_model,
                judge_endpoint=effective_judge_endpoint,
                generator_configured_separately=bool(gen_configured),
                judge_configured_separately=bool(judge_configured),
                self_evaluation_warning=self_evaluation_warning,
                allow_self_eval=bool(args.allow_self_eval),
                generator_temperature=float(args.generator_temperature),
                generator_seed_base=args.generator_seed_base,
                equivalence_enabled=bool(args.equivalence_detection),
                equivalence_top_m=int(args.equivalence_top_m),
                equivalence_min_similarity=float(args.equivalence_min_similarity),
            )

    asyncio.run(_run_all())


if __name__ == "__main__":
    main()
