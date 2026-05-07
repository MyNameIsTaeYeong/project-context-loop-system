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
