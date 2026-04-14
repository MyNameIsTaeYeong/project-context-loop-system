"""Coordinator 테스트 — git_code 수집 + 파이프라인 직접 처리."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from context_loop.config import Config
from context_loop.ingestion.coordinator import (
    CoordinatorAgent,
    PipelineResult,
    ProductResult,
)
from context_loop.ingestion.git_config import (
    GitSourceConfig,
    load_git_source_config,
)
from context_loop.storage.metadata_store import MetadataStore


# --- Helpers ---


def _git(args: list[str], cwd: Path) -> None:
    env = {**os.environ, "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@test.com"}
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True, env=env)


def _init_git_repo(repo_dir: Path, files: dict[str, str]) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], cwd=repo_dir)
    _git(["config", "user.email", "test@test.com"], cwd=repo_dir)
    _git(["config", "user.name", "Test"], cwd=repo_dir)
    _git(["config", "commit.gpgsign", "false"], cwd=repo_dir)
    for path, content in files.items():
        fp = repo_dir / path
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content, encoding="utf-8")
    _git(["add", "."], cwd=repo_dir)
    _git(["commit", "-m", "init"], cwd=repo_dir)


def _make_config(tmp_path: Path, repo_url: str) -> Config:
    config = Config(config_path=tmp_path / "config.yaml")
    config.set("app.data_dir", str(tmp_path / "data"))
    config.set("sources.git.enabled", True)
    config.set("sources.git.supported_extensions", [".go", ".py"])
    config.set("sources.git.file_size_limit_kb", 500)
    config.set("sources.git.repositories", [
        {
            "url": repo_url,
            "branch": "main",
            "products": {
                "vpc": {
                    "display_name": "VPC",
                    "paths": ["services/vpc/**"],
                    "exclude": [],
                },
            },
        },
    ])
    return config


# --- Fixtures ---


@pytest.fixture
async def store(tmp_path: Path) -> MetadataStore:
    s = MetadataStore(tmp_path / "test.db")
    await s.initialize()
    yield s  # type: ignore[misc]
    await s.close()


# --- Tests: Data Types ---


class TestDataTypes:
    def test_product_result(self) -> None:
        pr = ProductResult(product="vpc")
        assert pr.product == "vpc"
        assert len(pr.files) == 0
        assert len(pr.errors) == 0


# --- Tests: Coordinator basic ---


class TestCoordinatorBasic:
    async def test_disabled_returns_empty(self, store: MetadataStore, tmp_path: Path) -> None:
        config = Config(config_path=tmp_path / "config.yaml")
        git_cfg = GitSourceConfig(enabled=False)
        coord = CoordinatorAgent(store, config, git_config=git_cfg)
        result = await coord.run()
        assert len(result.product_results) == 0

    async def test_validation_issues_logged(self, store: MetadataStore, tmp_path: Path) -> None:
        config = Config(config_path=tmp_path / "config.yaml")
        git_cfg = GitSourceConfig(
            enabled=True,
            repositories=[],
            categories={},
        )
        coord = CoordinatorAgent(store, config, git_config=git_cfg)
        result = await coord.run()
        assert any("repositories" in e["error"] for e in result.errors)


# --- Tests: run() — 파일 수집 ---


class TestRun:
    async def test_run_collects_files(self, store: MetadataStore, tmp_path: Path) -> None:
        """run()이 파일을 수집하여 ProductResult에 넣는다."""
        origin = tmp_path / "origin"
        _init_git_repo(origin, {
            "services/vpc/main.go": "package main",
            "services/vpc/handler.go": "package handler",
        })

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)

        coord = CoordinatorAgent(store, config, git_config=git_cfg)
        result = await coord.run()

        assert len(result.product_results) == 1
        pr = result.product_results[0]
        assert pr.product == "vpc"
        assert pr.repo_url == str(origin)
        assert len(pr.files) == 2
        paths = {f.relative_path for f in pr.files}
        assert "services/vpc/main.go" in paths
        assert "services/vpc/handler.go" in paths


# --- Tests: run_and_store() — git_code 저장 ---


class TestRunAndStore:
    async def test_creates_git_code_documents(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """run_and_store()가 git_code 문서를 DB에 저장한다."""
        origin = tmp_path / "origin"
        _init_git_repo(origin, {
            "services/vpc/main.go": "package main",
            "services/vpc/handler.go": "package handler",
        })

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)

        coord = CoordinatorAgent(store, config, git_config=git_cfg)
        await coord.run_and_store()

        git_codes = await store.list_documents(source_type="git_code")
        assert len(git_codes) == 2
        source_ids = {d["source_id"] for d in git_codes}
        assert "services/vpc/main.go" in source_ids
        assert "services/vpc/handler.go" in source_ids

        # 원본 코드 내용 확인
        for doc in git_codes:
            if doc["source_id"] == "services/vpc/main.go":
                assert doc["original_content"] == "package main"

    async def test_git_code_idempotent(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """run_and_store()를 두 번 실행해도 git_code가 중복 생성되지 않는다."""
        origin = tmp_path / "origin"
        _init_git_repo(origin, {"services/vpc/main.go": "package main"})

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)

        coord = CoordinatorAgent(store, config, git_config=git_cfg)
        await coord.run_and_store()
        await coord.run_and_store()

        git_codes = await store.list_documents(source_type="git_code")
        assert len(git_codes) == 1


# --- Tests: Pipeline Processing ---


class TestPipelineProcessing:
    """git_code → 파이프라인 직접 처리 (hybrid 고정) 테스트."""

    async def test_pipeline_available_false_without_deps(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        config = Config(config_path=tmp_path / "config.yaml")
        git_cfg = GitSourceConfig()
        coord = CoordinatorAgent(store, config, git_config=git_cfg)
        assert coord._pipeline_available is False

    async def test_pipeline_available_true_with_all_deps(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        from unittest.mock import MagicMock

        config = Config(config_path=tmp_path / "config.yaml")
        git_cfg = GitSourceConfig()
        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            vector_store=MagicMock(),
            graph_store=MagicMock(),
            pipeline_llm_client=MagicMock(),
            embedding_client=MagicMock(),
        )
        assert coord._pipeline_available is True

    async def test_pipeline_available_false_partial_deps(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        from unittest.mock import MagicMock

        config = Config(config_path=tmp_path / "config.yaml")
        git_cfg = GitSourceConfig()
        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            vector_store=MagicMock(),
        )
        assert coord._pipeline_available is False

    async def test_process_through_pipeline_skips_without_deps(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        config = Config(config_path=tmp_path / "config.yaml")
        git_cfg = GitSourceConfig()
        coord = CoordinatorAgent(store, config, git_config=git_cfg)
        result = await coord._process_through_pipeline(999)
        assert result is None

    async def test_process_through_pipeline_calls_process_document(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """파이프라인 호출 시 storage_method_override='hybrid'가 전달된다."""
        from unittest.mock import AsyncMock, MagicMock, patch

        config = Config(config_path=tmp_path / "config.yaml")
        git_cfg = GitSourceConfig()

        mock_vs = MagicMock()
        mock_gs = MagicMock()
        mock_llm = MagicMock()
        mock_emb = MagicMock()

        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            vector_store=mock_vs,
            graph_store=mock_gs,
            pipeline_llm_client=mock_llm,
            embedding_client=mock_emb,
        )

        doc_id = await store.create_document(
            source_type="git_code",
            source_id="main.go",
            title="main.go",
            original_content="package main",
            content_hash="h1",
        )

        expected_result = {
            "document_id": doc_id,
            "storage_method": "hybrid",
            "chunk_count": 1,
            "node_count": 2,
            "edge_count": 1,
        }

        with patch(
            "context_loop.processor.pipeline.process_document",
            new_callable=AsyncMock,
            return_value=expected_result,
        ) as mock_pd:
            result = await coord._process_through_pipeline(doc_id)

        assert result == expected_result
        mock_pd.assert_called_once()
        call_kwargs = mock_pd.call_args
        assert call_kwargs[0][0] == doc_id
        assert call_kwargs[1]["meta_store"] is store
        assert call_kwargs[1]["storage_method_override"] == "hybrid"

    async def test_process_through_pipeline_handles_error(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock, patch

        config = Config(config_path=tmp_path / "config.yaml")
        git_cfg = GitSourceConfig()
        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            vector_store=MagicMock(),
            graph_store=MagicMock(),
            pipeline_llm_client=MagicMock(),
            embedding_client=MagicMock(),
        )

        doc_id = await store.create_document(
            source_type="git_code",
            source_id="fail.go",
            title="fail.go",
            original_content="code",
            content_hash="h",
        )

        with patch(
            "context_loop.processor.pipeline.process_document",
            new_callable=AsyncMock,
            side_effect=RuntimeError("LLM 호출 실패"),
        ):
            result = await coord._process_through_pipeline(doc_id)

        assert result is None

    async def test_run_and_store_without_pipeline_deps(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """파이프라인 의존성 없이 run_and_store()는 git_code 저장만 수행."""
        origin = tmp_path / "origin"
        _init_git_repo(origin, {"services/vpc/main.go": "package main"})

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)

        coord = CoordinatorAgent(store, config, git_config=git_cfg)
        await coord.run_and_store()

        git_codes = await store.list_documents(source_type="git_code")
        assert len(git_codes) >= 1

    async def test_run_and_store_with_pipeline_processes_new_files(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """파이프라인 의존성이 있으면 신규 git_code에 파이프라인이 호출된다."""
        from unittest.mock import AsyncMock, MagicMock, patch

        origin = tmp_path / "origin"
        _init_git_repo(origin, {
            "services/vpc/main.go": "package main",
            "services/vpc/handler.go": "package handler",
        })

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)

        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            vector_store=MagicMock(),
            graph_store=MagicMock(),
            pipeline_llm_client=MagicMock(),
            embedding_client=MagicMock(),
        )

        call_count = 0

        async def mock_process_document(document_id, **kwargs):
            nonlocal call_count
            call_count += 1
            assert kwargs.get("storage_method_override") == "hybrid"
            return {
                "document_id": document_id,
                "storage_method": "hybrid",
                "chunk_count": 1,
                "node_count": 0,
                "edge_count": 0,
            }

        with patch(
            "context_loop.processor.pipeline.process_document",
            side_effect=mock_process_document,
        ):
            await coord.run_and_store()

        # 파일 2개 → 파이프라인 2회 호출
        assert call_count == 2

    async def test_run_and_store_pipeline_failure_does_not_block(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """파이프라인 실패가 다른 파일 저장을 중단하지 않는다."""
        from unittest.mock import AsyncMock, MagicMock, patch

        origin = tmp_path / "origin"
        _init_git_repo(origin, {
            "services/vpc/main.go": "package main",
            "services/vpc/handler.go": "package handler",
        })

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)

        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            vector_store=MagicMock(),
            graph_store=MagicMock(),
            pipeline_llm_client=MagicMock(),
            embedding_client=MagicMock(),
        )

        with patch(
            "context_loop.processor.pipeline.process_document",
            new_callable=AsyncMock,
            side_effect=RuntimeError("서버 다운"),
        ):
            await coord.run_and_store()

        # 파이프라인 실패에도 git_code는 모두 저장됨
        git_codes = await store.list_documents(source_type="git_code")
        assert len(git_codes) == 2

    async def test_run_and_store_idempotent_skips_pipeline(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """두 번째 실행에서는 변경 없으므로 파이프라인 호출 안 함."""
        from unittest.mock import MagicMock, patch

        origin = tmp_path / "origin"
        _init_git_repo(origin, {"services/vpc/main.go": "package main"})

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)

        # 첫 번째 실행 — 파이프라인 없이
        coord1 = CoordinatorAgent(store, config, git_config=git_cfg)
        await coord1.run_and_store()

        # 두 번째 실행 — 파이프라인 있지만 변경 없음
        call_count = 0

        async def mock_process_document(document_id, **kwargs):
            nonlocal call_count
            call_count += 1
            return {
                "document_id": document_id,
                "storage_method": "hybrid",
                "chunk_count": 1,
                "node_count": 0,
                "edge_count": 0,
            }

        coord2 = CoordinatorAgent(
            store, config, git_config=git_cfg,
            vector_store=MagicMock(),
            graph_store=MagicMock(),
            pipeline_llm_client=MagicMock(),
            embedding_client=MagicMock(),
        )

        with patch(
            "context_loop.processor.pipeline.process_document",
            side_effect=mock_process_document,
        ):
            await coord2.run_and_store()

        # 변경 없으므로 파이프라인 호출 0회
        assert call_count == 0
