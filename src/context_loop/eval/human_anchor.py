"""인간 앵커(human anchor) 입출력 + generator 정밀도 — PR #79 P5.

source-grounded 골드의 첫 run 절대값은 "잠정 기준선"이다(계획서 §7). 진짜
"절대"가 되려면 **소규모 인간 검증 기준선**이 필요하다 — 합성 generator 가
만든 골드 항목이 실제로 타당한지 사람이 표본 라벨링하고, 그 비율로
**generator 정밀도**를 산출해 대규모 합성 골드의 신뢰도를 보정한다.

흐름::

    [export] 골드셋 → 리뷰 CSV (사람이 채울 verdict 컬럼 비움)
       ↓ 사람이 human_valid / human_{doc,answer,graph}_valid 채움
    [import] 리뷰 CSV → {id: ReviewVerdict}
    [score]  generator 정밀도(전체 + 단위별) + 95% CI + answerable 비율

채점 단위(doc/answer/graph)별로 정밀도를 분리 산출한다 — 예: 그래프 트리플은
자주 틀리지만 doc 출처는 거의 맞을 수 있으므로 단위별 신뢰도가 다르다.

LLM 비의존(결정론) — 순수 I/O + 산술. ``bootstrap_ci_mean`` 으로 정밀도에
95% 신뢰구간을 동반한다.
"""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from context_loop.eval.gold_set import MEASUREMENT_UNITS, GoldItem, GoldSet
from context_loop.eval.metrics import bootstrap_ci_mean

# 리뷰 CSV 의 사람이 채우는 verdict 컬럼 (export 시 빈 값).
HUMAN_VERDICT_COLUMNS = (
    "human_valid",        # 전체: 이 골드 항목이 타당한가 (1/0/빈칸)
    "human_doc_valid",    # doc: evidence_span 이 출처 문서에 실제 있고 표적이 맞나
    "human_answer_valid",  # answer: reference_answer 가 근거에 충실하고 맞나
    "human_graph_valid",  # graph: (entity, relation, target) 트리플이 맞나
    "human_notes",
)

# export 시 사람에게 보여줄 읽기 전용 컨텍스트 컬럼 (verdict 판단 근거).
_CONTEXT_COLUMNS = (
    "id",
    "source_type",
    "difficulty",
    "measurement_units",
    "answerable",
    "query",
    "reference_answer",
    "evidence_spans",
    "supporting_facts",
    "relevant_doc_ids",
)

REVIEW_CSV_COLUMNS = (*_CONTEXT_COLUMNS, *HUMAN_VERDICT_COLUMNS)

_TRUE_TOKENS = frozenset({"1", "y", "yes", "true", "o", "참", "예", "맞음"})
_FALSE_TOKENS = frozenset({"0", "n", "no", "false", "x", "거짓", "아니오", "틀림"})


@dataclass
class ReviewVerdict:
    """리뷰 CSV 한 행의 인간 판정.

    각 ``*_valid`` 는 ``True`` (타당) / ``False`` (부적합) / ``None`` (미라벨)
    삼치값. 미라벨은 정밀도 분모에서 제외된다.
    """

    id: str
    valid: bool | None = None
    doc_valid: bool | None = None
    answer_valid: bool | None = None
    graph_valid: bool | None = None
    notes: str = ""

    def unit_valid(self, unit: str) -> bool | None:
        return {
            "doc": self.doc_valid,
            "answer": self.answer_valid,
            "graph": self.graph_valid,
        }.get(unit)


def parse_verdict_cell(raw: str | None) -> bool | None:
    """verdict 셀 문자열을 삼치값으로 파싱. 빈/모호 → ``None``."""
    if raw is None:
        return None
    t = raw.strip().lower()
    if not t:
        return None
    if t in _TRUE_TOKENS:
        return True
    if t in _FALSE_TOKENS:
        return False
    return None


def serves_unit(item: GoldItem, unit: str) -> bool:
    """item 이 측정 단위를 서빙하는지 — measurement_units 명시 시 그대로, 비면 추론.

    eval_search 의 동일 판정과 일치한다(doc←relevant_doc_ids,
    graph←relevant_graph_entities, answer←reference_answer).
    """
    if item.measurement_units:
        return unit in item.measurement_units
    if unit == "doc":
        return bool(item.relevant_doc_ids)
    if unit == "graph":
        return bool(item.relevant_graph_entities)
    if unit == "answer":
        return bool(item.reference_answer)
    return False


def _item_to_review_row(item: GoldItem) -> dict[str, Any]:
    evidence_spans = "; ".join(
        f.evidence_span for f in item.supporting_facts if f.evidence_span
    )
    facts = "; ".join(
        (
            f"{f.entity} --[{f.relation}]--> {f.target}"
            if f.relation and f.target
            else f.entity
        )
        for f in item.supporting_facts
    )
    row = {
        "id": item.id,
        "source_type": item.source_type,
        "difficulty": item.difficulty,
        "measurement_units": ",".join(item.measurement_units),
        "answerable": "1" if item.answerable else "0",
        "query": item.query,
        "reference_answer": item.reference_answer,
        "evidence_spans": evidence_spans,
        "supporting_facts": facts,
        "relevant_doc_ids": ",".join(str(d) for d in item.relevant_doc_ids),
    }
    # verdict 컬럼은 빈 값으로 둔다 (사람이 채움).
    for col in HUMAN_VERDICT_COLUMNS:
        row[col] = ""
    return row


def export_review_csv(
    gold: GoldSet,
    path: Path,
    *,
    sample_n: int | None = None,
    seed: int | None = None,
) -> int:
    """골드셋을 인간 리뷰용 CSV 로 내보낸다.

    Args:
        gold: 대상 골드셋.
        path: 출력 CSV 경로.
        sample_n: 표본 크기. ``None`` 이면 전체. 지정 시 source_type 층화
            결정론 샘플(소규모 인간 앵커 — 계획서 §7 의 50~100).
        seed: 샘플링 결정성 seed.

    Returns:
        기록된 행 수.
    """
    items = _select_items(gold.items, sample_n=sample_n, seed=seed)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(REVIEW_CSV_COLUMNS))
        writer.writeheader()
        for item in items:
            writer.writerow(_item_to_review_row(item))
    return len(items)


def _select_items(
    items: list[GoldItem],
    *,
    sample_n: int | None,
    seed: int | None,
) -> list[GoldItem]:
    """source_type 층화 결정론 샘플. sample_n None/충분 시 전체(원순서)."""
    if sample_n is None or sample_n >= len(items):
        return list(items)
    if sample_n <= 0:
        return []
    rng = random.Random(seed)
    groups: dict[str, list[GoldItem]] = {}
    for it in items:
        groups.setdefault(it.source_type, []).append(it)
    for g in groups.values():
        rng.shuffle(g)
    # round-robin 으로 그룹에서 균등 추출 → 한쪽 쏠림 방지.
    selected: list[GoldItem] = []
    iters = {k: iter(v) for k, v in sorted(groups.items())}
    exhausted: set[str] = set()
    while len(selected) < sample_n and len(exhausted) < len(iters):
        for k in list(iters.keys()):
            if k in exhausted:
                continue
            try:
                selected.append(next(iters[k]))
                if len(selected) >= sample_n:
                    break
            except StopIteration:
                exhausted.add(k)
    # 출력은 원래 골드 순서를 보존(재현·디버그 편의).
    order = {id(it): i for i, it in enumerate(items)}
    selected.sort(key=lambda it: order[id(it)])
    return selected


def import_review_csv(path: Path) -> dict[str, ReviewVerdict]:
    """리뷰 CSV 를 읽어 ``{id: ReviewVerdict}`` 로 반환한다.

    verdict 컬럼이 없는(미완성) CSV 도 안전하게 읽는다 — 없는 컬럼은 ``None``.
    """
    verdicts: dict[str, ReviewVerdict] = {}
    with open(path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            item_id = str(row.get("id") or "").strip()
            if not item_id:
                continue
            verdicts[item_id] = ReviewVerdict(
                id=item_id,
                valid=parse_verdict_cell(row.get("human_valid")),
                doc_valid=parse_verdict_cell(row.get("human_doc_valid")),
                answer_valid=parse_verdict_cell(row.get("human_answer_valid")),
                graph_valid=parse_verdict_cell(row.get("human_graph_valid")),
                notes=str(row.get("human_notes") or "").strip(),
            )
    return verdicts


def _precision_block(values: list[bool]) -> dict[str, float]:
    """라벨 리스트(True/False)의 정밀도 + 95% CI."""
    nums = [1.0 if v else 0.0 for v in values]
    ci = bootstrap_ci_mean(nums)
    return {
        "precision": ci["mean"],
        "ci_low": ci["ci_low"],
        "ci_high": ci["ci_high"],
        "n_labeled": len(nums),
    }


def generator_precision(
    gold: GoldSet,
    verdicts: dict[str, ReviewVerdict],
) -> dict[str, Any]:
    """인간 verdict 로부터 generator 정밀도(전체 + 단위별)를 산출한다.

    정밀도 = (사람이 타당하다고 라벨한 항목 수) / (라벨된 항목 수). 단위별
    정밀도는 그 단위를 서빙하면서 해당 단위 verdict 가 채워진 항목만 분모로 쓴다.
    미라벨(``None``)은 분모에서 제외 → "검증된 부분의 정밀도".

    Returns:
        ``{"n_total", "n_reviewed", "overall", "by_unit", "answerable_ratio"}``.
        ``overall`` / ``by_unit[unit]`` 은 ``{"precision","ci_low","ci_high",
        "n_labeled"}`` (+ 단위 블록은 ``"n_serving"``).
    """
    items = gold.items
    n_total = len(items)
    # 라벨된(어느 verdict 든 채워진) 항목 수 — 커버리지 진단.
    n_reviewed = sum(
        1 for it in items
        if it.id in verdicts and _has_any_verdict(verdicts[it.id])
    )

    overall_labels = [
        verdicts[it.id].valid
        for it in items
        if it.id in verdicts and verdicts[it.id].valid is not None
    ]
    overall = _precision_block([v for v in overall_labels if v is not None])

    by_unit: dict[str, dict[str, float]] = {}
    for unit in sorted(MEASUREMENT_UNITS):
        serving = [it for it in items if serves_unit(it, unit)]
        labels = [
            verdicts[it.id].unit_valid(unit)
            for it in serving
            if it.id in verdicts and verdicts[it.id].unit_valid(unit) is not None
        ]
        block = _precision_block([v for v in labels if v is not None])
        block["n_serving"] = len(serving)
        by_unit[unit] = block

    answerable_ratio = (
        sum(1 for it in items if it.answerable) / n_total if n_total else 0.0
    )

    return {
        "n_total": n_total,
        "n_reviewed": n_reviewed,
        "overall": overall,
        "by_unit": by_unit,
        "answerable_ratio": answerable_ratio,
    }


def _has_any_verdict(v: ReviewVerdict) -> bool:
    return any(
        x is not None
        for x in (v.valid, v.doc_valid, v.answer_valid, v.graph_valid)
    )
