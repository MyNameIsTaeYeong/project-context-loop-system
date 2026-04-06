#!/usr/bin/env python3
"""상품명 기반 파일 경로 자동 탐지 테스트 스크립트.

사용법:
    # 1) 현재 프로젝트 레포를 대상으로 테스트 (기본값)
    python scripts/test_product_paths.py

    # 2) 특정 레포 경로 지정
    python scripts/test_product_paths.py /path/to/repo

    # 3) 상품명 직접 지정 (쉼표 구분)
    python scripts/test_product_paths.py /path/to/repo --products vpc,billing,iam

    # 4) 확장자 필터 지정
    python scripts/test_product_paths.py /path/to/repo --products vpc --ext .go,.py,.proto
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from context_loop.ingestion.scope_analyzer import resolve_product_paths  # noqa: E402
from context_loop.ingestion.git_repository import parse_product_scopes  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="config 기반 상품명 → 파일 경로 자동 탐지 테스트",
    )
    parser.add_argument(
        "repo_path",
        nargs="?",
        default=str(project_root),
        help="대상 레포 경로 (기본: 현재 프로젝트)",
    )
    parser.add_argument(
        "--products", "-p",
        default="",
        help="상품명 (쉼표 구분, 예: vpc,billing,iam)",
    )
    parser.add_argument(
        "--ext", "-e",
        default="",
        help="확장자 필터 (쉼표 구분, 예: .go,.py,.proto). 미지정 시 모든 파일.",
    )
    args = parser.parse_args()

    clone_dir = Path(args.repo_path).resolve()
    if not clone_dir.is_dir():
        print(f"오류: 경로가 존재하지 않습니다: {clone_dir}")
        sys.exit(1)

    # 상품명 파싱
    if args.products:
        product_names = [p.strip() for p in args.products.split(",") if p.strip()]
    else:
        # 기본 예시: 현재 프로젝트의 주요 모듈명
        product_names = ["scope_analyzer", "git_repository", "coordinator", "confluence"]
        print(f"--products 미지정, 예시 상품명 사용: {product_names}")

    # 확장자 파싱
    extensions: list[str] | None = None
    if args.ext:
        extensions = [e.strip() if e.strip().startswith(".") else f".{e.strip()}"
                      for e in args.ext.split(",") if e.strip()]

    print(f"\n{'='*60}")
    print(f"대상 레포: {clone_dir}")
    print(f"상품명:    {product_names}")
    print(f"확장자:    {extensions or '(전체)'}")
    print(f"{'='*60}")

    # --- 1) resolve_product_paths 직접 호출 ---
    print(f"\n[resolve_product_paths] 실행 중...")
    result = resolve_product_paths(clone_dir, product_names, extensions)

    total_files = 0
    for name in product_names:
        paths = result[name]
        total_files += len(paths)
        print(f"\n  [{name}] {len(paths)}개 파일")
        for p in sorted(paths):
            print(f"    {p}")

    print(f"\n  총 {total_files}개 파일 탐지됨")

    # --- 2) parse_product_scopes 연동 테스트 ---
    print(f"\n{'='*60}")
    print("[parse_product_scopes 연동] paths 미정의 config 시뮬레이션")
    print(f"{'='*60}")

    repo_config = {
        "products": {
            name: {"display_name": name.replace("_", " ").title()}
            for name in product_names
        }
    }

    print(f"\n  입력 config:")
    for name, cfg in repo_config["products"].items():
        print(f"    {name}: display_name={cfg['display_name']}, paths=(미정의)")

    scopes = parse_product_scopes(repo_config, clone_dir=clone_dir, supported_extensions=extensions)

    print(f"\n  출력 결과:")
    for scope in scopes:
        print(f"\n    [{scope.name}] {scope.display_name} — {len(scope.paths)}개 paths 자동 생성")
        for p in sorted(scope.paths)[:10]:
            print(f"      {p}")
        if len(scope.paths) > 10:
            print(f"      ... 외 {len(scope.paths) - 10}개")


if __name__ == "__main__":
    main()
