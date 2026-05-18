"""Git sync purge API 엔드포인트 테스트.

DELETE /api/git-sync/repositories — 레포 전체 + 로컬 clone 정리
DELETE /api/git-sync/repositories/products — 단일 product 만 정리
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from context_loop.config import Config
from context_loop.ingestion.git_repository import (
    _repo_clone_dir,
    compute_content_hash,
)
from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore
from context_loop.web.api import git_sync as git_sync_module
from context_loop.web.app import create_app

REPO_URL = "git@github.com:co/repo.git"


@pytest.fixture(autouse=True)
def reset_sync_state() -> None:
    """모듈 레벨 _sync_status 격리."""
    git_sync_module._sync_status = git_sync_module.SyncStatus()


@pytest.fixture
async def stores(tmp_path: Path):  # type: ignore[misc]
    meta = MetadataStore(tmp_path / "test.db")
    await meta.initialize()
    vec = VectorStore(tmp_path)
    vec.initialize()
    graph = GraphStore(meta)
    yield meta, vec, graph
    await meta.close()


@pytest.fixture
def app_config(tmp_path: Path) -> Config:
    """git source 가 등록된 실제 Config (yaml 로드 없이 set으로 구성)."""
    config = Config(config_path=tmp_path / "config.yaml")
    config.set("app.data_dir", str(tmp_path / "data"))
    config.set("sources.git.enabled", True)
    config.set("sources.git.repositories", [
        {
            "url": REPO_URL,
            "branch": "main",
            "products": {
                "vpc": {"display_name": "VPC", "paths": ["services/vpc/**"]},
                "billing": {
                    "display_name": "Billing", "paths": ["services/billing/**"],
                },
            },
        },
    ])
    return config


@pytest.fixture
async def client(stores, app_config):  # type: ignore[misc]
    meta_store, vector_store, graph_store = stores
    app = create_app()
    app.state.config = app_config
    app.state.meta_store = meta_store
    app.state.vector_store = vector_store
    app.state.graph_store = graph_store
    app.state.embedding_client = None
    app.state.llm_client = None
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


async def _seed(
    store: MetadataStore,
    *,
    source_id: str,
    repo_url: str = REPO_URL,
    product: str,
) -> int:
    return await store.create_document(
        source_type="git_code",
        source_id=source_id,
        title=Path(source_id).name,
        original_content="x",
        content_hash=compute_content_hash(source_id),
        url=repo_url,
        author=product,
    )


# --- DELETE /api/git-sync/repositories ---


async def test_purge_repository_deletes_all_and_clone_dir(
    client, stores, app_config,
) -> None:
    meta, _, _ = stores
    a = await _seed(meta, source_id="services/vpc/a.go", product="vpc")
    b = await _seed(meta, source_id="services/billing/b.go", product="billing")

    clone = _repo_clone_dir(app_config.data_dir, REPO_URL)
    clone.mkdir(parents=True)
    (clone / "marker").write_text("x")

    resp = await client.delete(
        "/api/git-sync/repositories", params={"url": REPO_URL},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_git_code"] == 2
    assert body["deleted_clone_dir"] is True

    assert await meta.get_document(a) is None
    assert await meta.get_document(b) is None
    assert not clone.exists()


async def test_purge_repository_unknown_url_returns_404(client) -> None:
    resp = await client.delete(
        "/api/git-sync/repositories",
        params={"url": "git@github.com:co/unknown.git"},
    )
    assert resp.status_code == 404


async def test_purge_repository_conflict_when_sync_running(client) -> None:
    git_sync_module._sync_status.state = "running"
    resp = await client.delete(
        "/api/git-sync/repositories", params={"url": REPO_URL},
    )
    assert resp.status_code == 409


# --- DELETE /api/git-sync/repositories/products ---


async def test_purge_product_keeps_other_product_and_clone(
    client, stores, app_config,
) -> None:
    meta, _, _ = stores
    vpc = await _seed(meta, source_id="services/vpc/a.go", product="vpc")
    billing = await _seed(
        meta, source_id="services/billing/b.go", product="billing",
    )

    clone = _repo_clone_dir(app_config.data_dir, REPO_URL)
    clone.mkdir(parents=True)
    (clone / "marker").write_text("x")

    resp = await client.delete(
        "/api/git-sync/repositories/products",
        params={"url": REPO_URL, "product": "vpc"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_git_code"] == 1
    assert body["deleted_clone_dir"] is False

    assert await meta.get_document(vpc) is None
    assert await meta.get_document(billing) is not None
    assert clone.exists()


async def test_purge_product_unknown_returns_404(client) -> None:
    resp = await client.delete(
        "/api/git-sync/repositories/products",
        params={"url": REPO_URL, "product": "ghost"},
    )
    assert resp.status_code == 404


async def test_purge_product_unknown_repo_returns_404(client) -> None:
    resp = await client.delete(
        "/api/git-sync/repositories/products",
        params={"url": "git@github.com:co/unknown.git", "product": "vpc"},
    )
    assert resp.status_code == 404


async def test_purge_product_conflict_when_sync_running(client) -> None:
    git_sync_module._sync_status.state = "running"
    resp = await client.delete(
        "/api/git-sync/repositories/products",
        params={"url": REPO_URL, "product": "vpc"},
    )
    assert resp.status_code == 409
