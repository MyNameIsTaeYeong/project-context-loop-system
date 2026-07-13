"""git_code 자동 주기 싱크 — 러너(run_sync_in_background)와 엔진 빌더 테스트."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from context_loop.ingestion.git_config import GitSourceConfig, RepositoryConfig
from context_loop.sync.periodic import PeriodicSyncEngine
from context_loop.web.api import git_sync
from context_loop.web.app import _build_git_sync_engine


class FakeConfig:
    """config.get(key, default) 만 흉내내는 설정 객체."""

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self.values = values or {}

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)


def _git_config(**overrides: Any) -> GitSourceConfig:
    defaults: dict[str, Any] = {
        "enabled": True,
        "auto_sync_enabled": True,
        "repositories": [RepositoryConfig(url="git@example.com:org/repo.git")],
    }
    defaults.update(overrides)
    return GitSourceConfig(**defaults)


@pytest.fixture(autouse=True)
def reset_sync_status():
    """모듈 전역 싱크 상태를 테스트마다 초기화한다."""
    git_sync._sync_status = git_sync.SyncStatus()
    yield
    git_sync._sync_status = git_sync.SyncStatus()


_STORES = (MagicMock(), MagicMock(), MagicMock(), MagicMock())  # meta/vec/graph/embed


# --- run_sync_in_background (러너) ---


async def test_runner_executes_sync(monkeypatch: pytest.MonkeyPatch):
    ran: list[dict[str, Any]] = []

    async def fake_run_sync(config: Any, meta: Any, git_config: Any, **kwargs: Any) -> None:
        # 러너가 진입 전에 running 마킹을 했는지 — 수동 트리거 409 가드의 전제.
        ran.append({"state": git_sync._sync_status.state})

    monkeypatch.setattr(git_sync, "_run_sync", fake_run_sync)
    monkeypatch.setattr(git_sync, "load_git_source_config", lambda c: _git_config())

    executed = await git_sync.run_sync_in_background(FakeConfig(), *_STORES)

    assert executed is True
    assert ran == [{"state": "running"}]


async def test_runner_skips_when_already_running(monkeypatch: pytest.MonkeyPatch):
    async def fail_run_sync(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("이미 실행 중이면 _run_sync 가 호출되면 안 됨")

    monkeypatch.setattr(git_sync, "_run_sync", fail_run_sync)
    monkeypatch.setattr(git_sync, "load_git_source_config", lambda c: _git_config())
    git_sync._sync_status = git_sync.SyncStatus(state="running")

    assert await git_sync.run_sync_in_background(FakeConfig(), *_STORES) is False


@pytest.mark.parametrize(
    "git_config",
    [
        _git_config(enabled=False),
        _git_config(repositories=[]),
    ],
    ids=["disabled", "no-repositories"],
)
async def test_runner_skips_when_not_configured(
    monkeypatch: pytest.MonkeyPatch, git_config: GitSourceConfig,
):
    async def fail_run_sync(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("설정이 없으면 _run_sync 가 호출되면 안 됨")

    monkeypatch.setattr(git_sync, "_run_sync", fail_run_sync)
    monkeypatch.setattr(git_sync, "load_git_source_config", lambda c: git_config)

    assert await git_sync.run_sync_in_background(FakeConfig(), *_STORES) is False
    assert git_sync._sync_status.state == "idle"


# --- _build_git_sync_engine (게이트) ---


def _build(git_raw: dict[str, Any]):
    config = FakeConfig({"sources.git": git_raw})
    stores = [MagicMock()] * 5
    return _build_git_sync_engine(config, *stores)


def test_builder_returns_none_when_disabled():
    assert _build({}) is None
    assert _build({"enabled": True}) is None
    assert _build({"auto_sync_enabled": True}) is None


def test_builder_returns_none_without_repositories():
    assert _build({"enabled": True, "auto_sync_enabled": True}) is None


def test_builder_creates_engine_with_interval():
    engine = _build({
        "enabled": True,
        "auto_sync_enabled": True,
        "sync_interval_minutes": 15,
        "repositories": [{"url": "git@example.com:org/repo.git"}],
    })
    assert isinstance(engine, PeriodicSyncEngine)
    assert engine.interval_minutes == 15
    assert engine.name == "git"
