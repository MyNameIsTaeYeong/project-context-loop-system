#!/usr/bin/env python3
"""상품명 기반 파일 경로 자동 탐지 스크립트.

config/default.yaml에 정의된 Git URL과 상품명을 읽어
레포를 clone/pull 한 뒤 상품별 파일 경로를 탐지한다.

사용법:
    # config yaml에 정의된 대로 실행 (인자 없음)
    python scripts/run_product_paths.py

    # 상품명 오버라이드
    python scripts/run_product_paths.py --products vpc,billing

    # 확장자 오버라이드
    python scripts/run_product_paths.py --ext .go,.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from context_loop.config import Config  # noqa: E402
from context_loop.ingestion.git_repository import (  # noqa: E402
    clone_or_pull,
    parse_product_scopes,
    _repo_clone_dir,
)


async def run(args: argparse.Namespace) -> None:
    # --- Config 로드 ---
    config_path = Path(args.config) if args.config else None
    config = Config(config_path=config_path)

    git_config = config.get("sources.git", {}) or {}
    repos = git_config.get("repositories") or []
    supported_extensions = git_config.get("supported_extensions") or []
    data_dir = Path(config.get("app.data_dir", "~/.context-loop/data")).expanduser()

    if not repos:
        print("오류: config yaml에 repositories가 정의되지 않았습니다.")
        print()
        print("config/default.yaml 에 다음과 같이 정의하세요:")
        print("  sources:")
        print("    git:")
        print("      repositories:")
        print("        - url: \"git@github.com:company/repo.git\"")
        print("          branch: \"main\"")
        print("          products:")
        print("            vpc:")
        print("              display_name: \"VPC 서비스\"")
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

    # --- 레포별 처리 ---
    for repo_cfg in repos:
        repo_url = repo_cfg.get("url", "")
        branch = repo_cfg.get("branch", "main")
        products_raw = repo_cfg.get("products") or {}

        if not repo_url:
            print("경고: url이 비어있는 레포 건너뜀")
            continue

        # 상품명 오버라이드
        if args.products:
            product_names = [p.strip() for p in args.products.split(",") if p.strip()]
            for name in product_names:
                if name not in products_raw:
                    products_raw[name] = {"display_name": name}

        if not products_raw:
            print(f"경고: 상품명이 없는 레포 건너뜀: {repo_url}")
            continue

        # clone/pull
        clone_dir = _repo_clone_dir(data_dir, repo_url)
        print(f"레포: {repo_url}")
        print(f"  clone 경로: {clone_dir}")
        print(f"  브랜치: {branch}")

        clone_dir.parent.mkdir(parents=True, exist_ok=True)
        is_new, prev_hash = await clone_or_pull(repo_url, clone_dir, branch)
        status = "새로 clone 완료" if is_new else f"pull 완료 (이전: {prev_hash[:8]})"
        print(f"  상태: {status}")

        # parse_product_scopes로 탐지
        print(f"\n{'='*60}")
        print(f"상품명:    {list(products_raw.keys())}")
        print(f"확장자:    {extensions or '(전체)'} ({ext_source})")
        print(f"{'='*60}")

        scopes = parse_product_scopes(
            repo_cfg, clone_dir=clone_dir, supported_extensions=extensions,
        )

        total_files = 0
        for scope in scopes:
            total_files += len(scope.paths)
            has_manual = bool((products_raw.get(scope.name) or {}).get("paths"))
            label = "수동 paths" if has_manual else "자동 탐지"
            print(f"\n  [{scope.name}] {scope.display_name} — {len(scope.paths)}개 파일 ({label})")
            for p in sorted(scope.paths):
                print(f"    {p}")

        print(f"\n  총 {total_files}개 파일 탐지됨 (제한 없음, 전체 결과)")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="config yaml 기반 상품명 → 파일 경로 자동 탐지",
    )
    parser.add_argument(
        "--products", "-p",
        default="",
        help="상품명 오버라이드 (쉼표 구분, 예: vpc,billing)",
    )
    parser.add_argument(
        "--ext", "-e",
        default="",
        help="확장자 오버라이드 (쉼표 구분, 예: .go,.py). 미지정 시 yaml 값 사용.",
    )
    parser.add_argument(
        "--config", "-c",
        default="",
        help="사용자 config 파일 경로. 미지정 시 기본 경로 사용.",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
