#!/usr/bin/env python3
"""두 평가 run 결과(baseline vs treatment) 의 동치성 검증 + paired 비교.

``eval_search.py`` 가 산출한 ``*.summary.json`` 두 개와 ``*.csv`` 두 개를 받아:

1. **config 동치성** — 두 run 이 같은 골드셋(SHA256), 같은 검색 설정
   (embedding_model, llm_model, top_k, max_chunks, similarity_threshold,
   rerank_enabled, hyde_enabled) 에서 실행됐는지 확인. 다른 키가 있으면
   ``--allow-config-mismatch`` 없이는 종료.
2. **per-question paired diff** — 두 CSV 를 ``id`` 컬럼으로 inner join 하여
   메트릭별 차이(treatment − baseline) 산출.
3. **통계 검정** — paired Wilcoxon signed-rank (직접 구현; scipy 없으면
   stdlib 폴백) + 차이의 95% bootstrap CI (1000회 resample).
4. **출력** — stdout 표 + ``compare_runs.json`` 으로 저장.

사용 예::

    python scripts/compare_runs.py \\
        --baseline eval/runs/baseline.summary.json \\
        --treatment eval/runs/multiview.summary.json \\
        --baseline-csv eval/runs/baseline.csv \\
        --treatment-csv eval/runs/multiview.csv \\
        --out eval/runs/compare_baseline_vs_multiview.json
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import random
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("compare_runs")


# 동치성 검증 대상 키 — 두 run 의 config 가 이 키에 대해 모두 같아야 한다.
EQUIVALENCE_KEYS: tuple[str, ...] = (
    "gold_set_sha256",
    "embedding_model",
    "llm_model",
    "top_k",
    "max_chunks",
    "similarity_threshold",
    "rerank_enabled",
    "hyde_enabled",
)


# 비교할 메트릭의 표준 prefix. CSV 컬럼명을 prefix 로 매칭.
METRIC_PREFIXES: tuple[str, ...] = (
    "recall@",
    "precision@",
    "hit@",
    "ndcg@",
    "mrr",
    "graph_recall@",
    "graph_precision@",
    "graph_hit@",
    "graph_ndcg@",
    "graph_mrr",
    "judge_score",
    "elapsed_ms",
)


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------


def _load_json(path: Path) -> dict[str, Any]:
    """JSON 파일을 dict 로 로드. 실패 시 SystemExit."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise SystemExit(f"파일을 찾을 수 없음: {path}")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"JSON 파싱 실패 {path}: {exc}")
    if not isinstance(data, dict):
        raise SystemExit(f"summary JSON 이 객체가 아님: {path}")
    return data


def _load_csv_rows(path: Path) -> list[dict[str, str]]:
    """CSV 를 dict 리스트로 로드."""
    try:
        with open(path, encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            return list(reader)
    except FileNotFoundError:
        raise SystemExit(f"파일을 찾을 수 없음: {path}")


def _try_float(s: Any) -> float | None:
    """문자열/숫자를 float 로 시도 — 빈 값 / None / 'None' / 비숫자는 None."""
    if s is None:
        return None
    if isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = str(s).strip()
    if not s or s.lower() == "none":
        return None
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 동치성 검증
# ---------------------------------------------------------------------------


def check_equivalence(
    baseline_cfg: dict[str, Any],
    treatment_cfg: dict[str, Any],
) -> list[tuple[str, Any, Any]]:
    """두 config dict 의 EQUIVALENCE_KEYS 가 모두 동일한지 비교.

    Returns:
        다른 키들의 ``(key, baseline_value, treatment_value)`` 리스트.
        빈 리스트면 모두 일치.
    """
    diffs: list[tuple[str, Any, Any]] = []
    for k in EQUIVALENCE_KEYS:
        b = baseline_cfg.get(k)
        t = treatment_cfg.get(k)
        if b != t:
            diffs.append((k, b, t))
    return diffs


# ---------------------------------------------------------------------------
# 통계 — paired Wilcoxon, bootstrap CI
# ---------------------------------------------------------------------------


def _signed_rank_statistic(diffs: list[float]) -> tuple[float, int]:
    """Wilcoxon signed-rank 통계량 W (양의 순위합) 와 n_nonzero 를 계산.

    동률(tie) 은 평균 순위로 분배. 0 차이는 제외 (Wilcoxon 표준 처리).
    """
    nonzero = [d for d in diffs if d != 0.0]
    if not nonzero:
        return 0.0, 0
    abs_vals = sorted(((abs(d), i) for i, d in enumerate(nonzero)), key=lambda x: x[0])

    ranks: dict[int, float] = {}
    i = 0
    while i < len(abs_vals):
        j = i
        while j + 1 < len(abs_vals) and abs_vals[j + 1][0] == abs_vals[i][0]:
            j += 1
        # 동률 그룹 [i..j] 에 평균 순위 부여.
        avg_rank = (i + j) / 2.0 + 1.0  # 1-based
        for k in range(i, j + 1):
            ranks[abs_vals[k][1]] = avg_rank
        i = j + 1

    w_plus = sum(ranks[idx] for idx, d in enumerate(nonzero) if d > 0)
    return w_plus, len(nonzero)


def wilcoxon_p_value(diffs: list[float]) -> tuple[float, float, int]:
    """Wilcoxon signed-rank 의 양측 p-value 를 정규근사로 계산.

    n < 6 이면 정규근사 정확도가 낮으므로 호출부가 경고를 띄울 것.

    Returns:
        ``(p_value, z, n_nonzero)``. p_value 는 양측. n_nonzero=0 이면
        ``(1.0, 0.0, 0)``.
    """
    w_plus, n = _signed_rank_statistic(diffs)
    if n == 0:
        return 1.0, 0.0, 0
    mean = n * (n + 1) / 4.0
    var = n * (n + 1) * (2 * n + 1) / 24.0
    if var == 0.0:
        return 1.0, 0.0, n
    z = (w_plus - mean) / math.sqrt(var)
    # 양측 p — 정규분포 CDF 의 보수에서 2배.
    p = 2.0 * (1.0 - _standard_normal_cdf(abs(z)))
    return p, z, n


def _standard_normal_cdf(x: float) -> float:
    """표준정규 CDF — math.erf 기반."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bootstrap_ci(
    diffs: list[float],
    *,
    n_resample: int = 1000,
    alpha: float = 0.05,
    seed: int = 42,
) -> tuple[float, float, float]:
    """차이의 평균에 대한 부트스트랩 신뢰구간.

    Returns:
        ``(mean, lower, upper)``. diffs 비어 있으면 ``(0.0, 0.0, 0.0)``.
    """
    if not diffs:
        return 0.0, 0.0, 0.0
    rng = random.Random(seed)
    means: list[float] = []
    n = len(diffs)
    for _ in range(n_resample):
        sample = [diffs[rng.randint(0, n - 1)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo_idx = max(0, int((alpha / 2.0) * n_resample))
    hi_idx = min(n_resample - 1, int((1.0 - alpha / 2.0) * n_resample))
    return sum(diffs) / n, means[lo_idx], means[hi_idx]


# ---------------------------------------------------------------------------
# Per-question paired diff
# ---------------------------------------------------------------------------


def _select_metric_columns(rows: list[dict[str, str]]) -> list[str]:
    """CSV 헤더에서 METRIC_PREFIXES 로 시작하는 컬럼만 추출."""
    if not rows:
        return []
    cols: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r:
            if k in seen:
                continue
            for p in METRIC_PREFIXES:
                if k.startswith(p):
                    cols.append(k)
                    seen.add(k)
                    break
    return cols


def paired_diff(
    baseline_rows: list[dict[str, str]],
    treatment_rows: list[dict[str, str]],
) -> tuple[dict[str, list[float]], int, int]:
    """두 CSV 를 id 기준 inner join 하여 메트릭별 (treatment − baseline) 산출.

    Returns:
        ``(diffs_by_metric, n_paired, n_skipped)``. 한 쪽에라도 값이 없거나
        None 인 행은 해당 메트릭에서 skip 된다.
    """
    by_id_b = {r.get("id"): r for r in baseline_rows if r.get("id")}
    by_id_t = {r.get("id"): r for r in treatment_rows if r.get("id")}
    common_ids = sorted(set(by_id_b.keys()) & set(by_id_t.keys()))
    cols_b = _select_metric_columns(baseline_rows)
    cols_t = _select_metric_columns(treatment_rows)
    metric_cols = [c for c in cols_b if c in cols_t]

    diffs: dict[str, list[float]] = {c: [] for c in metric_cols}
    n_skipped = 0
    for qid in common_ids:
        rb = by_id_b[qid]
        rt = by_id_t[qid]
        for c in metric_cols:
            vb = _try_float(rb.get(c))
            vt = _try_float(rt.get(c))
            if vb is None or vt is None:
                n_skipped += 1
                continue
            diffs[c].append(vt - vb)
    return diffs, len(common_ids), n_skipped


# ---------------------------------------------------------------------------
# Pretty print
# ---------------------------------------------------------------------------


def _print_summary(
    *,
    baseline_label: str,
    treatment_label: str,
    cfg_diffs: list[tuple[str, Any, Any]],
    n_paired: int,
    diff_stats: dict[str, dict[str, float]],
    allow_mismatch: bool,
) -> None:
    print("\n" + "=" * 72)
    print(f"  compare_runs: {baseline_label}  ->  {treatment_label}  (N={n_paired})")
    print("=" * 72)
    if cfg_diffs:
        marker = "[ALLOWED]" if allow_mismatch else "[BLOCKED]"
        print(f"  CONFIG MISMATCH {marker}")
        for k, b, t in cfg_diffs:
            print(f"    {k:>28s}  baseline={b!r}  treatment={t!r}")
    else:
        print("  config: equivalence keys 모두 일치")
    print("-" * 72)
    header = (
        f"  {'metric':<24s} {'mean Δ':>10s} {'CI95 lo':>10s} "
        f"{'CI95 hi':>10s} {'p':>8s} {'n':>5s}"
    )
    print(header)
    for metric, stats in diff_stats.items():
        print(
            f"  {metric:<24s} "
            f"{stats['mean']:>10.4f} {stats['ci_lo']:>10.4f} {stats['ci_hi']:>10.4f} "
            f"{stats['p_value']:>8.4f} {int(stats['n']):>5d}",
        )
    print("=" * 72 + "\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def run(args: argparse.Namespace) -> int:
    baseline = _load_json(Path(args.baseline))
    treatment = _load_json(Path(args.treatment))

    baseline_cfg = baseline.get("config") or {}
    treatment_cfg = treatment.get("config") or {}
    cfg_diffs = check_equivalence(baseline_cfg, treatment_cfg)

    if cfg_diffs and not args.allow_config_mismatch:
        print(
            "config 동치성 위반 — 두 run 의 다음 키가 다릅니다:",
            file=sys.stderr,
        )
        for k, b, t in cfg_diffs:
            print(f"  {k}: baseline={b!r} treatment={t!r}", file=sys.stderr)
        print(
            "비교를 강행하려면 --allow-config-mismatch 추가.",
            file=sys.stderr,
        )
        return 2

    baseline_rows = _load_csv_rows(Path(args.baseline_csv))
    treatment_rows = _load_csv_rows(Path(args.treatment_csv))
    diffs_by_metric, n_paired, n_skipped = paired_diff(
        baseline_rows, treatment_rows,
    )

    if n_paired == 0:
        print(
            "paired 항목이 0 — id 컬럼 매칭 결과가 비어 있습니다.",
            file=sys.stderr,
        )
        return 3

    diff_stats: dict[str, dict[str, float]] = {}
    for metric, diffs in diffs_by_metric.items():
        if not diffs:
            continue
        mean, lo, hi = bootstrap_ci(
            diffs,
            n_resample=args.bootstrap_resamples,
            seed=args.seed,
        )
        p, z, n_nz = wilcoxon_p_value(diffs)
        diff_stats[metric] = {
            "mean": mean,
            "ci_lo": lo,
            "ci_hi": hi,
            "p_value": p,
            "z": z,
            "n": float(len(diffs)),
            "n_nonzero": float(n_nz),
        }

    baseline_label = baseline.get("label", "baseline")
    treatment_label = treatment.get("label", "treatment")
    _print_summary(
        baseline_label=baseline_label,
        treatment_label=treatment_label,
        cfg_diffs=cfg_diffs,
        n_paired=n_paired,
        diff_stats=diff_stats,
        allow_mismatch=args.allow_config_mismatch,
    )

    out = {
        "baseline_label": baseline_label,
        "treatment_label": treatment_label,
        "n_paired": n_paired,
        "n_skipped_cells": n_skipped,
        "config_diffs": [
            {"key": k, "baseline": b, "treatment": t} for k, b, t in cfg_diffs
        ],
        "allow_config_mismatch": bool(args.allow_config_mismatch),
        "metrics": diff_stats,
        "bootstrap_resamples": args.bootstrap_resamples,
        "seed": args.seed,
    }
    out_path = Path(args.out) if args.out else None
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(out, f, indent=2, ensure_ascii=False)
        print(f"  saved   : {out_path}\n")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="두 평가 run 의 동치성 검증 + paired 비교",
    )
    parser.add_argument(
        "--baseline", required=True,
        help="baseline summary JSON 경로 (eval_search.py 산출)",
    )
    parser.add_argument(
        "--treatment", required=True,
        help="treatment summary JSON 경로",
    )
    parser.add_argument(
        "--baseline-csv", required=True,
        help="baseline 의 per-question CSV 경로",
    )
    parser.add_argument(
        "--treatment-csv", required=True,
        help="treatment 의 per-question CSV 경로",
    )
    parser.add_argument(
        "--out", default="",
        help="결과 JSON 저장 경로 (미지정 시 저장 생략)",
    )
    parser.add_argument(
        "--allow-config-mismatch", action="store_true",
        help="config 동치성 키가 달라도 강행 (라벨 비교가 거짓 개선이 될 수 있음)",
    )
    parser.add_argument(
        "--bootstrap-resamples", type=int, default=1000,
        help="bootstrap CI 의 resample 횟수 (기본 1000)",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="bootstrap 결정론용 시드 (기본 42)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    _setup_logging(args.verbose)
    sys.exit(run(args))


if __name__ == "__main__":
    main()
