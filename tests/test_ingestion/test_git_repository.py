"""Git 레포지토리 수집 모듈 테스트."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from context_loop.config import Config
from context_loop.ingestion.git_repository import (
    FileInfo,
    ProductScope,
    _repo_clone_dir,
    clone_or_pull,
    collect_files,
    compute_content_hash,
    delete_removed_files,
    filter_file,
    get_changed_files,
    get_changed_products,
    group_files_by_directory,
    match_product,
    parse_product_scopes,
    purge_synced_results,
    store_git_code,
    sync_repository,
)
from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore

# --- Fixtures ---


@pytest.fixture
async def store(tmp_path: Path) -> MetadataStore:
    s = MetadataStore(tmp_path / "test.db")
    await s.initialize()
    yield s  # type: ignore[misc]
    await s.close()


def _git(args: list[str], cwd: Path) -> None:
    """테스트용 git 명령 실행 (서명 비활성화)."""
    env = {**os.environ, "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@test.com"}
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True, env=env)


def _init_git_repo(repo_dir: Path, files: dict[str, str] | None = None) -> None:
    """테스트용 bare가 아닌 git 레포를 초기화한다."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], cwd=repo_dir)
    _git(["config", "user.email", "test@test.com"], cwd=repo_dir)
    _git(["config", "user.name", "Test"], cwd=repo_dir)
    _git(["config", "commit.gpgsign", "false"], cwd=repo_dir)
    if files:
        for path, content in files.items():
            file_path = repo_dir / path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")
        _git(["add", "."], cwd=repo_dir)
        _git(["commit", "-m", "init"], cwd=repo_dir)


@pytest.fixture
def sample_scopes() -> list[ProductScope]:
    return [
        ProductScope(
            name="vpc",
            display_name="VPC Service",
            paths=["services/vpc/**"],
            exclude=["**/*_test.go", "**/vendor/**"],
        ),
        ProductScope(
            name="billing",
            display_name="Billing Service",
            paths=["services/billing/**"],
            exclude=[],
        ),
    ]


# --- Unit Tests: Pure Functions ---


class TestComputeContentHash:
    def test_deterministic(self) -> None:
        assert compute_content_hash("hello") == compute_content_hash("hello")

    def test_different_content(self) -> None:
        assert compute_content_hash("a") != compute_content_hash("b")


class TestFilterFile:
    def test_supported_extension(self) -> None:
        assert filter_file("main.py", 1000, [".py", ".go"], 500) is True

    def test_unsupported_extension(self) -> None:
        assert filter_file("image.png", 1000, [".py", ".go"], 500) is False

    def test_file_too_large(self) -> None:
        # 500KB 제한, 파일 600KB
        assert filter_file("main.py", 600 * 1024, [".py"], 500) is False

    def test_file_within_limit(self) -> None:
        assert filter_file("main.py", 400 * 1024, [".py"], 500) is True


class TestMatchProduct:
    def test_match_vpc(self, sample_scopes: list[ProductScope]) -> None:
        assert match_product("services/vpc/handler/create.go", sample_scopes) == "vpc"

    def test_match_billing(self, sample_scopes: list[ProductScope]) -> None:
        assert match_product("services/billing/api.py", sample_scopes) == "billing"

    def test_no_match(self, sample_scopes: list[ProductScope]) -> None:
        assert match_product("lib/utils.py", sample_scopes) is None

    def test_excluded_pattern(self, sample_scopes: list[ProductScope]) -> None:
        assert match_product("services/vpc/handler/create_test.go", sample_scopes) is None

    def test_excluded_vendor(self, sample_scopes: list[ProductScope]) -> None:
        assert match_product("services/vpc/vendor/lib.go", sample_scopes) is None


class TestParseProductScopes:
    def test_parse(self) -> None:
        repo_config: dict[str, Any] = {
            "url": "git@github.com:co/repo.git",
            "products": {
                "vpc": {
                    "display_name": "VPC",
                    "paths": ["services/vpc/**"],
                    "exclude": ["**/*_test.go"],
                }
            },
        }
        scopes = parse_product_scopes(repo_config)
        assert len(scopes) == 1
        assert scopes[0].name == "vpc"
        assert scopes[0].display_name == "VPC"
        assert scopes[0].paths == ["services/vpc/**"]

    def test_empty_products(self) -> None:
        assert parse_product_scopes({"url": "x"}) == []

    def test_auto_resolve_paths_when_empty(self, tmp_path: Path) -> None:
        """paths 미정의 시 clone_dir에서 자동 탐지."""
        (tmp_path / "controller").mkdir()
        (tmp_path / "controller" / "vpc_controller.go").write_text("package vpc")
        (tmp_path / "service").mkdir()
        (tmp_path / "service" / "vpc_service.go").write_text("package vpc")

        repo_config = {
            "products": {
                "vpc": {"display_name": "VPC"},  # paths 없음
            }
        }
        scopes = parse_product_scopes(repo_config, clone_dir=tmp_path)
        assert len(scopes) == 1
        assert scopes[0].name == "vpc"
        assert "controller/vpc_controller.go" in scopes[0].paths
        assert "service/vpc_service.go" in scopes[0].paths

    def test_manual_paths_not_overridden(self, tmp_path: Path) -> None:
        """paths가 이미 정의되어 있으면 자동 탐지하지 않음."""
        (tmp_path / "vpc_extra.go").write_text("package vpc")

        repo_config = {
            "products": {
                "vpc": {
                    "display_name": "VPC",
                    "paths": ["services/vpc/**"],  # 수동 정의
                }
            }
        }
        scopes = parse_product_scopes(repo_config, clone_dir=tmp_path)
        assert scopes[0].paths == ["services/vpc/**"]

    def test_auto_resolve_without_clone_dir(self) -> None:
        """clone_dir 미제공 시 자동 탐지 미실행."""
        repo_config = {
            "products": {
                "vpc": {"display_name": "VPC"},  # paths 없음
            }
        }
        scopes = parse_product_scopes(repo_config)  # clone_dir 없음
        assert scopes[0].paths == []

    def test_auto_resolve_with_exclude(self, tmp_path: Path) -> None:
        """exclude 패턴이 자동 탐지에 적용되는지 확인."""
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "vpc_service.go").write_text("package vpc")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "vpc_test.go").write_text("package vpc")

        repo_config = {
            "products": {
                "vpc": {
                    "display_name": "VPC",
                    "exclude": ["tests/**"],
                },
            }
        }
        scopes = parse_product_scopes(repo_config, clone_dir=tmp_path)
        assert "src/vpc_service.go" in scopes[0].paths
        assert "tests/vpc_test.go" not in scopes[0].paths


class TestGetChangedProducts:
    def test_returns_unique_products(self) -> None:
        files = [
            FileInfo("a.py", Path("a.py"), "vpc", "", "", 0),
            FileInfo("b.py", Path("b.py"), "vpc", "", "", 0),
            FileInfo("c.py", Path("c.py"), "billing", "", "", 0),
        ]
        assert get_changed_products(files) == {"vpc", "billing"}


class TestGroupFilesByDirectory:
    def _make_file(self, path: str) -> FileInfo:
        return FileInfo(path, Path(path), "test", "", "", 0)

    def test_basic_grouping(self) -> None:
        files = [self._make_file(f"dir/{i}.py") for i in range(5)]
        groups = group_files_by_directory(files)
        assert "dir" in groups
        assert len(groups["dir"]) == 5

    def test_small_group_merged_to_parent(self) -> None:
        files = [self._make_file("a/b/1.py"), self._make_file("a/b/2.py")]
        groups = group_files_by_directory(files, min_files_per_group=3)
        # 2 files < min 3 → merged to parent "a"
        assert "a" in groups
        assert len(groups["a"]) == 2


# --- Integration Tests: Git Operations ---


class TestCloneOrPull:
    async def test_clone_new_repo(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin"
        _init_git_repo(origin, {"README.md": "# Hello"})

        clone_dir = tmp_path / "clone"
        is_new, prev_commit = await clone_or_pull(str(origin), clone_dir, "main")
        assert is_new is True
        assert prev_commit is None
        assert (clone_dir / "README.md").exists()

    async def test_pull_existing_repo(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin"
        _init_git_repo(origin, {"README.md": "# v1"})

        clone_dir = tmp_path / "clone"
        await clone_or_pull(str(origin), clone_dir, "main")

        # origin에 새 커밋 추가
        (origin / "new.txt").write_text("new file")
        _git(["add", "."], cwd=origin)
        _git(["commit", "-m", "add new"], cwd=origin)

        is_new, prev_commit = await clone_or_pull(str(origin), clone_dir, "main")
        assert is_new is False
        assert prev_commit is not None
        assert (clone_dir / "new.txt").exists()


class TestGetChangedFiles:
    async def test_no_changes(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_git_repo(repo, {"a.py": "pass"})

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        )
        head = result.stdout.strip()

        changed = await get_changed_files(repo, head)
        assert changed == []

    async def test_initial_clone_returns_none(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_git_repo(repo, {"a.py": "pass"})
        changed = await get_changed_files(repo, None)
        assert changed is None

    async def test_detects_changed_files(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _init_git_repo(repo, {"a.py": "v1"})

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        )
        prev_hash = result.stdout.strip()

        # 새 파일 추가 + 커밋
        (repo / "b.py").write_text("v1")
        _git(["add", "."], cwd=repo)
        _git(["commit", "-m", "add b"], cwd=repo)

        changed = await get_changed_files(repo, prev_hash)
        assert changed is not None
        assert "b.py" in changed


class TestCollectFiles:
    def test_collect_with_scope(self, tmp_path: Path, sample_scopes: list[ProductScope]) -> None:
        repo = tmp_path / "repo"
        (repo / "services" / "vpc" / "handler").mkdir(parents=True)
        (repo / "services" / "vpc" / "handler" / "create.go").write_text("package handler")
        (repo / "services" / "billing").mkdir(parents=True)
        (repo / "services" / "billing" / "api.py").write_text("def api(): pass")
        (repo / "lib").mkdir()
        (repo / "lib" / "utils.py").write_text("# not in scope")

        files = collect_files(repo, sample_scopes, [".go", ".py"], 500)
        paths = {f.relative_path for f in files}
        assert "services/vpc/handler/create.go" in paths
        assert "services/billing/api.py" in paths
        assert "lib/utils.py" not in paths

    def test_collect_incremental(self, tmp_path: Path, sample_scopes: list[ProductScope]) -> None:
        repo = tmp_path / "repo"
        (repo / "services" / "vpc").mkdir(parents=True)
        (repo / "services" / "vpc" / "a.go").write_text("v1")
        (repo / "services" / "vpc" / "b.go").write_text("v1")

        # 증분: a.go만 변경됨
        files = collect_files(
            repo, sample_scopes, [".go"], 500,
            changed_files=["services/vpc/a.go"],
        )
        assert len(files) == 1
        assert files[0].relative_path == "services/vpc/a.go"

    def test_excludes_test_files(self, tmp_path: Path, sample_scopes: list[ProductScope]) -> None:
        repo = tmp_path / "repo"
        (repo / "services" / "vpc").mkdir(parents=True)
        (repo / "services" / "vpc" / "handler_test.go").write_text("test")
        (repo / "services" / "vpc" / "handler.go").write_text("impl")

        files = collect_files(repo, sample_scopes, [".go"], 500)
        paths = {f.relative_path for f in files}
        assert "services/vpc/handler_test.go" not in paths
        assert "services/vpc/handler.go" in paths


# --- Integration Tests: Store Operations ---


class TestStoreGitCode:
    async def test_create_new(self, store: MetadataStore) -> None:
        fi = FileInfo(
            relative_path="services/vpc/main.go",
            absolute_path=Path("/tmp/main.go"),
            product="vpc",
            content="package main",
            content_hash=compute_content_hash("package main"),
            size_bytes=13,
        )
        result = await store_git_code(store, fi, "git@github.com:co/repo.git")
        assert result["created"] is True
        assert result["changed"] is True
        assert result["source_type"] == "git_code"
        assert result["source_id"] == "services/vpc/main.go"

    async def test_unchanged(self, store: MetadataStore) -> None:
        fi = FileInfo("a.go", Path("a.go"), "vpc", "v1", compute_content_hash("v1"), 2)
        await store_git_code(store, fi, "url")
        result = await store_git_code(store, fi, "url")
        assert result["created"] is False
        assert result["changed"] is False

    async def test_updated(self, store: MetadataStore) -> None:
        fi1 = FileInfo("a.go", Path("a.go"), "vpc", "v1", compute_content_hash("v1"), 2)
        await store_git_code(store, fi1, "url")

        fi2 = FileInfo("a.go", Path("a.go"), "vpc", "v2", compute_content_hash("v2"), 2)
        result = await store_git_code(store, fi2, "url")
        assert result["created"] is False
        assert result["changed"] is True
        assert result["original_content"] == "v2"


class TestDeleteRemovedFiles:
    async def test_delete(self, store: MetadataStore) -> None:
        doc_id = await store.create_document(
            source_type="git_code",
            source_id="old.py",
            title="old.py",
            original_content="x",
            content_hash="h",
        )
        removed = await delete_removed_files(store, ["old.py"])
        assert removed == ["old.py"]
        assert await store.get_document(doc_id) is None

    async def test_skip_nonexistent(self, store: MetadataStore) -> None:
        removed = await delete_removed_files(store, ["nonexistent.py"])
        assert removed == []


# --- Integration Test: Full sync_repository ---


class TestSyncRepository:
    async def test_full_sync(self, store: MetadataStore, tmp_path: Path) -> None:
        # 로컬 origin 레포 생성
        origin = tmp_path / "origin"
        _init_git_repo(origin, {
            "services/vpc/main.go": "package main",
            "services/vpc/handler.go": "package handler",
            "lib/util.py": "# not in scope",
        })

        # Config 생성
        config = Config(config_path=tmp_path / "config.yaml")
        config.set("app.data_dir", str(tmp_path / "data"))
        config.set("sources.git.enabled", True)
        config.set("sources.git.supported_extensions", [".go", ".py"])
        config.set("sources.git.file_size_limit_kb", 500)

        repo_config: dict[str, Any] = {
            "url": str(origin),
            "branch": "main",
            "products": {
                "vpc": {
                    "display_name": "VPC",
                    "paths": ["services/vpc/**"],
                    "exclude": [],
                }
            },
        }

        result = await sync_repository(store, config, repo_config)
        assert len(result.created) == 2
        assert len(result.errors) == 0

        # git_code 문서가 저장되었는지 확인
        docs = await store.list_documents(source_type="git_code")
        assert len(docs) == 2
        source_ids = {d["source_id"] for d in docs}
        assert "services/vpc/main.go" in source_ids
        assert "services/vpc/handler.go" in source_ids

    async def test_incremental_sync(self, store: MetadataStore, tmp_path: Path) -> None:
        origin = tmp_path / "origin"
        _init_git_repo(origin, {
            "services/vpc/main.go": "v1",
        })

        config = Config(config_path=tmp_path / "config.yaml")
        config.set("app.data_dir", str(tmp_path / "data"))
        config.set("sources.git.enabled", True)
        config.set("sources.git.supported_extensions", [".go"])
        config.set("sources.git.file_size_limit_kb", 500)

        repo_config: dict[str, Any] = {
            "url": str(origin),
            "branch": "main",
            "products": {
                "vpc": {
                    "display_name": "VPC",
                    "paths": ["services/vpc/**"],
                }
            },
        }

        # 초기 동기화
        r1 = await sync_repository(store, config, repo_config)
        assert len(r1.created) == 1

        # origin에 파일 수정 + 새 파일 추가
        (origin / "services" / "vpc" / "main.go").write_text("v2")
        (origin / "services" / "vpc" / "new.go").write_text("new")
        _git(["add", "."], cwd=origin)
        _git(["commit", "-m", "update"], cwd=origin)

        # 증분 동기화
        r2 = await sync_repository(store, config, repo_config)
        assert len(r2.created) == 1  # new.go
        assert len(r2.updated) == 1  # main.go


# --- Purge: 싱크 결과 일괄 삭제 ---


@pytest.fixture
async def stores(tmp_path: Path):
    """purge 테스트용 격리된 메타·벡터·그래프 스토어."""
    meta = MetadataStore(tmp_path / "test.db")
    await meta.initialize()
    vec = VectorStore(tmp_path)
    vec.initialize()
    graph = GraphStore(meta)
    yield meta, vec, graph
    await meta.close()


async def _seed_git_doc(
    store: MetadataStore,
    *,
    source_id: str,
    repo_url: str,
    product: str,
    content: str = "x",
) -> int:
    return await store.create_document(
        source_type="git_code",
        source_id=source_id,
        title=Path(source_id).name,
        original_content=content,
        content_hash=compute_content_hash(content),
        url=repo_url,
        author=product,
    )


async def _seed_derived(
    store: MetadataStore,
    *,
    source_type: str,
    source_id: str,
    sources: list[int],
) -> int:
    """파생 문서를 만들고 source 연결을 등록한다."""
    doc_id = await store.create_document(
        source_type=source_type,
        source_id=source_id,
        title=source_id,
        original_content="y",
        content_hash=compute_content_hash(f"{source_id}-{sources}"),
    )
    for src_id in sources:
        await store.add_document_source(doc_id, src_id, file_path=None)
    return doc_id


class TestPurgeSyncedResults:
    async def test_purge_repository_removes_all_git_code(self, stores) -> None:
        meta, vec, graph = stores
        repo = "git@github.com:co/repo.git"
        a = await _seed_git_doc(
            meta, source_id="services/vpc/a.go", repo_url=repo, product="vpc",
        )
        b = await _seed_git_doc(
            meta, source_id="services/billing/b.go", repo_url=repo, product="billing",
        )
        # 다른 레포는 영향 받지 않아야 한다.
        other = await _seed_git_doc(
            meta, source_id="x.go", repo_url="git@github.com:co/other.git",
            product="vpc",
        )

        result = await purge_synced_results(
            meta_store=meta,
            vector_store=vec,
            graph_store=graph,
            repo_url=repo,
        )
        assert result.deleted_git_code == 2
        assert result.deleted_derived == 0
        assert result.deleted_clone_dir is False
        assert await meta.get_document(a) is None
        assert await meta.get_document(b) is None
        assert await meta.get_document(other) is not None

    async def test_purge_product_scopes_to_one_product(self, stores) -> None:
        meta, vec, graph = stores
        repo = "git@github.com:co/repo.git"
        vpc = await _seed_git_doc(
            meta, source_id="services/vpc/a.go", repo_url=repo, product="vpc",
        )
        billing = await _seed_git_doc(
            meta, source_id="services/billing/b.go", repo_url=repo, product="billing",
        )

        result = await purge_synced_results(
            meta_store=meta,
            vector_store=vec,
            graph_store=graph,
            repo_url=repo,
            product="vpc",
        )
        assert result.deleted_git_code == 1
        assert result.product == "vpc"
        assert await meta.get_document(vpc) is None
        assert await meta.get_document(billing) is not None

    async def test_purge_cascades_derived_with_only_purged_sources(
        self, stores,
    ) -> None:
        meta, vec, graph = stores
        repo = "git@github.com:co/repo.git"
        g1 = await _seed_git_doc(
            meta, source_id="services/vpc/a.go", repo_url=repo, product="vpc",
        )
        g2 = await _seed_git_doc(
            meta, source_id="services/vpc/b.go", repo_url=repo, product="vpc",
        )
        # 파생: vpc 소스 두 개에만 의존 → 삭제되어야 함.
        derived = await _seed_derived(
            meta,
            source_type="code_doc",
            source_id="vpc:architecture",
            sources=[g1, g2],
        )

        result = await purge_synced_results(
            meta_store=meta,
            vector_store=vec,
            graph_store=graph,
            repo_url=repo,
            product="vpc",
        )
        assert result.deleted_git_code == 2
        assert result.deleted_derived == 1
        assert await meta.get_document(derived) is None

    async def test_purge_keeps_derived_with_external_sources(self, stores) -> None:
        meta, vec, graph = stores
        repo_a = "git@github.com:co/a.git"
        repo_b = "git@github.com:co/b.git"
        ga = await _seed_git_doc(
            meta, source_id="a.go", repo_url=repo_a, product="vpc",
        )
        gb = await _seed_git_doc(
            meta, source_id="b.go", repo_url=repo_b, product="vpc",
        )
        # 두 레포의 코드를 함께 참조하는 파생 → repo_a 만 purge 시 살려야 함.
        cross = await _seed_derived(
            meta,
            source_type="code_doc",
            source_id="vpc:cross",
            sources=[ga, gb],
        )

        result = await purge_synced_results(
            meta_store=meta,
            vector_store=vec,
            graph_store=graph,
            repo_url=repo_a,
        )
        assert result.deleted_git_code == 1
        assert result.deleted_derived == 0
        assert await meta.get_document(cross) is not None
        # gb 는 다른 레포 소속이라 그대로 남아야 한다.
        assert await meta.get_document(gb) is not None

    async def test_purge_repo_removes_local_clone_dir(self, stores, tmp_path: Path) -> None:
        meta, vec, graph = stores
        repo = "git@github.com:co/repo.git"
        await _seed_git_doc(
            meta, source_id="a.go", repo_url=repo, product="vpc",
        )

        data_dir = tmp_path / "data"
        clone_dir = _repo_clone_dir(data_dir, repo)
        clone_dir.mkdir(parents=True)
        (clone_dir / "marker.txt").write_text("present")

        result = await purge_synced_results(
            meta_store=meta,
            vector_store=vec,
            graph_store=graph,
            repo_url=repo,
            data_dir=data_dir,
        )
        assert result.deleted_clone_dir is True
        assert not clone_dir.exists()

    async def test_purge_product_does_not_remove_clone_dir(
        self, stores, tmp_path: Path,
    ) -> None:
        meta, vec, graph = stores
        repo = "git@github.com:co/repo.git"
        await _seed_git_doc(
            meta, source_id="a.go", repo_url=repo, product="vpc",
        )

        data_dir = tmp_path / "data"
        clone_dir = _repo_clone_dir(data_dir, repo)
        clone_dir.mkdir(parents=True)
        (clone_dir / "marker.txt").write_text("present")

        result = await purge_synced_results(
            meta_store=meta,
            vector_store=vec,
            graph_store=graph,
            repo_url=repo,
            product="vpc",
            data_dir=data_dir,
        )
        # product purge 는 clone 을 절대 건드리지 않는다.
        assert result.deleted_clone_dir is False
        assert clone_dir.exists()

    async def test_purge_no_match_returns_zero(self, stores) -> None:
        meta, vec, graph = stores
        result = await purge_synced_results(
            meta_store=meta,
            vector_store=vec,
            graph_store=graph,
            repo_url="git@github.com:co/missing.git",
            product="ghost",
        )
        assert result.deleted_git_code == 0
        assert result.deleted_derived == 0
        assert result.deleted_clone_dir is False
