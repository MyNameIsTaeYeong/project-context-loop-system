"""레포지토리 디렉토리 분석 기반 상품 스코프 제안 모듈.

Git 레포의 디렉토리 트리를 LLM에 전달하여 상품 단위를 식별하고,
config에 넣을 수 있는 products 스코프(paths, exclude)를 제안받는다.
D-027 설계: "최초에 LLM이 레포 디렉토리 트리를 분석하여 스코프를 제안하고,
사람이 검토 후 확정. 이후 완전 자동."

대규모 레포 대응: 2-pass 분석 방식
- Pass 1: 얕은 트리(depth 2)로 상품 영역 식별 → 소규모 LLM 호출
- Pass 2: 식별된 영역별로 상세 서브트리 전달 → 영역별 소규모 LLM 호출
- 각 호출이 작으므로 타임아웃 없음
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from context_loop.processor.llm_client import LLMClient, extract_json

logger = logging.getLogger(__name__)

# Pass 1에서 사용할 얕은 트리 깊이
_PASS1_DEPTH = 2
# 이 줄 수 이하면 단일 호출로 처리 (소규모 레포)
_SINGLE_PASS_THRESHOLD = 300


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
당신은 소프트웨어 아키텍트입니다.
Git 레포지토리의 디렉토리 트리를 분석하여 독립적인 상품(서비스/모듈) 단위를 식별하세요.
각 상품에 대해 config에 넣을 수 있는 스코프 정보를 제안하세요.

중요: "상품"은 비즈니스 기능 단위입니다. 아키텍처 레이어(controller, service, repository,
model, domain, handler, route 등)는 상품이 아닙니다.
레이어형 구조에서는 여러 레이어에 걸쳐 동일 이름으로 존재하는 하위 디렉토리가 하나의 상품입니다.
예시: controller/vpc/, service/vpc/, repository/vpc/ → "vpc" 상품 (paths: controller/vpc/**, service/vpc/**, repository/vpc/**)"""

_PASS1_PROMPT_TEMPLATE = """\
다음은 Git 레포지토리의 상위 디렉토리 구조입니다:

```
{tree}
```

이 레포에서 독립적인 상품(서비스/모듈/컴포넌트) 영역을 식별하세요.
각 영역의 루트 디렉토리 경로와 간단한 설명을 JSON으로 반환하세요:

```json
{{
  "areas": [
    {{
      "name": "영문-이름 (kebab-case)",
      "display_name": "한글 표시명",
      "description": "한 줄 설명",
      "root_path": "해당 영역의 루트 디렉토리 (예: services/vpc)"
    }}
  ]
}}
```

규칙:
- 공통 라이브러리(lib/, pkg/, common/)는 별도 영역으로 식별
- 인프라 코드(terraform/, deploy/, infra/)는 별도 영역으로 식별
- 최상위 디렉토리가 이미 상품 단위면 그대로 사용
- 모노레포에서 services/ 하위 각 디렉토리가 상품이면 개별로 식별
- **레이어형 구조 주의**: controller/, service/, repository/, handler/, route/, model/, domain/ 등은 아키텍처 레이어이지 상품이 아닙니다.
  여러 레이어 디렉토리 하위에 동일 이름(예: vpc, billing)이 반복되면 그것이 상품입니다.
  이 경우 root_path는 상품 이름(예: "vpc")으로 하고, 실제 경로는 여러 레이어에 걸칩니다."""

_PASS2_PROMPT_TEMPLATE = """\
다음은 "{area_name}" ({area_description}) 영역의 상세 디렉토리 구조입니다:

```
{subtree}
```

이 영역의 코드 스코프를 정의하세요. JSON으로 반환:

```json
{{
  "name": "{area_name}",
  "display_name": "{area_display_name}",
  "description": "{area_description}",
  "paths": ["포함할 glob 패턴 (예: {root_path}/**)"],
  "exclude": ["제외할 glob 패턴 (예: **/*_test.go)"]
}}
```

규칙:
- paths는 이 영역의 코드만 포함하도록 구체적으로 지정
- 테스트 파일(*_test.*, *_spec.*, test_*), vendor/node_modules 등은 exclude에 추가
- 빌드 산출물, 설정 파일 등 코드가 아닌 것은 exclude에 추가
- 레이어형 구조(controller/, service/, repository/ 등)에 상품 코드가 분산되어 있으면 paths에 모든 레이어 경로를 포함 (예: ["controller/vpc/**", "service/vpc/**", "repository/vpc/**"])"""

_SINGLE_PASS_PROMPT_TEMPLATE = """\
다음은 Git 레포지토리의 디렉토리 트리입니다:

```
{tree}
```

이 레포에서 독립적인 상품(서비스/모듈/컴포넌트) 단위를 식별하고,
각 상품에 대해 다음 정보를 JSON으로 반환하세요:

```json
{{
  "products": [
    {{
      "name": "상품-영문-이름 (kebab-case)",
      "display_name": "상품 한글 표시명",
      "description": "이 상품이 무엇인지 한 줄 설명",
      "paths": ["포함할 경로 glob 패턴 (예: services/vpc/**)"],
      "exclude": ["제외할 경로 glob 패턴 (예: **/*_test.go, **/vendor/**)"]
    }}
  ]
}}
```

규칙:
- 테스트 파일(*_test.*, *_spec.*, test_*), vendor/node_modules 등은 exclude에 넣으세요.
- paths는 해당 상품의 코드만 포함하도록 최대한 구체적으로 지정하세요.
- 공통 라이브러리(lib/, pkg/, common/ 등)는 별도 상품으로 분리하세요.
- 인프라 코드(terraform/, deploy/, infra/ 등)가 있으면 별도 상품으로 분리하세요.
- 상품 이름은 디렉토리 구조에서 자연스럽게 유추하세요.
- **레이어형 구조 주의**: controller/, service/, repository/, handler/, route/, model/, domain/ 등 아키텍처 레이어는 상품이 아닙니다.
  여러 레이어 하위에 동일 이름이 반복되면(예: controller/vpc/, service/vpc/) 그것이 상품입니다.
  상품의 paths에는 관련된 모든 레이어 경로를 포함하세요.
  예: "vpc" 상품 → paths: ["controller/vpc/**", "service/vpc/**", "repository/vpc/**"]"""


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ProductScopeProposal:
    """LLM이 제안한 단일 상품 스코프."""

    name: str
    display_name: str
    description: str
    paths: list[str]
    exclude: list[str] = field(default_factory=list)

    def to_config_dict(self) -> dict[str, Any]:
        """config.yaml의 products 항목으로 변환한다."""
        result: dict[str, Any] = {
            "display_name": self.display_name,
            "paths": self.paths,
        }
        if self.exclude:
            result["exclude"] = self.exclude
        return result


@dataclass
class ScopeAnalysisResult:
    """스코프 분석 결과."""

    products: list[ProductScopeProposal]
    raw_tree: str
    raw_llm_response: str

    def to_config_dict(self) -> dict[str, dict[str, Any]]:
        """config.yaml의 products 섹션 전체를 dict로 변환한다."""
        return {p.name: p.to_config_dict() for p in self.products}

    def summary(self) -> str:
        """사람이 읽을 수 있는 요약 문자열을 반환한다."""
        lines = [f"총 {len(self.products)}개 상품 식별:\n"]
        for p in self.products:
            lines.append(f"  [{p.name}] {p.display_name}")
            lines.append(f"    설명: {p.description}")
            lines.append(f"    paths: {p.paths}")
            if p.exclude:
                lines.append(f"    exclude: {p.exclude}")
            lines.append("")
        return "\n".join(lines)


@dataclass
class _AreaInfo:
    """Pass 1에서 식별된 영역 정보 (내부용)."""

    name: str
    display_name: str
    description: str
    root_path: str


# ---------------------------------------------------------------------------
# Layer pattern detection (코드 레벨 레이어 구조 감지)
# ---------------------------------------------------------------------------

# 일반적인 아키텍처 레이어 디렉토리 이름
_KNOWN_LAYER_NAMES = frozenset({
    "controller", "controllers",
    "service", "services",
    "repository", "repositories",
    "model", "models",
    "domain", "domains",
    "handler", "handlers",
    "route", "routes",
    "middleware", "middlewares",
    "view", "views",
    "dto", "dtos",
    "entity", "entities",
    "adapter", "adapters",
    "port", "ports",
    "usecase", "usecases",
    "api",
    "dao",
    "mapper", "mappers",
    "grpc",
    "rest",
    "graphql",
})


def _detect_layered_products(clone_dir: Path) -> list[_AreaInfo] | None:
    """레이어형 디렉토리 구조에서 상품 목록을 코드 레벨로 추출한다.

    최상위 디렉토리가 아키텍처 레이어이고, 그 하위에 동일 이름의
    서브디렉토리가 2개 이상의 레이어에 걸쳐 존재하면 레이어형으로 판단한다.

    Returns:
        레이어형이면 _AreaInfo 리스트, 아니면 None.
    """
    try:
        top_dirs = [
            d for d in clone_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".")
            and d.name not in {"node_modules", "vendor", "__pycache__", ".venv", "venv"}
        ]
    except PermissionError:
        return None

    # 최상위 디렉토리 중 레이어 이름인 것을 식별
    layer_dirs = [d for d in top_dirs if d.name.lower() in _KNOWN_LAYER_NAMES]

    if len(layer_dirs) < 2:
        return None  # 레이어가 2개 미만이면 레이어형이 아님

    # 각 레이어의 하위 디렉토리 이름 수집
    layer_children: dict[str, set[str]] = {}
    for ld in layer_dirs:
        try:
            children = {
                c.name for c in ld.iterdir()
                if c.is_dir() and not c.name.startswith(".")
            }
        except PermissionError:
            children = set()
        layer_children[ld.name] = children

    # 2개 이상의 레이어에 공통으로 존재하는 서브디렉토리 = 상품
    all_children: set[str] = set()
    for children in layer_children.values():
        all_children |= children

    products: list[str] = []
    for child in sorted(all_children):
        count = sum(1 for children in layer_children.values() if child in children)
        if count >= 2:
            products.append(child)

    if not products:
        return None

    logger.info(
        "레이어형 구조 감지: 레이어=%s, 상품=%d개",
        [d.name for d in layer_dirs], len(products),
    )

    return [
        _AreaInfo(
            name=product,
            display_name=product,
            description=f"레이어형 구조에서 감지된 상품 ({product})",
            root_path=product,  # 실제 경로가 아닌 상품명 → _collect_subtrees가 처리
        )
        for product in products
    ]


# ---------------------------------------------------------------------------
# Tree building
# ---------------------------------------------------------------------------


def _count_files(
    directory: Path,
    supported_extensions: list[str] | None = None,
) -> int:
    """디렉토리 내 직속 파일 수를 반환한다 (재귀 아님)."""
    try:
        files = [f for f in directory.iterdir() if f.is_file()]
    except PermissionError:
        return 0
    if supported_extensions:
        files = [f for f in files if f.suffix.lower() in supported_extensions]
    return len(files)


def build_directory_tree(
    root: Path,
    supported_extensions: list[str] | None = None,
    max_depth: int = 5,
    max_entries: int = 500,
    directories_only: bool = False,
) -> str:
    """디렉토리 트리 문자열을 생성한다.

    Args:
        root: 레포 루트 경로.
        supported_extensions: 표시할 파일 확장자. None이면 모든 파일.
        max_depth: 최대 탐색 깊이.
        max_entries: 최대 항목 수 (초과 시 잘림).
        directories_only: True면 디렉토리만 표시하고 파일은 개수만 표시.

    Returns:
        트리 문자열.
    """
    lines: list[str] = []
    _skip_dirs = {".git", "node_modules", "vendor", "__pycache__", ".venv", "venv"}

    def _walk(path: Path, depth: int, prefix: str) -> None:
        if depth > max_depth or len(lines) >= max_entries:
            return

        try:
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
        except PermissionError:
            return

        dirs = [e for e in entries if e.is_dir() and e.name not in _skip_dirs]
        files = [e for e in entries if e.is_file()]

        if supported_extensions:
            files = [f for f in files if f.suffix.lower() in supported_extensions]

        for d in dirs:
            if len(lines) >= max_entries:
                lines.append(f"{prefix}... (잘림)")
                return
            if directories_only:
                file_count = _count_files(d, supported_extensions)
                lines.append(f"{prefix}{d.name}/ ({file_count} files)")
            else:
                lines.append(f"{prefix}{d.name}/")
            _walk(d, depth + 1, prefix + "  ")

        if not directories_only:
            for f in files:
                if len(lines) >= max_entries:
                    lines.append(f"{prefix}... (잘림)")
                    return
                lines.append(f"{prefix}{f.name}")

    _walk(root, 0, "")

    if not lines:
        return "(빈 디렉토리)"

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _parse_proposals(raw_json: Any) -> list[ProductScopeProposal]:
    """LLM JSON 응답을 ProductScopeProposal 리스트로 파싱한다."""
    if isinstance(raw_json, dict):
        products_list = raw_json.get("products", [])
    elif isinstance(raw_json, list):
        products_list = raw_json
    else:
        raise ValueError(f"예상하지 못한 JSON 형식: {type(raw_json)}")

    proposals: list[ProductScopeProposal] = []
    for item in products_list:
        if not isinstance(item, dict):
            continue
        name = item.get("name", "")
        if not name:
            continue
        proposals.append(
            ProductScopeProposal(
                name=name,
                display_name=item.get("display_name", name),
                description=item.get("description", ""),
                paths=item.get("paths", []),
                exclude=item.get("exclude", []),
            )
        )
    return proposals


def _parse_areas(raw_json: Any) -> list[_AreaInfo]:
    """Pass 1 LLM 응답을 _AreaInfo 리스트로 파싱한다."""
    if isinstance(raw_json, dict):
        areas_list = raw_json.get("areas", [])
    elif isinstance(raw_json, list):
        areas_list = raw_json
    else:
        raise ValueError(f"예상하지 못한 JSON 형식: {type(raw_json)}")

    areas: list[_AreaInfo] = []
    for item in areas_list:
        if not isinstance(item, dict):
            continue
        name = item.get("name", "")
        root_path = item.get("root_path", "")
        if not name or not root_path:
            continue
        areas.append(
            _AreaInfo(
                name=name,
                display_name=item.get("display_name", name),
                description=item.get("description", ""),
                root_path=root_path,
            )
        )
    return areas


def _parse_single_proposal(raw_json: Any, fallback_area: _AreaInfo) -> ProductScopeProposal:
    """Pass 2 LLM 응답(단일 상품)을 파싱한다."""
    if not isinstance(raw_json, dict):
        raise ValueError(f"예상하지 못한 JSON 형식: {type(raw_json)}")

    return ProductScopeProposal(
        name=raw_json.get("name", fallback_area.name),
        display_name=raw_json.get("display_name", fallback_area.display_name),
        description=raw_json.get("description", fallback_area.description),
        paths=raw_json.get("paths", [f"{fallback_area.root_path}/**"]),
        exclude=raw_json.get("exclude", []),
    )


# ---------------------------------------------------------------------------
# Single-pass (소규모 레포)
# ---------------------------------------------------------------------------


async def _analyze_single_pass(
    tree: str,
    llm_client: LLMClient,
) -> list[ProductScopeProposal]:
    """소규모 레포: 한 번의 LLM 호출로 전체 분석."""
    prompt = _SINGLE_PASS_PROMPT_TEMPLATE.format(tree=tree)
    raw_response = await llm_client.complete(
        prompt, system=_SYSTEM_PROMPT, max_tokens=4096, temperature=0.0,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    parsed = extract_json(raw_response)
    return _parse_proposals(parsed)


# ---------------------------------------------------------------------------
# Two-pass (대규모 레포)
# ---------------------------------------------------------------------------


async def _pass1_identify_areas(
    clone_dir: Path,
    llm_client: LLMClient,
    supported_extensions: list[str] | None,
) -> list[_AreaInfo]:
    """Pass 1: 얕은 트리로 상품 영역을 식별한다."""
    shallow_tree = build_directory_tree(
        clone_dir,
        supported_extensions=supported_extensions,
        max_depth=_PASS1_DEPTH,
        max_entries=500,
        directories_only=True,
    )
    prompt = _PASS1_PROMPT_TEMPLATE.format(tree=shallow_tree)
    raw_response = await llm_client.complete(
        prompt, system=_SYSTEM_PROMPT, max_tokens=2048, temperature=0.0,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    parsed = extract_json(raw_response)
    return _parse_areas(parsed)


def _collect_subtrees(
    clone_dir: Path,
    area: _AreaInfo,
    supported_extensions: list[str] | None,
) -> tuple[str, str]:
    """영역의 서브트리를 수집한다.

    root_path가 직접 존재하면 해당 디렉토리의 서브트리를 반환하고,
    존재하지 않으면 레이어형 구조로 판단하여 여러 레이어에서 상품명으로
    매칭되는 서브디렉토리를 모아 반환한다.

    Returns:
        (subtree_text, effective_root_path) 튜플.
        subtree_text가 빈 문자열이면 매칭 디렉토리를 찾지 못한 것.
    """
    subtree_root = clone_dir / area.root_path
    if subtree_root.is_dir():
        tree = build_directory_tree(
            subtree_root,
            supported_extensions=supported_extensions,
            max_depth=4,
            max_entries=300,
        )
        return tree, area.root_path

    # 레이어형 구조: 최상위 디렉토리들에서 area.root_path(상품명)과
    # 일치하는 하위 디렉토리를 찾는다.
    product_name = Path(area.root_path).name
    parts: list[str] = []
    found_paths: list[str] = []
    try:
        top_dirs = sorted(
            [d for d in clone_dir.iterdir() if d.is_dir() and not d.name.startswith(".")],
            key=lambda p: p.name,
        )
    except PermissionError:
        return "", area.root_path

    for top_dir in top_dirs:
        candidate = top_dir / product_name
        if candidate.is_dir():
            rel = f"{top_dir.name}/{product_name}"
            tree = build_directory_tree(
                candidate,
                supported_extensions=supported_extensions,
                max_depth=3,
                max_entries=100,
            )
            parts.append(f"[{rel}]\n{tree}")
            found_paths.append(rel)

    if not parts:
        return "", area.root_path

    return "\n\n".join(parts), ", ".join(found_paths)


async def _pass2_refine_area(
    clone_dir: Path,
    area: _AreaInfo,
    llm_client: LLMClient,
    supported_extensions: list[str] | None,
) -> ProductScopeProposal:
    """Pass 2: 단일 영역의 상세 서브트리를 분석하여 스코프를 확정한다."""
    subtree, effective_root = _collect_subtrees(
        clone_dir, area, supported_extensions,
    )
    if not subtree:
        logger.warning("영역 경로를 찾지 못함: %s, 기본 패턴 사용", area.root_path)
        return ProductScopeProposal(
            name=area.name,
            display_name=area.display_name,
            description=area.description,
            paths=[f"{area.root_path}/**"],
        )
    prompt = _PASS2_PROMPT_TEMPLATE.format(
        area_name=area.name,
        area_display_name=area.display_name,
        area_description=area.description,
        root_path=effective_root,
        subtree=subtree,
    )
    raw_response = await llm_client.complete(
        prompt, system=_SYSTEM_PROMPT, max_tokens=1024, temperature=0.0,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    parsed = extract_json(raw_response)
    return _parse_single_proposal(parsed, area)


async def _analyze_two_pass(
    clone_dir: Path,
    llm_client: LLMClient,
    supported_extensions: list[str] | None,
    max_concurrent: int = 5,
) -> list[ProductScopeProposal]:
    """대규모 레포: 2-pass 분석.

    레이어형 구조가 감지되면 Pass 1(LLM)을 건너뛰고 코드 레벨로 영역을 추출한다.
    그 외에는 Pass 1(얕은 트리 → LLM) → Pass 2(영역별 서브트리 → LLM).
    """
    # 레이어형 구조 감지 시도 (LLM 없이)
    areas = _detect_layered_products(clone_dir)
    if areas:
        logger.info("레이어형 구조 감지: %d개 상품, Pass 1 건너뜀", len(areas))
    else:
        # Pass 1: LLM으로 영역 식별
        areas = await _pass1_identify_areas(clone_dir, llm_client, supported_extensions)
        if not areas:
            logger.warning("Pass 1에서 상품 영역을 식별하지 못했습니다.")
            return []
        logger.info("Pass 1 완료: %d개 영역 식별", len(areas))

    # Pass 2 — 병렬 실행 (세마포어로 동시성 제한)
    semaphore = asyncio.Semaphore(max_concurrent)

    async def _refine_with_limit(area: _AreaInfo) -> ProductScopeProposal:
        async with semaphore:
            return await _pass2_refine_area(
                clone_dir, area, llm_client, supported_extensions
            )

    results = await asyncio.gather(
        *[_refine_with_limit(a) for a in areas],
        return_exceptions=True,
    )

    proposals: list[ProductScopeProposal] = []
    for area, result in zip(areas, results):
        if isinstance(result, Exception):
            logger.error("Pass 2 실패 (%s): %s, 기본 패턴 사용", area.name, result)
            proposals.append(
                ProductScopeProposal(
                    name=area.name,
                    display_name=area.display_name,
                    description=area.description,
                    paths=[f"{area.root_path}/**"],
                )
            )
        else:
            proposals.append(result)

    logger.info("Pass 2 완료: %d개 상품 스코프 확정", len(proposals))
    return proposals


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def analyze_repository_scope(
    clone_dir: Path,
    llm_client: LLMClient,
    supported_extensions: list[str] | None = None,
    max_depth: int = 5,
) -> ScopeAnalysisResult:
    """레포지토리를 분석하여 상품 스코프를 제안받는다.

    소규모 레포(트리 300줄 이하)는 단일 LLM 호출로 처리하고,
    대규모 레포는 2-pass 방식으로 분할 호출한다.

    Args:
        clone_dir: 로컬 clone 경로.
        llm_client: LLM 클라이언트.
        supported_extensions: 트리에 표시할 파일 확장자.
        max_depth: 디렉토리 트리 최대 깊이.

    Returns:
        ScopeAnalysisResult.
    """
    # 전체 트리 크기 판단
    full_tree = build_directory_tree(
        clone_dir,
        supported_extensions=supported_extensions,
        max_depth=max_depth,
        max_entries=2000,
    )
    tree_lines = full_tree.count("\n") + 1
    raw_responses: list[str] = []

    if tree_lines <= _SINGLE_PASS_THRESHOLD:
        # 소규모 레포 → 단일 호출
        logger.info("소규모 레포 (%d줄), 단일 호출 분석", tree_lines)
        proposals = await _analyze_single_pass(full_tree, llm_client)
    else:
        # 대규모 레포 → 2-pass
        logger.info("대규모 레포 (%d줄), 2-pass 분석", tree_lines)
        proposals = await _analyze_two_pass(
            clone_dir, llm_client, supported_extensions
        )

    logger.info("스코프 분석 완료: %d개 상품 식별", len(proposals))
    return ScopeAnalysisResult(
        products=proposals,
        raw_tree=full_tree,
        raw_llm_response="(multi-pass)" if tree_lines > _SINGLE_PASS_THRESHOLD else "",
    )
