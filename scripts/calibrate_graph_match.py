#!/usr/bin/env python3
"""그래프 T4 (embedding cosine) 임계값 τ 를 캘리브레이션한다.

`graph_match.py` 의 ``DEFAULT_GRAPH_MATCH_THRESHOLD = 0.78`` 은 초기 추정값
이며, 인덱싱된 실제 graph_nodes 의 description 분포에 맞춰 재산정해야 한다.

방법:
1. 인덱싱된 모든 graph_nodes 의 description 임베딩을 추출.
2. **양성 쌍 (P)** — 같은 ``entity_name`` (alias 일 가능성, 즉 의미 동일) 또는
   같은 ``entity_type`` + name 의 정규화(NFKC+공백제거) 일치 쌍. 코드 변경이나
   인덱싱 LLM 차이로 인한 표기 변형이 양성에 해당.
3. **음성 쌍 (N)** — 임의 비매칭 쌍 (다른 entity_name + 정규화도 다름).
   ``--n-neg`` 로 개수 조정.
4. P/N cosine 분포에서 ``F1 = 2·P·R/(P+R)`` 최댓값을 만드는 τ 를 탐색.
5. ROC/PR 분포를 그래프 없이 표로 보고 + 권장 τ 출력.

출력: stdout 표 + ``--output`` 지정 시 JSON 저장.

사용 예::

    python scripts/calibrate_graph_match.py \\
        --config ~/.context-loop/config.yaml \\
        --n-neg 1000 \\
        --output eval/graph_threshold_calibration.json

결과 권장 τ 가 현재 default 와 ±0.03 이상 다르면 `graph_match.py:33` 의 default
를 갱신하고, 골드셋·평가 모두에 새 τ 적용.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any

# 프로젝트 루트를 sys.path 에 추가
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from context_loop.config import Config  # noqa: E402
from context_loop.eval.graph_match import cosine_similarity  # noqa: E402
from context_loop.storage.metadata_store import MetadataStore  # noqa: E402

logger = logging.getLogger("calibrate_graph_match")


def _normalize_name(name: str) -> str:
    """T3 와 동일한 정규화 — NFKC + 공백/구두점 제거 + 소문자."""
    if not name:
        return ""
    nfkc = unicodedata.normalize("NFKC", name)
    return "".join(c for c in nfkc.lower() if c.isalnum())


async def _load_node_descriptions(
    meta_store: MetadataStore,
) -> list[dict[str, Any]]:
    """모든 graph_nodes 에서 (id, name, type, description, embedding) 추출.

    description 이 빈 노드는 제외 (T4 비교 의미 없음).
    """
    nodes = await meta_store.get_all_graph_nodes()
    out: list[dict[str, Any]] = []
    for node in nodes:
        name = str(node.get("entity_name") or "").strip()
        etype = str(node.get("entity_type") or "").strip()
        if not name:
            continue
        # properties 에서 description 추출
        props_raw = node.get("properties")
        description = ""
        if props_raw:
            try:
                props = (
                    json.loads(props_raw) if isinstance(props_raw, str) else props_raw
                )
                if isinstance(props, dict):
                    description = str(props.get("description") or "")
            except (json.JSONDecodeError, TypeError):
                description = ""
        if not description:
            continue
        out.append({
            "id": node.get("id"),
            "name": name,
            "type": etype,
            "name_normalized": _normalize_name(name),
            "description": description,
        })
    return out


async def _embed_descriptions(
    nodes: list[dict[str, Any]],
    embedding_client: Any,
) -> None:
    """description 임베딩을 일괄 계산하여 노드에 박는다."""
    from context_loop.eval.graph_match import aembed_with_client  # noqa: PLC0415

    texts = [n["description"] for n in nodes]
    embeddings = await aembed_with_client(embedding_client, texts)
    for n, emb in zip(nodes, embeddings):
        n["embedding"] = list(emb) if emb is not None else None


def _build_pair_buckets(
    nodes: list[dict[str, Any]],
    *,
    n_neg: int,
    rng: random.Random,
) -> tuple[list[tuple[dict, dict, str]], list[tuple[dict, dict, str]]]:
    """양성/음성 쌍 후보를 만든다. S3 보강 — 자명 양성 비중을 줄이고 실제로
    T4 임베딩 매칭이 필요한 어려운 케이스를 포함.

    **양성**:
        - ``trivial-normalize``: 같은 name_normalized + 같은 type (T3 가 이미
          잡는 자명 양성 — 비교 baseline)
        - ``alias-only``: 같은 type, 다른 name_normalized 이지만 (a) 한쪽 이름이
          다른 쪽의 substring 이거나 (b) prefix 4글자 공유. 약한 alias 후보 —
          T4 의 description 임베딩이 실제로 잡아내야 하는 케이스.
        - 두 유형을 모두 포함해 τ 가 자명 양성에만 최적화되지 않게 한다.

    **음성**:
        - ``unrelated``: 다른 name_normalized + 다른 type (기본).
        - ``type-drift``: 같은 name_normalized 인데 다른 type (system → service
          시나리오의 반대 — T4 type-agnostic 흡수 의도가 false positive 를
          만드는 케이스).

    Returns:
        (positives, negatives) — 각 쌍은 (node_a, node_b, kind) 의 3-tuple.
    """
    by_normalized_and_type: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    by_normalized_only: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for n in nodes:
        by_normalized_and_type[(n["name_normalized"], n["type"])].append(n)
        by_normalized_only[n["name_normalized"]].append(n)
        by_type[n["type"]].append(n)

    positives: list[tuple[dict, dict, str]] = []

    # 양성 1: trivial-normalize (같은 normalized + type)
    for group in by_normalized_and_type.values():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                positives.append((group[i], group[j], "trivial-normalize"))

    # 양성 2: alias-only (S3 N-H2 보강) — 같은 type 내에서 이름이 다른 alias.
    # substring/prefix 휴리스틱으로 약한 양성 후보 발굴.
    for etype, group in by_type.items():
        if len(group) < 2:
            continue
        # type 그룹 내에서 normalized 이름이 다른 쌍 중 substring/prefix 공유
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                ni = group[i]["name_normalized"]
                nj = group[j]["name_normalized"]
                if ni == nj:
                    continue  # trivial 과 중복
                # substring 또는 4글자 이상 prefix 공유 → 약한 alias 후보
                shared_prefix = 0
                for k in range(min(len(ni), len(nj))):
                    if ni[k] == nj[k]:
                        shared_prefix += 1
                    else:
                        break
                if ni in nj or nj in ni or shared_prefix >= 4:
                    positives.append((group[i], group[j], "alias-only"))

    # 음성 1: unrelated (다른 normalized + 다른 type)
    negatives: list[tuple[dict, dict, str]] = []
    candidate_count = 0
    while (
        sum(1 for _, _, k in negatives if k == "unrelated") < n_neg
        and candidate_count < n_neg * 10
    ):
        a, b = rng.sample(nodes, 2)
        candidate_count += 1
        if (a["name_normalized"], a["type"]) == (b["name_normalized"], b["type"]):
            continue
        if a["name_normalized"] == b["name_normalized"]:
            continue  # type-drift 후보로 분리
        if a["type"] == b["type"]:
            continue  # 같은 type 다른 이름은 alias 가능성 있어 음성으로 부적합
        negatives.append((a, b, "unrelated"))

    # 음성 2: type-drift (S3 보강) — 같은 정규화 이름 + 다른 type. T4 type-
    # agnostic 매칭이 의도된 흡수 vs false positive 의 경계 케이스.
    for nname, group in by_normalized_only.items():
        types_seen = {g["type"] for g in group}
        if len(types_seen) < 2:
            continue
        # 그룹 내에서 다른 type 인 쌍
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                if group[i]["type"] != group[j]["type"]:
                    negatives.append((group[i], group[j], "type-drift"))

    return positives, negatives


def _apply_threshold_to_module(new_tau: float) -> None:
    """``graph_match.py`` 의 ``DEFAULT_GRAPH_MATCH_THRESHOLD`` 상수를 직접 갱신.

    S3 — auto-tune τ. 정규식으로 안전하게 라인 교체. 기존 주석은 보존.
    여러 매칭이 있어도 첫 정의만 바꾼다 (정규식이 단순화).
    """
    import re as _re  # noqa: PLC0415
    module_path = (
        Path(__file__).resolve().parent.parent
        / "src" / "context_loop" / "eval" / "graph_match.py"
    )
    text = module_path.read_text(encoding="utf-8")
    pattern = _re.compile(
        r"^DEFAULT_GRAPH_MATCH_THRESHOLD\s*=\s*[\d.]+",
        flags=_re.MULTILINE,
    )
    new_line = f"DEFAULT_GRAPH_MATCH_THRESHOLD = {new_tau:.2f}"
    new_text, n_sub = pattern.subn(new_line, text, count=1)
    if n_sub != 1:
        raise RuntimeError(
            "graph_match.py 에서 DEFAULT_GRAPH_MATCH_THRESHOLD 정의를 찾지 못함",
        )
    module_path.write_text(new_text, encoding="utf-8")


def _compute_metrics_at_threshold(
    pos_sims: list[float],
    neg_sims: list[float],
    threshold: float,
) -> dict[str, float]:
    """주어진 τ 에서 precision / recall / F1."""
    tp = sum(1 for s in pos_sims if s >= threshold)
    fn = sum(1 for s in pos_sims if s < threshold)
    fp = sum(1 for s in neg_sims if s >= threshold)
    tn = sum(1 for s in neg_sims if s < threshold)
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "threshold": threshold,
        "tp": tp, "fn": fn, "fp": fp, "tn": tn,
        "precision": precision, "recall": recall, "f1": f1,
    }


async def run(args: argparse.Namespace) -> int:
    config = Config(config_path=Path(args.config) if args.config else None)
    meta_store = MetadataStore(config.data_dir / "metadata.db")
    await meta_store.initialize()

    try:
        logger.info("graph_nodes 로드 중...")
        nodes = await _load_node_descriptions(meta_store)
        logger.info("description 있는 노드 수: %d", len(nodes))

        if len(nodes) < 10:
            print(
                "노드 수가 10 미만이라 캘리브레이션 의미가 없습니다. "
                "인덱싱된 graph_nodes 가 description 을 갖추도록 보강하세요.",
                file=sys.stderr,
            )
            return 2

        # embedding client 빌드
        from context_loop.web.app import _build_embedding_client  # noqa: PLC0415

        embedding_client = _build_embedding_client(config)
        logger.info("description 임베딩 계산 중 (n=%d)...", len(nodes))
        await _embed_descriptions(nodes, embedding_client)
        valid = [n for n in nodes if n.get("embedding") is not None]
        logger.info("임베딩 성공 노드 수: %d", len(valid))

        rng = random.Random(args.seed)
        positives, negatives = _build_pair_buckets(
            valid, n_neg=args.n_neg, rng=rng,
        )
        logger.info("양성 쌍=%d, 음성 쌍=%d", len(positives), len(negatives))

        if not positives:
            print(
                "양성 쌍을 만들 수 없습니다 (같은 정규화 이름+타입 그룹의 중복 "
                "노드 부재). 인덱싱이 alias 를 별도 노드로 만들지 않으면 발생.",
                file=sys.stderr,
            )
            print(
                "Workaround: --synth-pos-from-name 으로 한 노드 description 을 "
                "약간 변형한 합성 양성 쌍을 생성 (구현은 후속 작업).",
                file=sys.stderr,
            )
            return 2

        pos_sims = [
            cosine_similarity(a["embedding"], b["embedding"])
            for a, b, _kind in positives
        ]
        neg_sims = [
            cosine_similarity(a["embedding"], b["embedding"])
            for a, b, _kind in negatives
        ]

        # 종류별 카운트 — S3 보강의 효과 가시화
        pos_kind_counts: dict[str, int] = defaultdict(int)
        for _a, _b, k in positives:
            pos_kind_counts[k] += 1
        neg_kind_counts: dict[str, int] = defaultdict(int)
        for _a, _b, k in negatives:
            neg_kind_counts[k] += 1

        # τ 후보 — 0.50 ~ 0.95, 0.01 간격
        thresholds = [round(0.50 + i * 0.01, 2) for i in range(46)]
        results = [
            _compute_metrics_at_threshold(pos_sims, neg_sims, t)
            for t in thresholds
        ]
        best = max(results, key=lambda r: r["f1"])

        # stdout 표
        print("\n" + "=" * 76)
        print(f"  graph τ 캘리브레이션 — n_pos={len(pos_sims)}, n_neg={len(neg_sims)}")
        print(f"    양성 종류: {dict(pos_kind_counts)}")
        print(f"    음성 종류: {dict(neg_kind_counts)}")
        print("=" * 76)
        print(f"  {'τ':>6s}  {'precision':>10s}  {'recall':>10s}  {'F1':>10s}")
        for r in results:
            mark = " ←" if r["threshold"] == best["threshold"] else ""
            print(
                f"  {r['threshold']:>6.2f}  {r['precision']:>10.4f}  "
                f"{r['recall']:>10.4f}  {r['f1']:>10.4f}{mark}",
            )
        print("=" * 76)
        print(f"  권장 τ = {best['threshold']:.2f}  (F1 = {best['f1']:.4f})")
        print(
            f"  현재 default 0.78 과 차이: "
            f"{best['threshold'] - 0.78:+.2f}",
        )
        if abs(best["threshold"] - 0.78) >= 0.03:
            print(
                f"  → graph_match.py:33 의 DEFAULT_GRAPH_MATCH_THRESHOLD 를 "
                f"{best['threshold']:.2f} 로 갱신 권장",
            )

        # S3 — auto-tune τ 옵션. --apply 명시 시 graph_match.py:33 의 default
        # 를 자동 갱신 (사용자가 결과를 검토 후 명시 활성화).
        if args.apply:
            new_tau = best["threshold"]
            if abs(new_tau - 0.78) >= 0.005:
                _apply_threshold_to_module(new_tau)
                print(
                    f"  [APPLIED] graph_match.py 의 "
                    f"DEFAULT_GRAPH_MATCH_THRESHOLD 를 {new_tau:.2f} 로 갱신",
                )
            else:
                print(
                    f"  [SKIP] 권장 τ({new_tau:.2f}) 가 default 0.78 과 "
                    f"0.005 미만 차이 — 갱신 없음",
                )
        print()

        if args.output:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump({
                    "n_nodes": len(valid),
                    "n_positive_pairs": len(pos_sims),
                    "n_negative_pairs": len(neg_sims),
                    "seed": args.seed,
                    "current_default": 0.78,
                    "recommended": best["threshold"],
                    "recommended_f1": best["f1"],
                    "delta": best["threshold"] - 0.78,
                    "table": results,
                    "positive_sim_stats": {
                        "min": min(pos_sims),
                        "max": max(pos_sims),
                        "mean": sum(pos_sims) / len(pos_sims),
                    },
                    "negative_sim_stats": {
                        "min": min(neg_sims),
                        "max": max(neg_sims),
                        "mean": sum(neg_sims) / len(neg_sims),
                    },
                }, f, indent=2, ensure_ascii=False)
            print(f"  결과 저장: {out_path}")

        return 0
    finally:
        await meta_store.close()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="그래프 T4 임계값 τ 캘리브레이션 (alias 양성 vs 무관 음성)",
    )
    parser.add_argument(
        "--config", "-c", default="",
        help="사용자 config 파일 경로 (미지정 시 ~/.context-loop/config.yaml)",
    )
    parser.add_argument(
        "--n-neg", type=int, default=1000,
        help="음성 쌍 표본 수 (기본 1000). 너무 적으면 통계 불안정.",
    )
    parser.add_argument(
        "--seed", type=int, default=42,
        help="음성 쌍 샘플링 시드 (기본 42).",
    )
    parser.add_argument(
        "--output", "-o", default="",
        help="결과 JSON 저장 경로. 미지정 시 stdout 만.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="S3 — 권장 τ 를 graph_match.py 의 DEFAULT_GRAPH_MATCH_THRESHOLD "
             "에 자동 반영. 기본은 stdout 권장만 출력. default 와의 차이가 "
             "0.005 미만이면 갱신 skip (의미 없는 변경 회피).",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    _setup_logging(args.verbose)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
