"""스코프 분석기 테스트 — 2-pass 대규모 레포 지원 포함."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from context_loop.ingestion.scope_analyzer import (
    ProductScopeProposal,
    ScopeAnalysisResult,
    _AreaInfo,
    _parse_areas,
    _parse_proposals,
    _parse_single_proposal,
    _SINGLE_PASS_THRESHOLD,
    analyze_repository_scope,
    build_directory_tree,
)
from context_loop.processor.llm_client import LLMClient


# --- Mock LLM ---


class MockLLMClient(LLMClient):
    """LLM 응답을 미리 지정하는 목 클라이언트."""

    def __init__(self, response: str | list[str]) -> None:
        # list이면 호출 순서대로 다른 응답 반환
        if isinstance(response, list):
            self._responses = response
        else:
            self._responses = [response]
        self._response_index = 0
        self.last_prompt: str = ""
        self.prompts: list[str] = []
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
        self.prompts.append(prompt)
        self.call_count += 1
        idx = min(self._response_index, len(self._responses) - 1)
        self._response_index += 1
        return self._responses[idx]


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


# --- Tests: _parse_areas (2-pass) ---


class TestParseAreas:
    def test_parse_areas_dict(self) -> None:
        raw = {
            "areas": [
                {
                    "name": "vpc",
                    "display_name": "VPC 서비스",
                    "description": "VPC 관리",
                    "root_path": "services/vpc",
                },
                {
                    "name": "billing",
                    "display_name": "과금",
                    "description": "과금 처리",
                    "root_path": "services/billing",
                },
            ]
        }
        areas = _parse_areas(raw)
        assert len(areas) == 2
        assert areas[0].name == "vpc"
        assert areas[0].root_path == "services/vpc"
        assert areas[1].display_name == "과금"

    def test_parse_areas_list(self) -> None:
        raw = [
            {"name": "app", "root_path": "src", "display_name": "App", "description": ""},
        ]
        areas = _parse_areas(raw)
        assert len(areas) == 1
        assert areas[0].name == "app"

    def test_skip_missing_name_or_root(self) -> None:
        raw = {
            "areas": [
                {"name": "valid", "root_path": "a"},
                {"name": "no-root"},           # root_path 없음
                {"root_path": "no-name"},      # name 없음
                "not-a-dict",
            ]
        }
        areas = _parse_areas(raw)
        assert len(areas) == 1
        assert areas[0].name == "valid"

    def test_invalid_format_raises(self) -> None:
        with pytest.raises(ValueError, match="예상하지 못한 JSON"):
            _parse_areas("not a dict or list")


# --- Tests: _parse_single_proposal (Pass 2) ---


class TestParseSingleProposal:
    def test_parse_full(self) -> None:
        raw = {
            "name": "vpc",
            "display_name": "VPC 서비스",
            "description": "VPC 관리",
            "paths": ["services/vpc/**"],
            "exclude": ["**/*_test.go"],
        }
        fallback = _AreaInfo("vpc", "VPC", "desc", "services/vpc")
        proposal = _parse_single_proposal(raw, fallback)
        assert proposal.name == "vpc"
        assert proposal.paths == ["services/vpc/**"]
        assert proposal.exclude == ["**/*_test.go"]

    def test_fallback_values(self) -> None:
        raw = {}  # 모든 필드 누락
        fallback = _AreaInfo("myapp", "My App", "desc", "src/myapp")
        proposal = _parse_single_proposal(raw, fallback)
        assert proposal.name == "myapp"
        assert proposal.display_name == "My App"
        assert proposal.paths == ["src/myapp/**"]

    def test_invalid_format_raises(self) -> None:
        fallback = _AreaInfo("x", "X", "", "x")
        with pytest.raises(ValueError, match="예상하지 못한 JSON"):
            _parse_single_proposal("not a dict", fallback)


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


# --- Tests: analyze_repository_scope (single-pass, small repo) ---


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

        # 소규모 → 단일 호출
        assert mock_llm.call_count == 1
        assert "services/" in mock_llm.last_prompt

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


# --- Tests: analyze_repository_scope (two-pass, large repo) ---


class TestAnalyzeTwoPass:
    def _make_large_repo(self, tmp_path: Path) -> Path:
        """_SINGLE_PASS_THRESHOLD 줄을 초과하는 대규모 레포 생성."""
        # 60 dirs × 6 lines each = 360+ lines → 2-pass 트리거
        for i in range(60):
            svc_dir = tmp_path / "services" / f"svc{i:02d}"
            svc_dir.mkdir(parents=True)
            for j in range(5):
                (svc_dir / f"file{j}.go").write_text(f"package svc{i}")
        (tmp_path / "infra" / "terraform").mkdir(parents=True)
        for k in range(10):
            (tmp_path / "infra" / "terraform" / f"mod{k}.tf").write_text("resource {}")
        return tmp_path

    async def test_two_pass_triggered(self, tmp_path: Path) -> None:
        """대규모 레포에서 2-pass 분석이 트리거되는지 확인."""
        repo = self._make_large_repo(tmp_path)

        # Pass 1 응답: 2개 영역 식별
        pass1_response = json.dumps({
            "areas": [
                {
                    "name": "services",
                    "display_name": "서비스",
                    "description": "마이크로서비스 모음",
                    "root_path": "services",
                },
                {
                    "name": "infra",
                    "display_name": "인프라",
                    "description": "Terraform 코드",
                    "root_path": "infra",
                },
            ]
        })

        # Pass 2 응답: 각 영역별 스코프
        pass2_services = json.dumps({
            "name": "services",
            "display_name": "서비스",
            "description": "마이크로서비스 모음",
            "paths": ["services/**"],
            "exclude": ["**/*_test.go"],
        })
        pass2_infra = json.dumps({
            "name": "infra",
            "display_name": "인프라",
            "description": "Terraform 코드",
            "paths": ["infra/**"],
        })

        mock_llm = MockLLMClient([pass1_response, pass2_services, pass2_infra])
        result = await analyze_repository_scope(repo, mock_llm)

        # 1(pass1) + 2(pass2 per area) = 3 호출
        assert mock_llm.call_count == 3
        assert len(result.products) == 2

        names = {p.name for p in result.products}
        assert names == {"services", "infra"}

        # raw_llm_response는 multi-pass 표시
        assert "multi-pass" in result.raw_llm_response

    async def test_two_pass_area_not_found_fallback(self, tmp_path: Path) -> None:
        """Pass 1에서 식별된 경로가 존재하지 않으면 기본 glob 패턴으로 폴백."""
        repo = self._make_large_repo(tmp_path)

        pass1_response = json.dumps({
            "areas": [
                {
                    "name": "nonexistent",
                    "display_name": "없는 디렉토리",
                    "description": "존재하지 않는 경로",
                    "root_path": "does/not/exist",
                },
            ]
        })

        mock_llm = MockLLMClient([pass1_response])
        result = await analyze_repository_scope(repo, mock_llm)

        # Pass 2 LLM 호출 없이 폴백 (Pass 1만 1회)
        assert mock_llm.call_count == 1
        assert len(result.products) == 1
        assert result.products[0].name == "nonexistent"
        assert result.products[0].paths == ["does/not/exist/**"]

    async def test_two_pass_no_areas_returns_empty(self, tmp_path: Path) -> None:
        """Pass 1에서 영역을 식별하지 못하면 빈 결과 반환."""
        repo = self._make_large_repo(tmp_path)

        pass1_response = json.dumps({"areas": []})
        mock_llm = MockLLMClient([pass1_response])

        result = await analyze_repository_scope(repo, mock_llm)
        assert len(result.products) == 0
        assert mock_llm.call_count == 1

    async def test_small_repo_uses_single_pass(self, tmp_path: Path) -> None:
        """소규모 레포(<= 300줄)는 단일 호출로 분석."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("pass")

        response = json.dumps({"products": [{"name": "app", "paths": ["src/**"]}]})
        mock_llm = MockLLMClient(response)

        result = await analyze_repository_scope(tmp_path, mock_llm)
        assert mock_llm.call_count == 1
        assert len(result.products) == 1
        # single-pass면 raw_llm_response가 빈 문자열
        assert result.raw_llm_response == ""

    async def test_two_pass_pass2_error_fallback(self, tmp_path: Path) -> None:
        """Pass 2에서 LLM 오류 시 기본 패턴으로 폴백."""
        repo = self._make_large_repo(tmp_path)

        pass1_response = json.dumps({
            "areas": [
                {"name": "svc", "display_name": "서비스", "description": "서비스", "root_path": "services"},
            ]
        })
        # Pass 2 응답: 잘못된 JSON → extract_json 실패
        pass2_bad = "이것은 JSON이 아닙니다. 그냥 텍스트입니다."

        mock_llm = MockLLMClient([pass1_response, pass2_bad])
        result = await analyze_repository_scope(repo, mock_llm)

        # 오류 시에도 폴백 proposal이 생성됨
        assert len(result.products) == 1
        assert result.products[0].name == "svc"
        assert result.products[0].paths == ["services/**"]
