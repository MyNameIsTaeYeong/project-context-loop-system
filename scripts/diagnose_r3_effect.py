#!/usr/bin/env python3
"""R3 (가상 질문 인덱싱) 효과의 정밀 진단.

`eval_search.py` 의 baseline / R3 두 CSV 를 받아 doc-level metric 으로는 잘
드러나지 않는 R3 의 미세 효과를 분리한다:

1. **Set difference 분석** — R3 가 새로 잡은 정답 doc / 잃은 정답 doc / 변화
   없는 doc 수. 이 셋이 모두 0 이면 R3 의 doc-level 효과는 진짜 0.
2. **순위 변화** — 같은 정답이 R3 에서 더 위로 올라갔는지 (MRR 세부).
3. **per-query Δrecall 분포** — 어떤 질의에서 손해/이득 봤는지 정렬.
4. **매칭 view 분포** — R3 csv 에 `top1_view` 컬럼이 있으면 view 별 매칭 정답률.

사용 예::

    python scripts/diagnose_r3_effect.py \\
        --baseline eval/runs/baseline.csv \\
        --r3 eval/runs/r3.csv \\
        --out _workspace/eval/r3_diagnosis.txt
"""

from __future__ import annotations

import argparse
import csv
import statistics
from pathlib import Path
from typing import Any


def _parse_ids(s: str) -> list[int]:
    """CSV 의 콤마 결합된 doc_id 문자열을 int 리스트로 복원."""
    if not s:
        return []
    out: list[int] = []
    for x in s.split(","):
        x = x.strip()
        if x:
            try:
                out.append(int(x))
            except ValueError:
                pass
    return out


def _load(path: Path) -> dict[str, dict[str, Any]]:
    """eval_search.py 결과 CSV 를 id 키 dict 로 로드."""
    out: dict[str, dict[str, Any]] = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            qid = row.get("id", "")
            if not qid:
                continue
            out[qid] = row
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="R3 효과 정밀 진단")
    ap.add_argument("--baseline", required=True, help="baseline CSV 경로")
    ap.add_argument("--r3", required=True, help="R3 CSV 경로")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument(
        "--out", default=None,
        help="진단 결과 출력 파일 (없으면 stdout 만)",
    )
    args = ap.parse_args()

    base = _load(Path(args.baseline))
    r3 = _load(Path(args.r3))

    common_ids = sorted(set(base.keys()) & set(r3.keys()))
    if not common_ids:
        raise SystemExit("두 CSV 의 id 가 공통이 없습니다 — 동일 골드셋인지 확인")

    lines: list[str] = []

    def out(s: str = "") -> None:
        lines.append(s)
        print(s)

    out(f"# R3 정밀 진단 — {len(common_ids)} 질의")
    out(f"top-k = {args.top_k}\n")

    # ------------------------------------------------------------------ #
    # 1. Set difference (R3 가 새로 잡은 / 잃은 / 그대로)
    # ------------------------------------------------------------------ #
    total_new = 0
    total_lost = 0
    total_kept = 0
    queries_with_new_hit: list[tuple[str, list[int]]] = []
    queries_with_lost_hit: list[tuple[str, list[int]]] = []

    for qid in common_ids:
        b = base[qid]
        r = r3[qid]
        relevant = set(_parse_ids(b.get("relevant_doc_ids", "")))
        b_top = set(_parse_ids(b.get("retrieved_doc_ids", ""))[: args.top_k])
        r_top = set(_parse_ids(r.get("retrieved_doc_ids", ""))[: args.top_k])

        b_hit = b_top & relevant
        r_hit = r_top & relevant

        new = r_hit - b_hit   # R3 만 잡은 정답
        lost = b_hit - r_hit  # baseline 만 잡은 정답
        kept = r_hit & b_hit  # 둘 다 잡은 정답

        total_new += len(new)
        total_lost += len(lost)
        total_kept += len(kept)
        if new:
            queries_with_new_hit.append((qid, sorted(new)))
        if lost:
            queries_with_lost_hit.append((qid, sorted(lost)))

    out("## 1. Set difference (정답 doc 단위)")
    out(f"  R3 가 새로 잡은 정답:  {total_new}")
    out(f"  R3 가 잃은 정답:       {total_lost}")
    out(f"  둘 다 잡은 정답:       {total_kept}")
    net = total_new - total_lost
    out(f"  순효과:                {net:+d}")
    if total_new + total_lost == 0:
        out(
            "  → R3 는 doc-level 에서 **새 정답 발견 효과 0**. "
            "효과의 본질은 매칭 view 변화일 뿐.",
        )
    else:
        ratio = total_new / max(total_new + total_kept, 1)
        out(
            f"  → R3 의 새 정답 비율 = {ratio:.1%} "
            f"(전체 R3 hit 의 {ratio:.1%} 가 baseline 이 못 잡던 것)",
        )

    if queries_with_new_hit:
        out("\n  R3 가 새로 잡은 질의 (최대 5건):")
        for qid, docs in queries_with_new_hit[:5]:
            q = r3[qid].get("query", "")[:80]
            out(f"    [{qid}] {q}  → +doc {docs}")
    if queries_with_lost_hit:
        out("\n  R3 가 잃은 질의 (최대 5건):")
        for qid, docs in queries_with_lost_hit[:5]:
            q = r3[qid].get("query", "")[:80]
            out(f"    [{qid}] {q}  → -doc {docs}")

    out("")

    # ------------------------------------------------------------------ #
    # 2. MRR / hit@1 / precision@1 세부 비교 — 같은 정답의 등수 변화
    # ------------------------------------------------------------------ #

    def _float(v: Any, default: float = 0.0) -> float:
        try:
            return float(v)
        except (ValueError, TypeError):
            return default

    base_mrr = [_float(base[q].get("mrr")) for q in common_ids]
    r3_mrr = [_float(r3[q].get("mrr")) for q in common_ids]

    out("## 2. MRR 비교 (정답이 top-1 에 더 가깝게?)")
    out(f"  baseline MRR 평균: {statistics.mean(base_mrr):.4f}")
    out(f"  R3       MRR 평균: {statistics.mean(r3_mrr):.4f}")
    out(f"  Δ              : {statistics.mean(r3_mrr) - statistics.mean(base_mrr):+.4f}")
    paired_diffs = [r - b for b, r in zip(base_mrr, r3_mrr)]
    pos = sum(1 for d in paired_diffs if d > 0)
    neg = sum(1 for d in paired_diffs if d < 0)
    zero = sum(1 for d in paired_diffs if d == 0)
    out(f"  paired diff: 양수 {pos} / 음수 {neg} / 0 {zero}")
    if paired_diffs and any(d != 0 for d in paired_diffs):
        out(f"  최대 향상: {max(paired_diffs):+.3f}")
        out(f"  최대 손실: {min(paired_diffs):+.3f}")

    out("")

    # ------------------------------------------------------------------ #
    # 3. per-query Δrecall 분포 (R3 가 어디서 손해/이득)
    # ------------------------------------------------------------------ #
    recall_key = f"recall@{args.top_k}"
    deltas: list[tuple[float, str, str]] = []
    for qid in common_ids:
        b_r = _float(base[qid].get(recall_key))
        r_r = _float(r3[qid].get(recall_key))
        if b_r != r_r:
            q = r3[qid].get("query", "")[:80]
            deltas.append((r_r - b_r, qid, q))

    deltas.sort()

    out(f"## 3. Per-query Δ{recall_key} 분포")
    out(f"  변화 있는 질의: {len(deltas)} / {len(common_ids)}")
    if deltas:
        out("\n  R3 손해 (최대 5건):")
        for d, qid, q in deltas[:5]:
            out(f"    [{qid}] {d:+.2f}  {q}")
        out("\n  R3 이득 (최대 5건):")
        for d, qid, q in deltas[-5:]:
            out(f"    [{qid}] {d:+.2f}  {q}")
    else:
        out("  → 모든 질의에서 recall 변화 없음 (R3 의 doc-level 효과 0)")

    out("")

    # ------------------------------------------------------------------ #
    # 4. (선택) 매칭 view 분포 — R3 CSV 에 top1_view 컬럼이 있으면
    # ------------------------------------------------------------------ #
    sample = next(iter(r3.values()))
    if "top1_view" in sample:
        from collections import Counter
        views: Counter[str] = Counter()
        view_by_hit: dict[str, list[int]] = {"hit": [], "miss": []}
        for qid in common_ids:
            r = r3[qid]
            v = r.get("top1_view", "?")
            views[v] += 1
            hit = _float(r.get(f"hit@{args.top_k}"))
            view_by_hit["hit" if hit > 0 else "miss"].append(v)
        out("## 4. R3 의 top-1 매칭 view 분포")
        for v, c in views.most_common():
            out(f"  {v}: {c}")
        # view 별 hit 률
        out("\n  view 별 hit 률:")
        for v in views:
            n_hit = sum(1 for x in view_by_hit["hit"] if x == v)
            n_total = views[v]
            out(f"    {v}: {n_hit}/{n_total} ({n_hit/n_total:.1%})")
    else:
        out("## 4. 매칭 view 분포 — 스킵 (R3 CSV 에 top1_view 컬럼 없음)")

    out("")

    # ------------------------------------------------------------------ #
    # 5. 결론
    # ------------------------------------------------------------------ #
    out("## 5. 진단 결론")
    mrr_delta = statistics.mean(r3_mrr) - statistics.mean(base_mrr)

    if total_new == 0 and total_lost == 0:
        # 시나리오 A: 완전 동일
        out("  · R3 와 baseline 의 정답 doc set 이 100% 동일")
        out("    → R3 가 검색 결과 set 을 전혀 바꾸지 못함")
        if mrr_delta > 0.01:
            out(f"  ✓ MRR 미세 향상 Δ={mrr_delta:+.4f} — 같은 정답이 더 위로")
            out("    → R3 의 가치는 ranking 정밀도 (precision@1 차원)")
        elif abs(mrr_delta) <= 0.01:
            out(f"  ✗ MRR 도 변화 없음 (Δ={mrr_delta:+.4f})")
            out("    → R3 가 doc-level metric 으로는 측정 불가. "
                "judge / chunk-level / UX 평가로만 가치 확인 가능")
        else:
            out(f"  ✗ MRR 도 회귀 (Δ={mrr_delta:+.4f})")
    elif total_new > total_lost:
        # 시나리오 B: net 이득
        out(f"  ✓ R3 가 net +{net} 정답 doc 추가 발견")
        out(f"    (새로 잡음 {total_new} / 잃음 {total_lost})")
        out(f"  · MRR Δ = {mrr_delta:+.4f}")
        out("    → R3 의 doc-level 효과 양수")
    elif total_new == total_lost:
        # 시나리오 C: set 교환 (수평 이동)
        out(f"  ~ R3 가 정답 doc set 을 일부 교환 — 새로 {total_new}, 잃음 {total_lost}")
        out("    (net = 0 이지만 검색 결과 자체는 바뀜)")
        out(f"  · MRR Δ = {mrr_delta:+.4f}")
        out("    → R3 가 작동하긴 함. 그러나 새 정답과 잃은 정답이 상쇄")
        out("    검토 필요: 잃은 정답이 (a) baseline 의 우연한 hit 였는지,")
        out("              (b) R3 가 진짜 회귀시킨 것인지 — §1 의 잃은 질의")
        out("              샘플을 §3 의 손해 패턴과 비교")
    else:
        # 시나리오 D: net 손해
        out(f"  ✗ R3 가 net {net} 정답 손해")
        out(f"    (새로 잡음 {total_new} / 잃음 {total_lost})")
        out(f"  · MRR Δ = {mrr_delta:+.4f}")
        out("    → R3 가 회귀를 만든 것. 폐기 고려 또는 회귀 원인 분석 필요")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        print(f"\n결과 저장: {args.out}")


if __name__ == "__main__":
    main()
