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
    # lifespan 대신 직접 스토어를 주입.
    # llm_client / embedding_client 는 라우트가 Depends 로 요구하므로 None 으로
    # 등록만 해 둔다 (실제 호출은 mock 으로 격리되거나 None-가드로 스킵됨).
    app.state.meta_store = meta_store
    app.state.vector_store = vector_store
    app.state.graph_store = graph_store
    app.state.llm_client = None
    app.state.embedding_client = None

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac
