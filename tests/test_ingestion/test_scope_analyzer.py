"""스코프 분석기 테스트 — config 기반 상품명 → 파일 경로 자동 탐지."""

from __future__ import annotations

from pathlib import Path

from context_loop.ingestion.scope_analyzer import (
    _filename_matches_product,
    _plural_variants,
    resolve_product_paths,
)


# --- Tests: _plural_variants ---


class TestPluralVariants:
    def test_regular(self) -> None:
        assert _plural_variants("vpc") == {"vpc", "vpcs"}

    def test_consonant_y(self) -> None:
        """자음+y → ies 변형 포함."""
        v = _plural_variants("policy")
        assert "policy" in v
        assert "policys" in v
        assert "policies" in v

    def test_vowel_y(self) -> None:
        """모음+y → ies 변형 미생성."""
        v = _plural_variants("key")
        assert "key" in v
        assert "keys" in v
        assert "kies" not in v

    def test_sibilant_s(self) -> None:
        v = _plural_variants("address")
        assert "addresses" in v

    def test_sibilant_ch(self) -> None:
        v = _plural_variants("batch")
        assert "batches" in v

    def test_sibilant_x(self) -> None:
        v = _plural_variants("box")
        assert "boxes" in v


# --- Tests: _filename_matches_product ---


class TestFilenameMatchesProduct:
    def test_underscore_token(self) -> None:
        variants = _plural_variants("vpc")
        assert _filename_matches_product("vpc_controller.go", variants) is True

    def test_hyphen_token(self) -> None:
        variants = _plural_variants("vpc")
        assert _filename_matches_product("cloud-vpc-config.yaml", variants) is True

    def test_plural_match(self) -> None:
        variants = _plural_variants("vpc")
        assert _filename_matches_product("vpcs_handler.go", variants) is True

    def test_no_boundary_no_match(self) -> None:
        """상품명이 토큰 경계 없이 포함된 경우 매칭하지 않음."""
        variants = _plural_variants("vpc")
        assert _filename_matches_product("evpc_handler.go", variants) is False

    def test_suffix_no_match(self) -> None:
        variants = _plural_variants("vpc")
        assert _filename_matches_product("vpcache.go", variants) is False

    def test_case_insensitive(self) -> None:
        variants = _plural_variants("vpc")
        assert _filename_matches_product("VPC_Controller.go", variants) is True

    def test_policy_plural(self) -> None:
        variants = _plural_variants("policy")
        assert _filename_matches_product("policies_service.go", variants) is True

    def test_mixed_separators(self) -> None:
        """_ 와 - 가 혼재된 파일명."""
        variants = _plural_variants("vpc")
        assert _filename_matches_product("cloud_vpc-handler.go", variants) is True


# --- Tests: resolve_product_paths ---


class TestResolveProductPaths:
    def test_basic_file_matching(self, tmp_path: Path) -> None:
        """기본 파일명 매칭."""
        (tmp_path / "controller").mkdir()
        (tmp_path / "controller" / "vpc_controller.go").write_text("package vpc")
        (tmp_path / "service").mkdir()
        (tmp_path / "service" / "vpc_service.go").write_text("package vpc")
        (tmp_path / "service" / "billing_service.go").write_text("package billing")

        result = resolve_product_paths(tmp_path, ["vpc", "billing"])

        assert "controller/vpc_controller.go" in result["vpc"]
        assert "service/vpc_service.go" in result["vpc"]
        assert "service/billing_service.go" in result["billing"]
        assert "service/billing_service.go" not in result["vpc"]

    def test_plural_file_matching(self, tmp_path: Path) -> None:
        """복수형 파일명 매칭."""
        (tmp_path / "handler").mkdir()
        (tmp_path / "handler" / "vpcs_handler.go").write_text("package vpc")
        (tmp_path / "handler" / "policies_handler.go").write_text("package policy")

        result = resolve_product_paths(tmp_path, ["vpc", "policy"])

        assert "handler/vpcs_handler.go" in result["vpc"]
        assert "handler/policies_handler.go" in result["policy"]

    def test_deep_directory_scan(self, tmp_path: Path) -> None:
        """깊은 디렉토리에 있는 파일도 탐지."""
        deep = tmp_path / "src" / "main" / "controller"
        deep.mkdir(parents=True)
        (deep / "vpc_controller.go").write_text("package vpc")
        proto = tmp_path / "proto"
        proto.mkdir()
        (proto / "vpc_message.proto").write_text("syntax = proto3;")

        result = resolve_product_paths(tmp_path, ["vpc"])

        assert "src/main/controller/vpc_controller.go" in result["vpc"]
        assert "proto/vpc_message.proto" in result["vpc"]

    def test_extension_filter(self, tmp_path: Path) -> None:
        """확장자 필터 적용."""
        (tmp_path / "vpc_controller.go").write_text("package vpc")
        (tmp_path / "vpc_readme.md").write_text("# VPC")

        result = resolve_product_paths(tmp_path, ["vpc"], supported_extensions=[".go"])

        assert "vpc_controller.go" in result["vpc"]
        assert "vpc_readme.md" not in result["vpc"]

    def test_skip_dirs(self, tmp_path: Path) -> None:
        """node_modules, .git 등은 스킵."""
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "vpc_module.js").write_text("//")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "vpc_service.py").write_text("pass")

        result = resolve_product_paths(tmp_path, ["vpc"])

        paths = result["vpc"]
        assert "src/vpc_service.py" in paths
        assert not any("node_modules" in p for p in paths)

    def test_no_false_positive(self, tmp_path: Path) -> None:
        """상품명이 토큰 경계 없이 포함된 파일은 매칭하지 않음."""
        (tmp_path / "evpc_handler.go").write_text("package evpc")
        (tmp_path / "vpcache_util.go").write_text("package cache")
        (tmp_path / "vpc_handler.go").write_text("package vpc")

        result = resolve_product_paths(tmp_path, ["vpc"])

        assert "vpc_handler.go" in result["vpc"]
        assert "evpc_handler.go" not in result["vpc"]
        assert "vpcache_util.go" not in result["vpc"]

    def test_no_match_returns_empty(self, tmp_path: Path) -> None:
        """매칭 없으면 빈 리스트."""
        (tmp_path / "main.go").write_text("package main")

        result = resolve_product_paths(tmp_path, ["vpc"])

        assert result["vpc"] == []

    def test_multiple_products(self, tmp_path: Path) -> None:
        """여러 상품이 동시에 매칭."""
        (tmp_path / "vpc_controller.go").write_text("pass")
        (tmp_path / "vpc_subnet_handler.go").write_text("pass")
        (tmp_path / "billing_service.go").write_text("pass")

        result = resolve_product_paths(tmp_path, ["vpc", "subnet", "billing"])

        assert "vpc_controller.go" in result["vpc"]
        assert "vpc_subnet_handler.go" in result["vpc"]
        assert "vpc_subnet_handler.go" in result["subnet"]
        assert "billing_service.go" in result["billing"]

    def test_exclude_patterns(self, tmp_path: Path) -> None:
        """exclude 패턴에 매칭되는 파일은 제외."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "vpc_service.go").write_text("package vpc")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "vpc_test.go").write_text("package vpc")
        (tmp_path / "vendor").mkdir()
        (tmp_path / "vendor" / "vpc_lib.go").write_text("package vpc")

        result = resolve_product_paths(
            tmp_path, ["vpc"], exclude_patterns=["tests/**", "vendor/**"],
        )

        assert "src/vpc_service.go" in result["vpc"]
        assert "tests/vpc_test.go" not in result["vpc"]
        # vendor는 skip_dirs로도 제외되지만 exclude로도 제외
        assert not any("vendor" in p for p in result["vpc"])

    def test_exclude_patterns_none(self, tmp_path: Path) -> None:
        """exclude_patterns가 None이면 모든 파일 포함."""
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "vpc_test.go").write_text("package vpc")

        result = resolve_product_paths(tmp_path, ["vpc"], exclude_patterns=None)

        assert "tests/vpc_test.go" in result["vpc"]
