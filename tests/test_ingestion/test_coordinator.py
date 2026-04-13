"""Coordinator Agent 테스트."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from context_loop.config import Config
from context_loop.ingestion.coordinator import (
    CategoryDocument,
    CoordinatorAgent,
    DirectorySummary,
    FileSummary,
    PipelineResult,
    ProductResult,
)
from context_loop.ingestion.git_config import (
    CategoryConfig,
    GitSourceConfig,
    LLMEndpointConfig,
    ProcessingConfig,
    RepositoryConfig,
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


# --- Mock Worker / Category Agent ---


class MockWorker:
    """Worker Agent 목 구현 — 파일 내용을 합쳐서 요약 반환."""

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
        doc = f"# {directory}\n\n{product} 디렉토리 문서. 파일 {len(files)}개."
        return DirectorySummary(
            directory=directory,
            product=product,
            file_summaries=file_summaries,
            document=doc,
        )


class MockCategoryAgent:
    """Category Agent 목 구현."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []  # (product, category, dir_count)

    async def generate_document(
        self,
        product: str,
        category: CategoryConfig,
        directory_summaries: list[DirectorySummary],
    ) -> CategoryDocument:
        self.calls.append((product, category.name, len(directory_summaries)))
        doc = (
            f"# [{product}] {category.display_name}\n\n"
            f"대상: {category.target_audience}\n"
            f"디렉토리 {len(directory_summaries)}개 분석."
        )
        return CategoryDocument(
            product=product,
            category=category.name,
            document=doc,
            source_directories=[ds.directory for ds in directory_summaries],
        )


# --- Tests: Data Types ---


class TestDataTypes:
    def test_directory_summary(self) -> None:
        ds = DirectorySummary(
            directory="services/vpc",
            product="vpc",
            file_summaries=[FileSummary("a.go", "summary a")],
            document="doc",
        )
        assert ds.product == "vpc"
        assert len(ds.file_summaries) == 1

    def test_category_document(self) -> None:
        cd = CategoryDocument(
            product="vpc",
            category="architecture",
            document="doc",
            source_directories=["dir1", "dir2"],
        )
        assert cd.product == "vpc"
        assert len(cd.source_directories) == 2

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
        """전체 파이프라인: git clone → Worker → Category Agent."""
        origin = tmp_path / "origin"
        _init_git_repo(origin, {
            "services/vpc/main.go": "package main",
            "services/vpc/handler.go": "package handler",
        })

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)

        worker = MockWorker()
        cat_agent = MockCategoryAgent()

        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            worker=worker, category_agent=cat_agent,
        )
        result = await coord.run()

        # Worker 호출 확인
        assert len(worker.calls) >= 1
        assert worker.calls[0][1] == "vpc"  # product

        # Category Agent 호출 확인 (기본 5개 카테고리)
        assert len(cat_agent.calls) == 5
        cat_names = {c[1] for c in cat_agent.calls}
        assert "architecture" in cat_names
        assert "development" in cat_names

        # 결과 확인
        assert len(result.product_results) == 1
        pr = result.product_results[0]
        assert pr.product == "vpc"
        assert len(pr.directory_summaries) >= 1
        assert len(pr.category_documents) == 5

    async def test_worker_only_no_category(self, store: MetadataStore, tmp_path: Path) -> None:
        """Worker만 있고 Category Agent 없으면 Level 2까지만 처리."""
        origin = tmp_path / "origin"
        _init_git_repo(origin, {"services/vpc/main.go": "package main"})

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)

        worker = MockWorker()
        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            worker=worker, category_agent=None,
        )
        result = await coord.run()

        pr = result.product_results[0]
        assert len(pr.directory_summaries) >= 1
        assert len(pr.category_documents) == 0  # Category Agent 없음

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
            worker=worker, category_agent=None,
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
            worker=FailingWorker(), category_agent=None,
        )
        result = await coord.run()

        pr = result.product_results[0]
        assert len(pr.errors) > 0
        assert "Worker 실패" in pr.errors[0]["error"]


# --- Tests: Storage helpers ---


class TestStorageHelpers:
    async def test_store_directory_summary(self, store: MetadataStore, tmp_path: Path) -> None:
        config = Config(config_path=tmp_path / "config.yaml")
        git_cfg = GitSourceConfig()
        coord = CoordinatorAgent(store, config, git_config=git_cfg)

        ds = DirectorySummary(
            directory="services/vpc/handler",
            product="vpc",
            file_summaries=[FileSummary("a.go", "summary")],
            document="# handler\n디렉토리 문서",
        )
        doc_id = await coord.store_directory_summary(ds)
        assert doc_id > 0

        doc = await store.get_document(doc_id)
        assert doc is not None
        assert doc["source_type"] == "code_summary"
        assert doc["source_id"] == "vpc:services/vpc/handler"

    async def test_store_directory_summary_idempotent(self, store: MetadataStore, tmp_path: Path) -> None:
        config = Config(config_path=tmp_path / "config.yaml")
        git_cfg = GitSourceConfig()
        coord = CoordinatorAgent(store, config, git_config=git_cfg)

        ds = DirectorySummary("dir", "vpc", [], "doc content")
        id1 = await coord.store_directory_summary(ds)
        id2 = await coord.store_directory_summary(ds)
        assert id1 == id2  # 동일 content → 동일 ID

    async def test_store_category_document(self, store: MetadataStore, tmp_path: Path) -> None:
        config = Config(config_path=tmp_path / "config.yaml")
        git_cfg = load_git_source_config(config)
        coord = CoordinatorAgent(store, config, git_config=git_cfg)

        cd = CategoryDocument(
            product="vpc",
            category="architecture",
            document="# VPC 아키텍처\n설명...",
            source_directories=["dir1"],
        )
        doc_id = await coord.store_category_document(cd)
        assert doc_id > 0

        doc = await store.get_document(doc_id)
        assert doc is not None
        assert doc["source_type"] == "code_doc"
        assert doc["source_id"] == "vpc:architecture"
        assert "아키텍처" in doc["title"]

    async def test_store_category_document_with_sources(self, store: MetadataStore, tmp_path: Path) -> None:
        """document_sources 연결이 저장되는지 확인."""
        config = Config(config_path=tmp_path / "config.yaml")
        git_cfg = load_git_source_config(config)
        coord = CoordinatorAgent(store, config, git_config=git_cfg)

        # git_code 문서 생성
        git_id = await store.create_document(
            source_type="git_code", source_id="vpc.tf",
            title="vpc.tf", original_content="code", content_hash="h1",
        )

        cd = CategoryDocument("vpc", "architecture", "doc", ["dir1"])
        doc_id = await coord.store_category_document(cd, source_git_code_ids=[git_id])

        # document_sources 확인
        sources = await store.get_document_sources(doc_id)
        assert len(sources) == 1
        assert sources[0]["source_doc_id"] == git_id

    async def test_run_and_store(self, store: MetadataStore, tmp_path: Path) -> None:
        """run_and_store()가 DB에 code_summary + code_doc을 저장하는지 확인."""
        origin = tmp_path / "origin"
        _init_git_repo(origin, {"services/vpc/main.go": "package main"})

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)

        worker = MockWorker()
        cat_agent = MockCategoryAgent()

        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            worker=worker, category_agent=cat_agent,
        )
        result = await coord.run_and_store()

        # code_summary 저장 확인
        summaries = await store.list_documents(source_type="code_summary")
        assert len(summaries) >= 1

        # code_doc 저장 확인
        code_docs = await store.list_documents(source_type="code_doc")
        assert len(code_docs) == 5  # 기본 카테고리 5개


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
        cat_agent = MockCategoryAgent()

        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            worker=worker, category_agent=cat_agent,
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

    async def test_run_and_store_links_code_summary_to_git_code(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """code_summary가 document_sources를 통해 git_code와 연결된다."""
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

        summaries = await store.list_documents(source_type="code_summary")
        assert len(summaries) >= 1

        # code_summary → git_code 연결 확인
        summary = summaries[0]
        sources = await store.get_document_sources(summary["id"])
        assert len(sources) == 2  # main.go, handler.go

        source_paths = {s.get("file_path") for s in sources}
        assert "services/vpc/main.go" in source_paths
        assert "services/vpc/handler.go" in source_paths

    async def test_run_and_store_links_code_doc_to_git_code(
        self, store: MetadataStore, tmp_path: Path,
    ) -> None:
        """code_doc가 document_sources를 통해 git_code와 연결된다."""
        origin = tmp_path / "origin"
        _init_git_repo(origin, {"services/vpc/main.go": "package main"})

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)
        worker = MockWorker()
        cat_agent = MockCategoryAgent()

        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            worker=worker, category_agent=cat_agent,
        )
        await coord.run_and_store()

        code_docs = await store.list_documents(source_type="code_doc")
        assert len(code_docs) == 5  # 5 categories

        # 모든 code_doc이 git_code와 연결되어야 함
        for doc in code_docs:
            sources = await store.get_document_sources(doc["id"])
            assert len(sources) >= 1
            # git_code 문서의 source_type 확인
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
        """git_code에서 역방향으로 code_doc/code_summary를 조회할 수 있다."""
        origin = tmp_path / "origin"
        _init_git_repo(origin, {"services/vpc/main.go": "package main"})

        config = _make_config(tmp_path, str(origin))
        git_cfg = load_git_source_config(config)
        worker = MockWorker()
        cat_agent = MockCategoryAgent()

        coord = CoordinatorAgent(
            store, config, git_config=git_cfg,
            worker=worker, category_agent=cat_agent,
        )
        await coord.run_and_store()

        git_codes = await store.list_documents(source_type="git_code")
        assert len(git_codes) == 1

        # git_code → 참조하는 문서 조회
        referencing = await store.get_documents_by_source(git_codes[0]["id"])
        # code_summary 1개 + code_doc 5개 = 6개
        assert len(referencing) == 6


class TestCollectGitCodeIds:
    def test_basic_matching(self) -> None:
        from context_loop.ingestion.coordinator import _collect_git_code_ids

        git_code_map = {
            "src/payment/processor.py": 10,
            "src/payment/validator.py": 11,
            "src/auth/login.py": 12,
        }
        ids = _collect_git_code_ids(["src/payment"], git_code_map)
        assert set(ids) == {10, 11}

    def test_root_directory(self) -> None:
        from context_loop.ingestion.coordinator import _collect_git_code_ids

        git_code_map = {"a.py": 1, "b.py": 2}
        ids = _collect_git_code_ids(["."], git_code_map)
        assert set(ids) == {1, 2}

    def test_no_match(self) -> None:
        from context_loop.ingestion.coordinator import _collect_git_code_ids

        git_code_map = {"src/a.py": 1}
        ids = _collect_git_code_ids(["lib"], git_code_map)
        assert ids == []

    def test_no_duplicate_ids(self) -> None:
        from context_loop.ingestion.coordinator import _collect_git_code_ids

        git_code_map = {"src/a.py": 1}
        ids = _collect_git_code_ids(["src", "src"], git_code_map)
        assert ids == [1]  # 중복 없음
