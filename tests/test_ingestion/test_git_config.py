"""Git 소스 설정 모듈 테스트."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from context_loop.config import Config
from context_loop.ingestion.git_config import (
    CategoryConfig,
    GitSourceConfig,
    LLMEndpointConfig,
    ProcessingConfig,
    RepositoryConfig,
    load_git_source_config,
)


# --- Fixtures ---


def _make_config(tmp_path: Path, overrides: dict | None = None) -> Config:
    """테스트용 Config를 생성한다."""
    user_config = tmp_path / "config.yaml"
    if overrides:
        user_config.write_text(yaml.dump(overrides, allow_unicode=True))
    return Config(config_path=user_config)


# --- LLMEndpointConfig ---


class TestLLMEndpointConfig:
    def test_is_configured_true(self) -> None:
        cfg = LLMEndpointConfig(endpoint="http://localhost:8080/v1", model="gpt-4o")
        assert cfg.is_configured is True

    def test_is_configured_false_no_endpoint(self) -> None:
        cfg = LLMEndpointConfig(endpoint="", model="gpt-4o")
        assert cfg.is_configured is False

    def test_is_configured_false_no_model(self) -> None:
        cfg = LLMEndpointConfig(endpoint="http://localhost:8080/v1", model="")
        assert cfg.is_configured is False

    def test_is_configured_false_empty(self) -> None:
        cfg = LLMEndpointConfig()
        assert cfg.is_configured is False


# --- CategoryConfig ---


class TestCategoryConfig:
    def test_source_id(self) -> None:
        cat = CategoryConfig(
            name="architecture",
            display_name="아키텍처",
            target_audience="아키텍트",
            prompt="문서를 작성하세요.",
        )
        assert cat.source_id("vpc") == "vpc:architecture"
        assert cat.source_id("billing") == "billing:architecture"


# --- GitSourceConfig ---


class TestGitSourceConfig:
    def _make_git_config(self, **kwargs) -> GitSourceConfig:
        defaults = {
            "enabled": True,
            "categories": {
                "arch": CategoryConfig("arch", "아키텍처", "아키텍트", "prompt"),
            },
            "supported_extensions": [".py"],
            "repositories": [
                RepositoryConfig(url="git@github.com:co/repo.git", products={"vpc": {}}),
            ],
            "_global_llm": LLMEndpointConfig("http://global:8080/v1", "global-model"),
        }
        defaults.update(kwargs)
        return GitSourceConfig(**defaults)

    def test_get_category_list(self) -> None:
        cfg = self._make_git_config()
        cats = cfg.get_category_list()
        assert len(cats) == 1
        assert cats[0].name == "arch"

    def test_get_category_by_name(self) -> None:
        cfg = self._make_git_config()
        assert cfg.get_category("arch") is not None
        assert cfg.get_category("nonexistent") is None

    def test_resolve_endpoint_agent_specific(self) -> None:
        """에이전트별 설정이 있으면 그것을 사용."""
        processing = ProcessingConfig(
            worker=LLMEndpointConfig("http://worker:8080/v1", "haiku"),
        )
        cfg = self._make_git_config(processing=processing)
        resolved = cfg.resolve_endpoint("worker")
        assert resolved.endpoint == "http://worker:8080/v1"
        assert resolved.model == "haiku"

    def test_resolve_endpoint_fallback_to_global(self) -> None:
        """에이전트별 설정이 비어있으면 글로벌 폴백."""
        processing = ProcessingConfig(
            worker=LLMEndpointConfig("", ""),  # 비어있음
        )
        cfg = self._make_git_config(processing=processing)
        resolved = cfg.resolve_endpoint("worker")
        assert resolved.endpoint == "http://global:8080/v1"
        assert resolved.model == "global-model"

    def test_resolve_endpoint_unknown_agent(self) -> None:
        cfg = self._make_git_config()
        with pytest.raises(ValueError, match="알 수 없는 에이전트"):
            cfg.resolve_endpoint("unknown_agent")

    def test_build_llm_client(self) -> None:
        """LLM 클라이언트 생성."""
        from context_loop.processor.llm_client import EndpointLLMClient

        cfg = self._make_git_config()
        client = cfg.build_llm_client("worker")
        assert isinstance(client, EndpointLLMClient)

    def test_build_llm_client_no_endpoint_raises(self) -> None:
        """엔드포인트 미설정 시 에러."""
        cfg = self._make_git_config(
            _global_llm=LLMEndpointConfig("", ""),
            processing=ProcessingConfig(),
        )
        with pytest.raises(ValueError, match="엔드포인트가 설정되지 않았습니다"):
            cfg.build_llm_client("worker")

    def test_validate_ok(self) -> None:
        cfg = self._make_git_config()
        issues = cfg.validate()
        assert issues == []

    def test_validate_enabled_no_repos(self) -> None:
        cfg = self._make_git_config(repositories=[])
        issues = cfg.validate()
        assert any("repositories가 비어있습니다" in i for i in issues)

    def test_validate_repo_no_url(self) -> None:
        cfg = self._make_git_config(
            repositories=[RepositoryConfig(url="", products={"vpc": {}})],
        )
        issues = cfg.validate()
        assert any("url이 비어있습니다" in i for i in issues)

    def test_validate_repo_no_products(self) -> None:
        cfg = self._make_git_config(
            repositories=[RepositoryConfig(url="http://x", products={})],
        )
        issues = cfg.validate()
        assert any("products가 정의되지 않았습니다" in i for i in issues)

    def test_validate_empty_categories(self) -> None:
        cfg = self._make_git_config(categories={})
        issues = cfg.validate()
        assert any("categories가 비어있습니다" in i for i in issues)

    def test_validate_category_empty_prompt(self) -> None:
        cfg = self._make_git_config(
            categories={
                "bad": CategoryConfig("bad", "Bad", "nobody", ""),
            },
        )
        issues = cfg.validate()
        assert any("prompt가 비어있습니다" in i for i in issues)

    def test_validate_no_extensions(self) -> None:
        cfg = self._make_git_config(supported_extensions=[])
        issues = cfg.validate()
        assert any("supported_extensions가 비어있습니다" in i for i in issues)

    def test_validate_no_endpoints_anywhere(self) -> None:
        cfg = self._make_git_config(
            _global_llm=LLMEndpointConfig("", ""),
            processing=ProcessingConfig(),
        )
        issues = cfg.validate()
        assert any("엔드포인트" in i for i in issues)
        # worker, synthesizer, orchestrator 모두에 대해
        assert len([i for i in issues if "엔드포인트" in i]) == 3


# --- load_git_source_config ---


class TestLoadGitSourceConfig:
    def test_load_defaults(self, tmp_path: Path) -> None:
        """default.yaml에서 기본 설정이 로드되는지 확인."""
        config = _make_config(tmp_path)
        git_cfg = load_git_source_config(config)

        assert git_cfg.enabled is False
        assert git_cfg.file_size_limit_kb == 500
        assert ".py" in git_cfg.supported_extensions
        assert len(git_cfg.repositories) == 0

        # 기본 카테고리 5개
        assert len(git_cfg.categories) == 5
        assert "architecture" in git_cfg.categories
        assert "development" in git_cfg.categories
        assert "infrastructure" in git_cfg.categories
        assert "pricing" in git_cfg.categories
        assert "business" in git_cfg.categories

        # 기본 프로세싱 설정
        assert git_cfg.processing.max_concurrent_workers == 10
        assert git_cfg.processing.max_files_per_worker == 30

    def test_load_with_user_override(self, tmp_path: Path) -> None:
        """사용자 설정이 기본 설정을 오버라이드하는지 확인."""
        config = _make_config(tmp_path, {
            "sources": {
                "git": {
                    "enabled": True,
                    "file_size_limit_kb": 1000,
                    "repositories": [
                        {
                            "url": "git@github.com:co/repo.git",
                            "branch": "develop",
                            "products": {
                                "vpc": {
                                    "display_name": "VPC",
                                    "paths": ["services/vpc/**"],
                                },
                            },
                        },
                    ],
                },
            },
            "llm": {
                "endpoint": "http://global:8080/v1",
                "model": "global-model",
                "api_key": "key123",
            },
        })
        git_cfg = load_git_source_config(config)

        assert git_cfg.enabled is True
        assert git_cfg.file_size_limit_kb == 1000
        assert len(git_cfg.repositories) == 1
        assert git_cfg.repositories[0].url == "git@github.com:co/repo.git"
        assert git_cfg.repositories[0].branch == "develop"
        assert "vpc" in git_cfg.repositories[0].products

        # 글로벌 LLM 폴백
        assert git_cfg._global_llm.endpoint == "http://global:8080/v1"
        assert git_cfg._global_llm.model == "global-model"

    def test_load_agent_endpoints(self, tmp_path: Path) -> None:
        """에이전트별 엔드포인트가 파싱되는지 확인."""
        config = _make_config(tmp_path, {
            "sources": {
                "git": {
                    "processing": {
                        "worker": {
                            "endpoint": "http://haiku:8080/v1",
                            "model": "haiku",
                            "api_key": "wk",
                        },
                        "synthesizer": {
                            "endpoint": "http://sonnet:8080/v1",
                            "model": "sonnet",
                        },
                    },
                },
            },
        })
        git_cfg = load_git_source_config(config)

        assert git_cfg.processing.worker.endpoint == "http://haiku:8080/v1"
        assert git_cfg.processing.worker.model == "haiku"
        assert git_cfg.processing.worker.api_key == "wk"
        assert git_cfg.processing.synthesizer.endpoint == "http://sonnet:8080/v1"
        # orchestrator는 미설정 → 빈값
        assert git_cfg.processing.orchestrator.endpoint == ""

    def test_load_categories_from_default(self, tmp_path: Path) -> None:
        """기본 카테고리의 prompt, target_audience 로드 확인."""
        config = _make_config(tmp_path)
        git_cfg = load_git_source_config(config)

        arch = git_cfg.get_category("architecture")
        assert arch is not None
        assert arch.display_name == "아키텍처"
        assert "아키텍트" in arch.target_audience
        assert "아키텍처 문서를 작성하세요" in arch.prompt

    def test_endpoint_resolution_integration(self, tmp_path: Path) -> None:
        """에이전트 엔드포인트 해소 통합 테스트."""
        config = _make_config(tmp_path, {
            "sources": {
                "git": {
                    "processing": {
                        "worker": {
                            "endpoint": "http://haiku:8080/v1",
                            "model": "haiku",
                        },
                        # synthesizer, orchestrator 미설정 → 글로벌 폴백
                    },
                },
            },
            "llm": {
                "endpoint": "http://global:8080/v1",
                "model": "global-model",
            },
        })
        git_cfg = load_git_source_config(config)

        # worker → 에이전트 설정 사용
        worker = git_cfg.resolve_endpoint("worker")
        assert worker.endpoint == "http://haiku:8080/v1"
        assert worker.model == "haiku"

        # synthesizer → 글로벌 폴백
        synthesizer = git_cfg.resolve_endpoint("synthesizer")
        assert synthesizer.endpoint == "http://global:8080/v1"
        assert synthesizer.model == "global-model"

        # orchestrator → 글로벌 폴백
        orchestrator = git_cfg.resolve_endpoint("orchestrator")
        assert orchestrator.endpoint == "http://global:8080/v1"

    def test_custom_category_added(self, tmp_path: Path) -> None:
        """사용자 정의 카테고리가 추가되는지 확인."""
        config = _make_config(tmp_path, {
            "sources": {
                "git": {
                    "categories": {
                        "security": {
                            "display_name": "보안 검토",
                            "target_audience": "보안팀",
                            "prompt": "보안 관점에서 분석하세요.",
                        },
                    },
                },
            },
        })
        git_cfg = load_git_source_config(config)

        # deep merge로 기본 5개 + 사용자 1개 = 6개
        assert "security" in git_cfg.categories
        assert git_cfg.categories["security"].display_name == "보안 검토"
        # 기본 카테고리도 유지
        assert "architecture" in git_cfg.categories
