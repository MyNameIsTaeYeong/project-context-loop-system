"""Git 소스 설정 모듈.

config.yaml의 `sources.git` 섹션을 타입 안전한 dataclass로 파싱하고,
에이전트별 LLM 엔드포인트 해소(폴백), 카테고리 정의 접근,
설정 검증 기능을 제공한다. (D-028, D-029)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from context_loop.config import Config
from context_loop.processor.llm_client import EndpointLLMClient, LLMClient


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class LLMEndpointConfig:
    """에이전트별 LLM 엔드포인트 설정."""

    endpoint: str = ""
    model: str = ""
    api_key: str = ""

    @property
    def is_configured(self) -> bool:
        """엔드포인트와 모델이 모두 설정되어 있는지 여부."""
        return bool(self.endpoint) and bool(self.model)


@dataclass
class CategoryConfig:
    """문서 카테고리 정의 (D-028).

    config에서 코드 변경 없이 카테고리를 자유롭게 추가/수정 가능.
    """

    name: str
    display_name: str
    target_audience: str
    prompt: str

    def source_id(self, product: str) -> str:
        """이 카테고리의 code_doc source_id를 생성한다.

        예: "vpc:architecture", "billing:pricing"
        """
        return f"{product}:{self.name}"


@dataclass
class ProcessingConfig:
    """멀티에이전트 처리 설정 (D-027, D-029)."""

    max_concurrent_workers: int = 10
    max_files_per_worker: int = 30
    min_files_per_worker: int = 3
    worker: LLMEndpointConfig = field(default_factory=LLMEndpointConfig)
    synthesizer: LLMEndpointConfig = field(default_factory=LLMEndpointConfig)
    orchestrator: LLMEndpointConfig = field(default_factory=LLMEndpointConfig)


@dataclass
class RepositoryConfig:
    """단일 Git 레포지토리 설정."""

    url: str
    branch: str = "main"
    products: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class GitSourceConfig:
    """sources.git 전체 설정을 타입 안전하게 관리한다."""

    enabled: bool = False
    sync_interval_minutes: int = 60
    file_size_limit_kb: int = 500
    supported_extensions: list[str] = field(default_factory=list)
    repositories: list[RepositoryConfig] = field(default_factory=list)
    categories: dict[str, CategoryConfig] = field(default_factory=dict)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)

    # 글로벌 LLM 폴백 설정 (llm.* 섹션에서 로드)
    _global_llm: LLMEndpointConfig = field(
        default_factory=LLMEndpointConfig, repr=False
    )

    def get_category_list(self) -> list[CategoryConfig]:
        """카테고리 목록을 반환한다."""
        return list(self.categories.values())

    def get_category(self, name: str) -> CategoryConfig | None:
        """이름으로 카테고리를 조회한다."""
        return self.categories.get(name)

    def resolve_endpoint(self, agent: str) -> LLMEndpointConfig:
        """에이전트별 엔드포인트를 해소한다 (D-029).

        agent별 설정이 비어있으면 글로벌 llm.* 설정으로 폴백한다.

        Args:
            agent: "worker", "synthesizer", "orchestrator" 중 하나.

        Returns:
            해소된 LLMEndpointConfig.

        Raises:
            ValueError: 알 수 없는 agent 이름.
        """
        agent_config = getattr(self.processing, agent, None)
        if agent_config is None:
            raise ValueError(
                f"알 수 없는 에이전트: {agent}. "
                "사용 가능: worker, synthesizer, orchestrator"
            )

        if agent_config.is_configured:
            return agent_config

        # 글로벌 폴백
        return self._global_llm

    def build_llm_client(
        self,
        agent: str,
        *,
        timeout: float = 600.0,
    ) -> LLMClient:
        """에이전트용 LLM 클라이언트를 생성한다 (D-029).

        Args:
            agent: "worker", "synthesizer", "orchestrator" 중 하나.
            timeout: HTTP 요청 타임아웃(초). 기본 600초.

        Returns:
            EndpointLLMClient 인스턴스.

        Raises:
            ValueError: 엔드포인트가 설정되지 않은 경우.
        """
        cfg = self.resolve_endpoint(agent)
        if not cfg.endpoint:
            raise ValueError(
                f"{agent} 에이전트의 엔드포인트가 설정되지 않았습니다. "
                "sources.git.processing.{agent} 또는 llm.endpoint를 설정하세요."
            )
        return EndpointLLMClient(
            endpoint=cfg.endpoint,
            model=cfg.model,
            api_key=cfg.api_key or "none",
            timeout=timeout,
        )

    def validate(self) -> list[str]:
        """설정을 검증하고 경고/오류 메시지 리스트를 반환한다.

        Returns:
            문제가 있으면 메시지 리스트, 없으면 빈 리스트.
        """
        issues: list[str] = []

        if self.enabled and not self.repositories:
            issues.append("sources.git.enabled=true이지만 repositories가 비어있습니다.")

        for i, repo in enumerate(self.repositories):
            if not repo.url:
                issues.append(f"repositories[{i}].url이 비어있습니다.")
            if not repo.products:
                issues.append(
                    f"repositories[{i}] ({repo.url}): products가 정의되지 않았습니다."
                )

        if not self.categories:
            issues.append("categories가 비어있습니다. 문서 생성이 불가합니다.")

        for name, cat in self.categories.items():
            if not cat.prompt.strip():
                issues.append(f"categories.{name}.prompt가 비어있습니다.")

        if not self.supported_extensions:
            issues.append("supported_extensions가 비어있습니다.")

        # 엔드포인트 검증: 최소 글로벌 또는 에이전트별 하나는 설정 필요
        if self.enabled and not self._global_llm.is_configured:
            for agent in ("worker", "synthesizer", "orchestrator"):
                agent_cfg = getattr(self.processing, agent)
                if not agent_cfg.is_configured:
                    issues.append(
                        f"{agent} 에이전트의 엔드포인트가 미설정이고 "
                        "글로벌 llm.endpoint도 비어있습니다."
                    )

        return issues


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _parse_endpoint_config(raw: dict[str, Any] | None) -> LLMEndpointConfig:
    """dict에서 LLMEndpointConfig를 파싱한다."""
    if not raw or not isinstance(raw, dict):
        return LLMEndpointConfig()
    return LLMEndpointConfig(
        endpoint=raw.get("endpoint", "") or "",
        model=raw.get("model", "") or "",
        api_key=raw.get("api_key", "") or "",
    )


def _parse_category(name: str, raw: dict[str, Any]) -> CategoryConfig:
    """dict에서 CategoryConfig를 파싱한다."""
    return CategoryConfig(
        name=name,
        display_name=raw.get("display_name", name),
        target_audience=raw.get("target_audience", ""),
        prompt=raw.get("prompt", ""),
    )


def _parse_repository(raw: dict[str, Any]) -> RepositoryConfig:
    """dict에서 RepositoryConfig를 파싱한다."""
    return RepositoryConfig(
        url=raw.get("url", ""),
        branch=raw.get("branch", "main"),
        products=raw.get("products") or {},
    )


def load_git_source_config(config: Config) -> GitSourceConfig:
    """Config 인스턴스에서 GitSourceConfig를 로드한다.

    Args:
        config: 애플리케이션 Config 인스턴스.

    Returns:
        파싱된 GitSourceConfig.
    """
    git_raw: dict[str, Any] = config.get("sources.git", {}) or {}

    # 카테고리 파싱
    categories_raw: dict[str, Any] = git_raw.get("categories") or {}
    categories = {
        name: _parse_category(name, cat_raw)
        for name, cat_raw in categories_raw.items()
        if isinstance(cat_raw, dict)
    }

    # 처리 설정 파싱
    proc_raw: dict[str, Any] = git_raw.get("processing") or {}
    processing = ProcessingConfig(
        max_concurrent_workers=proc_raw.get("max_concurrent_workers", 10),
        max_files_per_worker=proc_raw.get("max_files_per_worker", 30),
        min_files_per_worker=proc_raw.get("min_files_per_worker", 3),
        worker=_parse_endpoint_config(proc_raw.get("worker")),
        synthesizer=_parse_endpoint_config(proc_raw.get("synthesizer")),
        orchestrator=_parse_endpoint_config(proc_raw.get("orchestrator")),
    )

    # 레포지토리 파싱
    repos_raw: list[dict[str, Any]] = git_raw.get("repositories") or []
    repositories = [
        _parse_repository(r) for r in repos_raw if isinstance(r, dict)
    ]

    # 글로벌 LLM 폴백
    global_llm = LLMEndpointConfig(
        endpoint=config.get("llm.endpoint", "") or "",
        model=config.get("llm.model", "") or "",
        api_key=config.get("llm.api_key", "") or "",
    )

    return GitSourceConfig(
        enabled=bool(git_raw.get("enabled", False)),
        sync_interval_minutes=git_raw.get("sync_interval_minutes", 60),
        file_size_limit_kb=git_raw.get("file_size_limit_kb", 500),
        supported_extensions=git_raw.get("supported_extensions") or [],
        repositories=repositories,
        categories=categories,
        processing=processing,
        _global_llm=global_llm,
    )
