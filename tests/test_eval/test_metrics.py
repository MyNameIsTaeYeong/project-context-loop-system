"""검색 메트릭 단위 테스트."""

from __future__ import annotations

import math

from context_loop.eval.metrics import (
    aggregate,
    aggregate_with_variance,
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


# --- aggregate_with_variance ---


def test_aggregate_with_variance_empty() -> None:
    assert aggregate_with_variance([]) == {}


def test_aggregate_with_variance_single_run() -> None:
    """n=1 이면 std 는 0, min=max=mean."""
    out = aggregate_with_variance([{"recall@5": 0.6, "mrr": 0.4}])
    assert out["recall@5"] == {
        "mean": 0.6, "std": 0.0, "min": 0.6, "max": 0.6, "n": 1,
    }
    assert out["mrr"]["std"] == 0.0


def test_aggregate_with_variance_basic_stats() -> None:
    """mean/std/min/max 가 알려진 케이스에 맞는다.

    [0.6, 0.5, 0.7] → mean=0.6, 표본 분산=(0.01+0+0.01)/2=0.01, std=0.1.
    """
    runs = [
        {"recall@5": 0.6, "mrr": 0.5},
        {"recall@5": 0.5, "mrr": 0.4},
        {"recall@5": 0.7, "mrr": 0.6},
    ]
    out = aggregate_with_variance(runs)
    r = out["recall@5"]
    assert abs(r["mean"] - 0.6) < 1e-9
    assert abs(r["std"] - 0.1) < 1e-9
    assert r["min"] == 0.5
    assert r["max"] == 0.7
    assert r["n"] == 3
    # mrr 도 동일 패턴
    m = out["mrr"]
    assert abs(m["mean"] - 0.5) < 1e-9
    assert abs(m["std"] - 0.1) < 1e-9


def test_aggregate_with_variance_handles_missing_keys() -> None:
    """일부 잡에만 있는 메트릭(예: judge_score) 도 가진 잡만 모아 통계."""
    runs = [
        {"recall@5": 0.6, "judge_score": 4.0},
        {"recall@5": 0.4},
        {"recall@5": 0.5, "judge_score": 5.0},
    ]
    out = aggregate_with_variance(runs)
    assert out["recall@5"]["n"] == 3
    # judge_score 는 잡 2개만 있음
    assert out["judge_score"]["n"] == 2
    assert abs(out["judge_score"]["mean"] - 4.5) < 1e-9


def test_aggregate_with_variance_zero_variance() -> None:
    """모든 잡 값이 같으면 std=0."""
    runs = [{"x": 0.5}, {"x": 0.5}, {"x": 0.5}]
    out = aggregate_with_variance(runs)
    assert out["x"]["std"] == 0.0
    assert out["x"]["mean"] == 0.5


def test_aggregate_with_variance_ignores_dict_values() -> None:
    """``metrics`` dict 안에 ``graph_match_tiers_total`` 같은 nested dict 가
    있어도 TypeError 없이 숫자 키만 집계한다. 호출자(eval_search.py)가
    각 골드셋 metrics 에 dict 값을 섞어 넣어도 안전해야 한다 — 이전에는
    ``float({...})`` 에서 ``TypeError: float() argument must be a string or
    real number, not 'dict'`` 가 발생했다."""
    runs = [
        {
            "recall@5": 0.7,
            "mrr": 0.5,
            "graph_match_tiers_total": {"T1": 10, "T2": 5},
            "graph_rel_match_tiers_total": {"T1": 3, "T2": 2},
        },
        {
            "recall@5": 0.8,
            "mrr": 0.6,
            "graph_match_tiers_total": {"T1": 12, "T2": 4},
        },
    ]
    out = aggregate_with_variance(runs)
    # 숫자 키는 정상 집계
    assert "recall@5" in out
    assert "mrr" in out
    assert abs(out["recall@5"]["mean"] - 0.75) < 1e-9
    # 비숫자 키(dict)는 결과에 들어가지 않음 — silent skip
    assert "graph_match_tiers_total" not in out
    assert "graph_rel_match_tiers_total" not in out


def test_aggregate_with_variance_ignores_non_numeric_types() -> None:
    """str, list, bool 같은 비숫자 값도 silently skip 한다 (aggregate 와 동일 정책)."""
    runs = [
        {"x": 1.0, "label": "run1", "flags": [True, False], "active": True},
        {"x": 2.0, "label": "run2", "flags": [True], "active": False},
    ]
    out = aggregate_with_variance(runs)
    assert out["x"]["mean"] == 1.5
    # 비숫자 키 모두 skip
    assert "label" not in out
    assert "flags" not in out
    assert "active" not in out  # bool 은 numeric 으로 취급 안 함 (aggregate 와 일관)


# ---------------------------------------------------------------------------
# Generic 메트릭이 (name, type) 튜플 키로도 동작 (R1 — graph 채점)
# ---------------------------------------------------------------------------


def test_recall_at_k_works_with_tuple_keys() -> None:
    """그래프 채점에서 (name.lower(), type) 튜플 키로 호출 가능."""
    retrieved: list[tuple[str, str]] = [
        ("인증 서비스", "system"),
        ("결제 팀", "team"),
        ("vpc", "concept"),
    ]
    relevant = {("인증 서비스", "system"), ("결제 팀", "team")}
    assert recall_at_k(retrieved, relevant, k=3) == 1.0
    assert precision_at_k(retrieved, relevant, k=3) == 2 / 3


def test_mrr_works_with_tuple_keys() -> None:
    retrieved: list[tuple[str, str]] = [
        ("x", "system"),
        ("인증 서비스", "system"),
    ]
    relevant = {("인증 서비스", "system")}
    assert mrr(retrieved, relevant) == 0.5


def test_hit_at_k_works_with_tuple_keys() -> None:
    retrieved: list[tuple[str, str]] = [("a", "t"), ("b", "t")]
    relevant = {("c", "t")}
    assert hit_at_k(retrieved, relevant, k=2) is False
    assert hit_at_k([("a", "t")], {("a", "t")}, k=1) is True


def test_ndcg_at_k_works_with_tuple_keys() -> None:
    retrieved: list[tuple[str, str]] = [("a", "system"), ("b", "team")]
    relevant = {("b", "team")}
    val = ndcg_at_k(retrieved, relevant, k=2)
    expected = (1.0 / math.log2(3)) / 1.0
    assert abs(val - expected) < 1e-9
