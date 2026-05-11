"""검색 메트릭 단위 테스트."""

from __future__ import annotations

import math

from context_loop.eval.metrics import (
    aggregate,
    hit_at_k,
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)


# --- recall_at_k ---


def test_recall_at_k_all_found() -> None:
    """모든 정답이 top-k 안에 있으면 1.0."""
    assert recall_at_k([1, 2, 3], [1, 2], k=3) == 1.0


def test_recall_at_k_partial() -> None:
    """정답 2개 중 1개만 top-k 안에 있으면 0.5."""
    assert recall_at_k([1, 99], [1, 2], k=2) == 0.5


def test_recall_at_k_empty_relevant() -> None:
    """정답이 없으면 0.0 (정의 불가 → 0 처리)."""
    assert recall_at_k([1, 2], [], k=2) == 0.0


def test_recall_at_k_truncates() -> None:
    """k 가 결과 길이보다 작으면 그만큼만 본다."""
    # top-1 만 보면 정답 [1,2] 중 하나만 잡혀 0.5
    assert recall_at_k([1, 2, 3], [1, 2], k=1) == 0.5


# --- precision_at_k ---


def test_precision_at_k_basic() -> None:
    assert precision_at_k([1, 2, 3], [1], k=3) == 1 / 3


def test_precision_at_k_zero_k() -> None:
    assert precision_at_k([1, 2], [1], k=0) == 0.0


def test_precision_at_k_empty_retrieved() -> None:
    assert precision_at_k([], [1], k=5) == 0.0


# --- mrr ---


def test_mrr_first_position() -> None:
    """첫 번째가 정답이면 1.0."""
    assert mrr([5, 9, 1], [5]) == 1.0


def test_mrr_second_position() -> None:
    assert mrr([99, 5, 1], [5]) == 0.5


def test_mrr_no_hit() -> None:
    assert mrr([1, 2, 3], [99]) == 0.0


def test_mrr_takes_first_only() -> None:
    """여러 정답이 있어도 첫 정답의 등수만 본다."""
    # 정답 7 이 1위, 9 가 3위 → 1/1 = 1.0
    assert mrr([7, 1, 9], [7, 9]) == 1.0


# --- ndcg_at_k ---


def test_ndcg_at_k_perfect() -> None:
    """모든 정답이 위쪽에 있으면 1.0."""
    assert ndcg_at_k([1, 2, 99], [1, 2], k=3) == 1.0


def test_ndcg_at_k_lower_when_relevant_lower() -> None:
    """정답이 뒤로 밀리면 점수가 낮아진다."""
    high = ndcg_at_k([1, 99, 99], [1], k=3)
    low = ndcg_at_k([99, 99, 1], [1], k=3)
    assert high > low > 0.0


def test_ndcg_at_k_empty_relevant() -> None:
    assert ndcg_at_k([1, 2], [], k=2) == 0.0


def test_ndcg_at_k_zero_k() -> None:
    assert ndcg_at_k([1, 2], [1], k=0) == 0.0


def test_ndcg_at_k_known_value() -> None:
    """수식으로 계산 가능한 케이스."""
    # retrieved=[A, B, C], relevant={A, C}, k=3
    # DCG = 1/log2(2) + 0 + 1/log2(4) = 1.0 + 0.5 = 1.5
    # IDCG = 1/log2(2) + 1/log2(3) ≈ 1.0 + 0.6309 ≈ 1.6309
    val = ndcg_at_k(["A", "B", "C"], ["A", "C"], k=3)
    expected = 1.5 / (1.0 + 1.0 / math.log2(3))
    assert abs(val - expected) < 1e-9


# --- hit_at_k ---


def test_hit_at_k_true() -> None:
    assert hit_at_k([1, 2, 3], [3], k=3) is True


def test_hit_at_k_false_outside_k() -> None:
    """top-k 밖에 정답이 있으면 False."""
    assert hit_at_k([1, 2, 3], [99], k=3) is False
    # k=1 만 보면 1만 보이고 정답 2 는 못 잡음
    assert hit_at_k([1, 2], [2], k=1) is False


# --- aggregate ---


def test_aggregate_averages_numeric_keys() -> None:
    rows = [
        {"id": "q1", "recall@5": 1.0, "mrr": 0.5},
        {"id": "q2", "recall@5": 0.0, "mrr": 0.0},
    ]
    summary = aggregate(rows)
    assert summary["recall@5"] == 0.5
    assert summary["mrr"] == 0.25
    # 비숫자 키 (id) 는 집계 대상 아님
    assert "id" not in summary


def test_aggregate_ignores_bool() -> None:
    """bool 은 isinstance(int) 이지만 집계에서 제외한다."""
    rows = [{"flag": True, "x": 1.0}, {"flag": False, "x": 3.0}]
    summary = aggregate(rows)
    assert "flag" not in summary
    assert summary["x"] == 2.0


def test_aggregate_handles_missing_keys() -> None:
    """일부 행에만 있는 키도 집계 (있는 행만 평균)."""
    rows = [
        {"recall@5": 1.0, "judge_score": 4},
        {"recall@5": 0.0},
    ]
    summary = aggregate(rows)
    assert summary["recall@5"] == 0.5
    assert summary["judge_score"] == 4.0


def test_aggregate_empty() -> None:
    assert aggregate([]) == {}


def test_aggregate_exclude_drops_id_columns() -> None:
    rows = [
        {"source_document_id": 4720, "recall@5": 1.0},
        {"source_document_id": 4721, "recall@5": 0.0},
    ]
    summary = aggregate(rows, exclude={"source_document_id"})
    assert "source_document_id" not in summary
    assert summary["recall@5"] == 0.5
