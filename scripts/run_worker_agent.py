#!/usr/bin/env python3
"""Worker Agent 수동 테스트 스크립트 — Phase 9.5.

실제 LLM 엔드포인트를 사용하여 Worker Agent의 동작을 확인한다.
결과는 scripts/output/{product}/ 디렉토리에 마크다운 파일로 저장된다.

두 가지 모드를 지원:
1. 로컬 파일 테스트: 지정된 디렉토리의 파일을 직접 분석
2. 전체 파이프라인: Git clone → 상품별 파일 수집 → Worker 실행

사용법:
    # 로컬 디렉토리 테스트 (Git clone 없이)
    python scripts/run_worker_agent.py --local-dir /path/to/code --product vpc

    # 전체 파이프라인 (config yaml의 레포 사용)
    python scripts/run_worker_agent.py --full-pipeline

    # 사용자 config 파일 지정
    python scripts/run_worker_agent.py --local-dir ./src --product myapp -c my_config.yaml

결과 출력 구조:
    scripts/output/{product}/
    ├── {directory}/
    │   ├── _level1_{filename}.md    # Level 1: 파일별 요약
    │   └── _level2_summary.md       # Level 2: 디렉토리 종합 문서
    └── ...
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
from context_loop.ingestion.coordinator import (  # noqa: E402
    CoordinatorAgent,
    DirectorySummary,
)
from context_loop.ingestion.git_config import load_git_source_config  # noqa: E402
from context_loop.ingestion.git_repository import (  # noqa: E402
    FileInfo,
    _repo_clone_dir,
    clone_or_pull,
    collect_files,
    compute_content_hash,
    group_files_by_directory,
    parse_product_scopes,
)
from context_loop.ingestion.worker_agent import LLMWorkerAgent  # noqa: E402

# 결과 출력 디렉토리
OUTPUT_DIR = Path(__file__).resolve().parent / "output"


# ---------------------------------------------------------------------------
# 결과 저장
# ---------------------------------------------------------------------------


def _save_directory_result(product: str, ds: DirectorySummary) -> Path:
    """DirectorySummary를 마크다운 파일로 저장한다.

    Returns:
        생성된 디렉토리 경로.
    """
    # 디렉토리명에서 슬래시를 유지하여 하위 구조 생성
    dir_out = OUTPUT_DIR / product / ds.directory
    dir_out.mkdir(parents=True, exist_ok=True)

    # Level 1: 파일별 요약
    for fs in ds.file_summaries:
        safe_name = Path(fs.relative_path).name
        stem = Path(safe_name).stem
        file_path = dir_out / f"_level1_{stem}.md"
        file_path.write_text(
            f"# {fs.relative_path}\n\n{fs.summary}\n",
            encoding="utf-8",
        )

    # Level 2: 디렉토리 종합 문서
    summary_path = dir_out / "_level2_summary.md"
    summary_path.write_text(
        f"# [{ds.product}] {ds.directory}\n\n{ds.document}\n",
        encoding="utf-8",
    )

    return dir_out


# ---------------------------------------------------------------------------
# 공통: LLM 클라이언트 생성
# ---------------------------------------------------------------------------


def _build_llm_clients(git_config):
    """worker/synthesizer LLM 클라이언트를 생성한다."""
    try:
        worker_llm = git_config.build_llm_client("worker")
        synthesizer_llm = git_config.build_llm_client("synthesizer")
        return worker_llm, synthesizer_llm
    except ValueError as e:
        print(f"오류: {e}")
        print()
        print("config에 LLM 엔드포인트를 설정하세요:")
        print("  llm:")
        print('    endpoint: "http://localhost:11434/v1"')
        print('    model: "your-model"')
        sys.exit(1)


# ---------------------------------------------------------------------------
# 파일 수집
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# 모드 1: 로컬 디렉토리 테스트
# ---------------------------------------------------------------------------


async def run_local(args: argparse.Namespace) -> None:
    """로컬 디렉토리의 파일을 Worker Agent로 분석한다."""
    config = Config(config_path=Path(args.config) if args.config else None)
    git_config = load_git_source_config(config)
    worker_llm, synthesizer_llm = _build_llm_clients(git_config)
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
    print("Worker Agent 실행 중...")
    start = time.time()
    result = await agent.process_directory(dir_name, product, files)
    elapsed = time.time() - start

    # 결과 저장
    out_dir = _save_directory_result(product, result)

    # 콘솔 출력
    print(f"\n{'='*60}")
    print(f"완료: {elapsed:.1f}초")
    print(f"결과 저장: {out_dir}")
    print(f"{'='*60}")

    print(f"\n=== Level 1: 파일 요약 ({len(result.file_summaries)}개) ===\n")
    for fs in result.file_summaries:
        print(f"--- {fs.relative_path} ---")
        print(fs.summary)
        print()

    print(f"=== Level 2: 디렉토리 종합 문서 ===\n")
    print(result.document)


# ---------------------------------------------------------------------------
# 모드 2: 전체 파이프라인
# ---------------------------------------------------------------------------


async def run_full_pipeline(args: argparse.Namespace) -> None:
    """전체 파이프라인: Git clone → 상품별 Worker 실행 → 결과 저장."""
    config = Config(config_path=Path(args.config) if args.config else None)
    git_config = load_git_source_config(config)

    # 설정 검증
    issues = git_config.validate()
    if issues:
        print("설정 문제:")
        for issue in issues:
            print(f"  - {issue}")
        sys.exit(1)

    worker_llm, synthesizer_llm = _build_llm_clients(git_config)
    agent = LLMWorkerAgent(worker_llm, synthesizer_llm)

    print("전체 파이프라인 실행 중...")
    start = time.time()

    total_dirs = 0
    total_files = 0

    for repo_config in git_config.repositories:
        repo_url = repo_config.url
        branch = repo_config.branch

        # git clone/pull
        data_dir = Path(config.get("app.data_dir", "~/.context-loop/data")).expanduser()
        clone_dir = _repo_clone_dir(data_dir, repo_url)
        is_new, _ = await clone_or_pull(repo_url, clone_dir, branch)
        print(f"\n레포: {repo_url} ({'clone' if is_new else 'pull'})")

        # 상품 스코프 파싱
        repo_dict = {
            "url": repo_url,
            "branch": branch,
            "products": repo_config.products,
        }
        scopes = parse_product_scopes(
            repo_dict,
            clone_dir=clone_dir,
            supported_extensions=git_config.supported_extensions or None,
        )

        # 상품별 처리
        for scope in scopes:
            product_files = collect_files(
                clone_dir,
                [scope],
                git_config.supported_extensions,
                git_config.file_size_limit_kb,
            )
            if not product_files:
                print(f"  [{scope.name}] 파일 없음, 건너뜀")
                continue

            # 디렉토리 그룹핑
            groups = group_files_by_directory(
                product_files,
                max_files_per_group=git_config.processing.max_files_per_worker,
                min_files_per_group=git_config.processing.min_files_per_worker,
            )

            print(f"  [{scope.name}] {len(product_files)}개 파일, {len(groups)}개 디렉토리")

            for directory, dir_files in groups.items():
                result = await agent.process_directory(
                    directory, scope.name, dir_files,
                )
                out_dir = _save_directory_result(scope.name, result)
                total_dirs += 1
                total_files += len(result.file_summaries)
                print(f"    {directory} ({len(dir_files)}개 파일) -> {out_dir}")

    elapsed = time.time() - start

    print(f"\n{'='*60}")
    print(f"완료: {elapsed:.1f}초")
    print(f"디렉토리: {total_dirs}개, 파일: {total_files}개")
    print(f"결과 저장: {OUTPUT_DIR}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


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
        help="전체 파이프라인 모드 (Git clone → Worker 실행)",
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
