"""Confluence MCP 3-scope 싱크 웹 API 엔드포인트 테스트."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore
from context_loop.sync.mcp_sync import SyncResult
from context_loop.web.app import create_app
from context_loop.web.api import confluence_mcp as cmcp_module


# --- Fixtures ---


class _StubConfig:
    """싱크 엔드포인트가 참조하는 설정 키만 덮어쓰는 최소 stub."""

    def __init__(self, mapping: dict[str, Any] | None = None) -> None:
        self._m = mapping or {
            "sources.confluence_mcp.server_url": "http://mock-mcp/",
            "sources.confluence_mcp.transport": "http",
        }

    def get(self, key: str, default: Any = None) -> Any:
        return self._m.get(key, default)


class _FakeMCPSession:
    """내부 MCP 함수를 monkeypatch 로 덮어쓸 예정이라 실제 사용 안 됨."""


def _fake_connect_mcp(*_args: Any, **_kwargs: Any):
    class Ctx:
        async def __aenter__(self) -> _FakeMCPSession:
            return _FakeMCPSession()

        async def __aexit__(self, *_: Any) -> None:
            return None

    return Ctx()


@pytest.fixture(autouse=True)
def reset_target_state(monkeypatch) -> None:
    """테스트 간 모듈 레벨 상태 격리 + keyring 우회."""
    cmcp_module._target_locks.clear()
    cmcp_module._target_status.clear()
    # CI/테스트 환경은 keyring backend 가 없으므로 토큰 조회를 우회한다.
    monkeypatch.setattr(cmcp_module, "_get_token", lambda: None)


@pytest.fixture
async def stores(tmp_path: Path):  # type: ignore[misc]
    meta = MetadataStore(tmp_path / "test.db")
    await meta.initialize()
    vec = VectorStore(tmp_path)
    vec.initialize()
    graph = GraphStore(meta)
    yield meta, vec, graph
    await meta.close()


class _DummyEmbeddings:
    """get_embedding_client 의존성만 채우기 위한 placeholder. Phase 2 는
    _run_sync_in_background 안에서만 돌고, 테스트는 BackgroundTasks 의
    실행을 기다리지 않으므로 이 객체의 메서드는 호출되지 않는다."""


@pytest.fixture
async def client(stores):  # type: ignore[misc]
    """설정까지 주입된 테스트용 AsyncClient."""
    meta_store, vector_store, graph_store = stores
    app = create_app()
    app.state.config = _StubConfig()
    app.state.meta_store = meta_store
    app.state.vector_store = vector_store
    app.state.graph_store = graph_store
    app.state.embedding_client = _DummyEmbeddings()
    app.state.llm_client = None

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


# --- 스토어만으로 검증 가능한 엔드포인트 ---


async def test_list_sync_targets_empty(client) -> None:
    resp = await client.get("/api/confluence-mcp/sync-targets")
    assert resp.status_code == 200
    assert resp.json() == {"targets": []}


async def test_list_sync_targets_returns_entries_with_status(client, stores) -> None:
    meta, _, _ = stores
    await meta.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )

    resp = await client.get("/api/confluence-mcp/sync-targets")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["targets"]) == 1
    t = body["targets"][0]
    assert t["scope"] == "subtree"
    assert t["name"] == "Root"
    assert t["status"] == {"state": "idle"}


async def test_get_sync_target_detail_found(client, stores) -> None:
    meta, _, _ = stores
    t = await meta.upsert_sync_target(
        scope="page", space_key="ENG", page_id="1", name="P",
    )
    resp = await client.get(f"/api/confluence-mcp/sync-targets/{t['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == t["id"]
    assert resp.json()["status"] == {"state": "idle"}


async def test_get_sync_target_detail_not_found(client) -> None:
    resp = await client.get("/api/confluence-mcp/sync-targets/9999")
    assert resp.status_code == 404


async def test_delete_sync_target_success(client, stores) -> None:
    meta, _, _ = stores
    t = await meta.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    resp = await client.delete(f"/api/confluence-mcp/sync-targets/{t['id']}")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True, "deleted_documents": 0}
    assert await meta.get_sync_target(t["id"]) is None


async def test_delete_sync_target_not_found(client) -> None:
    resp = await client.delete("/api/confluence-mcp/sync-targets/9999")
    assert resp.status_code == 404


async def test_delete_sync_target_cascades_orphan_documents(client, stores) -> None:
    meta, _, _ = stores
    t = await meta.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    doc_id = await meta.create_document(
        source_type="confluence_mcp", source_id="100",
        title="T", original_content="x", content_hash="h1",
    )
    await meta.upsert_membership(
        target_id=t["id"], page_id="100", space_key="ENG",
    )

    resp = await client.delete(f"/api/confluence-mcp/sync-targets/{t['id']}")
    assert resp.status_code == 200
    assert resp.json()["deleted_documents"] == 1
    assert await meta.get_document(doc_id) is None


async def test_trigger_sync_target_404(client) -> None:
    resp = await client.post("/api/confluence-mcp/sync-targets/9999/sync")
    assert resp.status_code == 404


async def test_trigger_sync_target_conflict_when_lock_held(client, stores) -> None:
    meta, _, _ = stores
    t = await meta.upsert_sync_target(
        scope="page", space_key="ENG", page_id="1", name="P",
    )
    # 이미 실행 중인 것처럼 락 점유
    lock = cmcp_module._get_target_lock(t["id"])
    await lock.acquire()
    try:
        resp = await client.post(
            f"/api/confluence-mcp/sync-targets/{t['id']}/sync",
        )
        assert resp.status_code == 409
    finally:
        lock.release()


# --- MCP-의존 엔드포인트 (mock) ---


async def test_search_merged_with_query(client, monkeypatch) -> None:
    monkeypatch.setattr(cmcp_module, "connect_mcp", _fake_connect_mcp)

    async def fake_get_all_spaces(_session):
        return [
            {"key": "ENG", "name": "Engineering"},
            {"key": "OPS", "name": "Operations"},
            {"key": "MKT", "name": "Marketing"},
        ]

    from context_loop.ingestion.mcp_confluence import SearchEnvelope

    async def fake_search_envelope(_session, _q, limit=25, start=0):
        return SearchEnvelope(
            results=[{"id": "1", "title": "Arch Overview"}],
            total_size=42, size=1, start=start, limit=limit,
        )

    monkeypatch.setattr(cmcp_module, "get_all_spaces", fake_get_all_spaces)
    monkeypatch.setattr(cmcp_module, "search_content_envelope", fake_search_envelope)

    resp = await client.get("/api/confluence-mcp/search?q=engineering")
    assert resp.status_code == 200
    body = resp.json()
    # 공간은 'engineering' 부분일치 검색으로 ENG 만 매치
    assert [s["key"] for s in body["spaces"]] == ["ENG"]
    assert body["pages"][0]["id"] == "1"
    assert body["total_pages"] == 42


async def test_search_merged_empty_query_returns_all_spaces(client, monkeypatch) -> None:
    monkeypatch.setattr(cmcp_module, "connect_mcp", _fake_connect_mcp)

    async def fake_get_all_spaces(_session):
        return [{"key": "A", "name": "Alpha"}, {"key": "B", "name": "Beta"}]

    monkeypatch.setattr(cmcp_module, "get_all_spaces", fake_get_all_spaces)

    resp = await client.get("/api/confluence-mcp/search?q=")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["spaces"]) == 2
    assert body["pages"] == []
    assert body["total_pages"] is None


async def test_estimate_space_returns_count(client, monkeypatch) -> None:
    monkeypatch.setattr(cmcp_module, "connect_mcp", _fake_connect_mcp)

    async def fake_estimate(_session, space_key):
        assert space_key == "ENG"
        return 342

    monkeypatch.setattr(cmcp_module, "estimate_space_page_count", fake_estimate)

    resp = await client.get("/api/confluence-mcp/spaces/ENG/estimate")
    assert resp.status_code == 200
    assert resp.json() == {"space_key": "ENG", "estimated_pages": 342}


async def test_create_sync_target_subtree(client, stores, monkeypatch) -> None:
    meta, _, _ = stores
    monkeypatch.setattr(cmcp_module, "connect_mcp", _fake_connect_mcp)

    async def fake_get_page_with_ancestors(_session, page_id):
        return {
            "id": page_id,
            "title": "Overview",
            "space": {"key": "ENG", "name": "Engineering"},
            "ancestors": [{"title": "Docs"}],
        }

    monkeypatch.setattr(
        cmcp_module, "get_page_with_ancestors", fake_get_page_with_ancestors,
    )

    async def fake_execute(*_a, **_k):
        return SyncResult()

    monkeypatch.setattr(cmcp_module, "execute_sync_target", fake_execute)

    resp = await client.post(
        "/api/confluence-mcp/sync-targets",
        json={"scope": "subtree", "page_id": "100"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    target = body["target"]
    assert target["scope"] == "subtree"
    assert target["space_key"] == "ENG"
    assert target["page_id"] == "100"
    assert target["name"] == "Engineering / Docs / Overview"

    # DB에 실제로 등록되었는지
    rows = await meta.list_sync_targets()
    assert len(rows) == 1
    assert rows[0]["name"] == "Engineering / Docs / Overview"


async def test_create_sync_target_space(client, stores, monkeypatch) -> None:
    meta, _, _ = stores
    monkeypatch.setattr(cmcp_module, "connect_mcp", _fake_connect_mcp)

    async def fake_get_space_info(_session, space_key):
        return {"key": space_key, "name": "Engineering"}

    monkeypatch.setattr(cmcp_module, "get_space_info", fake_get_space_info)

    async def fake_execute(*_a, **_k):
        return SyncResult()

    monkeypatch.setattr(cmcp_module, "execute_sync_target", fake_execute)

    resp = await client.post(
        "/api/confluence-mcp/sync-targets",
        json={"scope": "space", "space_key": "ENG"},
    )
    assert resp.status_code == 200
    target = resp.json()["target"]
    assert target["scope"] == "space"
    assert target["space_key"] == "ENG"
    assert target["page_id"] is None
    assert target["name"] == "Engineering"


async def test_create_sync_target_rejects_invalid_scope(client) -> None:
    resp = await client.post(
        "/api/confluence-mcp/sync-targets",
        json={"scope": "weird"},
    )
    assert resp.status_code == 400


async def test_create_sync_target_page_requires_page_id(client) -> None:
    resp = await client.post(
        "/api/confluence-mcp/sync-targets",
        json={"scope": "page"},
    )
    assert resp.status_code == 400
    assert "page_id" in resp.json()["detail"]


async def test_create_sync_target_space_requires_space_key(client) -> None:
    resp = await client.post(
        "/api/confluence-mcp/sync-targets",
        json={"scope": "space"},
    )
    assert resp.status_code == 400
    assert "space_key" in resp.json()["detail"]


async def test_create_sync_target_mcp_connect_error(client, monkeypatch) -> None:
    from context_loop.ingestion.mcp_confluence import MCPConnectionError

    def bad_connect(*_a, **_k):
        class Ctx:
            async def __aenter__(self):
                raise MCPConnectionError("unreachable")

            async def __aexit__(self, *_):
                return None

        return Ctx()

    monkeypatch.setattr(cmcp_module, "connect_mcp", bad_connect)

    resp = await client.post(
        "/api/confluence-mcp/sync-targets",
        json={"scope": "space", "space_key": "ENG"},
    )
    assert resp.status_code == 502


async def test_estimate_mcp_connect_error(client, monkeypatch) -> None:
    from context_loop.ingestion.mcp_confluence import MCPConnectionError

    def bad_connect(*_a, **_k):
        class Ctx:
            async def __aenter__(self):
                raise MCPConnectionError("unreachable")

            async def __aexit__(self, *_):
                return None

        return Ctx()

    monkeypatch.setattr(cmcp_module, "connect_mcp", bad_connect)

    resp = await client.get("/api/confluence-mcp/spaces/ENG/estimate")
    assert resp.status_code == 502
