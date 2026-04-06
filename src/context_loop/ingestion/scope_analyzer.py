"""Config 기반 상품명 → 파일 경로 자동 탐지 모듈.

config에 정의된 상품명을 기반으로 Git 레포 전체를 스캔하여
해당 상품과 관련된 파일의 정확한 경로를 추출한다.
파일명을 토큰화하여 상품명(복수형 포함)과 경계 인식 매칭을 수행한다.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def _plural_variants(name: str) -> set[str]:
    """상품명의 복수형 변형을 생성한다.

    예: "vpc" → {"vpc", "vpcs"}
        "policy" → {"policy", "policys", "policies"}
        "address" → {"address", "addresss", "addresses"}
    """
    variants = {name, name + "s"}
    if name.endswith("y") and not name.endswith(("ay", "ey", "oy", "uy")):
        # consonant + y → ies (policy → policies)
        variants.add(name[:-1] + "ies")
    elif name.endswith(("s", "sh", "ch", "x", "z")):
        # sibilant → es (address → addresses, batch → batches)
        variants.add(name + "es")
    return variants


def _filename_matches_product(filename: str, variants: set[str]) -> bool:
    """파일명이 상품명(복수형 포함)을 토큰으로 포함하는지 판정한다.

    stem을 ``_`` 또는 ``-`` 로 분리한 토큰 중 하나라도
    variants에 포함되면 매칭으로 간주한다.

    Args:
        filename: 파일 이름 (예: "vpc_controller.go").
        variants: _plural_variants()가 반환한 변형 집합.

    Returns:
        매칭 여부.
    """
    stem = Path(filename).stem.lower()
    # _ 와 - 모두 구분자로 처리
    tokens = set()
    for part in stem.split("_"):
        for sub in part.split("-"):
            if sub:
                tokens.add(sub)
    return bool(tokens & variants)


def resolve_product_paths(
    clone_dir: Path,
    product_names: list[str],
    supported_extensions: list[str] | None = None,
) -> dict[str, list[str]]:
    """config에 정의된 상품명을 기반으로 레포 전체에서 관련 파일 경로를 탐지한다.

    레포 루트부터 전체 트리를 1회 순회하며, 각 파일명을 토큰화하여
    상품명(복수형 포함)과 매칭한다. 매칭된 파일의 정확한 상대 경로를 반환한다.

    Args:
        clone_dir: 로컬 clone 경로.
        product_names: config에 정의된 상품명 리스트 (예: ["vpc", "subnet"]).
        supported_extensions: 대상 파일 확장자. None이면 모든 파일.

    Returns:
        상품명 → 매칭된 파일 상대 경로 리스트. 매칭 없는 상품은 빈 리스트.
    """
    skip_dirs = {".git", "node_modules", "vendor", "__pycache__", ".venv", "venv"}

    # 상품별 복수형 변형을 미리 계산
    product_variants: dict[str, set[str]] = {
        name: _plural_variants(name.lower())
        for name in product_names
    }

    result: dict[str, list[str]] = {name: [] for name in product_names}

    # BFS로 레포 전체 순회 (1회)
    queue: list[Path] = [clone_dir]
    while queue:
        current = queue.pop(0)
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except PermissionError:
            continue

        for entry in entries:
            if entry.is_dir():
                if not entry.name.startswith(".") and entry.name not in skip_dirs:
                    queue.append(entry)
                continue

            # 파일: 확장자 필터
            if supported_extensions:
                if entry.suffix.lower() not in supported_extensions:
                    continue

            # 각 상품명에 대해 매칭 판정
            for name, variants in product_variants.items():
                if _filename_matches_product(entry.name, variants):
                    rel_path = str(entry.relative_to(clone_dir))
                    result[name].append(rel_path)

    for name, paths in result.items():
        logger.info("상품 '%s' 파일 탐지: %d개 파일", name, len(paths))

    return result
