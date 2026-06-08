"""잠정→절대 승격 보고 — PR #79 P6.

source-grounded 골드의 절대값은 갖춰지기 전엔 "잠정 기준선"이다(계획서 §7).
절대 인용 시 **반드시 함께 보고**해야 하는 것:

    값 + 95% CI + generator 정밀도(인간검증) + answerable 비율 + provenance(4모델 분리)

이 모듈은 세 산출물을 묶어 **단위별(doc/answer/graph) 잠정/절대 라벨**과 승격
보고를 만든다:

- P4 채점: eval 요약(metrics + metric_ci) — 값 + 95% CI
- P5 인간 앵커: generator 정밀도(단위별) + answerable 비율
- 골드 metadata: provenance + 4모델 분리 여부

각 단위는 승격 기준을 **모두** 만족하면 ``absolute``, 아니면 ``provisional``
이며 부족한 근거(``missing``)를 함께 보고한다. LLM 비의존(순수 조립·판정).
"""

from __future__ import annotations

from typing import Any

# 단위 → eval 메트릭 키 후보(우선순위 순). 첫 번째로 발견되는 키를 채택한다.
# graph 는 신뢰 가능한 surface tier 를 1차로 본다(계획서 §6.3).
_UNIT_METRIC_CANDIDATES: dict[str, tuple[str, ...]] = {
    "doc": ("context_recall@",),
    "answer": ("answer_correct",),  # answer_correct(정확도) 우선, 없으면 answer_correctness
    "graph": ("graph_recall_surface@", "graph_recall@"),
}

# 승격 기준 기본 임계.
DEFAULT_MIN_PRECISION_LABELED = 20
DEFAULT_MIN_EVAL_N = 30


def _find_metric(
    metrics: dict[str, Any], candidates: tuple[str, ...],
) -> tuple[str, float] | None:
    """metrics 에서 candidate 프리픽스/정확 키를 우선순위대로 찾는다.

    ``answer_correct`` 는 ``answer_correctness`` 와 프리픽스가 겹치므로,
    정확 일치를 우선하고 없으면 프리픽스 매칭으로 폴백한다.
    """
    for cand in candidates:
        if cand in metrics and isinstance(metrics[cand], (int, float)):
            return cand, float(metrics[cand])
    # 프리픽스 매칭 (예: "context_recall@5", "graph_recall_surface@5").
    for cand in candidates:
        for k, v in sorted(metrics.items()):
            if k.startswith(cand) and isinstance(v, (int, float)) and not isinstance(v, bool):
                return k, float(v)
    return None


def _models_separated(gold_metadata: dict[str, Any]) -> bool | None:
    """골드 metadata 로부터 4모델 분리(추출·generator·judge ≠ 시스템) 판정.

    ``None`` 이면 분리 정보가 metadata 에 없음(레거시/비-source-grounded 골드).
    """
    keys = (
        "extraction_configured_separately",
        "generator_configured_separately",
        "judge_configured_separately",
    )
    if not any(k in gold_metadata for k in keys):
        return None
    return all(bool(gold_metadata.get(k)) for k in keys)


def build_promotion_report(
    *,
    eval_summary: dict[str, Any],
    precision: dict[str, Any],
    gold_metadata: dict[str, Any],
    min_precision_labeled: int = DEFAULT_MIN_PRECISION_LABELED,
    min_eval_n: int = DEFAULT_MIN_EVAL_N,
) -> dict[str, Any]:
    """eval 요약 + generator 정밀도 + 골드 provenance 를 묶어 승격 보고 생성.

    Args:
        eval_summary: ``eval_search.write_summary`` 산출 dict (``metrics`` +
            선택적 ``metric_ci``, ``n_successful`` 등).
        precision: ``human_anchor.generator_precision`` 산출 dict.
        gold_metadata: 골드셋 ``metadata`` (provenance + 분리 플래그).
        min_precision_labeled: 단위 절대 승격에 필요한 최소 인간 라벨 수.
        min_eval_n: 절대 승격에 필요한 최소 eval 성공 질의 수.

    Returns:
        단위별 라벨/근거 + 전체 라벨 + 공통 보고 항목(answerable/provenance/CI).
    """
    metrics = eval_summary.get("metrics") or {}
    metric_ci = eval_summary.get("metric_ci") or {}
    eval_n = int(
        eval_summary.get("n_successful")
        or eval_summary.get("n_queries")
        or 0
    )
    sep = _models_separated(gold_metadata)
    by_unit_prec = precision.get("by_unit") or {}
    answerable_ratio = precision.get("answerable_ratio")

    units: dict[str, Any] = {}
    for unit, candidates in _UNIT_METRIC_CANDIDATES.items():
        found = _find_metric(metrics, candidates)
        if found is None:
            continue  # 이 단위는 측정되지 않음 — 보고 제외.
        key, value = found
        ci = metric_ci.get(key)
        unit_prec = by_unit_prec.get(unit) or {}
        n_labeled = int(unit_prec.get("n_labeled") or 0)

        missing: list[str] = []
        if ci is None:
            missing.append("ci")  # absolute-mode eval 필요(--absolute-mode).
        if n_labeled < min_precision_labeled:
            missing.append("human_precision")  # P5 인간 라벨 부족.
        if sep is not True:
            missing.append("model_separation")  # 4모델 분리 미확인.
        if eval_n < min_eval_n:
            missing.append("eval_sample_size")

        units[unit] = {
            "metric_key": key,
            "value": value,
            "ci": ci,
            "generator_precision": unit_prec or None,
            "label": "absolute" if not missing else "provisional",
            "missing": missing,
        }

    serving = list(units.values())
    overall_label = (
        "absolute"
        if serving and all(u["label"] == "absolute" for u in serving)
        else "provisional"
    )

    provenance = {
        "extraction_model": gold_metadata.get("extraction_model", ""),
        "extraction_endpoint": gold_metadata.get("extraction_endpoint", ""),
        "generator_model": gold_metadata.get("generator_model", ""),
        "generator_endpoint": gold_metadata.get("generator_endpoint", ""),
        "judge_model": gold_metadata.get("judge_model", ""),
        "judge_endpoint": gold_metadata.get("judge_endpoint", ""),
        "seed": gold_metadata.get("seed"),
    }

    return {
        "overall_label": overall_label,
        "units": units,
        "answerable_ratio": answerable_ratio,
        "models_separated": sep,
        "eval_n": eval_n,
        "n_human_reviewed": precision.get("n_reviewed"),
        "provenance": provenance,
        "promotion_criteria": {
            "min_precision_labeled": min_precision_labeled,
            "min_eval_n": min_eval_n,
        },
    }


_MISSING_LABEL = {
    "ci": "95% CI 없음(--absolute-mode 로 eval 재실행)",
    "human_precision": "인간 검증 부족(P5 라벨 추가)",
    "model_separation": "4모델 분리 미확인(추출/generator/judge ≠ 시스템)",
    "eval_sample_size": "eval 표본 부족",
}


def render_promotion_report(report: dict[str, Any]) -> str:
    """승격 보고를 사람이 읽는 텍스트로 렌더링."""
    lines: list[str] = []
    lines.append("=" * 64)
    lines.append(f"  승격 보고 — 전체 라벨: {report['overall_label'].upper()}")
    lines.append("=" * 64)
    ar = report.get("answerable_ratio")
    sep = report.get("models_separated")
    sep_str = {True: "예", False: "아니오", None: "미상"}[sep]
    lines.append(
        f"  answerable 비율: {ar:.3f}" if isinstance(ar, (int, float))
        else "  answerable 비율: 미상"
    )
    lines.append(f"  4모델 분리: {sep_str}  |  eval N: {report.get('eval_n')}"
                 f"  |  인간 리뷰: {report.get('n_human_reviewed')}")
    lines.append("-" * 64)
    for unit, u in report.get("units", {}).items():
        ci = u.get("ci")
        ci_str = (
            f"[{ci['ci_low']:.3f}, {ci['ci_high']:.3f}]"
            if isinstance(ci, dict) else "[CI 없음]"
        )
        prec = u.get("generator_precision") or {}
        prec_str = (
            f"gen_prec={prec['precision']:.3f}(n={prec.get('n_labeled', 0)})"
            if prec else "gen_prec=미상"
        )
        lines.append(
            f"  [{u['label'].upper():10s}] {unit:7s} "
            f"{u['metric_key']}={u['value']:.3f} {ci_str}  {prec_str}"
        )
        if u["missing"]:
            reasons = ", ".join(_MISSING_LABEL.get(m, m) for m in u["missing"])
            lines.append(f"               승격 부족: {reasons}")
    lines.append("-" * 64)
    prov = report.get("provenance") or {}
    lines.append(
        "  provenance: "
        f"ext={prov.get('extraction_model') or '-'} / "
        f"gen={prov.get('generator_model') or '-'} / "
        f"judge={prov.get('judge_model') or '-'} / seed={prov.get('seed')}"
    )
    if report["overall_label"] == "provisional":
        lines.append(
            "  ⚠ 잠정 기준선 — 절대값 인용 금지. 상대 추적·sanity 용도로만 사용.",
        )
    lines.append("=" * 64)
    return "\n".join(lines)
