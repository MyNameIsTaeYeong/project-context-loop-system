"""스코프 분석기 테스트."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from context_loop.ingestion.scope_analyzer import (
    ProductScopeProposal,
    ScopeAnalysisResult,
    _parse_proposals,
    analyze_repository_scope,
    build_directory_tree,
)
from context_loop.processor.llm_client import LLMClient


# --- Mock LLM ---


class MockLLMClient(LLMClient):
    """LLM 응답을 미리 지정하는 목 클라이언트."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.last_prompt: str = ""
        self.call_count: int = 0

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        **kwargs: Any,
    ) -> str:
        self.last_prompt = prompt
        self.call_count += 1
        return self._response


# --- Tests: build_directory_tree ---


class TestBuildDirectoryTree:
    def test_basic_tree(self, tmp_path: Path) -> None:
        (tmp_path / "services" / "vpc").mkdir(parents=True)
        (tmp_path / "services" / "vpc" / "main.go").write_text("package main")
        (tmp_path / "services" / "billing").mkdir(parents=True)
        (tmp_path / "services" / "billing" / "api.py").write_text("pass")
        (tmp_path / "lib").mkdir()
        (tmp_path / "lib" / "utils.py").write_text("pass")

        tree = build_directory_tree(tmp_path)
        assert "services/" in tree
        assert "vpc/" in tree
        assert "main.go" in tree
        assert "billing/" in tree
        assert "api.py" in tree
        assert "lib/" in tree

    def test_skips_git_dir(self, tmp_path: Path) -> None:
        (tmp_path / ".git" / "objects").mkdir(parents=True)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("pass")

        tree = build_directory_tree(tmp_path)
        assert ".git" not in tree
        assert "main.py" in tree

    def test_skips_node_modules(self, tmp_path: Path) -> None:
        (tmp_path / "node_modules" / "foo").mkdir(parents=True)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.ts").write_text("export {}")

        tree = build_directory_tree(tmp_path)
        assert "node_modules" not in tree
        assert "app.ts" in tree

    def test_filter_by_extensions(self, tmp_path: Path) -> None:
        (tmp_path / "main.go").write_text("package main")
        (tmp_path / "readme.md").write_text("# README")
        (tmp_path / "data.json").write_text("{}")

        tree = build_directory_tree(tmp_path, supported_extensions=[".go"])
        assert "main.go" in tree
        assert "readme.md" not in tree
        assert "data.json" not in tree

    def test_max_depth(self, tmp_path: Path) -> None:
        deep = tmp_path / "a" / "b" / "c" / "d" / "e" / "f"
        deep.mkdir(parents=True)
        (deep / "deep.py").write_text("pass")

        tree = build_directory_tree(tmp_path, max_depth=2)
        assert "a/" in tree
        assert "b/" in tree
        # depth 2 이후는 잘림
        assert "deep.py" not in tree

    def test_max_entries(self, tmp_path: Path) -> None:
        for i in range(20):
            (tmp_path / f"file{i:02d}.py").write_text("pass")

        tree = build_directory_tree(tmp_path, max_entries=10)
        lines = tree.strip().split("\n")
        assert len(lines) <= 11  # 10 + 잘림 메시지

    def test_empty_directory(self, tmp_path: Path) -> None:
        tree = build_directory_tree(tmp_path)
        assert tree == "(빈 디렉토리)"

    def test_directories_only_mode(self, tmp_path: Path) -> None:
        (tmp_path / "services" / "vpc").mkdir(parents=True)
        (tmp_path / "services" / "vpc" / "main.go").write_text("package main")
        (tmp_path / "services" / "vpc" / "handler.go").write_text("package handler")
        (tmp_path / "services" / "billing").mkdir(parents=True)
        (tmp_path / "services" / "billing" / "api.py").write_text("pass")

        tree = build_directory_tree(tmp_path, directories_only=True)
        # 디렉토리는 파일 개수와 함께 표시
        assert "vpc/ (2 files)" in tree
        assert "billing/ (1 files)" in tree
        # 개별 파일 이름은 표시되지 않음
        assert "main.go" not in tree
        assert "handler.go" not in tree
        assert "api.py" not in tree

    def test_directories_only_with_extensions(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.go").write_text("package main")
        (tmp_path / "src" / "readme.md").write_text("# README")

        tree = build_directory_tree(
            tmp_path, supported_extensions=[".go"], directories_only=True,
        )
        assert "src/ (1 files)" in tree  # .go만 카운트


# --- Tests: _parse_proposals ---


class TestParseProposals:
    def test_parse_dict_format(self) -> None:
        raw = {
            "products": [
                {
                    "name": "vpc",
                    "display_name": "VPC Service",
                    "description": "VPC 관리 서비스",
                    "paths": ["services/vpc/**"],
                    "exclude": ["**/*_test.go"],
                },
                {
                    "name": "billing",
                    "display_name": "Billing",
                    "description": "과금 서비스",
                    "paths": ["services/billing/**"],
                },
            ]
        }
        proposals = _parse_proposals(raw)
        assert len(proposals) == 2
        assert proposals[0].name == "vpc"
        assert proposals[0].paths == ["services/vpc/**"]
        assert proposals[0].exclude == ["**/*_test.go"]
        assert proposals[1].name == "billing"
        assert proposals[1].exclude == []  # 미지정 시 빈 리스트

    def test_parse_list_format(self) -> None:
        raw = [
            {"name": "app", "paths": ["src/**"]},
        ]
        proposals = _parse_proposals(raw)
        assert len(proposals) == 1
        assert proposals[0].name == "app"

    def test_skip_invalid_entries(self) -> None:
        raw = {
            "products": [
                {"name": "valid", "paths": ["a/**"]},
                {"display_name": "no-name"},  # name 없음 → 건너뜀
                "not-a-dict",                  # dict 아님 → 건너뜀
            ]
        }
        proposals = _parse_proposals(raw)
        assert len(proposals) == 1

    def test_invalid_format_raises(self) -> None:
        with pytest.raises(ValueError, match="예상하지 못한 JSON"):
            _parse_proposals("not a dict or list")


# --- Tests: ProductScopeProposal ---


class TestProductScopeProposal:
    def test_to_config_dict(self) -> None:
        p = ProductScopeProposal(
            name="vpc",
            display_name="VPC",
            description="desc",
            paths=["services/vpc/**"],
            exclude=["**/*_test.go"],
        )
        d = p.to_config_dict()
        assert d["display_name"] == "VPC"
        assert d["paths"] == ["services/vpc/**"]
        assert d["exclude"] == ["**/*_test.go"]

    def test_to_config_dict_no_exclude(self) -> None:
        p = ProductScopeProposal("app", "App", "desc", ["src/**"])
        d = p.to_config_dict()
        assert "exclude" not in d


# --- Tests: ScopeAnalysisResult ---


class TestScopeAnalysisResult:
    def test_to_config_dict(self) -> None:
        result = ScopeAnalysisResult(
            products=[
                ProductScopeProposal("vpc", "VPC", "desc", ["services/vpc/**"]),
                ProductScopeProposal("billing", "Billing", "desc", ["services/billing/**"]),
            ],
            raw_tree="tree",
            raw_llm_response="response",
        )
        d = result.to_config_dict()
        assert "vpc" in d
        assert "billing" in d
        assert d["vpc"]["paths"] == ["services/vpc/**"]

    def test_summary(self) -> None:
        result = ScopeAnalysisResult(
            products=[
                ProductScopeProposal("vpc", "VPC", "VPC 관리", ["services/vpc/**"], ["*_test.go"]),
            ],
            raw_tree="tree",
            raw_llm_response="response",
        )
        summary = result.summary()
        assert "vpc" in summary
        assert "VPC 관리" in summary
        assert "1개 상품" in summary


# --- Tests: analyze_repository_scope (integration with mock LLM) ---


class TestAnalyzeRepositoryScope:
    async def test_full_analysis(self, tmp_path: Path) -> None:
        # 레포 디렉토리 구조 생성
        (tmp_path / "services" / "vpc").mkdir(parents=True)
        (tmp_path / "services" / "vpc" / "main.go").write_text("package main")
        (tmp_path / "services" / "vpc" / "handler.go").write_text("package handler")
        (tmp_path / "services" / "billing").mkdir(parents=True)
        (tmp_path / "services" / "billing" / "api.py").write_text("pass")
        (tmp_path / "infra" / "terraform").mkdir(parents=True)
        (tmp_path / "infra" / "terraform" / "main.tf").write_text('resource "aws_vpc" {}')

        # Mock LLM 응답
        llm_response = json.dumps({
            "products": [
                {
                    "name": "vpc",
                    "display_name": "VPC 서비스",
                    "description": "VPC 관리 서비스",
                    "paths": ["services/vpc/**"],
                    "exclude": ["**/*_test.go"],
                },
                {
                    "name": "billing",
                    "display_name": "과금 서비스",
                    "description": "과금 처리",
                    "paths": ["services/billing/**"],
                },
                {
                    "name": "infra",
                    "display_name": "인프라",
                    "description": "Terraform 인프라 코드",
                    "paths": ["infra/**"],
                },
            ]
        })
        mock_llm = MockLLMClient(llm_response)

        result = await analyze_repository_scope(tmp_path, mock_llm)

        # LLM 호출 확인
        assert mock_llm.call_count == 1
        assert "services/" in mock_llm.last_prompt
        assert "vpc/" in mock_llm.last_prompt

        # 결과 확인
        assert len(result.products) == 3
        names = {p.name for p in result.products}
        assert names == {"vpc", "billing", "infra"}

        # config dict 변환
        config_dict = result.to_config_dict()
        assert config_dict["vpc"]["paths"] == ["services/vpc/**"]

    async def test_markdown_wrapped_response(self, tmp_path: Path) -> None:
        """LLM이 ```json ... ``` 으로 감싸서 응답해도 파싱."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("pass")

        response = '```json\n{"products": [{"name": "app", "paths": ["src/**"]}]}\n```'
        mock_llm = MockLLMClient(response)

        result = await analyze_repository_scope(tmp_path, mock_llm)
        assert len(result.products) == 1
        assert result.products[0].name == "app"

    async def test_with_extension_filter(self, tmp_path: Path) -> None:
        """supported_extensions 필터가 트리에 적용되는지 확인."""
        (tmp_path / "main.go").write_text("package main")
        (tmp_path / "readme.md").write_text("# README")

        response = json.dumps({"products": [{"name": "app", "paths": ["**"]}]})
        mock_llm = MockLLMClient(response)

        result = await analyze_repository_scope(
            tmp_path, mock_llm, supported_extensions=[".go"]
        )
        # 트리에 .go만 포함
        assert "main.go" in result.raw_tree
        assert "readme.md" not in result.raw_tree
