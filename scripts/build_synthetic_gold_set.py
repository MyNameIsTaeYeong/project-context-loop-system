#!/usr/bin/env python3
"""LLM 으로 검색 평가용 골드셋을 자동 생성한다.

원리:
1. 인덱싱된 청크에서 계층 샘플링 (source_type 별 균등)
2. Generator LLM 으로 청크당 N개 질문 생성 (역방향 생성)
3. Judge LLM 의 3단계 품질 게이트로 사기성/노이즈 질문 탈락
4. 통과한 (질문, 정답 문서ID) 페어를 YAML 골드셋으로 저장

Generator 와 Judge 를 서로 다른 모델로 분리하면 자기 평가 편향이 줄어든다.

사용법
------

기본 (config 의 llm.* 를 Generator/Judge 양쪽에 사용)::

    python scripts/build_synthetic_gold_set.py \\
        --config ~/.context-loop/config.yaml \\
        --n-chunks 30 \\
        --questions-per-chunk 2 \\
        --output eval/gold_set.yaml

Generator/Judge 분리 (편향 회피, 권장)::

    python scripts/build_synthetic_gold_set.py \\
        --generator-endpoint http://strong-model:8080/v1 \\
        --generator-model gpt-4o \\
        --judge-endpoint http://other-family:8080/v1 \\
        --judge-model claude-haiku \\
        --output eval/gold_set.yaml

source_type 제한, 시드 고정 (재현성)::

    python scripts/build_synthetic_gold_set.py \\
        --source-types git_code,confluence \\
        --seed 42 \\
        --output eval/gold_set.yaml

빠른 실험 (게이트 OFF — 디버그/탐색 전용)::

    python scripts/build_synthetic_gold_set.py --no-filter --n-chunks 5
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from context_loop.processor.llm_client import LLMClient

# 프로젝트 루트를 sys.path 에 추가
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from context_loop.config import Config  # noqa: E402
from context_loop.eval.gold_set import GoldItem, GoldSet, save_gold_set  # noqa: E402
from context_loop.eval.llm import build_eval_llm_client, role_is_configured  # noqa: E402
from context_loop.eval.synth import (  # noqa: E402
    SynthRunConfig,
    filter_question,
    generate_questions,
    resolve_synth_run_config,
    stratified_sample,
)
from context_loop.storage.metadata_store import MetadataStore  # noqa: E402

logger = logging.getLogger("build_synthetic_gold_set")


# ---------------------------------------------------------------------------
# Chunk loading
# ---------------------------------------------------------------------------


async def load_candidate_chunks(
    store: MetadataStore,
    *,
    source_types: list[str] | None,
    min_chars: int,
    max_chars: int,
) -> list[dict[str, Any]]:
    """metadata_store 에서 청크 후보를 로드한다.

    각 항목 dict 형태::

        {
            "chunk_id": str,
            "document_id": int,
            "source_type": str,
            "content": str,            # 청크 본문 (Generator 입력)
            "section_path": str,
            "title": str,
        }

    너무 짧은(최소 chars 미만) 청크는 의미 추출이 어려워 제외한다.
    너무 긴 청크는 토큰 예산 폭주 방지로 제외 (기본 8000자 → 약 2000~3000 토큰).
    """
    documents = await store.list_documents()
    by_id = {d["id"]: d for d in documents}

    out: list[dict[str, Any]] = []
    for doc in documents:
        if source_types and doc.get("source_type") not in source_types:
            continue
        chunks = await store.get_chunks_by_document(doc["id"])
        for c in chunks:
            content: str = c.get("content") or ""
            if len(content) < min_chars or len(content) > max_chars:
                continue
            out.append({
                "chunk_id": c["id"],
                "document_id": doc["id"],
                "source_type": doc.get("source_type", ""),
                "content": content,
                "section_path": c.get("section_path") or "",
                "title": doc.get("title") or "",
            })
    # 결정론적 순서 보장 (같은 시드 → 같은 결과)
    out.sort(key=lambda x: (x["document_id"], x["chunk_id"]))
    logger.info(
        "후보 청크 로드 완료 — total=%d, doc_count=%d",
        len(out), len(by_id),
    )
    return out


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


async def build(
    run_config: SynthRunConfig,
    *,
    config: Config,
    generator: LLMClient,
    judge: LLMClient,
) -> GoldSet:
    """``SynthRunConfig`` 에 정의된 파라미터로 전체 파이프라인 실행."""

    rng = random.Random(run_config.seed)

    store = MetadataStore(config.data_dir / "metadata.db")
    await store.initialize()

    try:
        candidates = await load_candidate_chunks(
            store,
            source_types=run_config.source_types,
            min_chars=run_config.min_chars,
            max_chars=run_config.max_chars,
        )
        if not candidates:
            raise RuntimeError("후보 청크가 없습니다. 인덱싱된 문서가 있는지 확인하세요.")

        sampled = stratified_sample(
            candidates, n_total=run_config.n_chunks,
            key="source_type", rng=rng,
        )
        logger.info(
            "청크 샘플링 완료 — sampled=%d (요청 %d)",
            len(sampled), run_config.n_chunks,
        )

        # 일반성 게이트용 distractor 풀: 샘플과 다른 문서의 청크에서 무작위 추출
        distractor_pool = [
            c for c in candidates
            if c["chunk_id"] not in {s["chunk_id"] for s in sampled}
        ]
        rng.shuffle(distractor_pool)

        items: list[GoldItem] = []
        stats = {"generated": 0, "passed": 0, "fail_not_answerable": 0,
                 "fail_leakage": 0, "fail_generic": 0, "fail_parse": 0}

        for i, chunk in enumerate(sampled):
            logger.info(
                "[%d/%d] 질문 생성 — doc=%d, chunk=%s, source_type=%s",
                i + 1, len(sampled), chunk["document_id"],
                chunk["chunk_id"][:8], chunk["source_type"],
            )

            generated = await generate_questions(
                chunk["content"],
                n=run_config.questions_per_chunk,
                generator=generator,
                reasoning_mode=run_config.reasoning_mode,
            )
            stats["generated"] += len(generated)

            if not generated:
                logger.warning("  → 생성 실패 (빈 응답)")
                stats["fail_parse"] += 1
                continue

            # distractor 는 같은 source_type 내에서 우선 골라야 식별자 충돌이 적다
            same_type_distractors = [
                c for c in distractor_pool
                if c["source_type"] == chunk["source_type"]
            ][: run_config.n_distractors]
            if len(same_type_distractors) < run_config.n_distractors:
                # 부족하면 다른 type 으로 채움
                fill = [c for c in distractor_pool if c not in same_type_distractors]
                same_type_distractors += fill[
                    : run_config.n_distractors - len(same_type_distractors)
                ]

            for j, gq in enumerate(generated):
                if not run_config.apply_filter:
                    items.append(GoldItem(
                        id=f"q{len(items) + 1:04d}",
                        query=gq.query,
                        relevant_doc_ids=[chunk["document_id"]],
                        source_chunk_id=chunk["chunk_id"],
                        source_document_id=chunk["document_id"],
                        source_section_path=chunk["section_path"],
                        difficulty=gq.difficulty,
                        synthesized=True,
                    ))
                    stats["passed"] += 1
                    continue

                report = await filter_question(
                    gq.query,
                    chunk["content"],
                    [d["content"] for d in same_type_distractors],
                    judge=judge,
                    reasoning_mode=run_config.reasoning_mode,
                )
                if not report.passed:
                    key = f"fail_{report.reason}" if report.reason else "fail_parse"
                    stats[key] = stats.get(key, 0) + 1
                    logger.info(
                        "  q%d 탈락 — reason=%s, query=%s",
                        j + 1, report.reason, gq.query[:80],
                    )
                    continue

                items.append(GoldItem(
                    id=f"q{len(items) + 1:04d}",
                    query=gq.query,
                    relevant_doc_ids=[chunk["document_id"]],
                    source_chunk_id=chunk["chunk_id"],
                    source_document_id=chunk["document_id"],
                    source_section_path=chunk["section_path"],
                    difficulty=gq.difficulty,
                    synthesized=True,
                ))
                stats["passed"] += 1
                logger.info(
                    "  q%d 통과 — query=%s", j + 1, gq.query[:80],
                )

        gold = GoldSet(
            version=1,
            items=items,
            metadata={
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "n_chunks_sampled": len(sampled),
                "questions_per_chunk": run_config.questions_per_chunk,
                "filter_applied": run_config.apply_filter,
                "seed": run_config.seed,
                "source_types": run_config.source_types or [],
                "stats": stats,
                **run_config.metadata,
            },
        )
        save_gold_set(gold, run_config.output_path)
        logger.info(
            "골드셋 저장 — path=%s, items=%d, stats=%s",
            run_config.output_path, len(items), stats,
        )
        return gold

    finally:
        await store.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="검색 평가용 합성 골드셋 생성 (LLM 기반). "
                    "운영 디폴트는 config.eval.synth.* / eval.generator.* / "
                    "eval.judge.* 에 두고, 아래 인자는 일회성 override 로 사용.",
    )
    parser.add_argument(
        "--config", "-c", default="",
        help="사용자 config 파일 경로 (미지정 시 ~/.context-loop/config.yaml)",
    )
    # 합성 파라미터 — 미지정 시 config.eval.synth.* 사용
    parser.add_argument(
        "--output", "-o", default=None,
        help="저장 경로. 미지정 시 config.eval.synth.output.",
    )
    parser.add_argument(
        "--n-chunks", type=int, default=None,
        help="샘플링할 청크 수. 미지정 시 config.eval.synth.n_chunks.",
    )
    parser.add_argument(
        "--questions-per-chunk", type=int, default=None,
        help="청크당 생성 질문 수. 미지정 시 config.eval.synth.questions_per_chunk.",
    )
    parser.add_argument(
        "--source-types", default=None,
        help="쉼표 구분 source_type 화이트리스트 (예: 'git_code,confluence'). "
             "미지정 시 config.eval.synth.source_types (빈 배열 = 전체).",
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="랜덤 시드. 미지정 시 config.eval.synth.seed (null = 결정론적 정렬).",
    )
    parser.add_argument(
        "--no-filter", action="store_true",
        help="품질 게이트 강제 OFF (디버그 전용 — config 무시).",
    )
    parser.add_argument(
        "--n-distractors", type=int, default=None,
        help="일반성 게이트의 무관 청크 수. 미지정 시 config.eval.synth.n_distractors.",
    )
    parser.add_argument(
        "--min-chars", type=int, default=None,
        help="최소 청크 길이. 미지정 시 config.eval.synth.min_chars.",
    )
    parser.add_argument(
        "--max-chars", type=int, default=None,
        help="최대 청크 길이. 미지정 시 config.eval.synth.max_chars.",
    )
    parser.add_argument(
        "--reasoning-mode", default=None,
        help="LLM reasoning_mode 프로파일 키. 미지정 시 config.eval.synth.reasoning_mode.",
    )
    # Generator/Judge — 운영 디폴트는 config.eval.{generator,judge}.* 에 둔다.
    # 아래 CLI 인자는 일회성 실험용 override — 미지정 시 config 값 사용,
    # config 도 비어 있으면 상위 llm.* 로 폴백한다.
    parser.add_argument("--generator-endpoint", default="")
    parser.add_argument("--generator-model", default="")
    parser.add_argument("--generator-api-key", default="")
    parser.add_argument(
        "--generator-headers", default="",
        help="Generator 헤더 JSON (예: '{\"X-Org-Id\":\"abc\"}'). "
             "미지정 시 config.eval.generator.headers, 그것도 비면 llm.headers.",
    )
    parser.add_argument("--judge-endpoint", default="")
    parser.add_argument("--judge-model", default="")
    parser.add_argument("--judge-api-key", default="")
    parser.add_argument(
        "--judge-headers", default="",
        help="Judge 헤더 JSON. 미지정 시 config.eval.judge.headers, "
             "그것도 비면 llm.headers.",
    )

    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    _setup_logging(args.verbose)

    config = Config(config_path=Path(args.config) if args.config else None)

    # 합성 파라미터 합성 (CLI > config.eval.synth.*)
    run_config = resolve_synth_run_config(
        config,
        output=args.output,
        n_chunks=args.n_chunks,
        questions_per_chunk=args.questions_per_chunk,
        source_types=args.source_types,
        n_distractors=args.n_distractors,
        min_chars=args.min_chars,
        max_chars=args.max_chars,
        reasoning_mode=args.reasoning_mode,
        seed=args.seed,
        no_filter=args.no_filter,
    )
    logger.info(
        "Synth run config — output=%s, n_chunks=%d, qpc=%d, source_types=%s, "
        "filter=%s, reasoning=%s, seed=%s",
        run_config.output_path, run_config.n_chunks,
        run_config.questions_per_chunk, run_config.source_types,
        run_config.apply_filter, run_config.reasoning_mode, run_config.seed,
    )

    generator = build_eval_llm_client(
        config, "generator",
        endpoint_override=args.generator_endpoint,
        model_override=args.generator_model,
        api_key_override=args.generator_api_key,
        headers_override_json=args.generator_headers,
    )
    judge = build_eval_llm_client(
        config, "judge",
        endpoint_override=args.judge_endpoint,
        model_override=args.judge_model,
        api_key_override=args.judge_api_key,
        headers_override_json=args.judge_headers,
    )

    gen_configured = role_is_configured(
        config, "generator",
        endpoint_override=args.generator_endpoint,
        model_override=args.generator_model,
    )
    judge_configured = role_is_configured(
        config, "judge",
        endpoint_override=args.judge_endpoint,
        model_override=args.judge_model,
    )
    if not (gen_configured or judge_configured):
        logger.warning(
            "Generator/Judge 모두 system LLM (llm.*) 과 동일 — 자기 평가 편향 가능. "
            "config.yaml 의 eval.generator / eval.judge 에 별도 모델을 지정하거나 "
            "--generator-* / --judge-* 인자를 사용하세요.",
        )

    asyncio.run(build(
        run_config,
        config=config,
        generator=generator,
        judge=judge,
    ))


if __name__ == "__main__":
    main()
