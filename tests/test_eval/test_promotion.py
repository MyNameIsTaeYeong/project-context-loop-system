"""잠정→절대 승격 보고 테스트 (PR #79 P6)."""

from __future__ import annotations

from context_loop.eval.promotion import (
    build_promotion_report,
    render_promotion_report,
)


def _ci(mean: float, n: int = 40) -> dict:  # type: ignore[type-arg]
    return {"mean": mean, "ci_low": mean - 0.05, "ci_high": mean + 0.05, "n": n}


def _absolute_inputs() -> dict:  # type: ignore[type-arg]
    """모든 승격 기준을 만족하는 입력."""
    eval_summary = {
        "n_successful": 40,
        "metrics": {
            "context_recall@5": 0.7,
            "graph_recall_surface@5": 0.5,
            "answer_correct": 0.8,
        },
        "metric_ci": {
            "context_recall@5": _ci(0.7),
            "graph_recall_surface@5": _ci(0.5),
            "answer_correct": _ci(0.8),
        },
    }
    precision = {
        "answerable_ratio": 0.95,
        "n_reviewed": 60,
        "by_unit": {
            "doc": {"precision": 0.95, "ci_low": 0.9, "ci_high": 1.0, "n_labeled": 60},
            "answer": {"precision": 0.88, "ci_low": 0.8, "ci_high": 0.95, "n_labeled": 55},
            "graph": {"precision": 0.72, "ci_low": 0.6, "ci_high": 0.84, "n_labeled": 50},
        },
    }
    gold_metadata = {
        "extraction_configured_separately": True,
        "generator_configured_separately": True,
        "judge_configured_separately": True,
        "extraction_model": "ext-m", "generator_model": "gen-m",
        "judge_model": "jdg-m", "seed": 7,
    }
    return {
        "eval_summary": eval_summary,
        "precision": precision,
        "gold_metadata": gold_metadata,
    }


def test_all_units_absolute_when_criteria_met() -> None:
    report = build_promotion_report(**_absolute_inputs())
    assert report["overall_label"] == "absolute"
    for unit in ("doc", "answer", "graph"):
        assert report["units"][unit]["label"] == "absolute"
        assert report["units"][unit]["missing"] == []
    assert report["models_separated"] is True
    assert report["answerable_ratio"] == 0.95
    assert report["provenance"]["extraction_model"] == "ext-m"


def test_missing_ci_makes_provisional() -> None:
    inp = _absolute_inputs()
    inp["eval_summary"].pop("metric_ci")
    report = build_promotion_report(**inp)
    assert report["overall_label"] == "provisional"
    assert "ci" in report["units"]["doc"]["missing"]


def test_missing_human_precision_makes_provisional() -> None:
    inp = _absolute_inputs()
    # graph 라벨 수를 임계 미만으로
    inp["precision"]["by_unit"]["graph"]["n_labeled"] = 5
    report = build_promotion_report(**inp, min_precision_labeled=20)
    assert report["units"]["graph"]["label"] == "provisional"
    assert "human_precision" in report["units"]["graph"]["missing"]
    # doc/answer 는 여전히 절대 → 전체는 graph 때문에 잠정
    assert report["units"]["doc"]["label"] == "absolute"
    assert report["overall_label"] == "provisional"


def test_models_not_separated_makes_provisional() -> None:
    inp = _absolute_inputs()
    inp["gold_metadata"]["extraction_configured_separately"] = False
    report = build_promotion_report(**inp)
    assert report["models_separated"] is False
    for unit in report["units"].values():
        assert "model_separation" in unit["missing"]
        assert unit["label"] == "provisional"


def test_models_separated_none_when_metadata_absent() -> None:
    inp = _absolute_inputs()
    inp["gold_metadata"] = {}  # 분리 플래그 없음(레거시 골드)
    report = build_promotion_report(**inp)
    assert report["models_separated"] is None
    assert "model_separation" in report["units"]["doc"]["missing"]


def test_small_eval_sample_makes_provisional() -> None:
    inp = _absolute_inputs()
    inp["eval_summary"]["n_successful"] = 10
    report = build_promotion_report(**inp, min_eval_n=30)
    assert "eval_sample_size" in report["units"]["doc"]["missing"]


def test_unit_not_measured_is_excluded() -> None:
    """eval 메트릭에 없는 단위는 보고에서 제외된다."""
    inp = _absolute_inputs()
    del inp["eval_summary"]["metrics"]["graph_recall_surface@5"]
    report = build_promotion_report(**inp)
    assert "graph" not in report["units"]
    assert "doc" in report["units"]


def test_answer_correct_prefers_exact_over_correctness() -> None:
    """answer_correct(정확도)를 answer_correctness 보다 우선 채택."""
    inp = _absolute_inputs()
    inp["eval_summary"]["metrics"]["answer_correctness"] = 0.6
    inp["eval_summary"]["metric_ci"]["answer_correctness"] = _ci(0.6)
    report = build_promotion_report(**inp)
    assert report["units"]["answer"]["metric_key"] == "answer_correct"
    assert report["units"]["answer"]["value"] == 0.8


def test_graph_falls_back_to_fuzzy_when_no_surface() -> None:
    inp = _absolute_inputs()
    m = inp["eval_summary"]["metrics"]
    ci = inp["eval_summary"]["metric_ci"]
    m["graph_recall@5"] = 0.55
    ci["graph_recall@5"] = _ci(0.55)
    del m["graph_recall_surface@5"]
    del ci["graph_recall_surface@5"]
    report = build_promotion_report(**inp)
    assert report["units"]["graph"]["metric_key"] == "graph_recall@5"


def test_render_promotion_report_smoke() -> None:
    report = build_promotion_report(**_absolute_inputs())
    text = render_promotion_report(report)
    assert "ABSOLUTE" in text
    assert "answerable" in text
    assert "context_recall@5" in text
    # 절대면 잠정 경고가 없어야 함
    assert "잠정 기준선" not in text


def test_render_provisional_shows_warning() -> None:
    inp = _absolute_inputs()
    inp["eval_summary"].pop("metric_ci")
    text = render_promotion_report(build_promotion_report(**inp))
    assert "잠정 기준선" in text
    assert "PROVISIONAL" in text
