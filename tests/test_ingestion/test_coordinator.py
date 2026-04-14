"""Coordinator Agent 테스트."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from context_loop.config import Config
from context_loop.ingestion.coordinator import (
    CoordinatorAgent,
    DirectorySummary,
    FileSummary,
    PipelineResult,
    ProductResult,
)
from context_loop.ingestion.git_config import (
    GitSourceConfig,
    load_git_source_config,
)
from context_loop.ingestion.git_repository import FileInfo
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


# --- Mock Worker ---


class MockWorker:
    """Worker Agent 목 구현 — 파일별 요약 반환."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []  # (dir, product, file_count)

    async def process_directory(
        self,
        directory: str,
        product: str,
        files: list[FileInfo],
    ) -> DirectorySummary:
        self.calls.append((directory, product, len(files)))
        file_summaries = [
            FileSummary(f.relative_path, f"요약: {f.relative_path}")
            for f in files
        ]
        return DirectorySummary(
            directory=directory,
            product=product,
            file_summaries=file_summaries,
        )


# --- Tests: Data Types ---


class TestDataTypes:
    def test_directory_summary(self) -> None:
        ds = DirectorySummary(
            directory="services/vpc",
            product="vpc",
            file_summaries=[FileSummary("a.go", "summary a")],
        )
        assert ds.product == "vpc"
        assert len(ds.file_summaries) == 1

    def test_product_result(self) -> None:
        pr = ProductResult(product="vpc")
        assert pr.product == "vpc"
        assert len(pr.directory_summaries) == 0
        assert len(pr.errors) == 0


# --- Tests: Coordinator without agents ---


class TestCoordinatorNoAgents:
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
            repositories=[],  # 문제: 비어있음
            categories={},    # 문제: 비어있음
        )
        coord = CoordinatorAgent(store, config, git_config=git_cfg)
        result = await coord.run()
        assert any("repositories" in e["error"] for e in result.errors)


# --- Tests: Coordinator with mock agents ---


class TestCoordinatorWithMockAgents:
    async def test_full_pipeline(self, store: MetadataStore, tmp_path: Path) -> None:
        """전체 파이프라인: git clone → Worker → Level 1 파일 요약."""
        origin = tmp_path / "origin"
        _init_git_repo(origin, {
            "services/vpc/main.go": "package main",
            "services/vpc/handler.go": "package handler",
        })

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)

        worker = MockWorker()
        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            worker=worker,
        )
        result = await coord.run()

        # Worker 호출 확인
        assert len(worker.calls) >= 1
        assert worker.calls[0][1] == "vpc"  # product

        # 결과 확인
        assert len(result.product_results) == 1
        pr = result.product_results[0]
        assert pr.product == "vpc"
        assert len(pr.directory_summaries) >= 1

    async def test_concurrency_semaphore(self, store: MetadataStore, tmp_path: Path) -> None:
        """max_concurrent_workers 세마포어가 동작하는지 확인."""
        origin = tmp_path / "origin"
        files = {}
        for i in range(15):
            files[f"services/vpc/dir{i}/file.go"] = f"package dir{i}"
        _init_git_repo(origin, files)

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)
        # 동시 2개로 제한
        git_cfg.processing.max_concurrent_workers = 2
        git_cfg.processing.min_files_per_worker = 1

        worker = MockWorker()
        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            worker=worker,
        )
        # 세마포어 재설정
        coord._semaphore = asyncio.Semaphore(2)

        result = await coord.run()
        # 모든 디렉토리가 처리되었는지 확인
        assert len(worker.calls) > 0
        total_files = sum(c[2] for c in worker.calls)
        assert total_files == 15

    async def test_worker_error_captured(self, store: MetadataStore, tmp_path: Path) -> None:
        """Worker 에러가 결과에 캡처되는지 확인."""
        origin = tmp_path / "origin"
        _init_git_repo(origin, {"services/vpc/main.go": "package main"})

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)

        class FailingWorker:
            async def process_directory(self, directory, product, files):
                raise RuntimeError("Worker 실패!")

        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            worker=FailingWorker(),
        )
        result = await coord.run()

        pr = result.product_results[0]
        assert len(pr.errors) > 0
        assert "Worker 실패" in pr.errors[0]["error"]


# --- Tests: Storage helpers ---


class TestStorageHelpers:
    async def test_store_file_summary(self, store: MetadataStore, tmp_path: Path) -> None:
        """store_file_summary가 code_file_summary 문서를 저장한다."""
        config = Config(config_path=tmp_path / "config.yaml")
        git_cfg = GitSourceConfig()
        coord = CoordinatorAgent(store, config, git_config=git_cfg)

        fs = FileSummary("main.go", "파일 요약 내용")
        doc_id, needs_pipeline = await coord.store_file_summary(fs, "vpc")
        assert doc_id > 0
        assert needs_pipeline is True

        doc = await store.get_document(doc_id)
        assert doc is not None
        assert doc["source_type"] == "code_file_summary"
        assert doc["source_id"] == "vpc:main.go"

    async def test_store_file_summary_idempotent(self, store: MetadataStore, tmp_path: Path) -> None:
        config = Config(config_path=tmp_path / "config.yaml")
        git_cfg = GitSourceConfig()
        coord = CoordinatorAgent(store, config, git_config=git_cfg)

        fs = FileSummary("main.go", "요약")
        id1, needs1 = await coord.store_file_summary(fs, "vpc")
        id2, needs2 = await coord.store_file_summary(fs, "vpc")
        assert id1 == id2  # 동일 content → 동일 ID
        assert needs1 is True  # 최초 저장
        assert needs2 is False  # 변경 없음

    async def test_run_and_store(self, store: MetadataStore, tmp_path: Path) -> None:
        """run_and_store()가 DB에 code_file_summary를 저장하는지 확인."""
        origin = tmp_path / "origin"
        _init_git_repo(origin, {"services/vpc/main.go": "package main"})

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)

        worker = MockWorker()
        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            worker=worker,
        )
        result = await coord.run_and_store()

        # code_file_summary 저장 확인
        file_summaries = await store.list_documents(source_type="code_file_summary")
        assert len(file_summaries) >= 1


# --- Tests: Phase 9.7 — git_code 저장 + document_sources 연결 ---


class TestPhase97GitCodeStorage:
    async def test_run_populates_files_and_repo_url(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """run() 결과에 files와 repo_url이 채워진다."""
        origin = tmp_path / "origin"
        _init_git_repo(origin, {
            "services/vpc/main.go": "package main",
            "services/vpc/handler.go": "package handler",
        })

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)
        worker = MockWorker()

        coord = CoordinatorAgent(
            store, config, git_config=git_cfg, worker=worker,
        )
        result = await coord.run()

        pr = result.product_results[0]
        assert pr.repo_url == str(origin)
        assert len(pr.files) == 2
        paths = {f.relative_path for f in pr.files}
        assert "services/vpc/main.go" in paths
        assert "services/vpc/handler.go" in paths

    async def test_run_and_store_creates_git_code_documents(
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
        worker = MockWorker()

        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            worker=worker,
        )
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

    async def test_run_and_store_links_file_summary_to_git_code(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """code_file_summary가 document_sources를 통해 git_code와 연결된다."""
        origin = tmp_path / "origin"
        _init_git_repo(origin, {
            "services/vpc/main.go": "package main",
            "services/vpc/handler.go": "package handler",
        })

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)
        worker = MockWorker()

        coord = CoordinatorAgent(
            store, config, git_config=git_cfg, worker=worker,
        )
        await coord.run_and_store()

        file_summaries = await store.list_documents(source_type="code_file_summary")
        assert len(file_summaries) == 2

        # code_file_summary → git_code 연결 확인
        for fs_doc in file_summaries:
            sources = await store.get_document_sources(fs_doc["id"])
            assert len(sources) == 1
            src_doc = await store.get_document(sources[0]["source_doc_id"])
            assert src_doc is not None
            assert src_doc["source_type"] == "git_code"

    async def test_run_and_store_git_code_idempotent(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """run_and_store()를 두 번 실행해도 git_code가 중복 생성되지 않는다."""
        origin = tmp_path / "origin"
        _init_git_repo(origin, {"services/vpc/main.go": "package main"})

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)
        worker = MockWorker()

        coord = CoordinatorAgent(
            store, config, git_config=git_cfg, worker=worker,
        )
        await coord.run_and_store()
        await coord.run_and_store()

        git_codes = await store.list_documents(source_type="git_code")
        assert len(git_codes) == 1  # 중복 없음

    async def test_run_and_store_reverse_lookup(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """git_code에서 역방향으로 code_file_summary를 조회할 수 있다."""
        origin = tmp_path / "origin"
        _init_git_repo(origin, {"services/vpc/main.go": "package main"})

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)
        worker = MockWorker()

        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            worker=worker,
        )
        await coord.run_and_store()

        git_codes = await store.list_documents(source_type="git_code")
        assert len(git_codes) == 1

        # git_code → 참조하는 문서 조회
        referencing = await store.get_documents_by_source(git_codes[0]["id"])
        # code_file_summary 1개
        assert len(referencing) == 1
        assert referencing[0]["source_type"] == "code_file_summary"


# --- Tests: Phase 9.8 — Pipeline Processing ---


class TestPhase98PipelineProcessing:
    """Phase 9.8: 파이프라인 연결 테스트."""

    async def test_pipeline_available_false_without_deps(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """파이프라인 의존성 미설정 시 _pipeline_available은 False."""
        config = Config(config_path=tmp_path / "config.yaml")
        git_cfg = GitSourceConfig()
        coord = CoordinatorAgent(store, config, git_config=git_cfg)
        assert coord._pipeline_available is False

    async def test_pipeline_available_true_with_all_deps(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """모든 파이프라인 의존성이 설정되면 _pipeline_available은 True."""
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
        """일부 파이프라인 의존성만 설정되면 _pipeline_available은 False."""
        from unittest.mock import MagicMock

        config = Config(config_path=tmp_path / "config.yaml")
        git_cfg = GitSourceConfig()
        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            vector_store=MagicMock(),
            # graph_store, pipeline_llm_client, embedding_client 미설정
        )
        assert coord._pipeline_available is False

    async def test_process_through_pipeline_skips_without_deps(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """파이프라인 의존성 없으면 _process_through_pipeline은 None 반환."""
        config = Config(config_path=tmp_path / "config.yaml")
        git_cfg = GitSourceConfig()
        coord = CoordinatorAgent(store, config, git_config=git_cfg)

        result = await coord._process_through_pipeline(999)
        assert result is None

    async def test_process_through_pipeline_calls_process_document(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """파이프라인 의존성이 있으면 process_document가 호출된다."""
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

        # 테스트용 문서 생성
        doc_id = await store.create_document(
            source_type="code_file_summary",
            source_id="test:main.go",
            title="Test Doc",
            original_content="# Test\nSome content",
            content_hash="hash1",
        )

        expected_result = {
            "document_id": doc_id,
            "storage_method": "chunk",
            "chunk_count": 1,
            "node_count": 0,
            "edge_count": 0,
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
        assert call_kwargs[1]["vector_store"] is mock_vs
        assert call_kwargs[1]["graph_store"] is mock_gs
        assert call_kwargs[1]["llm_client"] is mock_llm
        assert call_kwargs[1]["embedding_client"] is mock_emb

    async def test_process_through_pipeline_handles_error(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """파이프라인 처리 실패 시 None을 반환하고 예외를 전파하지 않는다."""
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
            source_type="code_file_summary",
            source_id="test:fail",
            title="Fail Doc",
            original_content="content",
            content_hash="h",
        )

        with patch(
            "context_loop.processor.pipeline.process_document",
            new_callable=AsyncMock,
            side_effect=RuntimeError("LLM 호출 실패"),
        ):
            result = await coord._process_through_pipeline(doc_id)

        assert result is None  # 예외가 전파되지 않음

    async def test_store_file_summary_returns_needs_pipeline(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """store_file_summary의 needs_pipeline 반환값을 확인."""
        config = Config(config_path=tmp_path / "config.yaml")
        git_cfg = GitSourceConfig()
        coord = CoordinatorAgent(store, config, git_config=git_cfg)

        fs = FileSummary("main.go", "파일 요약")
        doc_id, needs = await coord.store_file_summary(fs, "vpc")
        assert doc_id > 0
        assert needs is True

        # 동일 내용 재저장 → 변경 없음
        _, needs2 = await coord.store_file_summary(fs, "vpc")
        assert needs2 is False

        # 내용 변경 → 재처리 필요
        fs_updated = FileSummary("main.go", "수정된 요약")
        _, needs3 = await coord.store_file_summary(fs_updated, "vpc")
        assert needs3 is True

    async def test_run_and_store_without_pipeline_deps(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """파이프라인 의존성 없이 run_and_store()는 기존처럼 저장만 수행."""
        origin = tmp_path / "origin"
        _init_git_repo(origin, {"services/vpc/main.go": "package main"})

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)

        worker = MockWorker()
        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            worker=worker,
            # 파이프라인 의존성 없음
        )
        result = await coord.run_and_store()

        # code_file_summary 저장 확인
        file_summaries = await store.list_documents(source_type="code_file_summary")
        assert len(file_summaries) >= 1

    async def test_run_and_store_with_pipeline_deps(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """파이프라인 의존성이 있으면 저장 후 파이프라인이 호출된다."""
        from unittest.mock import AsyncMock, MagicMock, patch

        origin = tmp_path / "origin"
        _init_git_repo(origin, {"services/vpc/main.go": "package main"})

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)

        worker = MockWorker()
        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            worker=worker,
            vector_store=MagicMock(),
            graph_store=MagicMock(),
            pipeline_llm_client=MagicMock(),
            embedding_client=MagicMock(),
        )

        call_count = 0

        async def mock_process_document(document_id, **kwargs):
            nonlocal call_count
            call_count += 1
            return {
                "document_id": document_id,
                "storage_method": "chunk",
                "chunk_count": 1,
                "node_count": 0,
                "edge_count": 0,
            }

        with patch(
            "context_loop.processor.pipeline.process_document",
            side_effect=mock_process_document,
        ):
            result = await coord.run_and_store()

        # Level 1 (code_file_summary) 1개만 파이프라인 처리
        assert call_count == 1

    async def test_run_and_store_pipeline_failure_does_not_block(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """파이프라인 처리 실패가 다른 문서 저장을 중단하지 않는다."""
        from unittest.mock import AsyncMock, MagicMock, patch

        origin = tmp_path / "origin"
        _init_git_repo(origin, {"services/vpc/main.go": "package main"})

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)

        worker = MockWorker()
        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            worker=worker,
            vector_store=MagicMock(),
            graph_store=MagicMock(),
            pipeline_llm_client=MagicMock(),
            embedding_client=MagicMock(),
        )

        # 모든 파이프라인 호출이 실패하도록 설정
        with patch(
            "context_loop.processor.pipeline.process_document",
            new_callable=AsyncMock,
            side_effect=RuntimeError("LLM 서버 다운"),
        ):
            result = await coord.run_and_store()

        # 파이프라인 실패에도 불구하고 문서가 저장됨
        file_summaries = await store.list_documents(source_type="code_file_summary")
        assert len(file_summaries) >= 1

    async def test_run_and_store_idempotent_skips_pipeline(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """두 번째 run_and_store()에서는 변경 없으므로 파이프라인 호출 안 함."""
        from unittest.mock import AsyncMock, MagicMock, patch

        origin = tmp_path / "origin"
        _init_git_repo(origin, {"services/vpc/main.go": "package main"})

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)

        worker = MockWorker()

        # 첫 번째 실행 — 파이프라인 없이
        coord1 = CoordinatorAgent(
            store, config, git_config=git_cfg,
            worker=worker,
        )
        await coord1.run_and_store()

        # 두 번째 실행 — 파이프라인 있지만 변경 없음
        call_count = 0

        async def mock_process_document(document_id, **kwargs):
            nonlocal call_count
            call_count += 1
            return {
                "document_id": document_id,
                "storage_method": "chunk",
                "chunk_count": 1,
                "node_count": 0,
                "edge_count": 0,
            }

        coord2 = CoordinatorAgent(
            store, config, git_config=git_cfg,
            worker=worker,
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

    async def test_git_code_not_processed_through_pipeline(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """git_code 문서는 파이프라인으로 처리되지 않는다."""
        from unittest.mock import AsyncMock, MagicMock, patch

        origin = tmp_path / "origin"
        _init_git_repo(origin, {"services/vpc/main.go": "package main"})

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)

        # Worker 없이 — git_code만 저장
        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            worker=None,
            vector_store=MagicMock(),
            graph_store=MagicMock(),
            pipeline_llm_client=MagicMock(),
            embedding_client=MagicMock(),
        )

        call_count = 0

        async def mock_process_document(document_id, **kwargs):
            nonlocal call_count
            call_count += 1
            return {"document_id": document_id, "storage_method": "chunk",
                    "chunk_count": 0, "node_count": 0, "edge_count": 0}

        with patch(
            "context_loop.processor.pipeline.process_document",
            side_effect=mock_process_document,
        ):
            await coord.run_and_store()

        # Worker가 없으므로 code_file_summary 없음
        # git_code만 저장됨 → 파이프라인 호출 0회
        assert call_count == 0
        git_codes = await store.list_documents(source_type="git_code")
        assert len(git_codes) >= 1
