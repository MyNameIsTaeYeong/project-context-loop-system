"""Phase 4 — 절대 점수 보고 모드(absolute-mode) 단위 테스트.

검증 항목:
  (i)  요건 위반(시드/표본/비편향/앵커) 이 정확히 잡힌다.
  (ii) 모든 요건 충족 시 위반 없음.
  (iii) write_summary(absolute_mode=True) 가 청크 메트릭별 CI 를 동반한다.
  (iv) 그래프 수치형 메트릭(표면 포함)은 CI 대상에 포함, judge / dict / bool
       키는 제외된다(R1).
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


def _ok_kwargs() -> dict:
    return dict(
        judge_enabled=True,
        judge_is_self=False,
        allow_self_judge=False,
        judge_seed_base=1000,
        judge_n_samples=3,
        vector_store_sha256="abc123",
    )


def test_all_requirements_met() -> None:
    assert eval_search.check_absolute_mode_requirements(**_ok_kwargs()) == []


def test_missing_judge_seed_base() -> None:
    kw = _ok_kwargs()
    kw["judge_seed_base"] = None
    v = eval_search.check_absolute_mode_requirements(**kw)
    assert any("judge-seed-base" in x for x in v)


def test_low_n_samples() -> None:
    kw = _ok_kwargs()
    kw["judge_n_samples"] = 1
    v = eval_search.check_absolute_mode_requirements(**kw)
    assert any("judge-n-samples" in x for x in v)


def test_self_judge_blocked() -> None:
    kw = _ok_kwargs()
    kw["judge_is_self"] = True
    v = eval_search.check_absolute_mode_requirements(**kw)
    assert any("self-evaluation" in x for x in v)


def test_self_judge_allowed_optin() -> None:
    kw = _ok_kwargs()
    kw["judge_is_self"] = True
    kw["allow_self_judge"] = True
    v = eval_search.check_absolute_mode_requirements(**kw)
    assert not any("self-evaluation" in x for x in v)


def test_empty_fingerprint_blocked() -> None:
    kw = _ok_kwargs()
    kw["vector_store_sha256"] = ""
    v = eval_search.check_absolute_mode_requirements(**kw)
    assert any("앵커링" in x or "지문" in x for x in v)


def test_summary_attaches_chunk_ci() -> None:
    rows = [
        {"id": "q1", "recall@5": 1.0, "precision@5": 0.5, "mrr": 1.0,
         "graph_recall@5": 0.0, "judge_score": 4.0, "mode": "chunk"},
        {"id": "q2", "recall@5": 0.0, "precision@5": 0.2, "mrr": 0.5,
         "graph_recall@5": 0.0, "judge_score": 3.0, "mode": "chunk"},
        {"id": "q3", "recall@5": 1.0, "precision@5": 0.4, "mrr": 0.33,
         "graph_recall@5": 1.0, "judge_score": 5.0, "mode": "chunk"},
    ]
    cis = eval_search._chunk_metric_cis(rows)
    # 청크 메트릭은 CI 포함.
    assert "recall@5" in cis and "precision@5" in cis and "mrr" in cis
    for key in ("recall@5", "precision@5", "mrr"):
        assert set(cis[key]) >= {"mean", "ci_low", "ci_high", "n"}
        assert cis[key]["ci_low"] <= cis[key]["mean"] <= cis[key]["ci_high"]
    # R1 — 그래프 수치형 메트릭도 이제 CI 포함.
    assert "graph_recall@5" in cis
    assert set(cis["graph_recall@5"]) >= {"mean", "ci_low", "ci_high", "n"}
    assert (
        cis["graph_recall@5"]["ci_low"]
        <= cis["graph_recall@5"]["mean"]
        <= cis["graph_recall@5"]["ci_high"]
    )
    # judge 점수는 메트릭이 아니므로 여전히 CI 대상에서 제외.
    assert "judge_score" not in cis


def test_summary_ci_includes_surface_and_graph_metrics() -> None:
    """R2/R3 — 표면 메트릭, R1 — 그래프 메트릭이 CI 대상에 포함."""
    rows = [
        {"id": "q1", "mode": "graph",
         "graph_recall@5": 1.0, "graph_recall_surface@5": 1.0,
         "graph_hit@5": 1, "graph_hit_surface@5": 1,
         "graph_ndcg@5": 0.8, "graph_ndcg_surface@5": 0.7,
         "graph_mrr": 1.0, "graph_mrr_surface": 1.0,
         "graph_match_tiers": {"exact": 1, "embedding": 0},
         "graph_match_score_avg": 1.0,
         "graph_t4_disabled": False},
        {"id": "q2", "mode": "graph",
         "graph_recall@5": 0.5, "graph_recall_surface@5": 0.0,
         "graph_hit@5": 1, "graph_hit_surface@5": 0,
         "graph_ndcg@5": 0.4, "graph_ndcg_surface@5": 0.0,
         "graph_mrr": 0.5, "graph_mrr_surface": 0.0,
         "graph_match_tiers": {"exact": 0, "embedding": 1},
         "graph_match_score_avg": 0.7,
         "graph_t4_disabled": False},
    ]
    cis = eval_search._chunk_metric_cis(rows)
    for key in (
        "graph_recall@5", "graph_recall_surface@5",
        "graph_hit@5", "graph_hit_surface@5",
        "graph_ndcg@5", "graph_ndcg_surface@5",
        "graph_mrr", "graph_mrr_surface",
    ):
        assert key in cis, key
        assert set(cis[key]) >= {"mean", "ci_low", "ci_high", "n"}
        assert cis[key]["n"] == 2
        assert cis[key]["ci_low"] <= cis[key]["mean"] <= cis[key]["ci_high"]
    # dict / bool / score 시그널은 CI 대상에서 제외.
    assert "graph_match_tiers" not in cis
    assert "graph_t4_disabled" not in cis
    assert "graph_match_score_avg" not in cis


def test_summary_ci_excludes_failed_rows() -> None:
    rows = [
        {"id": "q1", "recall@5": 1.0, "metric_failed": False},
        {"id": "q2", "recall@5": None, "metric_failed": True},
    ]
    cis = eval_search._chunk_metric_cis(rows)
    assert cis["recall@5"]["n"] == 1
