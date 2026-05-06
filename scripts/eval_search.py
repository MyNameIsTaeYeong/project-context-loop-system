#!/usr/bin/env python3
"""골드셋으로 검색 시스템을 정량 채점한다.

각 질의마다 ``assemble_context_with_sources`` 를 실행하여 top-k 결과를 받고,
정답 문서 ID 와 비교해 Recall@k / Precision@k / MRR / nDCG@k 를 계산한다.

운영 흐름:
1. 베이스라인 실행::

       python scripts/eval_search.py --gold-set eval/gold_set.yaml --label baseline

2. 코드 변경 (예: P0 멀티뷰 임베딩 적용 + 재인덱싱)
3. 변경 후 실행::

       python scripts/eval_search.py --gold-set eval/gold_set.yaml --label multiview

4. ``eval/runs/baseline.summary.json`` ↔ ``multiview.summary.json`` 비교 →
   효과 정량화. 자세한 per-question 결과는 ``*.csv`` 로 저장된다.

옵션:
- ``--judge`` 활성화 시 별도 LLM 으로 응답 품질을 0~5 점으로 채점.
  Generator/시스템과 다른 family 의 모델 권장 (자기 평가 편향 회피).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

# 프로젝트 루트를 sys.path 에 추가
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from context_loop.config import Config  # noqa: E402
from context_loop.eval.gold_set import GoldItem, load_gold_set  # noqa: E402
from context_loop.eval.llm import build_llm_client  # noqa: E402
from context_loop.eval.metrics import (  # noqa: E402
    aggregate,
    hit_at_k,
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
)
from context_loop.mcp.context_assembler import (  # noqa: E402
    AssembledContext,
    assemble_context_with_sources,
)
from context_loop.processor.llm_client import LLMClient, extract_json  # noqa: E402
from context_loop.storage.graph_store import GraphStore  # noqa: E402
from context_loop.storage.metadata_store import MetadataStore  # noqa: E402
from context_loop.storage.vector_store import VectorStore  # noqa: E402

logger = logging.getLogger("eval_search")


# ---------------------------------------------------------------------------
# Judge prompt — 응답 품질 채점 (옵션)
# ---------------------------------------------------------------------------


JUDGE_PROMPT_TEMPLATE = """\
질문: {query}

정답 근거 (출처 청크):
---
{source_chunk}
---

검색 시스템이 반환한 컨텍스트:
---
{retrieved_context}
---

검색된 컨텍스트가 정답 근거의 핵심 내용을 담고 있는지 0~5점으로 평가하라.
- 5: 정답 근거의 모든 핵심 정보를 담음
- 3: 일부만 담음
- 0: 무관하거나 누락

JSON 으로만 출력::

  {{"score": 0~5 정수, "reason": "한 줄 설명"}}
"""


async def judge_answer(
    query: str,
    source_chunk: str,
    retrieved_context: str,
    *,
    judge: LLMClient,
    reasoning_mode: str | None = "off",
) -> tuple[int, str]:
    """Judge LLM 으로 검색된 컨텍스트가 정답을 담는지 0~5 점 채점.

    파싱 실패 시 (-1, "parse_error").
    """
    prompt = JUDGE_PROMPT_TEMPLATE.format(
        query=query,
        source_chunk=source_chunk[:4000],
        retrieved_context=retrieved_context[:6000],
    )
    text = await judge.complete(
        prompt,
        max_tokens=256,
        temperature=0.0,
        reasoning_mode=reasoning_mode,
        purpose="goldset_judge_answer",
    )
    try:
        data = extract_json(text)
    except ValueError:
        return -1, "parse_error"
    if not isinstance(data, dict):
        return -1, "parse_error"
    score_raw = data.get("score")
    if not isinstance(score_raw, (int, float)):
        return -1, "parse_error"
    score = max(0, min(5, int(score_raw)))
    reason = str(data.get("reason") or "").strip()
    return score, reason


# ---------------------------------------------------------------------------
# Single query evaluation
# ---------------------------------------------------------------------------


async def evaluate_one(
    item: GoldItem,
    *,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
    embedding_client: Any,
    llm_client: LLMClient | None,
    reranker_client: Any,
    top_k: int,
    max_chunks: int,
    similarity_threshold: float,
    rerank_enabled: bool,
    rerank_top_k: int | None,
    rerank_score_threshold: float,
    hyde_enabled: bool,
    include_graph: bool,
    judge: LLMClient | None,
    reasoning_mode: str | None,
) -> dict[str, Any]:
    """단일 질의에 대한 검색 + 채점."""
    start = time.perf_counter()
    assembled: AssembledContext = await assemble_context_with_sources(
        item.query,
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=embedding_client,
        llm_client=llm_client,
        reranker_client=reranker_client,
        max_chunks=max_chunks,
        include_graph=include_graph,
        similarity_threshold=similarity_threshold,
        rerank_enabled=rerank_enabled,
        rerank_top_k=rerank_top_k,
        rerank_score_threshold=rerank_score_threshold,
        hyde_enabled=hyde_enabled,
    )
    elapsed_ms = (time.perf_counter() - start) * 1000

    retrieved_doc_ids = [s.document_id for s in assembled.sources]
    relevant = set(item.relevant_doc_ids)

    row: dict[str, Any] = {
        "id": item.id,
        "query": item.query,
        "difficulty": item.difficulty,
        "source_document_id": item.source_document_id,
        "retrieved_doc_ids": retrieved_doc_ids[:top_k],
        "retrieved_count": len(retrieved_doc_ids),
        "relevant_doc_ids": sorted(relevant),
        f"recall@{top_k}": recall_at_k(retrieved_doc_ids, relevant, top_k),
        f"precision@{top_k}": precision_at_k(retrieved_doc_ids, relevant, top_k),
        f"hit@{top_k}": int(hit_at_k(retrieved_doc_ids, relevant, top_k)),
        f"ndcg@{top_k}": ndcg_at_k(retrieved_doc_ids, relevant, top_k),
        "mrr": mrr(retrieved_doc_ids, relevant),
        "elapsed_ms": elapsed_ms,
    }

    if judge is not None:
        # 정답 청크 본문 조회 (없으면 정답 문서의 첫 청크)
        source_text = await _fetch_source_text(item, meta_store)
        score, reason = await judge_answer(
            item.query,
            source_text,
            assembled.context_text,
            judge=judge,
            reasoning_mode=reasoning_mode,
        )
        row["judge_score"] = score
        row["judge_reason"] = reason

    return row


async def _fetch_source_text(item: GoldItem, meta_store: MetadataStore) -> str:
    """골드 항목의 정답 청크 본문을 조회.

    source_chunk_id 가 있으면 그 청크를, 없으면 첫 정답 문서의 첫 청크를 사용.
    """
    if item.source_document_id is not None:
        chunks = await meta_store.get_chunks_by_document(item.source_document_id)
        if item.source_chunk_id:
            for c in chunks:
                if c.get("id") == item.source_chunk_id:
                    return c.get("content") or ""
        if chunks:
            return chunks[0].get("content") or ""
    if item.relevant_doc_ids:
        chunks = await meta_store.get_chunks_by_document(item.relevant_doc_ids[0])
        if chunks:
            return chunks[0].get("content") or ""
    return ""


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def write_csv(rows: list[dict[str, Any]], path: Path) -> None:
    """질의별 결과를 CSV 로 저장.

    list 형 컬럼은 콤마 결합 문자열로 직렬화.
    """
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    keys: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for k in r.keys():
            if k not in seen:
                seen.add(k)
                keys.append(k)

    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        for r in rows:
            row_str = {}
            for k in keys:
                v = r.get(k, "")
                if isinstance(v, list):
                    v = ",".join(str(x) for x in v)
                row_str[k] = v
            writer.writerow(row_str)


def write_summary(
    rows: list[dict[str, Any]],
    path: Path,
    *,
    label: str,
    config_summary: dict[str, Any],
) -> dict[str, Any]:
    """집계 요약을 JSON 으로 저장하고 반환."""
    summary = aggregate(rows)
    out = {
        "label": label,
        "n_queries": len(rows),
        "config": config_summary,
        "metrics": summary,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    return out


def print_summary(summary: dict[str, Any]) -> None:
    print("\n" + "=" * 60)
    print(f"  Run: {summary['label']}  |  N={summary['n_queries']}")
    print("=" * 60)
    metrics = summary.get("metrics", {})
    # 보기 좋은 순서로 키 정렬
    preferred = ["recall@", "precision@", "hit@", "ndcg@", "mrr",
                 "judge_score", "elapsed_ms"]
    keys = sorted(
        metrics.keys(),
        key=lambda k: next(
            (i for i, p in enumerate(preferred) if k.startswith(p)),
            len(preferred),
        ),
    )
    for k in keys:
        v = metrics[k]
        if "ms" in k:
            print(f"  {k:24s} {v:>10.1f}")
        else:
            print(f"  {k:24s} {v:>10.4f}")
    print("=" * 60 + "\n")


# ---------------------------------------------------------------------------
# Stores / clients (web/app.py 와 동일 빌더 재사용)
# ---------------------------------------------------------------------------


def _build_clients(config: Config) -> tuple[Any, Any, Any]:
    """LLM, embedding, reranker 클라이언트를 web/app.py 빌더로 생성."""
    from context_loop.web.app import (  # type: ignore[attr-defined]
        _build_embedding_client,
        _build_llm_client,
        _build_reranker_client,
    )
    return (
        _build_llm_client(config),
        _build_embedding_client(config),
        _build_reranker_client(config),
    )




# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> int:
    config = Config(config_path=Path(args.config) if args.config else None)
    data_dir = config.data_dir

    gold = load_gold_set(Path(args.gold_set))
    if not gold.items:
        print("골드셋에 항목이 없습니다.", file=sys.stderr)
        return 1
    if args.limit:
        gold.items = gold.items[: args.limit]

    meta_store = MetadataStore(data_dir / "metadata.db")
    await meta_store.initialize()
    vector_store = VectorStore(data_dir)
    vector_store.initialize()
    graph_store = GraphStore(meta_store)
    await graph_store.load_from_db()

    llm_client, embedding_client, reranker_client = _build_clients(config)

    # Judge 는 옵션 — 별도 엔드포인트 또는 같은 LLM 재사용.
    # _build_judge_llm 이 config.llm.headers / reasoning_profiles 를 자동 주입한다.
    judge: LLMClient | None = None
    if args.judge:
        if args.judge_endpoint and args.judge_model:
            judge = build_llm_client(
                config,
                endpoint_override=args.judge_endpoint,
                model_override=args.judge_model,
                api_key_override=args.judge_api_key,
                headers_override_json=args.judge_headers,
            )
        else:
            logger.warning(
                "--judge 가 켜져 있지만 --judge-endpoint/--judge-model 미지정 → "
                "기본 llm_client 를 Judge 로 재사용합니다 (자기 평가 편향 가능).",
            )
            judge = llm_client

    similarity_threshold = (
        args.similarity_threshold
        if args.similarity_threshold is not None
        else float(config.get("search.similarity_threshold", 0.0))
    )
    rerank_enabled = (
        args.rerank
        if args.rerank is not None
        else bool(config.get("search.reranker_enabled", False))
    )
    hyde_enabled = (
        args.hyde
        if args.hyde is not None
        else bool(config.get("search.hyde_enabled", False))
    )
    rerank_top_k = config.get("search.reranker_top_k") or None
    rerank_score_threshold = float(config.get("search.reranker_score_threshold", 0.0))

    config_summary = {
        "top_k": args.top_k,
        "max_chunks": args.max_chunks,
        "similarity_threshold": similarity_threshold,
        "rerank_enabled": rerank_enabled,
        "hyde_enabled": hyde_enabled,
        "include_graph": args.include_graph,
        "embedding_model": config.get("processor.embedding_model"),
        "llm_model": config.get("llm.model"),
        "judge_enabled": args.judge,
        "judge_model": args.judge_model or (config.get("llm.model") if args.judge else None),
    }

    rows: list[dict[str, Any]] = []
    try:
        for i, item in enumerate(gold.items):
            logger.info(
                "[%d/%d] q=%s | gold_doc=%s",
                i + 1, len(gold.items), item.id, item.relevant_doc_ids,
            )
            try:
                row = await evaluate_one(
                    item,
                    meta_store=meta_store,
                    vector_store=vector_store,
                    graph_store=graph_store,
                    embedding_client=embedding_client,
                    llm_client=llm_client if args.include_graph else None,
                    reranker_client=reranker_client,
                    top_k=args.top_k,
                    max_chunks=args.max_chunks,
                    similarity_threshold=similarity_threshold,
                    rerank_enabled=rerank_enabled,
                    rerank_top_k=rerank_top_k,
                    rerank_score_threshold=rerank_score_threshold,
                    hyde_enabled=hyde_enabled,
                    include_graph=args.include_graph,
                    judge=judge,
                    reasoning_mode=args.reasoning_mode,
                )
                rows.append(row)
            except Exception as exc:
                logger.exception("질의 %s 실패: %s", item.id, exc)
                rows.append({
                    "id": item.id,
                    "query": item.query,
                    "error": str(exc),
                })

        out_dir = Path(args.output_dir)
        csv_path = out_dir / f"{args.label}.csv"
        summary_path = out_dir / f"{args.label}.summary.json"

        write_csv(rows, csv_path)
        summary = write_summary(
            rows, summary_path, label=args.label, config_summary=config_summary,
        )
        print_summary(summary)
        print(f"  details : {csv_path}")
        print(f"  summary : {summary_path}\n")

    finally:
        await meta_store.close()

    return 0


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _parse_optional_bool(v: str | None) -> bool | None:
    if v is None:
        return None
    return v.lower() in ("1", "true", "yes", "on")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="골드셋으로 검색 시스템 정량 채점",
    )
    parser.add_argument("--config", "-c", default="")
    parser.add_argument(
        "--gold-set", "-g", required=True,
        help="골드셋 YAML 경로",
    )
    parser.add_argument(
        "--label", default="run",
        help="출력 파일 접두 (예: 'baseline', 'multiview')",
    )
    parser.add_argument(
        "--output-dir", default="eval/runs",
        help="결과 저장 디렉토리 (기본: eval/runs)",
    )
    parser.add_argument(
        "--top-k", type=int, default=5,
        help="메트릭 계산용 top-k (기본 5)",
    )
    parser.add_argument(
        "--max-chunks", type=int, default=10,
        help="검색 단계의 max_chunks (기본 10). top-k 보다 크게 잡아 over-fetch.",
    )
    parser.add_argument(
        "--similarity-threshold", type=float, default=None,
        help="config.search.similarity_threshold 오버라이드",
    )
    parser.add_argument(
        "--rerank", type=lambda v: _parse_optional_bool(v), default=None,
        help="리랭커 사용 여부 (true/false). 미지정 시 config 따름.",
    )
    parser.add_argument(
        "--hyde", type=lambda v: _parse_optional_bool(v), default=None,
        help="HyDE 사용 여부 (true/false). 미지정 시 config 따름.",
    )
    parser.add_argument(
        "--include-graph", action="store_true", default=True,
        help="그래프 컨텍스트 포함 (기본 켜짐)",
    )
    parser.add_argument(
        "--no-graph", action="store_false", dest="include_graph",
        help="그래프 컨텍스트 제외",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="평가할 질의 수 제한 (0 이면 전체)",
    )
    parser.add_argument(
        "--judge", action="store_true",
        help="Judge LLM 으로 응답 품질 0~5 점 채점 (느림, 비용 발생)",
    )
    # Judge override 가 모두 비면 시스템 LLM 재사용 (편향 경고 표시).
    # 별도 엔드포인트 지정 시 config.llm.headers / reasoning_profiles 자동 주입,
    # --judge-headers JSON 으로 헤더 통째 교체 가능.
    parser.add_argument("--judge-endpoint", default="")
    parser.add_argument("--judge-model", default="")
    parser.add_argument("--judge-api-key", default="")
    parser.add_argument(
        "--judge-headers", default="",
        help="Judge 전용 헤더 JSON (예: '{\"X-Org-Id\":\"abc\"}'). "
             "미지정 시 config.llm.headers 사용.",
    )
    parser.add_argument(
        "--reasoning-mode", default="off",
        help="LLM reasoning_mode (config.llm.reasoning_profiles 키, "
             "Judge 호출에 적용, 기본 'off')",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose)
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
