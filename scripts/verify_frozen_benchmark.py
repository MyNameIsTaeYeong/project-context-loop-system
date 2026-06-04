#!/usr/bin/env python3
"""고정 기준 벤치마크(frozen benchmark) 검증·생성 게이트 (Phase 5).

절대 점수를 신뢰·인용하기 전, "지금 인덱스/코퍼스/골드셋이 동결 시점과 같은가"
를 기계검증한다. 운영 출시 게이트가 CI 에서 호출하는 관문.

벤치마크 디렉터리 구조::

    eval/frozen/<name>/
        gold_set.yaml            # 불변 골드셋
        benchmark.manifest.json  # 동결 당시 지문/설정

사용법::

    # 1) 현재 인덱스/골드셋으로 manifest 생성(동결)
    python scripts/verify_frozen_benchmark.py --benchmark eval/frozen/main --create

    # 2) 인용/게이트 전 검증 — 드리프트 시 비정상 종료(exit 2)
    python scripts/verify_frozen_benchmark.py --benchmark eval/frozen/main

검증 대상 키: gold_set_sha256, vector_store_sha256, corpus_sha256,
graph_store_sha256. 하나라도 다르면 드리프트로 간주한다.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from context_loop.config import Config  # noqa: E402
from context_loop.eval.index_fingerprint import (  # noqa: E402
    combined_index_fingerprint,
)
from context_loop.storage.graph_store import GraphStore  # noqa: E402
from context_loop.storage.metadata_store import MetadataStore  # noqa: E402
from context_loop.storage.vector_store import VectorStore  # noqa: E402

MANIFEST_NAME = "benchmark.manifest.json"
GOLD_SET_NAME = "gold_set.yaml"

# 동치성 검증 대상 평탄 키.
ANCHOR_KEYS = (
    "gold_set_sha256",
    "vector_store_sha256",
    "corpus_sha256",
    "graph_store_sha256",
)


def _gold_set_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


async def compute_anchors(config: Config, gold_set_path: Path) -> dict[str, Any]:
    """현재 라이브 스토어 + 골드셋으로 앵커 지문을 계산한다."""
    data_dir = config.data_dir
    meta_store = MetadataStore(data_dir / "metadata.db")
    await meta_store.initialize()
    vector_store = VectorStore(data_dir)
    vector_store.initialize()
    graph_store = GraphStore(meta_store)
    await graph_store.load_from_db()
    try:
        fp = await combined_index_fingerprint(
            vector_store, graph_store, meta_store,
        )
    finally:
        await meta_store.close()

    return {
        "gold_set_sha256": _gold_set_sha256(gold_set_path),
        "vector_store_sha256": fp.get("vector", {}).get("sha256", ""),
        "corpus_sha256": fp.get("corpus", {}).get("sha256", ""),
        "graph_store_sha256": fp.get("graph", {}).get("sha256", ""),
        "index_fingerprint": fp,
    }


def compare_anchors(
    manifest: dict[str, Any], current: dict[str, Any],
) -> list[str]:
    """manifest 와 현재 앵커를 비교하여 드리프트 키 목록을 반환한다."""
    drift: list[str] = []
    for k in ANCHOR_KEYS:
        want = manifest.get(k, "")
        got = current.get(k, "")
        if want != got:
            drift.append(f"{k}: manifest={want!r} != current={got!r}")
    return drift


async def run(args: argparse.Namespace) -> int:
    bench_dir = Path(args.benchmark)
    gold_path = bench_dir / GOLD_SET_NAME
    manifest_path = bench_dir / MANIFEST_NAME
    config = Config(config_path=Path(args.config) if args.config else None)

    if args.create:
        if not gold_path.exists():
            print(f"골드셋이 없습니다: {gold_path}", file=sys.stderr)
            return 2
        bench_dir.mkdir(parents=True, exist_ok=True)
        anchors = await compute_anchors(config, gold_path)
        manifest = {
            "name": bench_dir.name,
            "created_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"),
            "gold_set_sha256": anchors["gold_set_sha256"],
            "vector_store_sha256": anchors["vector_store_sha256"],
            "corpus_sha256": anchors["corpus_sha256"],
            "graph_store_sha256": anchors["graph_store_sha256"],
            "index_fingerprint": anchors["index_fingerprint"],
            "embedding_model": str(config.get("processor.embedding_model") or ""),
            "llm_model": config.get("llm.model"),
        }
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)
        print(f"✅ manifest 생성: {manifest_path}")
        for k in ANCHOR_KEYS:
            print(f"   {k} = {manifest[k]}")
        return 0

    # 검증 모드
    if not manifest_path.exists():
        print(
            f"manifest 가 없습니다: {manifest_path}\n"
            f"먼저 --create 로 동결하세요.",
            file=sys.stderr,
        )
        return 2
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)
    current = await compute_anchors(config, gold_path)
    drift = compare_anchors(manifest, current)
    if drift:
        print("❌ 벤치마크 드리프트 감지 — 절대 점수 인용 불가:", file=sys.stderr)
        for d in drift:
            print(f"   - {d}", file=sys.stderr)
        print(
            "\n코퍼스/인덱스/골드셋이 동결 시점과 다릅니다. 재인덱싱했다면 "
            "--create 로 재동결 후 재캘리브레이션하세요.",
            file=sys.stderr,
        )
        return 2
    print(f"✅ 벤치마크 앵커 일치 — {bench_dir} (절대 점수 인용 가능)")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="고정 기준 벤치마크 검증/생성")
    parser.add_argument(
        "--benchmark", required=True,
        help="벤치마크 디렉터리 (gold_set.yaml + benchmark.manifest.json)",
    )
    parser.add_argument(
        "--create", action="store_true",
        help="현재 인덱스/골드셋으로 manifest 를 생성(동결)한다.",
    )
    parser.add_argument("--config", "-c", default="")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
