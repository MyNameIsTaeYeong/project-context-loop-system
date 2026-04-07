#!/usr/bin/env python3
"""Worker Agent 수동 테스트 스크립트 — Phase 9.5.

실제 LLM 엔드포인트를 사용하여 Worker Agent의 동작을 확인한다.
두 가지 모드를 지원:
1. 로컬 파일 테스트: 지정된 디렉토리의 파일을 직접 분석
2. 전체 파이프라인: Git clone → 상품별 파일 수집 → Worker 실행 → DB 저장

사용법:
    # 로컬 디렉토리 테스트 (Git clone 없이)
    python scripts/run_worker_agent.py --local-dir /path/to/code --product vpc

    # 전체 파이프라인 (config yaml의 레포 사용)
    python scripts/run_worker_agent.py --full-pipeline

    # 사용자 config 파일 지정
    python scripts/run_worker_agent.py --local-dir ./src --product myapp -c my_config.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from context_loop.config import Config  # noqa: E402
from context_loop.ingestion.coordinator import CoordinatorAgent  # noqa: E402
from context_loop.ingestion.git_config import load_git_source_config  # noqa: E402
from context_loop.ingestion.git_repository import (  # noqa: E402
    FileInfo,
    collect_files,
    compute_content_hash,
    parse_product_scopes,
)
from context_loop.ingestion.worker_agent import LLMWorkerAgent  # noqa: E402
from context_loop.storage.metadata_store import MetadataStore  # noqa: E402


def _collect_local_files(
    directory: Path,
    product: str,
    extensions: list[str] | None = None,
    max_files: int = 20,
) -> list[FileInfo]:
    """로컬 디렉토리에서 파일을 수집한다."""
    files: list[FileInfo] = []
    for p in sorted(directory.rglob("*")):
        if not p.is_file():
            continue
        if extensions and p.suffix not in extensions:
            continue
        # 바이너리/숨김 파일 스킵
        if p.name.startswith(".") or p.suffix in (".pyc", ".class", ".o"):
            continue
        try:
            content = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, PermissionError):
            continue

        rel = str(p.relative_to(directory))
        files.append(FileInfo(
            relative_path=rel,
            absolute_path=p,
            product=product,
            content=content,
            content_hash=compute_content_hash(content),
            size_bytes=p.stat().st_size,
        ))
        if len(files) >= max_files:
            break
    return files


async def run_local(args: argparse.Namespace) -> None:
    """로컬 디렉토리의 파일을 Worker Agent로 분석한다."""
    config = Config(config_path=Path(args.config) if args.config else None)
    git_config = load_git_source_config(config)

    # LLM 클라이언트 생성
    try:
        worker_llm = git_config.build_llm_client("worker")
        synthesizer_llm = git_config.build_llm_client("synthesizer")
    except ValueError as e:
        print(f"오류: {e}")
        print()
        print("config에 LLM 엔드포인트를 설정하세요:")
        print("  llm:")
        print('    endpoint: "http://localhost:11434/v1"')
        print('    model: "your-model"')
        sys.exit(1)

    agent = LLMWorkerAgent(worker_llm, synthesizer_llm)

    # 파일 수집
    local_dir = Path(args.local_dir).resolve()
    if not local_dir.is_dir():
        print(f"오류: 디렉토리가 존재하지 않습니다: {local_dir}")
        sys.exit(1)

    extensions = None
    if args.ext:
        extensions = [
            e.strip() if e.strip().startswith(".") else f".{e.strip()}"
            for e in args.ext.split(",") if e.strip()
        ]

    product = args.product or local_dir.name
    files = _collect_local_files(local_dir, product, extensions, max_files=args.max_files)

    if not files:
        print(f"오류: {local_dir}에서 파일을 찾을 수 없습니다.")
        sys.exit(1)

    print(f"대상 디렉토리: {local_dir}")
    print(f"상품명: {product}")
    print(f"수집된 파일: {len(files)}개")
    for f in files:
        print(f"  {f.relative_path} ({f.size_bytes:,} bytes)")
    print()

    # Worker Agent 실행
    dir_name = str(local_dir.name)
    print(f"Worker Agent 실행 중...")
    start = time.time()
    result = await agent.process_directory(dir_name, product, files)
    elapsed = time.time() - start

    # 결과 출력
    print(f"\n{'='*60}")
    print(f"완료: {elapsed:.1f}초")
    print(f"{'='*60}")

    print(f"\n=== Level 1: 파일 요약 ({len(result.file_summaries)}개) ===\n")
    for fs in result.file_summaries:
        print(f"--- {fs.relative_path} ---")
        print(fs.summary)
        print()

    print(f"\n=== Level 2: 디렉토리 종합 문서 ===\n")
    print(result.document)


async def run_full_pipeline(args: argparse.Namespace) -> None:
    """전체 파이프라인: Git clone → Worker → DB 저장."""
    config = Config(config_path=Path(args.config) if args.config else None)
    git_config = load_git_source_config(config)

    # 설정 검증
    issues = git_config.validate()
    if issues:
        print("설정 문제:")
        for issue in issues:
            print(f"  - {issue}")
        sys.exit(1)

    # LLM 클라이언트 생성
    try:
        worker_llm = git_config.build_llm_client("worker")
        synthesizer_llm = git_config.build_llm_client("synthesizer")
    except ValueError as e:
        print(f"오류: {e}")
        sys.exit(1)

    # 스토어 초기화
    data_dir = Path(config.get("app.data_dir", "~/.context-loop/data")).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    store = MetadataStore(data_dir / "metadata.db")
    await store.initialize()

    # Worker Agent + Coordinator 실행 (Category Agent 없이)
    worker = LLMWorkerAgent(worker_llm, synthesizer_llm)
    coordinator = CoordinatorAgent(
        store=store,
        config=config,
        git_config=git_config,
        worker=worker,
        category_agent=None,
    )

    print("전체 파이프라인 실행 중...")
    start = time.time()
    result = await coordinator.run_and_store()
    elapsed = time.time() - start

    # 결과 출력
    print(f"\n{'='*60}")
    print(f"완료: {elapsed:.1f}초")
    print(f"{'='*60}")
    print(f"  상품: {len(result.product_results)}개")
    print(f"  디렉토리: {result.total_directories}개")
    print(f"  파일: {result.total_files_processed}개")
    print(f"  오류: {len(result.errors)}개")

    for pr in result.product_results:
        print(f"\n  [{pr.product}]")
        for ds in pr.directory_summaries:
            print(f"    📁 {ds.directory} ({len(ds.file_summaries)}개 파일)")
            preview = ds.document[:120].replace("\n", " ")
            print(f"       {preview}...")

        if pr.errors:
            print(f"    오류:")
            for err in pr.errors:
                print(f"      - {err}")

    if result.errors:
        print(f"\n전체 오류:")
        for err in result.errors:
            print(f"  - {err}")

    await store.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Worker Agent 수동 테스트 (실제 LLM 엔드포인트 사용)",
    )
    parser.add_argument(
        "--local-dir", "-d",
        default="",
        help="분석할 로컬 디렉토리 경로",
    )
    parser.add_argument(
        "--product", "-p",
        default="",
        help="상품명 (--local-dir 모드에서 사용, 미지정 시 디렉토리명)",
    )
    parser.add_argument(
        "--ext", "-e",
        default="",
        help="확장자 필터 (쉼표 구분, 예: .go,.py)",
    )
    parser.add_argument(
        "--max-files", "-m",
        type=int,
        default=20,
        help="최대 파일 수 (기본: 20)",
    )
    parser.add_argument(
        "--full-pipeline",
        action="store_true",
        help="전체 파이프라인 모드 (Git clone → Worker → DB 저장)",
    )
    parser.add_argument(
        "--config", "-c",
        default="",
        help="사용자 config 파일 경로",
    )
    args = parser.parse_args()

    if args.full_pipeline:
        asyncio.run(run_full_pipeline(args))
    elif args.local_dir:
        asyncio.run(run_local(args))
    else:
        parser.print_help()
        print()
        print("예시:")
        print("  python scripts/run_worker_agent.py --local-dir ./src/context_loop/ingestion --product context-loop")
        print("  python scripts/run_worker_agent.py --full-pipeline")


if __name__ == "__main__":
    main()
