"""웹 모듈 테스트 공용 픽스처."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore
from context_loop.web.app import create_app


@pytest.fixture
async def stores(tmp_path: Path):
    """격리된 테스트 스토어를 생성한다."""
    meta_store = MetadataStore(tmp_path / "test.db")
    await meta_store.initialize()

    vector_store = VectorStore(tmp_path)
    vector_store.initialize()

    graph_store = GraphStore(meta_store)

    yield meta_store, vector_store, graph_store

    await meta_store.close()


@pytest.fixture
async def client(stores):
    """테스트용 AsyncClient를 생성한다."""
    meta_store, vector_store, graph_store = stores

    app = create_app()
    # lifespan 대신 직접 스토어를 주입
    app.state.meta_store = meta_store
    app.state.vector_store = vector_store
    app.state.graph_store = graph_store

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
