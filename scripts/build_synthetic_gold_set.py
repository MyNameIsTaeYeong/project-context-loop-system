#!/usr/bin/env python3
"""LLM 으로 검색 평가용 골드셋을 자동 생성한다.

원리:
1. 인덱싱된 청크 / 그래프 서브그래프에서 계층 샘플링 (source_type 별 균등)
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
        --source-types git_code,confluence \\
        --seed 42 \\
        --output eval/gold_set.yaml

그래프 기반 질문 포함 (R1 — chunk + graph 평가)::

    python scripts/build_synthetic_gold_set.py \\
        --source-types confluence,git_code \\
        --include-graph-questions \\
        --n-graph-nodes 20 \\
        --output eval/gold_set.yaml

그래프 인덱싱 강건성 옵션 (2차 — evidence + alias + 관계 채점)::

    python scripts/build_synthetic_gold_set.py \\
        --include-graph-questions --embed-graph-evidence true \\
        --score-relations --graph-match-threshold 0.78 \\
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
    build_subgraph_snippet,
    filter_question,
    generate_graph_questions,
    generate_questions,
    make_text_anchor,
    stratified_sample,
)
from context_loop.storage.graph_store import GraphStore  # noqa: E402
from context_loop.storage.metadata_store import MetadataStore  # noqa: E402

logger = logging.getLogger("build_synthetic_gold_set")


# ---------------------------------------------------------------------------
# Chunk loading
# ---------------------------------------------------------------------------


async def load_candidate_chunks(
    store: MetadataStore,
    *,
    source_types: list[str] | None,
    min_chars: int,
    max_chars: int,
) -> list[dict[str, Any]]:
    """metadata_store 에서 청크 후보를 로드한다.

    각 항목 dict 형태::

        {
            "chunk_id": str,            # debug only — 채점 키 아님
            "chunk_index": int,         # 결정론적 정렬 키 (D-7)
            "document_id": int,
            "source_type": str,
            "content": str,             # 청크 본문 (Generator 입력)
            "section_path": str,
            "title": str,
        }

    너무 짧은(최소 chars 미만) 청크는 의미 추출이 어려워 제외한다.
    너무 긴 청크는 토큰 예산 폭주 방지로 제외 (기본 8000자 → 약 2000~3000 토큰).
    """
    documents = await store.list_documents()
    by_id = {d["id"]: d for d in documents}

    out: list[dict[str, Any]] = []
    for doc in documents:
        if source_types and doc.get("source_type") not in source_types:
            continue
        chunks = await store.get_chunks_by_document(doc["id"])
        for c in chunks:
            content: str = c.get("content") or ""
            if len(content) < min_chars or len(content) > max_chars:
                continue
            out.append({
                "chunk_id": c["id"],
                "chunk_index": int(c.get("chunk_index") or 0),
                "document_id": doc["id"],
                "source_type": doc.get("source_type", ""),
                "content": content,
                "section_path": c.get("section_path") or "",
                "title": doc.get("title") or "",
            })
    # 결정론적 순서 보장 — chunk_id (uuid) 대신 chunk_index 사용 (D-7).
    out.sort(key=lambda x: (x["document_id"], x["chunk_index"]))
    logger.info(
        "후보 청크 로드 완료 — total=%d, doc_count=%d",
        len(out), len(by_id),
    )
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
    enable_graph_mode: bool = False,
    n_graph_nodes: int = 0,
    min_graph_neighbors: int = 1,
    embed_graph_evidence: bool = True,
    score_relations: bool = False,
    graph_match_threshold: float = DEFAULT_GRAPH_MATCH_THRESHOLD,
    embedding_client: Any | None = None,
    embedding_model_id: str = "",
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
    """

    rng = random.Random(seed)

    store = MetadataStore(config.data_dir / "metadata.db")
    await store.initialize()

    graph_store: GraphStore | None = None
    if enable_graph_mode:
        graph_store = GraphStore(store)
        await graph_store.load_from_db()

    try:
        candidates = await load_candidate_chunks(
            store,
            source_types=source_types,
            min_chars=min_chars,
            max_chars=max_chars,
        )
        if not candidates:
            raise RuntimeError("후보 청크가 없습니다. 인덱싱된 문서가 있는지 확인하세요.")

        sampled = stratified_sample(
            candidates, n_total=n_chunks, key="source_type", rng=rng,
        )
        logger.info(
            "청크 샘플링 완료 — sampled=%d (요청 %d)", len(sampled), n_chunks,
        )

        # 일반성 게이트용 distractor 풀: 샘플과 다른 문서의 청크에서 무작위 추출
        sampled_chunk_ids = {s["chunk_id"] for s in sampled}
        distractor_pool = [
            c for c in candidates if c["chunk_id"] not in sampled_chunk_ids
        ]
        rng.shuffle(distractor_pool)

        items: list[GoldItem] = []
        stats: dict[str, int] = {
            "generated": 0,
            "passed": 0,
            "fail_not_answerable": 0,
            "fail_leakage": 0,
            "fail_generic": 0,
            "fail_parse": 0,
            "graph_generated": 0,
            "graph_passed": 0,
        }

        for i, chunk in enumerate(sampled):
            logger.info(
                "[%d/%d] 질문 생성 — doc=%d, chunk_index=%d, source_type=%s",
                i + 1, len(sampled), chunk["document_id"],
                chunk["chunk_index"], chunk["source_type"],
            )

            generated = await generate_questions(
                chunk["content"],
                n=questions_per_chunk,
                generator=generator,
                reasoning_mode=reasoning_mode,
            )
            stats["generated"] += len(generated)

            if not generated:
                logger.warning("  → 생성 실패 (빈 응답)")
                stats["fail_parse"] += 1
                continue

            # distractor 는 같은 source_type 내에서 우선 골라야 식별자 충돌이 적다
            same_type_distractors = [
                c for c in distractor_pool
                if c["source_type"] == chunk["source_type"]
            ][:n_distractors]
            if len(same_type_distractors) < n_distractors:
                # 부족하면 다른 type 으로 채움
                fill = [c for c in distractor_pool if c not in same_type_distractors]
                same_type_distractors += fill[: n_distractors - len(same_type_distractors)]

            anchor = make_text_anchor(chunk["content"])

            for j, gq in enumerate(generated):
                if not apply_filter:
                    items.append(GoldItem(
                        id=f"q{len(items) + 1:04d}",
                        query=gq.query,
                        relevant_doc_ids=[chunk["document_id"]],
                        source_type=chunk["source_type"],
                        source_document_id=chunk["document_id"],
                        source_text_anchor=anchor,
                        source_section_path=chunk["section_path"],
                        difficulty=gq.difficulty,
                        synthesized=True,
                    ))
                    stats["passed"] += 1
                    continue

                report = await filter_question(
                    gq.query,
                    chunk["content"],
                    [d["content"] for d in same_type_distractors],
                    judge=judge,
                    reasoning_mode=reasoning_mode,
                )
                if not report.passed:
                    key = f"fail_{report.reason}" if report.reason else "fail_parse"
                    stats[key] = stats.get(key, 0) + 1
                    logger.info(
                        "  q%d 탈락 — reason=%s, query=%s",
                        j + 1, report.reason, gq.query[:80],
                    )
                    continue

                items.append(GoldItem(
                    id=f"q{len(items) + 1:04d}",
                    query=gq.query,
                    relevant_doc_ids=[chunk["document_id"]],
                    source_type=chunk["source_type"],
                    source_document_id=chunk["document_id"],
                    source_text_anchor=anchor,
                    source_section_path=chunk["section_path"],
                    difficulty=gq.difficulty,
                    synthesized=True,
                ))
                stats["passed"] += 1
                logger.info(
                    "  q%d 통과 — query=%s", j + 1, gq.query[:80],
                )

        # 그래프 모드 실행 — chunk 모드 이후에 머지
        if enable_graph_mode and graph_store is not None:
            await _run_graph_mode(
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
            )

        generation_modes = ["chunk"]
        if enable_graph_mode:
            generation_modes.append("graph")

        metadata: dict[str, Any] = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "n_chunks_sampled": len(sampled),
            "questions_per_chunk": questions_per_chunk,
            "filter_applied": apply_filter,
            "seed": seed,
            "source_types": source_types or [],
            "generation_modes": generation_modes,
            "stats": stats,
        }
        if enable_graph_mode:
            # 그래프 매칭 재현성을 위해 임베딩 모델 ID + 기본 τ 기록
            # (2차 — 설계 §4.3).
            metadata["embedding_model"] = embedding_model_id or ""
            metadata["graph_match_threshold_default"] = graph_match_threshold
            metadata["score_relations"] = score_relations
            metadata["embed_graph_evidence"] = embed_graph_evidence

        gold = GoldSet(version=1, items=items, metadata=metadata)
        save_gold_set(gold, output_path)
        logger.info(
            "골드셋 저장 — path=%s, items=%d, stats=%s",
            output_path, len(items), stats,
        )
        return gold

    finally:
        await store.close()


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
) -> None:
    """graph 모드 — subgraph 샘플링 후 질문 생성·게이트 적용."""
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
        return

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

    for i, sg in enumerate(sampled_sg):
        logger.info(
            "[graph %d/%d] 질문 생성 — entity=%s (%s), source_type=%s",
            i + 1, len(sampled_sg), sg["entity_name"], sg["entity_type"],
            sg["source_type"],
        )

        generated = await generate_graph_questions(
            sg,
            n=questions_per_chunk,
            generator=generator,
            reasoning_mode=reasoning_mode,
        )
        stats["graph_generated"] += len(generated)
        stats["generated"] += len(generated)

        if not generated:
            logger.warning("  → 그래프 생성 실패 (빈 응답)")
            stats["fail_parse"] += 1
            continue

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
                items.append(_make_graph_gold_item(
                    sg, gq, items, score_relations=score_relations,
                ))
                stats["passed"] += 1
                stats["graph_passed"] += 1
                continue

            report = await filter_question(
                gq.query,
                sg["subgraph_snippet"],
                distractor_snippets,
                judge=judge,
                reasoning_mode=reasoning_mode,
            )
            if not report.passed:
                key = f"fail_{report.reason}" if report.reason else "fail_parse"
                stats[key] = stats.get(key, 0) + 1
                logger.info(
                    "  graph q%d 탈락 — reason=%s, query=%s",
                    j + 1, report.reason, gq.query[:80],
                )
                continue

            items.append(_make_graph_gold_item(
                sg, gq, items, score_relations=score_relations,
            ))
            stats["passed"] += 1
            stats["graph_passed"] += 1
            logger.info(
                "  graph q%d 통과 — query=%s", j + 1, gq.query[:80],
            )

    # 모든 graph 항목 생성 후 description 임베딩을 배치로 계산하여 채운다.
    if embed_evidence:
        await _embed_graph_item_descriptions(items, embedding_client)


def _make_graph_gold_item(
    sg: dict[str, Any],
    gq: GeneratedGraphQuestion,
    existing_items: list[GoldItem],
    *,
    score_relations: bool = False,
) -> GoldItem:
    """그래프 질문을 GoldItem 으로 직렬화한다.

    ``relevant_graph_entities`` 에는 핵심 노드 1개만 기록한다 (W-3 — 이웃
    포함 시 채점 후해짐). 2차 확장으로 generator 가 채워준
    ``evidence_description`` / ``entity_aliases`` 를 함께 저장하고,
    ``score_relations`` 가 True 면 ``relation`` 도 ``GraphRelationRef`` 로
    emit 한다. ``description_embedding`` 은 호출자가 배치 임베딩 후 채운다.
    """
    # 그래프 노드 자체 description 을 fallback evidence 로 사용.
    description = gq.evidence_description or str(sg.get("entity_description") or "")

    entity_ref = GraphEntityRef(
        name=sg["entity_name"],
        type=sg["entity_type"],
        aliases=list(gq.entity_aliases),
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

    return GoldItem(
        id=f"q{len(existing_items) + 1:04d}",
        query=gq.query,
        relevant_doc_ids=list(sg["document_ids"]),
        relevant_graph_entities=[entity_ref],
        relevant_graph_relations=relations,
        source_type=sg["source_type"],
        source_document_id=sg["primary_document_id"],
        source_text_anchor=None,
        difficulty=gq.difficulty,
        synthesized=True,
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


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
        help="샘플링할 청크 수 (기본 30)",
    )
    parser.add_argument(
        "--questions-per-chunk", type=int, default=2,
        help="청크당 생성 질문 수 (기본 2)",
    )
    parser.add_argument(
        "--source-types", default="",
        help="쉼표로 구분된 source_type 화이트리스트 (예: 'git_code,confluence'). 빈 값이면 전체.",
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
        help="품질 게이트 비활성화 (디버그/탐색용 — 운영 골드셋에는 사용 금지).",
    )
    parser.add_argument(
        "--n-distractors", type=int, default=2,
        help="일반성 게이트의 무관 청크 수 (기본 2)",
    )
    parser.add_argument(
        "--min-chars", type=int, default=200,
        help="최소 청크 길이 (그 미만은 후보 제외, 기본 200자)",
    )
    parser.add_argument(
        "--max-chars", type=int, default=8000,
        help="최대 청크 길이 (그 초과는 후보 제외, 기본 8000자)",
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
             "--include-graph-questions 가 꺼져 있으면 무시.",
    )
    parser.add_argument(
        "--min-graph-neighbors", type=int, default=1,
        help="graph 후보의 1-hop 이웃 최소 수 (W-2, 기본 1).",
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

    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose)

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
    if not (gen_configured or judge_configured):
        logger.warning(
            "Generator/Judge 모두 system LLM (llm.*) 과 동일 — 자기 평가 편향 가능. "
            "config.yaml 의 eval.generator / eval.judge 에 별도 모델을 지정하거나 "
            "--generator-* / --judge-* 인자를 사용하세요.",
        )

    source_types = [s.strip() for s in args.source_types.split(",") if s.strip()] or None

    if args.n_gold_sets < 1:
        parser.error("--n-gold-sets 는 1 이상이어야 합니다.")

    # W-8: --n-graph-nodes 기본값을 --n-chunks 와 동일하게 (직관적).
    effective_n_graph_nodes = args.n_graph_nodes or args.n_chunks

    base_output = Path(args.output)
    base_seed = args.seed

    # 2차 — graph evidence 임베딩 계산용 클라이언트.
    embedding_client: Any | None = None
    embedding_model_id = ""
    if args.include_graph_questions and args.embed_graph_evidence:
        try:
            from context_loop.web.app import _build_embedding_client  # noqa: PLC0415
            embedding_client = _build_embedding_client(config)
            embedding_model_id = str(config.get("processor.embedding_model") or "")
        except Exception:
            logger.warning(
                "embedding 클라이언트 빌드 실패 — graph evidence 임베딩이 "
                "골드셋에 박히지 않습니다 (평가 시 lazy 계산됨).",
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
                enable_graph_mode=args.include_graph_questions,
                n_graph_nodes=effective_n_graph_nodes,
                min_graph_neighbors=args.min_graph_neighbors,
                embed_graph_evidence=bool(args.embed_graph_evidence),
                score_relations=bool(args.score_relations),
                graph_match_threshold=float(args.graph_match_threshold),
                embedding_client=embedding_client,
                embedding_model_id=embedding_model_id,
            )

    asyncio.run(_run_all())


if __name__ == "__main__":
    main()
