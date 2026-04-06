#!/usr/bin/env python3
"""상품명 기반 파일 경로 자동 탐지 테스트 스크립트.

config/default.yaml (또는 ~/.context-loop/config.yaml)에 정의된
상품명을 읽어 파일 경로 탐지를 실행한다.

사용법:
    # 1) yaml에 정의된 상품명 + 지정한 레포 경로로 테스트
    python scripts/test_product_paths.py /path/to/repo

    # 2) 상품명을 직접 오버라이드
    python scripts/test_product_paths.py /path/to/repo --products vpc,billing

    # 3) 확장자 필터 (미지정 시 yaml의 supported_extensions 사용)
    python scripts/test_product_paths.py /path/to/repo --ext .go,.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from context_loop.config import Config  # noqa: E402
from context_loop.ingestion.scope_analyzer import resolve_product_paths  # noqa: E402
from context_loop.ingestion.git_repository import parse_product_scopes  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description="config 기반 상품명 → 파일 경로 자동 탐지 테스트",
    )
    parser.add_argument(
        "repo_path",
        help="대상 레포의 로컬 경로 (이미 clone된 디렉토리)",
    )
    parser.add_argument(
        "--products", "-p",
        default="",
        help="상품명 직접 지정 (쉼표 구분). 미지정 시 yaml에서 읽음.",
    )
    parser.add_argument(
        "--ext", "-e",
        default="",
        help="확장자 필터 (쉼표 구분, 예: .go,.py). 미지정 시 yaml의 supported_extensions 사용.",
    )
    parser.add_argument(
        "--config", "-c",
        default="",
        help="사용자 config 파일 경로. 미지정 시 기본 경로 사용.",
    )
    args = parser.parse_args()

    clone_dir = Path(args.repo_path).resolve()
    if not clone_dir.is_dir():
        print(f"오류: 경로가 존재하지 않습니다: {clone_dir}")
        sys.exit(1)

    # --- Config 로드 ---
    config_path = Path(args.config) if args.config else None
    config = Config(config_path=config_path)

    git_config = config.get("sources.git", {}) or {}
    repos = git_config.get("repositories") or []
    supported_extensions = git_config.get("supported_extensions") or []

    # --- 상품명 결정 ---
    if args.products:
        product_names = [p.strip() for p in args.products.split(",") if p.strip()]
        products_raw = {name: {"display_name": name} for name in product_names}
        print(f"상품명 소스: --products 인자")
    elif repos:
        # yaml의 첫 번째 레포에서 상품명 읽기
        products_raw = repos[0].get("products") or {}
        product_names = list(products_raw.keys())
        repo_url = repos[0].get("url", "(미정의)")
        print(f"상품명 소스: config yaml (레포: {repo_url})")
    else:
        print("오류: yaml에 repositories가 정의되지 않았고, --products도 지정되지 않았습니다.")
        print()
        print("해결 방법:")
        print("  1) config/default.yaml에 상품명 정의:")
        print("     sources:")
        print("       git:")
        print("         repositories:")
        print("           - url: \"git@github.com:company/repo.git\"")
        print("             products:")
        print("               vpc:")
        print("                 display_name: \"VPC 서비스\"")
        print()
        print("  2) 또는 --products 인자로 직접 지정:")
        print("     python scripts/test_product_paths.py /path/to/repo -p vpc,billing")
        sys.exit(1)

    if not product_names:
        print("오류: 상품명이 비어있습니다.")
        sys.exit(1)

    # --- 확장자 결정 ---
    if args.ext:
        extensions: list[str] | None = [
            e.strip() if e.strip().startswith(".") else f".{e.strip()}"
            for e in args.ext.split(",") if e.strip()
        ]
        ext_source = "--ext 인자"
    elif supported_extensions:
        extensions = supported_extensions
        ext_source = "config yaml"
    else:
        extensions = None
        ext_source = "필터 없음 (전체 파일)"

    # --- 출력 ---
    print(f"\n{'='*60}")
    print(f"대상 레포:   {clone_dir}")
    print(f"상품명:      {product_names}")
    print(f"확장자:      {extensions or '(전체)'} ({ext_source})")
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

    repo_config = {"products": products_raw}
    scopes = parse_product_scopes(repo_config, clone_dir=clone_dir, supported_extensions=extensions)

    for scope in scopes:
        has_manual = bool((products_raw.get(scope.name) or {}).get("paths"))
        label = "(수동 paths 유지)" if has_manual else "(자동 탐지)"
        print(f"\n  [{scope.name}] {scope.display_name} — {len(scope.paths)}개 paths {label}")
        for p in sorted(scope.paths)[:15]:
            print(f"    {p}")
        if len(scope.paths) > 15:
            print(f"    ... 외 {len(scope.paths) - 15}개")


if __name__ == "__main__":
    main()
