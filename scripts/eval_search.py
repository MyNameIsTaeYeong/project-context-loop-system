#!/usr/bin/env python3
"""골드셋으로 검색 시스템을 정량 채점한다.

각 질의마다 ``assemble_context_with_sources`` 를 실행하여 top-k 결과를 받고,
정답 문서 ID 와 비교해 Recall@k / Precision@k / MRR / nDCG@k 를 계산한다.

운영 흐름:
1. 베이스라인 실행::

       python scripts/eval_search.py --gold-set eval/gold_set.yaml --label baseline

2. 코드 변경 (예: P0 멀티뷰 임베딩 적용 + 재인덱싱)
3. 변경 후 실행::

       python scripts/eval_search.py --gold-set eval/gold_set.yaml --label multiview

4. ``eval/runs/baseline.summary.json`` ↔ ``multiview.summary.json`` 비교 →
   효과 정량화. 자세한 per-question 결과는 ``*.csv`` 로 저장된다.

변동성 측정 (다중 골드셋):
``--gold-set-glob`` 으로 같은 source_type 의 N개 골드셋을 일괄 채점하면 잡별
요약 외에 ``{label}.aggregate.summary.json`` 으로 메트릭 mean/std/min/max 가
함께 저장된다. mean Δ 가 std 보다 크면 통계적으로 유의미한 개선::

    python scripts/eval_search.py \\
        --gold-set-glob "eval/gold_sets/git_code_*.yaml" \\
        --label baseline

옵션:
- ``--judge`` 활성화 시 별도 LLM 으로 응답 품질을 0~5 점으로 채점.
  Generator/시스템과 다른 family 의 모델 권장 (자기 평가 편향 회피).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import glob
import hashlib
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

# 프로젝트 루트를 sys.path 에 추가
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from context_loop.config import Config  # noqa: E402
from context_loop.eval.gold_set import GoldItem, load_gold_set  # noqa: E402
from context_loop.eval.graph_match import (  # noqa: E402
    DEFAULT_GRAPH_MATCH_THRESHOLD,
    EmbedFn,
    aggregate_tier_counts,
    build_embed_fn,
    run_entity_matching,
    run_relation_matching,
)
from context_loop.eval.llm import build_eval_llm_client, role_is_configured  # noqa: E402
from context_loop.eval.metrics import (  # noqa: E402
    aggregate,
    aggregate_with_variance,
    hit_at_k,
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)
from context_loop.mcp.context_assembler import (  # noqa: E402
    AssembledContext,
    assemble_context_with_sources,
)
from context_loop.processor.llm_client import LLMClient, extract_json  # noqa: E402
from context_loop.storage.graph_store import GraphStore  # noqa: E402
from context_loop.storage.metadata_store import MetadataStore  # noqa: E402
from context_loop.storage.vector_store import VectorStore  # noqa: E402

logger = logging.getLogger("eval_search")


# ---------------------------------------------------------------------------
# Judge prompts — 응답 품질 채점 (옵션, 3 모드)
# ---------------------------------------------------------------------------
#
# 모드별 설계:
#
# * **overlap** (legacy 기본) — Judge 에 source chunk + retrieved context 둘 다
#   노출. 의미 평가가 아닌 lexical/semantic overlap 채점으로 회귀하는 위험.
#   호환을 위해 유지하되 비추천 — 본 모드는 "이 검색 결과가 정답 청크의 핵심
#   정보를 그대로 담는가" 라는 측정만 가능.
#
# * **reference-free** (★ 권장 기본) — source chunk 를 노출하지 않고 질문 +
#   검색 컨텍스트만 보여줌. "이 컨텍스트로 질문에 답할 수 있는가" 만 평가하므로
#   진짜 RAG 답변 품질에 가깝다. lexical overlap 편향 차단.
#
# * **entailment** — source chunk vs retrieved context 의 의미 entailment 만
#   평가 (NLI 형식). overlap 보다 의미 추론에 무게.


JUDGE_PROMPT_OVERLAP = """\
질문: {query}

정답 근거 (출처 청크):
---
{source_chunk}
---

검색 시스템이 반환한 컨텍스트:
---
{retrieved_context}
---

검색된 컨텍스트가 정답 근거의 핵심 내용을 담고 있는지 0~5점으로 평가하라.
- 5: 정답 근거의 모든 핵심 정보를 담음
- 3: 일부만 담음
- 0: 무관하거나 누락

JSON 으로만 출력::

  {{"score": 0~5 정수, "reason": "한 줄 설명"}}
"""


JUDGE_PROMPT_REFERENCE_FREE = """\
질문: {query}

검색 시스템이 반환한 컨텍스트:
---
{retrieved_context}
---

위 컨텍스트에 **명시적으로 포함된 정보만** 사용해 질문에 정확하고 충분히
답할 수 있는지 0~5점으로 평가하라.

**중요 — self-knowledge 차단**:
- 컨텍스트에 없는 사실은 답에 쓰지 말 것 (당신의 학습 지식 사용 금지)
- 컨텍스트가 부분만 답할 수 있다면 부분 점수, 답이 완전히 학습 지식에 의존
  하면 0점.
- "이 컨텍스트에 답이 있는가" 가 평가 기준 — 일반 상식으로 답할 수 있다고
  high score 주지 말 것.

점수 기준:
- 5: 컨텍스트의 명시 정보만으로 정확하고 완전한 답이 도출됨
- 3: 컨텍스트가 답의 일부만 담음, 외부 지식 보완 시에만 완전
- 0: 컨텍스트가 답을 전혀 포함하지 않음 (무관/공백)

JSON 으로만 출력::

  {{"score": 0~5 정수, "reason": "한 줄 설명"}}
"""


JUDGE_PROMPT_ENTAILMENT = """\
질문: {query}

전제(정답 근거 청크):
---
{source_chunk}
---

가설(검색 시스템 반환 컨텍스트):
---
{retrieved_context}
---

가설이 전제의 의미를 함의하는지 0~5점으로 평가하라 (NLI entailment).
- 5: 가설이 전제의 모든 핵심 의미를 함의 (등가 또는 더 강함)
- 3: 가설이 전제의 일부 핵심 의미만 함의
- 0: 가설이 전제와 무관하거나 모순

JSON 으로만 출력::

  {{"score": 0~5 정수, "reason": "한 줄 설명"}}
"""


JUDGE_MODES = ("reference-free", "overlap", "entailment")
"""Judge 모드 식별자. CLI ``--judge-mode`` 인자로 선택."""


async def _judge_answer_single(
    query: str,
    source_chunk: str,
    retrieved_context: str,
    *,
    judge: LLMClient,
    reasoning_mode: str | None,
    mode: str,
    seed: int | None,
) -> tuple[int, str]:
    """단일 Judge 호출 — judge_answer 의 내부 헬퍼."""
    if mode == "overlap":
        template = JUDGE_PROMPT_OVERLAP
    elif mode == "entailment":
        template = JUDGE_PROMPT_ENTAILMENT
    else:
        template = JUDGE_PROMPT_REFERENCE_FREE

    prompt = template.format(
        query=query,
        source_chunk=source_chunk[:4000],
        retrieved_context=retrieved_context[:6000],
    )
    call_kwargs: dict[str, Any] = {
        "max_tokens": 256,
        "temperature": 0.0,
        "reasoning_mode": reasoning_mode,
        "purpose": f"goldset_judge_answer_{mode}",
    }
    if seed is not None:
        call_kwargs["seed"] = int(seed)
    text = await judge.complete(prompt, **call_kwargs)
    try:
        data = extract_json(text)
    except ValueError:
        return -1, "parse_error"
    if not isinstance(data, dict):
        return -1, "parse_error"
    score_raw = data.get("score")
    if not isinstance(score_raw, (int, float)):
        return -1, "parse_error"
    score = max(0, min(5, int(score_raw)))
    reason = str(data.get("reason") or "").strip()
    return score, reason


async def judge_answer(
    query: str,
    source_chunk: str,
    retrieved_context: str,
    *,
    judge: LLMClient,
    reasoning_mode: str | None = "off",
    mode: str = "reference-free",
    seed: int | None = None,
    n_samples: int = 1,
) -> tuple[int, str, dict[str, float]]:
    """Judge LLM 으로 검색된 컨텍스트의 응답 품질을 0~5 점 채점.

    Args:
        mode: ``"reference-free"`` (기본·권장) / ``"overlap"`` (legacy) /
            ``"entailment"`` 중 하나.
        seed: 결정성 seed. endpoint 지원 시 LLM 호출에 전달 (S3 N-H1 해소).
        n_samples: Judge 호출 반복 횟수 (S3 — Judge 분산 측정). 1 이면 기존
            동작, 2+ 면 동일 입력에 seed+i 로 N회 호출 후 median 을 최종
            점수, std/min/max 를 stats 로 반환.

    Returns:
        (score, reason, stats) — 세 요소 튜플.

        - ``score``: int (median if n_samples>1, parse 실패 시 -1)
        - ``reason``: str (median 호출의 reason; parse_error 면 "parse_error")
        - ``stats``: dict
          - ``n_samples``: 실제 성공한 샘플 수
          - ``n_parse_failed``: parse 실패한 샘플 수
          - 추가 (n_samples>=2): ``std``, ``min``, ``max``, ``samples`` (list[int])

    파싱 실패 (모든 샘플 실패) 시 (-1, "parse_error", {...}).
    """
    if n_samples < 1:
        n_samples = 1
    scores: list[int] = []
    reasons: list[str] = []
    parse_failed = 0
    for i in range(n_samples):
        # 각 샘플은 다른 seed (seed + i) — 같은 prompt 의 응답 분포 측정
        sample_seed = (seed + i) if seed is not None else None
        s, r = await _judge_answer_single(
            query, source_chunk, retrieved_context,
            judge=judge, reasoning_mode=reasoning_mode, mode=mode,
            seed=sample_seed,
        )
        if s < 0:
            parse_failed += 1
            continue
        scores.append(s)
        reasons.append(r)

    stats: dict[str, float] = {
        "n_samples": float(len(scores)),
        "n_parse_failed": float(parse_failed),
    }
    if not scores:
        return -1, "parse_error", stats

    # median 으로 최종 점수 — 단일 호출 outlier 영향 차단
    sorted_scores = sorted(scores)
    mid = len(sorted_scores) // 2
    if len(sorted_scores) % 2 == 1:
        median = sorted_scores[mid]
    else:
        median = int(round((sorted_scores[mid - 1] + sorted_scores[mid]) / 2))
    median_idx = scores.index(median) if median in scores else 0
    final_reason = reasons[median_idx]

    if len(scores) >= 2:
        import math as _math  # noqa: PLC0415
        mean = sum(scores) / len(scores)
        var = sum((x - mean) ** 2 for x in scores) / (len(scores) - 1)
        stats["std"] = _math.sqrt(var)
        stats["min"] = float(min(scores))
        stats["max"] = float(max(scores))
        stats["mean"] = mean
        stats["samples"] = list(scores)  # type: ignore[assignment]

    return median, final_reason, stats


# ---------------------------------------------------------------------------
# Single query evaluation
# ---------------------------------------------------------------------------


async def evaluate_one(
    item: GoldItem,
    *,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
    embedding_client: Any,
    llm_client: LLMClient | None,
    reranker_client: Any,
    top_k: int,
    max_chunks: int,
    similarity_threshold: float,
    rerank_enabled: bool,
    rerank_top_k: int | None,
    rerank_score_threshold: float,
    hyde_enabled: bool,
    include_graph: bool,
    judge: LLMClient | None,
    reasoning_mode: str | None,
    embed_fn: EmbedFn,
    graph_match_threshold: float = DEFAULT_GRAPH_MATCH_THRESHOLD,
    graph_match_strict: bool = False,
    score_relations: bool = False,
    judge_mode: str = "reference-free",
    judge_seed_base: int | None = None,
    judge_n_samples: int = 1,
    embedding_model_id: str = "",
) -> dict[str, Any]:
    """단일 질의에 대한 검색 + 채점.

    2차: graph entity 채점에 4-tier cascade 매칭을 사용하여 entity_type 명
    변경 / 표기 정규화 / 동의어 / 의미 매칭에 강건. 관계 채점은
    ``score_relations`` 옵션으로 활성화.

    3차: ``embed_fn`` 을 외부에서 주입받는다. 항목 동시 평가 시 캐시 효과를
    보존하기 위해 ``_evaluate_gold_set`` 가 1회만 빌드하여 모든 항목에
    공유한다 (D-7).
    """
    start = time.perf_counter()
    assembled: AssembledContext = await assemble_context_with_sources(
        item.query,
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=embedding_client,
        llm_client=llm_client,
        reranker_client=reranker_client,
        max_chunks=max_chunks,
        include_graph=include_graph,
        similarity_threshold=similarity_threshold,
        rerank_enabled=rerank_enabled,
        rerank_top_k=rerank_top_k,
        rerank_score_threshold=rerank_score_threshold,
        hyde_enabled=hyde_enabled,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000

    # P14 (S2) — tie-breaker 명시: vector store 의 동률(similarity tie) 시 결과
    # 순서가 도착 순서·인덱스 빌드 순서에 의존하면 메트릭이 비결정적이 된다.
    # `(similarity desc, document_id asc)` 의 명시적 stable sort 를 평가
    # 레이어에서 한 번 더 적용하여 결정성을 보장한다.
    # `assemble_context_with_sources` 자체의 sort 가 같은 키를 사용하면 no-op,
    # 다르면 메트릭만 결정적으로 보정 (시스템 운영 동작에는 영향 없음).
    sorted_sources = sorted(
        assembled.sources,
        key=lambda s: (-(s.similarity or 0.0), s.document_id),
    )
    retrieved_doc_ids = [s.document_id for s in sorted_sources]
    # R3 — 동치 그룹이 있으면 '대표 1개' 축약 후 기존 메트릭 재사용
    # (metrics.py 무변경). 없으면 평탄 채점으로 폴백 (하위호환).
    if item.relevant_doc_groups:
        scoring_retrieved, relevant = _reduce_equivalence(
            retrieved_doc_ids, item.relevant_doc_groups,
        )
    else:
        scoring_retrieved, relevant = retrieved_doc_ids, set(item.relevant_doc_ids)

    # graph-level 채점 — 4-tier cascade (exact → alias → normalize → embedding).
    # 임베딩 단계의 비용 통제는 외부 주입된 LRU 캐시 embed_fn 으로 흡수.
    entity_report = run_entity_matching(
        item.relevant_graph_entities,
        list(assembled.retrieved_graph_entities),
        embed_fn=embed_fn,
        threshold=graph_match_threshold,
        strict=graph_match_strict,
    )

    row: dict[str, Any] = {
        "id": item.id,
        "query": item.query,
        "mode": _classify_mode(item),
        "source_type": item.source_type,
        "difficulty": item.difficulty,
        "source_document_id": item.source_document_id,
        "retrieved_doc_ids": retrieved_doc_ids[:top_k],
        "retrieved_count": len(retrieved_doc_ids),
        # CSV/외부 진단(diagnose_r3_effect) 호환 — 평탄 원본 유지(축약 X).
        "relevant_doc_ids": sorted(set(item.relevant_doc_ids)),
        "n_answer_units": len(item.relevant_doc_groups or item.relevant_doc_ids),
        # chunk/doc-level (기존) — R3 동치 축약된 retrieved/relevant 로 채점.
        f"recall@{top_k}": recall_at_k(scoring_retrieved, relevant, top_k),
        f"precision@{top_k}": precision_at_k(scoring_retrieved, relevant, top_k),
        f"hit@{top_k}": int(hit_at_k(scoring_retrieved, relevant, top_k)),
        f"ndcg@{top_k}": ndcg_at_k(scoring_retrieved, relevant, top_k),
        "mrr": mrr(scoring_retrieved, relevant),
        # graph-level (D-5) — 매칭된 retrieved/relevant 키를 메트릭에 전달.
        # all_relevant_keys 가 골든 전체이므로 recall 분모는 정상 유지된다.
        f"graph_recall@{top_k}": recall_at_k(
            entity_report.retrieved_keys_in_rank_order,
            entity_report.all_relevant_keys,
            top_k,
        ),
        f"graph_precision@{top_k}": precision_at_k(
            entity_report.retrieved_keys_in_rank_order,
            entity_report.all_relevant_keys,
            top_k,
        ),
        f"graph_hit@{top_k}": int(hit_at_k(
            entity_report.retrieved_keys_in_rank_order,
            entity_report.all_relevant_keys,
            top_k,
        )),
        "graph_mrr": mrr(
            entity_report.retrieved_keys_in_rank_order,
            entity_report.all_relevant_keys,
        ),
        f"graph_ndcg@{top_k}": ndcg_at_k(
            entity_report.retrieved_keys_in_rank_order,
            entity_report.all_relevant_keys,
            top_k,
        ),
        # 2차 — tier 분포 / score 시그널.
        "graph_match_tiers": dict(entity_report.tier_counts),
        "graph_match_score_avg": entity_report.avg_score(),
        "graph_match_score_min": entity_report.min_score(),
        "graph_match_score_max": entity_report.max_score(),
        "elapsed_ms": elapsed_ms,
    }

    if score_relations and item.relevant_graph_relations:
        rel_retrieved_keys, rel_relevant_keys, rel_tier_counts, rel_scores = (
            run_relation_matching(
                item.relevant_graph_relations,
                list(assembled.retrieved_graph_relations),
                embed_fn=embed_fn,
                threshold=graph_match_threshold,
                strict=graph_match_strict,
            )
        )
        # all_relevant 는 골든 관계 전체.
        all_rel_keys: set[tuple[str, str, str]] = {
            (
                (g.source_name or "").strip().lower(),
                (g.target_name or "").strip().lower(),
                (g.relation_type or "").strip(),
            )
            for g in item.relevant_graph_relations
        }
        row[f"graph_rel_recall@{top_k}"] = recall_at_k(
            rel_retrieved_keys, all_rel_keys, top_k,
        )
        row[f"graph_rel_precision@{top_k}"] = precision_at_k(
            rel_retrieved_keys, all_rel_keys, top_k,
        )
        row[f"graph_rel_hit@{top_k}"] = int(hit_at_k(
            rel_retrieved_keys, all_rel_keys, top_k,
        ))
        row["graph_rel_mrr"] = mrr(rel_retrieved_keys, all_rel_keys)
        row["graph_rel_match_tiers"] = dict(rel_tier_counts)
        if rel_scores:
            row["graph_rel_match_score_avg"] = sum(rel_scores) / len(rel_scores)
            row["graph_rel_match_score_min"] = min(rel_scores)
            row["graph_rel_match_score_max"] = max(rel_scores)
        # 사용 의도 유지 — relevant 키 변수 (디버그 후속용).
        _ = rel_relevant_keys

    # 정답 청크 본문 조회 방식은 judge 채점 여부와 무관하게 row 에 기록 —
    # source 미해상도(fallback/empty) 의 빈도를 추적하기 위함.
    source_text, source_method = await _fetch_source_text(item, meta_store)
    row["source_fetch_method"] = source_method

    if judge is not None:
        # S3 N-M2 — reference-free 모드는 source_chunk 를 보지 않으므로 fallback
        # 여부와 무관. fallback 이 일어나도 retrieved_context 만 평가하면 됨.
        # overlap/entailment 모드만 source 가 필요하므로 fallback 시 skip.
        needs_source = judge_mode in ("overlap", "entailment")
        source_unreliable = (
            source_method.startswith("fallback_") or source_method == "empty"
        )
        if needs_source and source_unreliable:
            # 잘못된 근거로 채점하면 평균을 오염시키므로 skip.
            row["judge_score"] = None
            row["judge_reason"] = ""
            row["judge_skip_reason"] = "source_fallback"
            row["judge_parse_failed"] = False
        else:
            # S3 N-H1 — Judge 호출에도 seed 전파. item.id 기반 deterministic.
            judge_seed = (
                judge_seed_base + (hash(item.id) % 10_000_000)
                if judge_seed_base is not None
                else None
            )
            score, reason, judge_stats = await judge_answer(
                item.query,
                source_text,
                assembled.context_text,
                judge=judge,
                reasoning_mode=reasoning_mode,
                mode=judge_mode,
                seed=judge_seed,
                n_samples=judge_n_samples,
            )
            if score < 0:
                # parse_error — 평균에서 분리. aggregate 가 None 을 자동 스킵.
                row["judge_score"] = None
                row["judge_reason"] = reason
                row["judge_parse_failed"] = True
            else:
                row["judge_score"] = score
                row["judge_reason"] = reason
                row["judge_parse_failed"] = False
            # Judge 분산 통계 (n_samples >= 2 일 때만 std/min/max 채워짐)
            if judge_stats.get("std") is not None:
                row["judge_score_std"] = judge_stats.get("std")
                row["judge_score_min"] = judge_stats.get("min")
                row["judge_score_max"] = judge_stats.get("max")
                row["judge_score_mean"] = judge_stats.get("mean")
            row["judge_n_samples"] = int(judge_stats.get("n_samples") or 0)
            row["judge_n_parse_failed"] = int(judge_stats.get("n_parse_failed") or 0)

    # P12 — embed_fn 이 T4 단계 skip 을 한 번이라도 했는지 row 에 기록.
    t4_disabled = bool(getattr(embed_fn, "t4_disabled", False))
    if t4_disabled:
        row["graph_t4_disabled"] = True

    return row


def _reduce_equivalence(
    retrieved_doc_ids: list[int],
    groups: list[list[int]],
) -> tuple[list[int], set[int]]:
    """동치 그룹을 '대표 1개' 단위로 축약하여 (retrieved', relevant') 반환.

    각 동치 그룹 = 정답 1개 단위 (R3). 그룹의 대표 ID 는 'retrieved 안에서
    가장 먼저 등장하는 그룹 멤버'. retrieved 에 그룹 멤버가 없으면 그룹의 첫
    원소(정렬된)를 대표로 둔다 (=miss 로 카운트되어 recall 분모에 잡힘).

    반환된 retrieved' 는 원래 retrieved 의 순서를 보존하되, 한 그룹의 멤버는
    '첫 등장 1회'만 남기고 (중복 정답 캡), relevant' 는 그룹별 대표 set.
    그룹 외 doc(정답 아님)은 retrieved' 에 그대로 보존 → precision 분모 정확.
    """
    rep_of_group: list[int] = []
    member_to_rep: dict[int, int] = {}
    rank = {d: i for i, d in enumerate(retrieved_doc_ids)}
    for g in groups:
        present = [d for d in g if d in rank]
        rep = min(present, key=lambda d: rank[d]) if present else min(g)
        rep_of_group.append(rep)
        for d in g:
            member_to_rep[d] = rep
    relevant_reduced = set(rep_of_group)

    seen_reps: set[int] = set()
    retrieved_reduced: list[int] = []
    for d in retrieved_doc_ids:
        if d in member_to_rep:
            rep = member_to_rep[d]
            if rep in seen_reps:
                continue  # 같은 그룹 두 번째 출현 → drop (중복 정답 캡)
            seen_reps.add(rep)
            retrieved_reduced.append(rep)
        else:
            retrieved_reduced.append(d)  # 정답 아닌 doc → 보존 (precision 분모)
    return retrieved_reduced, relevant_reduced


def _classify_mode(item: GoldItem) -> str:
    """GoldItem 을 cross_doc / chunk / graph / hybrid 로 분류한다.

    - cross_document=True 면 "cross_doc" (우선)
    - relevant_doc_ids 만 있으면 "chunk"
    - relevant_graph_entities 만 있으면 "graph"
    - 둘 다 있으면 "hybrid"
    """
    if item.cross_document:
        return "cross_doc"
    has_doc = bool(item.relevant_doc_ids)
    has_graph = bool(item.relevant_graph_entities)
    if has_doc and has_graph:
        return "hybrid"
    if has_graph:
        return "graph"
    return "chunk"


def _normalize_for_anchor(text: str) -> str:
    """source_text_anchor 비교용 정규화 — 연속 whitespace 단일 공백."""
    return " ".join(text.split())


async def _fetch_source_text(
    item: GoldItem, meta_store: MetadataStore,
) -> tuple[str, str]:
    """골드 항목의 정답 청크 본문과 조회 방식을 반환.

    Returns:
        ``(content, method)`` — method 는 어느 경로로 본문을 잡았는지 식별:

        * ``"anchor"`` — ``source_text_anchor`` prefix 매칭 성공
        * ``"chunk_id"`` — ``source_chunk_id`` 일치 (deprecated 호환)
        * ``"fallback_first_chunk"`` — source_document 의 첫 청크로 폴백
        * ``"fallback_doc_first_chunk"`` — ``relevant_doc_ids[0]`` 의 첫 청크
          로 폴백 (source_document_id 도 없는 경우)
        * ``"empty"`` — 끝까지 조회 실패. content 는 빈 문자열.

    호출부는 ``"fallback_*"`` / ``"empty"`` 메소드일 때 judge 채점에서 제외해
    잘못된 근거로 매겨진 점수가 평균을 오염하지 않게 해야 한다.
    """
    if item.source_document_id is not None:
        chunks = await meta_store.get_chunks_by_document(item.source_document_id)

        if item.source_text_anchor:
            normalized_anchor = _normalize_for_anchor(item.source_text_anchor)
            for c in chunks:
                content = c.get("content") or ""
                if _normalize_for_anchor(content).startswith(normalized_anchor):
                    return content, "anchor"

        if item.source_chunk_id:
            for c in chunks:
                if c.get("id") == item.source_chunk_id:
                    return c.get("content") or "", "chunk_id"

        if chunks:
            return chunks[0].get("content") or "", "fallback_first_chunk"
    if item.relevant_doc_ids:
        chunks = await meta_store.get_chunks_by_document(item.relevant_doc_ids[0])
        if chunks:
            return chunks[0].get("content") or "", "fallback_doc_first_chunk"
    return "", "empty"


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    """질의별 결과를 CSV 로 저장.

    list 형 컬럼은 콤마 결합 문자열로 직렬화.
    """
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    keys: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                keys.append(k)

    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for r in rows:
            row_str = {}
            for k in keys:
                v = r.get(k, "")
                if isinstance(v, list):
                    v = ",".join(str(x) for x in v)
                row_str[k] = v
            writer.writerow(row_str)


def write_summary(
    rows: list[dict[str, Any]],
    path: Path,
    *,
    label: str,
    config_summary: dict[str, Any],
) -> dict[str, Any]:
    """집계 요약을 JSON 으로 저장하고 반환.

    전체 메트릭 평균 + mode 별 split (chunk / graph / hybrid / cross_doc) 도
    함께 보고한다.
    chunk-only 항목의 ``graph_*`` 메트릭은 자연 0.0 이라 전체 평균을 끌어내릴
    수 있어 W-5 의 권장 대응으로 mode 별 분리 보고를 추가했다.

    2차: ``graph_match_tiers`` (dict) 는 ``aggregate`` 가 숫자만 처리하므로
    별도로 누적 합산하여 보고한다.
    """
    excluded = {"source_document_id"}
    summary = aggregate(rows, exclude=excluded)

    rows_by_mode: dict[str, list[dict[str, Any]]] = {
        "chunk": [], "graph": [], "hybrid": [], "cross_doc": [],
    }
    for r in rows:
        mode = r.get("mode")
        if mode in rows_by_mode:
            rows_by_mode[mode].append(r)

    metrics_by_mode: dict[str, dict[str, Any]] = {}
    for mode, subset in rows_by_mode.items():
        if not subset:
            continue
        block_metrics: dict[str, Any] = aggregate(subset, exclude=excluded)
        # tier 카운트는 dict 라 aggregate 가 빠뜨림 — 누적 합산하여 추가.
        tier_dicts = [r.get("graph_match_tiers") or {} for r in subset]
        block_metrics["graph_match_tiers_total"] = aggregate_tier_counts(tier_dicts)
        if any(r.get("graph_rel_match_tiers") for r in subset):
            rel_tier_dicts = [r.get("graph_rel_match_tiers") or {} for r in subset]
            block_metrics["graph_rel_match_tiers_total"] = aggregate_tier_counts(rel_tier_dicts)
        metrics_by_mode[mode] = {
            "n": len(subset),
            "metrics": block_metrics,
        }

    # 전체 tier 누적도 보고.
    summary["graph_match_tiers_total"] = aggregate_tier_counts(
        [r.get("graph_match_tiers") or {} for r in rows],
    )
    if any(r.get("graph_rel_match_tiers") for r in rows):
        summary["graph_rel_match_tiers_total"] = aggregate_tier_counts(
            [r.get("graph_rel_match_tiers") or {} for r in rows],
        )

    # 실패 / 진단 통계 — 평균 메트릭과 별도로 분포·실패율을 명시 보고.
    n_total = len(rows)
    n_failed = sum(1 for r in rows if r.get("metric_failed"))
    n_successful = n_total - n_failed
    failure_rate = (n_failed / n_total) if n_total else 0.0

    judge_parse_failures = sum(
        1 for r in rows if r.get("judge_parse_failed")
    )
    judge_skipped = sum(
        1 for r in rows if r.get("judge_skip_reason") == "source_fallback"
    )
    judge_success_count = sum(
        1 for r in rows
        if isinstance(r.get("judge_score"), (int, float))
        and not isinstance(r.get("judge_score"), bool)
    )

    fetch_method_counts = Counter(
        r.get("source_fetch_method") for r in rows
        if r.get("source_fetch_method")
    )

    graph_t4_skip_count = sum(1 for r in rows if r.get("graph_t4_disabled"))
    graph_t4_disabled_any = graph_t4_skip_count > 0

    out = {
        "label": label,
        "n_queries": n_total,
        "n_failed": n_failed,
        "n_successful": n_successful,
        "failure_rate": failure_rate,
        "judge_score_parse_failures": judge_parse_failures,
        "judge_score_success_count": judge_success_count,
        "judge_skip_count": judge_skipped,
        "source_fetch_method_counts": dict(fetch_method_counts),
        "graph_t4_disabled": graph_t4_disabled_any,
        "graph_t4_skip_count": graph_t4_skip_count,
        "config": config_summary,
        "metrics": summary,
        "metrics_by_mode": metrics_by_mode,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    return out


def print_summary(summary: dict[str, Any]) -> None:
    print("\n" + "=" * 60)
    print(f"  Run: {summary['label']}  |  N={summary['n_queries']}")
    print("=" * 60)
    metrics = summary.get("metrics", {})
    # 보기 좋은 순서로 키 정렬 — graph_* 는 chunk 메트릭 다음에 배치
    preferred = ["recall@", "precision@", "hit@", "ndcg@", "mrr",
                 "graph_recall@", "graph_precision@", "graph_hit@",
                 "graph_ndcg@", "graph_mrr",
                 "graph_match_score_", "graph_rel_recall@",
                 "graph_rel_precision@", "graph_rel_hit@", "graph_rel_mrr",
                 "judge_score", "elapsed_ms"]
    keys = sorted(
        metrics.keys(),
        key=lambda k: next(
            (i for i, p in enumerate(preferred) if k.startswith(p)),
            len(preferred),
        ),
    )
    for k in keys:
        v = metrics[k]
        if isinstance(v, dict):
            # tier 카운트 dict 등은 한 줄로 압축 표시.
            print(f"  {k:24s} {v}")
        elif "ms" in k:
            print(f"  {k:24s} {v:>10.1f}")
        else:
            print(f"  {k:24s} {v:>10.4f}")

    metrics_by_mode = summary.get("metrics_by_mode") or {}
    for mode in ("chunk", "graph", "hybrid"):
        block = metrics_by_mode.get(mode)
        if not block:
            continue
        print(f"  [mode={mode}, n={block.get('n', 0)}]")
        sub = block.get("metrics") or {}
        for k in sorted(sub.keys(), key=lambda kk: next(
            (i for i, p in enumerate(preferred) if kk.startswith(p)),
            len(preferred),
        )):
            v = sub[k]
            if isinstance(v, dict):
                print(f"    {k:22s} {v}")
            elif "ms" in k:
                print(f"    {k:22s} {v:>10.1f}")
            else:
                print(f"    {k:22s} {v:>10.4f}")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Stores / clients (web/app.py 와 동일 빌더 재사용)
# ---------------------------------------------------------------------------


def _build_clients(config: Config) -> tuple[Any, Any, Any]:
    """LLM, embedding, reranker 클라이언트를 web/app.py 빌더로 생성."""
    from context_loop.web.app import (  # type: ignore[attr-defined]
        _build_embedding_client,
        _build_llm_client,
        _build_reranker_client,
    )
    return (
        _build_llm_client(config),
        _build_embedding_client(config),
        _build_reranker_client(config),
    )




# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _resolve_gold_paths(args: argparse.Namespace) -> list[Path]:
    """``--gold-set`` (단일) 또는 ``--gold-set-glob`` (다중) 을 정규화한다.

    글롭 매칭 결과는 사전순으로 정렬해 결정론적 처리. 매칭 0건이면 빈 리스트.
    """
    if args.gold_set_glob:
        matches = sorted(glob.glob(args.gold_set_glob))
        return [Path(m) for m in matches]
    return [Path(args.gold_set)]


def _label_for_run(base_label: str, gold_path: Path, multi: bool) -> str:
    """다중 잡일 때만 파일명 stem 을 라벨에 합쳐 결과 파일 충돌을 막는다."""
    if not multi:
        return base_label
    return f"{base_label}_{gold_path.stem}"


async def _evaluate_gold_set(
    gold_path: Path,
    *,
    label: str,
    config_summary: dict[str, Any],
    out_dir: Path,
    args: argparse.Namespace,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
    embedding_client: Any,
    llm_client: LLMClient,
    reranker_client: Any,
    judge: LLMClient | None,
    similarity_threshold: float,
    rerank_enabled: bool,
    rerank_top_k: int | None,
    rerank_score_threshold: float,
    hyde_enabled: bool,
    embedding_model_id: str = "",
) -> dict[str, Any] | None:
    """골드셋 1개를 채점하고 CSV/요약 JSON 을 저장. 실패 시 None.

    공유 자원(stores, clients) 은 호출자가 한 번만 초기화해 모든 잡에 재사용.
    """
    gold = load_gold_set(gold_path)
    if not gold.items:
        logger.warning("골드셋 %s 에 항목이 없습니다 — 건너뜀.", gold_path)
        return None
    if args.limit:
        gold.items = gold.items[: args.limit]

    logger.info("골드셋 채점 시작 — file=%s, n=%d, label=%s",
                gold_path, len(gold.items), label)

    # 3차 (D-7): embed_fn 을 1회 빌드하여 모든 항목에 공유 — 동시 평가 시
    # LRU 캐시 효과 보존.
    embed_fn = build_embed_fn(embedding_client, model_id=embedding_model_id)

    # 3차 (D-7): graph_store.build_entity_embeddings 를 항목 평가 시작 전에
    # 1회 호출 — 동시 평가 시 중복 build race 회피.
    if args.include_graph and graph_store.entity_embedding_count == 0:
        logger.info("entity embedding 사전 빌드 시작")
        try:
            await graph_store.build_entity_embeddings(embedding_client)
            logger.info(
                "entity embedding 사전 빌드 완료 — count=%d",
                graph_store.entity_embedding_count,
            )
        except Exception:
            logger.warning(
                "entity embedding 사전 빌드 실패 — 항목 평가 중 lazy 빌드 시도.",
                exc_info=True,
            )

    effective_concurrency = max(1, int(getattr(args, "concurrency", 1) or 1))
    sem = asyncio.Semaphore(effective_concurrency)
    total = len(gold.items)
    completed = 0

    async def _process_item(idx: int, item: GoldItem) -> dict[str, Any]:
        nonlocal completed
        async with sem:
            logger.info(
                "[%s start %d/%d] q=%s | gold_doc=%s",
                label, idx, total, item.id, item.relevant_doc_ids,
            )
            try:
                row = await evaluate_one(
                    item,
                    meta_store=meta_store,
                    vector_store=vector_store,
                    graph_store=graph_store,
                    embedding_client=embedding_client,
                    llm_client=llm_client if args.include_graph else None,
                    reranker_client=reranker_client,
                    top_k=args.top_k,
                    max_chunks=args.max_chunks,
                    similarity_threshold=similarity_threshold,
                    rerank_enabled=rerank_enabled,
                    rerank_top_k=rerank_top_k,
                    rerank_score_threshold=rerank_score_threshold,
                    hyde_enabled=hyde_enabled,
                    include_graph=args.include_graph,
                    judge=judge,
                    reasoning_mode=args.reasoning_mode,
                    embed_fn=embed_fn,
                    graph_match_threshold=args.graph_match_threshold,
                    graph_match_strict=args.graph_match_strict,
                    score_relations=args.score_relations,
                    embedding_model_id=embedding_model_id,
                    judge_mode=args.judge_mode,
                    judge_seed_base=args.judge_seed_base,
                    judge_n_samples=args.judge_n_samples,
                )
                row["_idx"] = idx
            except Exception as exc:
                logger.exception("질의 %s 실패: %s", item.id, exc)
                top_k = args.top_k
                row = {
                    "id": item.id,
                    "query": item.query,
                    "error": str(exc),
                    "metric_failed": True,
                    "_idx": idx,
                    # 표준 메트릭 키를 None 으로 명시 — aggregate 가 자동 스킵.
                    f"recall@{top_k}": None,
                    f"precision@{top_k}": None,
                    f"hit@{top_k}": None,
                    f"ndcg@{top_k}": None,
                    "mrr": None,
                }
            completed += 1
            logger.info(
                "[%s done %d/%d] (completed=%d) q=%s",
                label, idx, total, completed, item.id,
            )
            return row

    raw_results = await asyncio.gather(
        *(_process_item(i, it) for i, it in enumerate(gold.items, start=1)),
        return_exceptions=True,
    )

    rows: list[dict[str, Any]] = []
    for idx, r in enumerate(raw_results, start=1):
        if isinstance(r, BaseException):
            # _process_item 안에서 잡혔어야 함 — 여기 도달하면 방어적 처리.
            logger.error(
                "예외가 _process_item 밖으로 새어 나옴: idx=%d, exc=%s", idx, r,
            )
            top_k = args.top_k
            rows.append({
                "id": f"_idx{idx}",
                "error": str(r),
                "metric_failed": True,
                "_idx": idx,
                f"recall@{top_k}": None,
                f"precision@{top_k}": None,
                f"hit@{top_k}": None,
                f"ndcg@{top_k}": None,
                "mrr": None,
            })
        else:
            rows.append(r)
    # 사전 idx 순으로 정렬해 동시성에 무관한 결정론적 결과 순서를 회복.
    rows.sort(key=lambda r: r.get("_idx", 0))
    for r in rows:
        r.pop("_idx", None)

    csv_path = out_dir / f"{label}.csv"
    summary_path = out_dir / f"{label}.summary.json"
    write_csv(rows, csv_path)
    # gold_set 출처와 fingerprint 를 요약에 기록 — aggregate 결과 추적 +
    # compare_runs.py 동치성 검증용.
    enriched_config = dict(config_summary)
    enriched_config["gold_set"] = str(gold_path)
    try:
        gold_bytes = gold_path.read_bytes()
        enriched_config["gold_set_sha256"] = hashlib.sha256(gold_bytes).hexdigest()
    except OSError:
        enriched_config["gold_set_sha256"] = ""
    enriched_config["gold_set_n_items"] = len(gold.items)
    gold_meta = gold.metadata or {}
    enriched_config["gold_set_generator_model"] = (
        gold_meta.get("generator_model", "") or ""
    )
    enriched_config["gold_set_judge_model"] = (
        gold_meta.get("judge_model", "") or ""
    )
    enriched_config["gold_set_self_evaluation_warning"] = gold_meta.get(
        "self_evaluation_warning",
    )
    summary = write_summary(
        rows, summary_path, label=label, config_summary=enriched_config,
    )
    print_summary(summary)
    print(f"  details : {csv_path}")
    print(f"  summary : {summary_path}\n")
    return summary


async def run(args: argparse.Namespace) -> int:
    config = Config(config_path=Path(args.config) if args.config else None)
    data_dir = config.data_dir

    gold_paths = _resolve_gold_paths(args)
    if not gold_paths:
        print(
            f"--gold-set-glob 패턴 '{args.gold_set_glob}' 매칭 없음.",
            file=sys.stderr,
        )
        return 1
    multi = len(gold_paths) > 1

    meta_store = MetadataStore(data_dir / "metadata.db")
    await meta_store.initialize()
    vector_store = VectorStore(data_dir)
    vector_store.initialize()
    graph_store = GraphStore(meta_store)
    await graph_store.load_from_db()

    llm_client, embedding_client, reranker_client = _build_clients(config)

    # Judge 는 옵션 — config.eval.judge.* + CLI override 에서 자동 합성.
    # 분리 구성이 없으면 --allow-self-judge 가 명시되어야 system LLM 재사용 허용.
    judge: LLMClient | None = None
    judge_is_self = False
    if args.judge:
        judge_configured = role_is_configured(
            config, "judge",
            endpoint_override=args.judge_endpoint,
            model_override=args.judge_model,
        )
        if judge_configured:
            judge = build_eval_llm_client(
                config, "judge",
                endpoint_override=args.judge_endpoint,
                model_override=args.judge_model,
                api_key_override=args.judge_api_key,
                headers_override_json=args.judge_headers,
            )
        elif args.allow_self_judge:
            logger.warning(
                "--allow-self-judge 가 명시되어 system LLM 으로 Judge fallback. "
                "메트릭에 self-evaluation 편향이 기록됩니다.",
            )
            judge = llm_client
            judge_is_self = True
        else:
            raise SystemExit(
                "--judge 가 설정되었으나 config.eval.judge / --judge-* 가 비어 있습니다. "
                "system LLM 으로 Judge fallback 을 명시 허용하려면 "
                "--allow-self-judge 를 추가하세요.",
            )

    similarity_threshold = (
        args.similarity_threshold
        if args.similarity_threshold is not None
        else float(config.get("search.similarity_threshold", 0.0))
    )
    rerank_enabled = (
        args.rerank
        if args.rerank is not None
        else bool(config.get("search.reranker_enabled", False))
    )
    hyde_enabled = (
        args.hyde
        if args.hyde is not None
        else bool(config.get("search.hyde_enabled", False))
    )
    rerank_top_k = config.get("search.reranker_top_k") or None
    rerank_score_threshold = float(config.get("search.reranker_score_threshold", 0.0))

    embedding_model_id = str(config.get("processor.embedding_model") or "")
    config_summary = {
        "top_k": args.top_k,
        "max_chunks": args.max_chunks,
        "similarity_threshold": similarity_threshold,
        "rerank_enabled": rerank_enabled,
        "hyde_enabled": hyde_enabled,
        "include_graph": args.include_graph,
        "embedding_model": embedding_model_id,
        "llm_model": config.get("llm.model"),
        "judge_enabled": args.judge,
        "judge_model": args.judge_model or (config.get("llm.model") if args.judge else None),
        "judge_mode": args.judge_mode,
        # Self-evaluation 추적 — judge fall-through 시 메트릭이 편향됨을 명시.
        "judge_is_self": judge_is_self,
        "allow_self_judge": bool(args.allow_self_judge),
        # 2차 — graph 매칭 정책 / 재현성용 메타.
        "graph_match_threshold": args.graph_match_threshold,
        "graph_match_strict": args.graph_match_strict,
        "score_relations": args.score_relations,
        # 3차 — 동시성 메타 (재현 디버그용).
        "concurrency": max(1, int(getattr(args, "concurrency", 1) or 1)),
    }

    out_dir = Path(args.output_dir)
    per_run_summaries: list[dict[str, Any]] = []
    try:
        for gold_path in gold_paths:
            run_label = _label_for_run(args.label, gold_path, multi)
            try:
                summary = await _evaluate_gold_set(
                    gold_path,
                    label=run_label,
                    config_summary=config_summary,
                    out_dir=out_dir,
                    args=args,
                    meta_store=meta_store,
                    vector_store=vector_store,
                    graph_store=graph_store,
                    embedding_client=embedding_client,
                    llm_client=llm_client,
                    reranker_client=reranker_client,
                    judge=judge,
                    similarity_threshold=similarity_threshold,
                    rerank_enabled=rerank_enabled,
                    rerank_top_k=rerank_top_k,
                    rerank_score_threshold=rerank_score_threshold,
                    hyde_enabled=hyde_enabled,
                    embedding_model_id=embedding_model_id,
                )
            except Exception as exc:
                logger.exception("골드셋 %s 채점 중 실패: %s — 다음으로 진행.",
                                 gold_path, exc)
                continue
            if summary is not None:
                per_run_summaries.append(summary)

        if multi:
            _write_aggregate(
                per_run_summaries,
                out_dir=out_dir,
                label=args.label,
                gold_paths=gold_paths,
                config_summary=config_summary,
            )

    finally:
        await meta_store.close()

    return 0


def _write_aggregate(
    per_run_summaries: list[dict[str, Any]],
    *,
    out_dir: Path,
    label: str,
    gold_paths: list[Path],
    config_summary: dict[str, Any],
) -> None:
    """다중 잡 결과를 mean ± std 로 묶어 aggregate.summary.json 저장."""
    if not per_run_summaries:
        logger.warning("aggregate 대상 잡이 0개 — 모든 골드셋이 실패했거나 비어 있습니다.")
        return
    per_metric = [s.get("metrics") or {} for s in per_run_summaries]
    variance = aggregate_with_variance(per_metric)

    out = {
        "label": label,
        "n_gold_sets_requested": len(gold_paths),
        "n_gold_sets_evaluated": len(per_run_summaries),
        "gold_sets": [str(p) for p in gold_paths],
        "config": config_summary,
        "metrics": variance,
        "per_gold_set": [
            {
                "label": s.get("label"),
                "gold_set": (s.get("config") or {}).get("gold_set"),
                "n_queries": s.get("n_queries"),
                "metrics": s.get("metrics"),
            }
            for s in per_run_summaries
        ],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    agg_path = out_dir / f"{label}.aggregate.summary.json"
    agg_path.parent.mkdir(parents=True, exist_ok=True)
    with open(agg_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)

    print("\n" + "=" * 60)
    print(f"  Aggregate: {label}  |  N gold-sets = {len(per_run_summaries)}")
    print("=" * 60)
    preferred = ["recall@", "precision@", "hit@", "ndcg@", "mrr",
                 "judge_score", "elapsed_ms"]
    keys = sorted(
        variance.keys(),
        key=lambda k: next(
            (i for i, p in enumerate(preferred) if k.startswith(p)),
            len(preferred),
        ),
    )
    for k in keys:
        stats = variance[k]
        mean = stats["mean"]
        std = stats["std"]
        if "ms" in k:
            print(f"  {k:24s} mean={mean:>10.1f}  std={std:>8.1f}  "
                  f"min={stats['min']:>8.1f}  max={stats['max']:>8.1f}")
        else:
            print(f"  {k:24s} mean={mean:>10.4f}  std={std:>8.4f}  "
                  f"min={stats['min']:>8.4f}  max={stats['max']:>8.4f}")
    print("=" * 60)
    print(f"  aggregate : {agg_path}\n")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _parse_optional_bool(v: str | None) -> bool | None:
    if v is None:
        return None
    return v.lower() in ("1", "true", "yes", "on")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="골드셋으로 검색 시스템 정량 채점",
    )
    parser.add_argument("--config", "-c", default="")
    gold_group = parser.add_mutually_exclusive_group(required=True)
    gold_group.add_argument(
        "--gold-set", "-g", default=None,
        help="골드셋 YAML 단일 경로",
    )
    gold_group.add_argument(
        "--gold-set-glob", default=None,
        help="골드셋 YAML 글롭 패턴 (예: 'eval/gold_sets/git_code_*.yaml'). "
             "매칭된 N개 골드셋을 순차 채점하고 mean/std aggregate 를 저장.",
    )
    parser.add_argument(
        "--label", default="run",
        help="출력 파일 접두 (예: 'baseline', 'multiview')",
    )
    parser.add_argument(
        "--output-dir", default="eval/runs",
        help="결과 저장 디렉토리 (기본: eval/runs)",
    )
    parser.add_argument(
        "--top-k", type=int, default=5,
        help="메트릭 계산용 top-k (기본 5)",
    )
    parser.add_argument(
        "--max-chunks", type=int, default=10,
        help="검색 단계의 max_chunks (기본 10). top-k 보다 크게 잡아 over-fetch.",
    )
    parser.add_argument(
        "--similarity-threshold", type=float, default=None,
        help="config.search.similarity_threshold 오버라이드",
    )
    parser.add_argument(
        "--rerank", type=lambda v: _parse_optional_bool(v), default=None,
        help="리랭커 사용 여부 (true/false). 미지정 시 config 따름.",
    )
    parser.add_argument(
        "--hyde", type=lambda v: _parse_optional_bool(v), default=None,
        help="HyDE 사용 여부 (true/false). 미지정 시 config 따름.",
    )
    parser.add_argument(
        "--include-graph", action="store_true", default=True,
        help="그래프 컨텍스트 포함 (기본 켜짐)",
    )
    parser.add_argument(
        "--no-graph", action="store_false", dest="include_graph",
        help="그래프 컨텍스트 제외",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="평가할 질의 수 제한 (0 이면 전체)",
    )
    parser.add_argument(
        "--judge", action="store_true",
        help="Judge LLM 으로 응답 품질 0~5 점 채점 (느림, 비용 발생)",
    )
    parser.add_argument(
        "--allow-self-judge", action="store_true",
        help="config.eval.judge / --judge-* 가 비어 있을 때 system LLM 을 "
             "Judge 로 재사용하도록 명시 허용. 자기 평가 편향이 발생하므로 "
             "summary 의 judge_is_self=true 로 기록된다.",
    )
    # Judge override 가 모두 비면 시스템 LLM 재사용 (편향 경고 표시).
    # 별도 엔드포인트 지정 시 config.llm.headers / reasoning_profiles 자동 주입,
    # --judge-headers JSON 으로 헤더 통째 교체 가능.
    parser.add_argument("--judge-endpoint", default="")
    parser.add_argument("--judge-model", default="")
    parser.add_argument("--judge-api-key", default="")
    parser.add_argument(
        "--judge-headers", default="",
        help="Judge 전용 헤더 JSON (예: '{\"X-Org-Id\":\"abc\"}'). "
             "미지정 시 config.llm.headers 사용.",
    )
    # S2 — Judge 프롬프트 모드 (lexical overlap 편향 차단).
    parser.add_argument(
        "--judge-mode", default="reference-free",
        choices=list(JUDGE_MODES),
        help="Judge 프롬프트 모드 (기본 'reference-free' — 권장). "
             "'reference-free': source chunk 숨기고 retrieved 만 보여줘 "
             "'질문에 답할 수 있는가' 평가 (lexical overlap 편향 차단). "
             "'overlap': source + retrieved 노출 (legacy, 비추천). "
             "'entailment': source vs retrieved 의미 entailment 평가.",
    )
    # S3 — Judge 결정성·분산 측정.
    parser.add_argument(
        "--judge-seed-base", type=int, default=None,
        help="Judge LLM 호출 결정성 seed base (S3 N-H1). None 이면 미전달. "
             "정수 명시 시 항목별 seed = base + hash(item.id) % 10M 으로 "
             "endpoint 가 OpenAI 호환이면 결정적 응답.",
    )
    parser.add_argument(
        "--judge-n-samples", type=int, default=1,
        help="Judge 호출 반복 횟수 (S3 — 분산 측정). 1 이면 단일 호출, 2+ 면 "
             "seed+i 로 N회 호출 후 median 을 최종 점수, std/min/max 를 row 에 "
             "기록. 운영 안정성 진단용.",
    )
    parser.add_argument(
        "--reasoning-mode", default="off",
        help="LLM reasoning_mode (config.llm.reasoning_profiles 키, "
             "Judge 호출에 적용, 기본 'off')",
    )
    # 2차 — graph 채점 강건성 (R1/R2/R3).
    parser.add_argument(
        "--graph-match-threshold", type=float,
        default=DEFAULT_GRAPH_MATCH_THRESHOLD,
        help=f"4-tier 매칭의 T4 (embedding) 임계값 (기본 "
             f"{DEFAULT_GRAPH_MATCH_THRESHOLD}). 골드셋 metadata 의 "
             f"기본값을 무시한다.",
    )
    parser.add_argument(
        "--graph-match-strict", action="store_true",
        help="T2(alias)/T3(normalize)/T4(embedding) 단계를 모두 skip 하여 "
             "1차 동작(정확 비교만) 을 재현한다 (기본 False).",
    )
    parser.add_argument(
        "--score-relations", action="store_true",
        help="관계(엣지) 채점 메트릭 (graph_rel_*) 을 산출한다 (기본 False). "
             "골드셋의 relevant_graph_relations 가 비어 있으면 효과 없음.",
    )
    # 3차 — 항목 단위 병렬 처리 (R1).
    parser.add_argument(
        "--concurrency", type=int, default=1,
        help="골드셋 내 항목 동시 처리 수 (기본 1, 직렬). "
             "LLM endpoint rate limit 에 맞춰 4~8 권장. summary 에 기록.",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    if args.concurrency > 32:
        logger.warning(
            "--concurrency=%d 는 endpoint rate limit 초과 위험. 4~8 권장.",
            args.concurrency,
        )

    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
