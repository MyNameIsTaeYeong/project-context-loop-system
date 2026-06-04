"""Phase 4 — 절대 점수 보고 모드(absolute-mode) 단위 테스트.

검증 항목:
  (i)  요건 위반(시드/표본/비편향/앵커) 이 정확히 잡힌다.
  (ii) 모든 요건 충족 시 위반 없음.
  (iii) write_summary(absolute_mode=True) 가 청크 메트릭별 CI 를 동반한다.
  (iv) 그래프/judge 메트릭은 CI 대상에서 제외된다.
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
        include_graph=True,
        planner_seed_base=2000,
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


def test_missing_planner_seed_when_graph() -> None:
    kw = _ok_kwargs()
    kw["planner_seed_base"] = None
    v = eval_search.check_absolute_mode_requirements(**kw)
    assert any("planner-seed-base" in x for x in v)


def test_planner_seed_not_required_without_graph() -> None:
    kw = _ok_kwargs()
    kw["include_graph"] = False
    kw["planner_seed_base"] = None
    v = eval_search.check_absolute_mode_requirements(**kw)
    assert not any("planner-seed-base" in x for x in v)


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
    # graph_* / judge 는 청크 CI 대상에서 제외.
    assert "graph_recall@5" not in cis
    assert "judge_score" not in cis


def test_summary_ci_excludes_failed_rows() -> None:
    rows = [
        {"id": "q1", "recall@5": 1.0, "metric_failed": False},
        {"id": "q2", "recall@5": None, "metric_failed": True},
    ]
    cis = eval_search._chunk_metric_cis(rows)
    assert cis["recall@5"]["n"] == 1
