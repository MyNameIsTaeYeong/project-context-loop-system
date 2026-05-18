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

import math
from collections.abc import Iterable, Sequence
from typing import TypeVar

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
    """
    if not per_run_summaries:
        return {}

    keys: set[str] = set()
    for s in per_run_summaries:
        keys.update(s.keys())

    out: dict[str, dict[str, float]] = {}
    for k in sorted(keys):
        values = [float(s[k]) for s in per_run_summaries if k in s]
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
    return out
