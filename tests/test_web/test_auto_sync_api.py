"""자동 주기 싱크 UI 토글 API 테스트 (confluence-mcp / git-sync 공통)."""

from __future__ import annotations

from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from context_loop.web.app import create_app


class _StubConfig:
    """실제 Config 의 점(.) 경로 get/set 시맨틱을 미러링하는 스텁 (파일 I/O 없음).

    git 경로는 endpoint 가 점 경로로 set 한 값을 builder 가 중첩 dict
    (``config.get("sources.git")``)로 다시 읽으므로, 평면 dict 스텁으로는
    두 접근이 이어지지 않는다 — 실제와 동일한 중첩 구조가 필요하다.
    """

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self._data = dict(data or {})
        self.saved = 0

    def get(self, key_path: str, default: Any = None) -> Any:
        current: Any = self._data
        for k in key_path.split("."):
            if isinstance(current, dict) and k in current:
                current = current[k]
            else:
                return default
        return current

    def set(self, key_path: str, value: Any) -> None:
        keys = key_path.split(".")
        current = self._data
        for k in keys[:-1]:
            if k not in current or not isinstance(current[k], dict):
                current[k] = {}
            current = current[k]
        current[keys[-1]] = value

    def save(self) -> None:
        self.saved += 1


@pytest.fixture
async def app_and_client(stores):
    """config 주입 가능한 앱 + 클라이언트. 테스트가 켠 엔진은 종료 시 정리."""
    meta_store, vector_store, graph_store = stores

    app = create_app()
    app.state.meta_store = meta_store
    app.state.vector_store = vector_store
    app.state.graph_store = graph_store
    app.state.llm_client = None
    app.state.embedding_client = None
    app.state.config = _StubConfig()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield app, ac

    for attr in ("mcp_sync_engine", "git_sync_engine"):
        engine = getattr(app.state, attr, None)
        if engine is not None:
            await engine.stop()


def _enable_mcp_config(app) -> None:
    app.state.config.set("sources.confluence_mcp.enabled", True)
    app.state.config.set("sources.confluence_mcp.server_url", "http://mcp:3001/mcp")


def _enable_git_config(app) -> None:
    app.state.config.set("sources.git", {
        "enabled": True,
        "repositories": [{"url": "git@example.com:org/repo.git"}],
    })


# --- Confluence MCP ---


async def test_mcp_get_auto_sync_defaults(app_and_client):
    app, client = app_and_client
    r = await client.get("/api/confluence-mcp/auto-sync")
    assert r.status_code == 200
    auto = r.json()["auto_sync"]
    assert auto["enabled"] is False
    assert auto["running"] is False
    assert auto["interval_minutes"] == 30  # config 폴백 (UI 프리필용)
    assert auto["last_cycle_at"] is None


async def test_mcp_enable_requires_server_url(app_and_client):
    app, client = app_and_client
    r = await client.post(
        "/api/confluence-mcp/auto-sync", json={"enabled": True},
    )
    assert r.status_code == 400
    assert getattr(app.state, "mcp_sync_engine", None) is None


async def test_mcp_enable_disable_roundtrip(app_and_client):
    app, client = app_and_client
    _enable_mcp_config(app)

    r = await client.post(
        "/api/confluence-mcp/auto-sync",
        json={"enabled": True, "interval_minutes": 10},
    )
    assert r.status_code == 200
    auto = r.json()["auto_sync"]
    assert auto["enabled"] is True
    assert auto["running"] is True
    assert auto["interval_minutes"] == 10
    assert app.state.mcp_sync_engine is not None
    assert app.state.config.saved == 1
    assert app.state.config.get("sources.confluence_mcp.auto_sync_enabled") is True
    assert app.state.config.get("sources.confluence_mcp.sync_interval_minutes") == 10

    r = await client.post(
        "/api/confluence-mcp/auto-sync", json={"enabled": False},
    )
    assert r.status_code == 200
    auto = r.json()["auto_sync"]
    assert auto["enabled"] is False
    assert auto["running"] is False
    assert app.state.mcp_sync_engine is None
    assert app.state.config.get("sources.confluence_mcp.auto_sync_enabled") is False


async def test_mcp_interval_change_rebuilds_engine(app_and_client):
    app, client = app_and_client
    _enable_mcp_config(app)

    await client.post(
        "/api/confluence-mcp/auto-sync",
        json={"enabled": True, "interval_minutes": 10},
    )
    first = app.state.mcp_sync_engine

    r = await client.post(
        "/api/confluence-mcp/auto-sync",
        json={"enabled": True, "interval_minutes": 20},
    )
    assert r.json()["auto_sync"]["interval_minutes"] == 20
    second = app.state.mcp_sync_engine
    assert second is not first
    assert second.interval_minutes == 20


@pytest.mark.parametrize("bad", ["abc", 0, -5])
async def test_mcp_invalid_interval_rejected(app_and_client, bad):
    app, client = app_and_client
    _enable_mcp_config(app)
    r = await client.post(
        "/api/confluence-mcp/auto-sync",
        json={"enabled": True, "interval_minutes": bad},
    )
    assert r.status_code == 400
    assert getattr(app.state, "mcp_sync_engine", None) is None


# --- git_code ---


async def test_git_enable_requires_source_enabled(app_and_client):
    app, client = app_and_client
    r = await client.post("/api/git-sync/auto-sync", json={"enabled": True})
    assert r.status_code == 400


async def test_git_enable_requires_repositories(app_and_client):
    app, client = app_and_client
    app.state.config.set("sources.git", {"enabled": True, "repositories": []})
    r = await client.post("/api/git-sync/auto-sync", json={"enabled": True})
    assert r.status_code == 400


async def test_git_enable_disable_roundtrip(app_and_client):
    app, client = app_and_client
    _enable_git_config(app)

    r = await client.post(
        "/api/git-sync/auto-sync",
        json={"enabled": True, "interval_minutes": 45},
    )
    assert r.status_code == 200
    auto = r.json()["auto_sync"]
    assert auto["enabled"] is True
    assert auto["running"] is True
    assert auto["interval_minutes"] == 45
    assert app.state.git_sync_engine is not None

    # status 엔드포인트에도 동일 상태가 노출된다.
    r = await client.get("/api/git-sync/status")
    assert r.json()["auto_sync"]["running"] is True

    r = await client.post("/api/git-sync/auto-sync", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["auto_sync"]["running"] is False
    assert app.state.git_sync_engine is None
    # 소스 auto_sync 설정이 config 에 영속화됐는지 (엔진 재구성의 근거).
    assert app.state.config.get("sources.git.enabled") is True
    assert app.state.config.get("sources.git.auto_sync_enabled") is False
