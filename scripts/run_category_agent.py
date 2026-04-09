#!/usr/bin/env python3
"""Category Agent 수동 테스트 스크립트 — Phase 9.6.

Worker Agent가 생성한 Level 2 디렉토리 문서를 입력으로 받아
Category Agent의 Level 3 관점 문서 생성을 테스트한다.

두 가지 모드를 지원:
1. Worker 출력 디렉토리 사용: scripts/output/{product}/ 하위의 _level2_summary.md 파일 활용
2. 전체 파이프라인: Git clone → Worker → Category Agent 순차 실행

사용법:
    # Worker 출력 디렉토리에서 Level 2 문서를 읽어 Category Agent 실행
    python scripts/run_category_agent.py --input-dir scripts/output/vpc --product vpc

    # 특정 카테고리만 실행
    python scripts/run_category_agent.py --input-dir scripts/output/vpc --product vpc --categories architecture,development

    # 전체 파이프라인 (Git clone → Worker → Category)
    python scripts/run_category_agent.py --full-pipeline

    # 사용자 config 파일 지정
    python scripts/run_category_agent.py --input-dir scripts/output/vpc --product vpc -c my_config.yaml

결과 출력 구조:
    scripts/output/{product}/
    └── category/
        ├── architecture.md
        ├── development.md
        ├── infrastructure.md
        ├── pricing.md
        └── business.md
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
from context_loop.ingestion.category_agent import LLMCategoryAgent  # noqa: E402
from context_loop.ingestion.coordinator import (  # noqa: E402
    CategoryDocument,
    DirectorySummary,
    FileSummary,
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
# Level 2 문서 로드
# ---------------------------------------------------------------------------


def _load_level2_summaries(
    input_dir: Path,
    product: str,
) -> list[DirectorySummary]:
    """scripts/output/{product}/ 하위의 _level2_summary.md 파일을 로드한다."""
    summaries: list[DirectorySummary] = []

    for summary_file in sorted(input_dir.rglob("_level2_summary.md")):
        directory = str(summary_file.parent.relative_to(input_dir))
        document = summary_file.read_text(encoding="utf-8")

        # 같은 디렉토리의 Level 1 파일 요약도 로드
        file_summaries: list[FileSummary] = []
        for level1_file in sorted(summary_file.parent.glob("_level1_*.md")):
            content = level1_file.read_text(encoding="utf-8")
            # 파일명에서 원본 파일명 복원 (예: _level1_main.md → main)
            original_name = level1_file.stem.replace("_level1_", "")
            file_summaries.append(FileSummary(
                relative_path=f"{directory}/{original_name}",
                summary=content,
            ))

        summaries.append(DirectorySummary(
            directory=directory,
            product=product,
            file_summaries=file_summaries,
            document=document,
        ))

    return summaries


# ---------------------------------------------------------------------------
# 결과 저장
# ---------------------------------------------------------------------------


def _save_category_document(product: str, cat_doc: CategoryDocument) -> Path:
    """CategoryDocument를 마크다운 파일로 저장한다."""
    out_dir = OUTPUT_DIR / product / "category"
    out_dir.mkdir(parents=True, exist_ok=True)

    file_path = out_dir / f"{cat_doc.category}.md"
    file_path.write_text(
        f"# [{cat_doc.product}] {cat_doc.category}\n\n{cat_doc.document}\n",
        encoding="utf-8",
    )
    return file_path


# ---------------------------------------------------------------------------
# 모드 1: Worker 출력에서 Category Agent 실행
# ---------------------------------------------------------------------------


async def run_from_input(args: argparse.Namespace) -> None:
    """Worker 출력 디렉토리의 Level 2 문서를 읽어 Category Agent를 실행한다."""
    config = Config(config_path=Path(args.config) if args.config else None)
    git_config = load_git_source_config(config)

    # orchestrator LLM 클라이언트 생성
    try:
        orchestrator_llm = git_config.build_llm_client("orchestrator", stream=True)
    except ValueError as e:
        print(f"오류: {e}")
        print()
        print("config에 orchestrator 엔드포인트를 설정하세요:")
        print("  sources.git.processing.orchestrator:")
        print('    endpoint: "http://localhost:11434/v1"')
        print('    model: "your-model"')
        print("또는 글로벌 llm.endpoint를 설정하세요.")
        sys.exit(1)

    agent = LLMCategoryAgent(orchestrator_llm)

    # Level 2 문서 로드
    input_dir = Path(args.input_dir).resolve()
    if not input_dir.is_dir():
        print(f"오류: 디렉토리가 존재하지 않습니다: {input_dir}")
        sys.exit(1)

    product = args.product or input_dir.name
    summaries = _load_level2_summaries(input_dir, product)

    if not summaries:
        print(f"오류: {input_dir}에서 _level2_summary.md 파일을 찾을 수 없습니다.")
        print("먼저 run_worker_agent.py를 실행하여 Level 2 문서를 생성하세요.")
        sys.exit(1)

    print(f"입력 디렉토리: {input_dir}")
    print(f"상품명: {product}")
    print(f"로드된 디렉토리 요약: {len(summaries)}개")
    for s in summaries:
        print(f"  {s.directory} (파일 {len(s.file_summaries)}개)")
    print()

    # 카테고리 필터링
    categories = git_config.get_category_list()
    if args.categories:
        filter_names = {c.strip() for c in args.categories.split(",")}
        categories = [c for c in categories if c.name in filter_names]
        if not categories:
            print(f"오류: 일치하는 카테고리가 없습니다: {args.categories}")
            print(f"사용 가능: {', '.join(c.name for c in git_config.get_category_list())}")
            sys.exit(1)

    print(f"실행할 카테고리: {', '.join(c.name for c in categories)}")
    print()

    # Category Agent 실행
    start = time.time()

    for cat in categories:
        print(f"[{cat.name}] Category Agent 실행 중...")
        cat_start = time.time()
        result = await agent.generate_document(product, cat, summaries)
        cat_elapsed = time.time() - cat_start

        file_path = _save_category_document(product, result)
        print(f"[{cat.name}] 완료 ({cat_elapsed:.1f}초) -> {file_path}")

    elapsed = time.time() - start

    print(f"\n{'='*60}")
    print(f"전체 완료: {elapsed:.1f}초, {len(categories)}개 카테고리")
    print(f"결과 저장: {OUTPUT_DIR / product / 'category'}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# 모드 2: 전체 파이프라인 (Worker → Category)
# ---------------------------------------------------------------------------


async def run_full_pipeline(args: argparse.Namespace) -> None:
    """전체 파이프라인: Git clone → Worker → Category Agent 순차 실행."""
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
        orchestrator_llm = git_config.build_llm_client("orchestrator", stream=True)
    except ValueError as e:
        print(f"오류: {e}")
        sys.exit(1)

    worker_agent = LLMWorkerAgent(worker_llm, synthesizer_llm)
    category_agent = LLMCategoryAgent(orchestrator_llm)

    categories = git_config.get_category_list()
    if args.categories:
        filter_names = {c.strip() for c in args.categories.split(",")}
        categories = [c for c in categories if c.name in filter_names]

    print("전체 파이프라인 실행 중...")
    print(f"카테고리: {', '.join(c.name for c in categories)}")
    start = time.time()

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

            # 디렉토리 그룹핑 + Worker
            groups = group_files_by_directory(
                product_files,
                max_files_per_group=git_config.processing.max_files_per_worker,
                min_files_per_group=git_config.processing.min_files_per_worker,
            )

            print(f"  [{scope.name}] Worker 실행: {len(product_files)}개 파일, {len(groups)}개 디렉토리")

            dir_summaries: list[DirectorySummary] = []
            for directory, dir_files in groups.items():
                result = await worker_agent.process_directory(
                    directory, scope.name, dir_files,
                )
                dir_summaries.append(result)
                print(f"    Worker: {directory} ({len(dir_files)}개 파일)")

            # Category Agent
            print(f"  [{scope.name}] Category Agent 실행: {len(categories)}개 카테고리")
            for cat in categories:
                cat_doc = await category_agent.generate_document(
                    scope.name, cat, dir_summaries,
                )
                file_path = _save_category_document(scope.name, cat_doc)
                print(f"    Category: {cat.name} -> {file_path}")

    elapsed = time.time() - start

    print(f"\n{'='*60}")
    print(f"전체 완료: {elapsed:.1f}초")
    print(f"결과 저장: {OUTPUT_DIR}")
    print(f"{'='*60}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Category Agent 수동 테스트 (실제 LLM 엔드포인트 사용)",
    )
    parser.add_argument(
        "--input-dir", "-i",
        default="",
        help="Worker 출력 디렉토리 경로 (예: scripts/output/vpc)",
    )
    parser.add_argument(
        "--product", "-p",
        default="",
        help="상품명 (미지정 시 입력 디렉토리명 사용)",
    )
    parser.add_argument(
        "--categories",
        default="",
        help="실행할 카테고리 (쉼표 구분, 예: architecture,development). 미지정 시 전체.",
    )
    parser.add_argument(
        "--full-pipeline",
        action="store_true",
        help="전체 파이프라인 모드 (Git clone → Worker → Category)",
    )
    parser.add_argument(
        "--config", "-c",
        default="",
        help="사용자 config 파일 경로",
    )
    args = parser.parse_args()

    if args.full_pipeline:
        asyncio.run(run_full_pipeline(args))
    elif args.input_dir:
        asyncio.run(run_from_input(args))
    else:
        parser.print_help()
        print()
        print("예시:")
        print("  # Worker 출력에서 Category Agent 실행")
        print("  python scripts/run_category_agent.py --input-dir scripts/output/vpc --product vpc")
        print()
        print("  # 특정 카테고리만 실행")
        print("  python scripts/run_category_agent.py -i scripts/output/vpc -p vpc --categories architecture")
        print()
        print("  # 전체 파이프라인 (Worker + Category)")
        print("  python scripts/run_category_agent.py --full-pipeline")


if __name__ == "__main__":
    main()
