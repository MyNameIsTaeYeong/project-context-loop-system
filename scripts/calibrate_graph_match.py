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
) -> tuple[list[tuple[dict, dict]], list[tuple[dict, dict]]]:
    """양성/음성 쌍 후보를 만든다.

    - 양성: 같은 name_normalized + 같은 type (alias/표기 변형 가능성).
    - 음성: 다른 name_normalized + 다른 type 의 랜덤 쌍.
    """
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for n in nodes:
        by_key[(n["name_normalized"], n["type"])].append(n)

    positives: list[tuple[dict, dict]] = []
    for group in by_key.values():
        if len(group) < 2:
            continue
        # 같은 그룹의 모든 쌍을 양성으로
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                positives.append((group[i], group[j]))

    # 음성: 랜덤 샘플 (서로 다른 key)
    negatives: list[tuple[dict, dict]] = []
    candidate_count = 0
    while len(negatives) < n_neg and candidate_count < n_neg * 10:
        a, b = rng.sample(nodes, 2)
        candidate_count += 1
        if (a["name_normalized"], a["type"]) == (b["name_normalized"], b["type"]):
            continue
        negatives.append((a, b))

    return positives, negatives


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
            for a, b in positives
        ]
        neg_sims = [
            cosine_similarity(a["embedding"], b["embedding"])
            for a, b in negatives
        ]

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
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    _setup_logging(args.verbose)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
