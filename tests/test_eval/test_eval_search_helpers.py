"""eval_search 헬퍼 단위 테스트 — S1-5(실패행 graph 키), S1-6(per-pair 증거).

검증 항목:
  (i)   ``_failed_metric_keys`` 가 chunk 키는 항상, graph 키는 has_graph 일 때만
        None 으로 채운다 (chunk-only 질의는 graph 키 미포함).
  (ii)  ``_build_match_pairs`` 가 MatchReport.results 로부터 per-pair 증거를
        JSON 직렬화 가능 list 로 구성한다.
  (iii) graph_match_pairs / graph_match_tiers 등 list/dict 값은 CI 대상에서
        자연 제외된다 (_is_ci_metric).
"""

from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))
if str(_PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))

import eval_search  # type: ignore[import-not-found]  # noqa: E402

from context_loop.eval.gold_set import GraphEntityRef  # noqa: E402
from context_loop.eval.graph_match import run_entity_matching  # noqa: E402


def _embed(_text: str) -> list[float] | None:
    return None  # T4 비활성 — 표면 tier 만으로 테스트


# ---------------------------------------------------------------------------
# S1-5 — 실패 질의 row 의 표준 메트릭 None 키
# ---------------------------------------------------------------------------


def test_failed_metric_keys_chunk_only() -> None:
    """chunk-only 실패 질의 — chunk 키만 None, graph 키 미포함."""
    keys = eval_search._failed_metric_keys(5, has_graph=False)
    assert keys["recall@5"] is None
    assert keys["precision@5"] is None
    assert keys["hit@5"] is None
    assert keys["ndcg@5"] is None
    assert keys["mrr"] is None
    # graph 정답 없는 질의는 graph 키를 넣지 않는다 (분류 로직 존중).
    assert not any(k.startswith("graph_") for k in keys)


def test_failed_metric_keys_graph_item() -> None:
    """graph 정답 보유 실패 질의 — 수치 graph 메트릭도 None 명시."""
    keys = eval_search._failed_metric_keys(5, has_graph=True)
    for k in (
        "graph_recall@5", "graph_recall_surface@5",
        "graph_precision@5", "graph_precision_surface@5",
        "graph_hit@5", "graph_hit_surface@5",
        "graph_ndcg@5", "graph_ndcg_surface@5",
        "graph_mrr", "graph_mrr_surface",
    ):
        assert k in keys, f"{k} 누락"
        assert keys[k] is None
    # chunk 키도 함께 존재.
    assert keys["recall@5"] is None


def test_failed_graph_keys_excluded_from_average() -> None:
    """실패행의 graph None 키는 aggregate 평균에서 자동 스킵된다."""
    rows = [
        {
            "mode": "graph",
            "graph_recall@5": 1.0,
            "metric_failed": False,
        },
        {
            "mode": "graph",
            "metric_failed": True,
            **eval_search._failed_metric_keys(5, has_graph=True),
        },
    ]
    from context_loop.eval.metrics import aggregate

    out = aggregate(rows)
    # None 행 제외 → 성공 1개의 1.0 만 평균.
    assert out["graph_recall@5"] == 1.0


# ---------------------------------------------------------------------------
# S1-6 — per-pair 매칭 증거
# ---------------------------------------------------------------------------


def _ge(name: str, type_: str) -> GraphEntityRef:
    return GraphEntityRef(name=name, type=type_)


def test_build_match_pairs_records_matched_goldens() -> None:
    """매칭된 골든마다 golden_name/type/retrieved_index/tier/score 기록."""
    golden = [
        _ge("인증 서비스", "system"),  # 매칭
        _ge("없는 엔티티", "system"),  # 미매칭 → 제외
    ]
    retrieved = [_ge("인증 서비스", "system")]
    report = run_entity_matching(golden, retrieved, embed_fn=_embed)
    pairs = eval_search._build_match_pairs(golden, report)
    assert len(pairs) == 1
    pair = pairs[0]
    assert pair["golden_name"] == "인증 서비스"
    assert pair["golden_type"] == "system"
    assert pair["retrieved_index"] == 0
    assert pair["tier"] == "exact"
    assert pair["score"] == 1.0


def test_build_match_pairs_empty_when_no_match() -> None:
    """전부 미매칭이면 빈 list."""
    golden = [_ge("A", "system")]
    retrieved = [_ge("B", "service")]
    report = run_entity_matching(golden, retrieved, embed_fn=_embed)
    assert eval_search._build_match_pairs(golden, report) == []


def test_match_pairs_not_a_ci_metric() -> None:
    """graph_match_pairs(list) 는 CI 집계 대상에서 자연 제외된다."""
    rows = [
        {
            "mode": "graph",
            "metric_failed": False,
            "graph_recall@5": 1.0,
            "graph_match_pairs": [
                {"golden_name": "X", "golden_type": "system",
                 "retrieved_index": 0, "tier": "exact", "score": 1.0},
            ],
        },
    ]
    cis = eval_search._chunk_metric_cis(rows)
    assert "graph_match_pairs" not in cis
    # 수치 graph 메트릭은 CI 에 포함.
    assert "graph_recall@5" in cis


# ---------------------------------------------------------------------------
# source-grounded (PR #79 P4) — 측정 단위 일급화 + answerable 위생
# ---------------------------------------------------------------------------


def _gold_item(**kw):  # type: ignore[no-untyped-def]
    from context_loop.eval.gold_set import GoldItem
    base = {"id": "q1", "query": "?"}
    base.update(kw)
    return GoldItem(**base)


def test_serves_unit_explicit_measurement_units() -> None:
    item = _gold_item(measurement_units=["doc", "graph"])
    assert eval_search._serves_unit(item, "doc") is True
    assert eval_search._serves_unit(item, "graph") is True
    assert eval_search._serves_unit(item, "answer") is False


def test_serves_unit_legacy_inference() -> None:
    """measurement_units 가 비면 정답키 보유로 단위를 추론한다."""
    item = _gold_item(relevant_doc_ids=[1])
    assert eval_search._serves_unit(item, "doc") is True
    assert eval_search._serves_unit(item, "graph") is False
    item2 = _gold_item(relevant_doc_ids=[], reference_answer="답")
    assert eval_search._serves_unit(item2, "answer") is True
    assert eval_search._serves_unit(item2, "doc") is False


def test_write_summary_answerable_hygiene(tmp_path: Path) -> None:
    """answerable=False 행은 메트릭 평균 분모에서 제외되고 별도 보고된다."""
    rows = [
        {"id": "a", "mode": "chunk", "recall@5": 1.0,
         "measurement_units": ["doc"], "answerable": True},
        {"id": "b", "mode": "chunk", "recall@5": 1.0,
         "measurement_units": ["doc"], "answerable": True},
        # 회수 불가 표적 — recall 0 이지만 분모에서 제외돼야 함
        {"id": "c", "mode": "chunk", "recall@5": 0.0,
         "measurement_units": ["doc"], "answerable": False},
    ]
    out = eval_search.write_summary(
        rows, tmp_path / "s.summary.json",
        label="t", config_summary={},
    )
    # answerable=2개만 평균 → recall 1.0 (c 의 0.0 제외)
    assert out["metrics"]["recall@5"] == 1.0
    assert out["n_unanswerable"] == 1
    assert out["unanswerable_ids"] == ["c"]
    assert out["measurement_unit_coverage"]["doc"] == 3


def test_write_summary_legacy_rows_unchanged(tmp_path: Path) -> None:
    """answerable/measurement_units 없는 레거시 행은 전부 분모에 포함(무변경)."""
    rows = [
        {"id": "a", "mode": "chunk", "recall@5": 1.0},
        {"id": "b", "mode": "chunk", "recall@5": 0.0},
    ]
    out = eval_search.write_summary(
        rows, tmp_path / "s.summary.json",
        label="t", config_summary={},
    )
    assert out["metrics"]["recall@5"] == 0.5  # 둘 다 포함
    assert out["n_unanswerable"] == 0
    assert out["measurement_unit_coverage"] == {"doc": 0, "answer": 0, "graph": 0}


def test_context_recall_is_ci_metric() -> None:
    """context_recall@k 가 bootstrap CI 대상에 포함된다."""
    rows = [
        {"context_recall@5": 1.0}, {"context_recall@5": 0.0},
        {"context_recall@5": 1.0}, {"context_recall@5": 1.0},
    ]
    cis = eval_search._chunk_metric_cis(rows)
    assert "context_recall@5" in cis
    assert "mean" in cis["context_recall@5"]
