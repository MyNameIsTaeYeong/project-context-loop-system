#!/usr/bin/env python3
"""인간 앵커 리뷰 도구 — source-grounded 골드의 generator 정밀도 보정 (PR #79 P5).

소규모 인간 검증으로 합성 generator 의 정밀도를 산출해 대규모 합성 골드의
신뢰도를 보정한다(계획서 §7 의 2-tier "잠정→절대" 승격).

사용법::

    # 1) 리뷰용 CSV 내보내기 (소규모 표본)
    python scripts/gold_human_review.py export \\
        --gold gold/source_grounded.yaml --out review.csv \\
        --sample-n 80 --seed 1

    # 2) 사람이 review.csv 의 human_*_valid 컬럼을 1/0 으로 채움

    # 3) 채워진 CSV 로 generator 정밀도(전체 + 단위별) 산출
    python scripts/gold_human_review.py score \\
        --gold gold/source_grounded.yaml --review review.csv \\
        --out precision.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 프로젝트 루트를 sys.path 에 추가
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from context_loop.eval.gold_set import load_gold_set  # noqa: E402
from context_loop.eval.human_anchor import (  # noqa: E402
    export_review_csv,
    generator_precision,
    import_review_csv,
)
from context_loop.eval.promotion import (  # noqa: E402
    build_promotion_report,
    render_promotion_report,
)


def _cmd_export(args: argparse.Namespace) -> int:
    gold = load_gold_set(Path(args.gold))
    n = export_review_csv(
        gold, Path(args.out),
        sample_n=args.sample_n, seed=args.seed,
    )
    print(f"리뷰 CSV 내보냄 — {n} 행 → {args.out}")
    print("human_valid / human_doc_valid / human_answer_valid / "
          "human_graph_valid 컬럼을 1/0 으로 채운 뒤 score 를 실행하세요.")
    return 0


def _cmd_score(args: argparse.Namespace) -> int:
    gold = load_gold_set(Path(args.gold))
    verdicts = import_review_csv(Path(args.review))
    result = generator_precision(gold, verdicts)

    print("\n=== generator 정밀도 (인간 앵커) ===")
    print(f"  골드 항목: {result['n_total']}  |  리뷰됨: {result['n_reviewed']}")
    print(f"  answerable 비율: {result['answerable_ratio']:.3f}")
    ov = result["overall"]
    print(
        f"  전체 precision: {ov['precision']:.3f} "
        f"[{ov['ci_low']:.3f}, {ov['ci_high']:.3f}]  (n={ov['n_labeled']})"
    )
    print("  단위별:")
    for unit, blk in result["by_unit"].items():
        print(
            f"    {unit:7s} precision={blk['precision']:.3f} "
            f"[{blk['ci_low']:.3f}, {blk['ci_high']:.3f}]  "
            f"(labeled={blk['n_labeled']}/serving={blk['n_serving']})"
        )

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"\n정밀도 보고 저장 — {out_path}")
    return 0


def _cmd_promote(args: argparse.Namespace) -> int:
    gold = load_gold_set(Path(args.gold))
    verdicts = import_review_csv(Path(args.review)) if args.review else {}
    precision = generator_precision(gold, verdicts)
    with open(args.eval_summary, encoding="utf-8") as f:
        eval_summary = json.load(f)
    report = build_promotion_report(
        eval_summary=eval_summary,
        precision=precision,
        gold_metadata=gold.metadata or {},
        min_precision_labeled=args.min_precision_labeled,
        min_eval_n=args.min_eval_n,
    )
    print(render_promotion_report(report))
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
        print(f"\n승격 보고 저장 — {out_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("export", help="골드셋 → 리뷰 CSV")
    p_export.add_argument("--gold", required=True, help="골드셋 YAML 경로")
    p_export.add_argument("--out", required=True, help="출력 CSV 경로")
    p_export.add_argument(
        "--sample-n", type=int, default=None,
        help="표본 크기 (source_type 층화 결정론 샘플). 미지정 시 전체. "
             "인간 앵커는 보통 50~100.",
    )
    p_export.add_argument(
        "--seed", type=int, default=None, help="샘플링 결정성 seed.",
    )
    p_export.set_defaults(func=_cmd_export)

    p_score = sub.add_parser("score", help="채워진 리뷰 CSV → generator 정밀도")
    p_score.add_argument("--gold", required=True, help="골드셋 YAML 경로")
    p_score.add_argument("--review", required=True, help="채워진 리뷰 CSV 경로")
    p_score.add_argument(
        "--out", default="", help="정밀도 보고 JSON 출력 경로 (선택).",
    )
    p_score.set_defaults(func=_cmd_score)

    p_prom = sub.add_parser(
        "promote",
        help="eval 요약 + 인간 정밀도 + provenance → 단위별 잠정/절대 승격 보고",
    )
    p_prom.add_argument("--gold", required=True, help="골드셋 YAML 경로")
    p_prom.add_argument(
        "--eval-summary", required=True,
        help="eval_search 의 *.summary.json 경로 (--absolute-mode 권장 — CI 포함)",
    )
    p_prom.add_argument(
        "--review", default="",
        help="채워진 리뷰 CSV (선택). 없으면 인간 정밀도 미반영 → 잠정.",
    )
    p_prom.add_argument(
        "--min-precision-labeled", type=int, default=20,
        help="단위 절대 승격에 필요한 최소 인간 라벨 수 (기본 20).",
    )
    p_prom.add_argument(
        "--min-eval-n", type=int, default=30,
        help="절대 승격에 필요한 최소 eval 성공 질의 수 (기본 30).",
    )
    p_prom.add_argument(
        "--out", default="", help="승격 보고 JSON 출력 경로 (선택).",
    )
    p_prom.set_defaults(func=_cmd_promote)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
