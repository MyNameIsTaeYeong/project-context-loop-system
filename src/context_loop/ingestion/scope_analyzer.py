"""레포지토리 디렉토리 분석 기반 상품 스코프 제안 모듈.

Git 레포의 디렉토리 트리를 LLM에 전달하여 상품 단위를 식별하고,
config에 넣을 수 있는 products 스코프(paths, exclude)를 제안받는다.
D-027 설계: "최초에 LLM이 레포 디렉토리 트리를 분석하여 스코프를 제안하고,
사람이 검토 후 확정. 이후 완전 자동."
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from context_loop.processor.llm_client import LLMClient, extract_json

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
당신은 소프트웨어 아키텍트입니다.
Git 레포지토리의 디렉토리 트리를 분석하여 독립적인 상품(서비스/모듈) 단위를 식별하세요.
각 상품에 대해 config에 넣을 수 있는 스코프 정보를 제안하세요."""

_USER_PROMPT_TEMPLATE = """\
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
- 상품 이름은 디렉토리 구조에서 자연스럽게 유추하세요."""


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
        """config.yaml의 products 섹션 전체를 dict로 변환한다.

        Returns:
            {"product-name": {"display_name": ..., "paths": [...], "exclude": [...]}}
        """
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


def build_directory_tree(
    root: Path,
    supported_extensions: list[str] | None = None,
    max_depth: int = 5,
    max_entries: int = 500,
) -> str:
    """디렉토리 트리 문자열을 생성한다.

    Args:
        root: 레포 루트 경로.
        supported_extensions: 표시할 파일 확장자. None이면 모든 파일.
        max_depth: 최대 탐색 깊이.
        max_entries: 최대 항목 수 (초과 시 잘림).

    Returns:
        트리 문자열 (예: "services/\\n  vpc/\\n    main.go\\n    handler.go")
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

        # 디렉토리와 파일 분리
        dirs = [e for e in entries if e.is_dir() and e.name not in _skip_dirs]
        files = [e for e in entries if e.is_file()]

        if supported_extensions:
            files = [f for f in files if f.suffix.lower() in supported_extensions]

        for d in dirs:
            if len(lines) >= max_entries:
                lines.append(f"{prefix}... (잘림)")
                return
            lines.append(f"{prefix}{d.name}/")
            _walk(d, depth + 1, prefix + "  ")

        for f in files:
            if len(lines) >= max_entries:
                lines.append(f"{prefix}... (잘림)")
                return
            lines.append(f"{prefix}{f.name}")

    _walk(root, 0, "")

    if not lines:
        return "(빈 디렉토리)"

    return "\n".join(lines)


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


async def analyze_repository_scope(
    clone_dir: Path,
    llm_client: LLMClient,
    supported_extensions: list[str] | None = None,
    max_depth: int = 5,
) -> ScopeAnalysisResult:
    """레포지토리를 분석하여 상품 스코프를 제안받는다.

    Args:
        clone_dir: 로컬 clone 경로.
        llm_client: LLM 클라이언트.
        supported_extensions: 트리에 표시할 파일 확장자.
        max_depth: 디렉토리 트리 최대 깊이.

    Returns:
        ScopeAnalysisResult.
    """
    tree = build_directory_tree(
        clone_dir,
        supported_extensions=supported_extensions,
        max_depth=max_depth,
    )

    prompt = _USER_PROMPT_TEMPLATE.format(tree=tree)
    raw_response = await llm_client.complete(
        prompt,
        system=_SYSTEM_PROMPT,
        max_tokens=4096,
        temperature=0.0,
    )

    parsed = extract_json(raw_response)
    proposals = _parse_proposals(parsed)

    logger.info("스코프 분석 완료: %d개 상품 식별", len(proposals))
    return ScopeAnalysisResult(
        products=proposals,
        raw_tree=tree,
        raw_llm_response=raw_response,
    )
