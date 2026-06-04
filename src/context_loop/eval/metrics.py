"""검색 품질 정량 메트릭.

모든 함수는 결정론적 순수 함수로 작성되어 단위 테스트가 쉽다.
입력은 "검색 결과 ID 리스트(순서 보존)" + "정답 ID 집합" 형태로 통일한다.

사용 예::

    retrieved = [200, 142, 78, 89, 31]      # top-5 검색 결과 (순서대로)
    relevant = {142, 89}                     # 정답 문서 집합
    recall_at_k(retrieved, relevant, k=5)   # 1.0
    mrr(retrieved, relevant)                 # 0.5  (첫 정답이 2위)
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Sequence
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def recall_at_k(retrieved: Sequence[T], relevant: Iterable[T], k: int) -> float:
    """top-k 안에 포함된 정답 비율.

    정답이 0개면 0.0 을 반환한다 (정의 불가 → 0 처리).
    """
    rel_set = set(relevant)
    if not rel_set:
        return 0.0
    top_k = set(retrieved[:k])
    return len(top_k & rel_set) / len(rel_set)


def precision_at_k(retrieved: Sequence[T], relevant: Iterable[T], k: int) -> float:
    """top-k 중 정답 비율.

    k 가 0 이면 0.0 을 반환한다.
    """
    if k <= 0:
        return 0.0
    rel_set = set(relevant)
    top_k = list(retrieved[:k])
    if not top_k:
        return 0.0
    hits = sum(1 for r in top_k if r in rel_set)
    return hits / k


def mrr(retrieved: Sequence[T], relevant: Iterable[T]) -> float:
    """Mean Reciprocal Rank — 첫 정답의 등수 역수.

    검색 결과 안에 정답이 하나도 없으면 0.0.
    """
    rel_set = set(relevant)
    for i, r in enumerate(retrieved, start=1):
        if r in rel_set:
            return 1.0 / i
    return 0.0


def ndcg_at_k(retrieved: Sequence[T], relevant: Iterable[T], k: int) -> float:
    """Normalized Discounted Cumulative Gain @ k.

    이 시스템은 binary relevance (정답/오답) 만 다루므로, gain 은 1/0.
    DCG = sum( rel_i / log2(i+1) ), IDCG 는 정답 m=min(|relevant|, k)개를
    1~m 위에 배치한 이상적 DCG.
    """
    if k <= 0:
        return 0.0
    rel_set = set(relevant)
    if not rel_set:
        return 0.0

    dcg = 0.0
    for i, r in enumerate(retrieved[:k], start=1):
        if r in rel_set:
            dcg += 1.0 / math.log2(i + 1)

    ideal_hits = min(len(rel_set), k)
    idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
    if idcg == 0.0:
        return 0.0
    return dcg / idcg


def hit_at_k(retrieved: Sequence[T], relevant: Iterable[T], k: int) -> bool:
    """top-k 안에 정답이 하나라도 있는가."""
    rel_set = set(relevant)
    return any(r in rel_set for r in retrieved[:k])


def aggregate(
    rows: list[dict[str, float]],
    *,
    exclude: Iterable[str] = (),
) -> dict[str, float]:
    """질의별 메트릭 dict 리스트의 평균을 계산한다.

    각 dict 의 모든 숫자 키에 대해 평균을 낸다. 비숫자 키는 무시.
    ``exclude`` 에 들어간 키는 숫자여도 집계에서 제외한다 — 식별자(예:
    ``source_document_id``) 처럼 평균에 의미가 없는 컬럼을 거르는 용도.
    """
    if not rows:
        return {}
    excluded = set(exclude)
    keys: set[str] = set()
    for r in rows:
        for k, v in r.items():
            if k in excluded:
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                keys.add(k)

    out: dict[str, float] = {}
    for k in keys:
        values = [
            float(r[k]) for r in rows
            if k in r and isinstance(r[k], (int, float)) and not isinstance(r[k], bool)
        ]
        if values:
            out[k] = sum(values) / len(values)
    return out


def bootstrap_ci_mean(
    values: Iterable[float],
    *,
    n_resample: int = 1000,
    alpha: float = 0.05,
    seed: int = 42,
) -> dict[str, float]:
    """단일 표본 평균의 부트스트랩 신뢰구간을 계산한다.

    절대 점수에 불확실성을 동반시키기 위한 용도 — per-query 메트릭 값 리스트를
    받아 (평균, (1-alpha) 신뢰구간 하한/상한) 을 반환한다. ``seed`` 고정으로
    재현 가능. scipy 의존 없이 stdlib ``random`` 만 사용한다.

    Args:
        values: per-query 메트릭 값들.
        n_resample: 부트스트랩 리샘플 횟수.
        alpha: 유의수준(0.05 → 95% CI).
        seed: 재현성 seed.

    Returns:
        ``{"mean", "ci_low", "ci_high", "n"}``. 값이 없으면 모두 0.0.
    """
    import random

    vals = [
        float(v) for v in values
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    ]
    n = len(vals)
    if n == 0:
        return {"mean": 0.0, "ci_low": 0.0, "ci_high": 0.0, "n": 0}
    mean = sum(vals) / n
    if n == 1:
        return {"mean": mean, "ci_low": mean, "ci_high": mean, "n": 1}

    rng = random.Random(seed)
    resample_means: list[float] = []
    for _ in range(n_resample):
        total = 0.0
        for _ in range(n):
            total += vals[rng.randrange(n)]
        resample_means.append(total / n)
    resample_means.sort()
    lo_idx = max(0, int((alpha / 2.0) * n_resample))
    hi_idx = min(n_resample - 1, int((1.0 - alpha / 2.0) * n_resample))
    return {
        "mean": mean,
        "ci_low": resample_means[lo_idx],
        "ci_high": resample_means[hi_idx],
        "n": n,
    }


def aggregate_with_variance(
    per_run_summaries: list[dict[str, float]],
) -> dict[str, dict[str, float]]:
    """여러 골드셋의 ``aggregate`` 결과를 모아 평균/편차를 낸다.

    같은 검색 시스템을 N개의 골드셋에 돌렸을 때 메트릭 변동성을 측정하기 위한
    상위 집계 함수. 입력은 각 골드셋의 ``aggregate`` 출력 dict 리스트, 출력은
    ::

        {
            "recall@5": {"mean": .., "std": .., "min": .., "max": .., "n": ..},
            "mrr":      {"mean": .., ...},
            ...
        }

    어떤 골드셋엔 있고 다른 데엔 없는 키는 가진 골드셋만 모아 통계를 낸다
    (judge 비활성 잡 등). 표준편차는 ``n>=2`` 일 때만 계산하며, n=1 이면 0.0.
    표본 표준편차 (n-1 분모, ddof=1) 를 사용해 작은 N 의 편차 과소추정을 방지.

    호출자가 ``metrics`` dict 안에 ``graph_match_tiers_total`` 같은 nested
    dict / list 값을 넣어 전달할 수 있다 (eval_search.py 가 그렇다). 이런
    비숫자 값은 평균/표준편차 정의가 없으므로 무시한다 — ``aggregate`` 와
    동일한 정책. 무시되는 키는 ``logger.debug`` 로 한 번씩 보고하여 디버깅
    시 누락을 인지 가능하게 한다.
    """
    if not per_run_summaries:
        return {}

    keys: set[str] = set()
    for s in per_run_summaries:
        keys.update(s.keys())

    out: dict[str, dict[str, float]] = {}
    skipped_keys: set[str] = set()
    for k in sorted(keys):
        numeric_values: list[float] = []
        has_nonnumeric = False
        for s in per_run_summaries:
            if k not in s:
                continue
            v = s[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                has_nonnumeric = True
                continue
            numeric_values.append(float(v))
        if has_nonnumeric and not numeric_values:
            skipped_keys.add(k)
        values = numeric_values
        if not values:
            continue
        n = len(values)
        mean = sum(values) / n
        if n >= 2:
            # ddof=1 표본 분산
            var = sum((v - mean) ** 2 for v in values) / (n - 1)
            std = math.sqrt(var)
        else:
            std = 0.0
        out[k] = {
            "mean": mean,
            "std": std,
            "min": min(values),
            "max": max(values),
            "n": n,
        }
    if skipped_keys:
        logger.debug(
            "aggregate_with_variance: 비숫자(dict/list/str) 값만 있는 키 무시 — %s",
            sorted(skipped_keys),
        )
    return out
