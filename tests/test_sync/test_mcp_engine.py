"""MCPSyncEngine 자동 주기 실행 엔진 테스트."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from context_loop.storage.metadata_store import MetadataStore
from context_loop.sync.mcp_engine import MCPSyncEngine


@pytest.fixture
async def meta_store(tmp_path: Path):  # type: ignore[misc]
    store = MetadataStore(tmp_path / "test.db")
    await store.initialize()
    yield store
    await store.close()


async def _add_target(store: MetadataStore, space_key: str) -> int:
    target = await store.upsert_sync_target(
        scope="space", space_key=space_key, page_id=None, name=f"Space {space_key}",
    )
    return int(target["id"])


# --- run_once ---


async def test_run_once_syncs_all_targets(meta_store: MetadataStore):
    id_a = await _add_target(meta_store, "AAA")
    id_b = await _add_target(meta_store, "BBB")

    synced: list[int] = []

    async def run_target(target_id: int) -> None:
        synced.append(target_id)

    engine = MCPSyncEngine(meta_store, run_target)
    attempted = await engine.run_once()

    assert attempted == 2
    assert sorted(synced) == sorted([id_a, id_b])
    assert engine.last_cycle_at is not None


async def test_run_once_no_targets(meta_store: MetadataStore):
    calls: list[int] = []

    async def run_target(target_id: int) -> None:
        calls.append(target_id)

    engine = MCPSyncEngine(meta_store, run_target)
    assert await engine.run_once() == 0
    assert calls == []


async def test_run_once_isolates_target_failure(meta_store: MetadataStore):
    """한 대상의 예외가 나머지 대상 싱크를 막지 않는다."""
    id_a = await _add_target(meta_store, "AAA")
    id_b = await _add_target(meta_store, "BBB")
    id_c = await _add_target(meta_store, "CCC")

    synced: list[int] = []

    async def run_target(target_id: int) -> None:
        if target_id == id_b:
            raise RuntimeError("simulated sync failure")
        synced.append(target_id)

    engine = MCPSyncEngine(meta_store, run_target)
    attempted = await engine.run_once()

    assert attempted == 3
    assert sorted(synced) == sorted([id_a, id_c])


# --- start / stop 라이프사이클 ---


async def test_loop_runs_cycles_and_stops(meta_store: MetadataStore):
    await _add_target(meta_store, "AAA")

    cycles = asyncio.Event()
    calls: list[int] = []

    async def run_target(target_id: int) -> None:
        calls.append(target_id)
        cycles.set()

    engine = MCPSyncEngine(
        meta_store, run_target,
        interval_minutes=0.001,       # 60ms 주기
        initial_delay_seconds=0,
    )
    engine.start()
    await asyncio.wait_for(cycles.wait(), timeout=5)
    assert engine.is_running

    await engine.stop()
    assert not engine.is_running
    assert len(calls) >= 1


async def test_stop_wakes_initial_delay_promptly(meta_store: MetadataStore):
    """긴 initial delay 중이라도 stop 은 즉시 반환한다."""

    async def run_target(target_id: int) -> None:  # noqa: ARG001
        pass

    engine = MCPSyncEngine(
        meta_store, run_target,
        interval_minutes=60,
        initial_delay_seconds=3600,
    )
    engine.start()
    await asyncio.sleep(0)  # 루프 태스크가 sleep 에 진입하도록 양보

    await asyncio.wait_for(engine.stop(), timeout=2)
    assert not engine.is_running


async def test_start_is_idempotent(meta_store: MetadataStore):
    """이미 실행 중이면 start 를 다시 불러도 태스크가 하나만 유지된다."""

    async def run_target(target_id: int) -> None:  # noqa: ARG001
        pass

    engine = MCPSyncEngine(
        meta_store, run_target,
        interval_minutes=60,
        initial_delay_seconds=3600,
    )
    engine.start()
    first_task = engine._task
    engine.start()
    assert engine._task is first_task

    await engine.stop()


async def test_stop_interrupts_cycle_between_targets(meta_store: MetadataStore):
    """사이클 도중 stop 요청이 오면 남은 대상은 건너뛴다."""
    await _add_target(meta_store, "AAA")
    await _add_target(meta_store, "BBB")

    calls: list[int] = []

    async def run_target(target_id: int) -> None:
        calls.append(target_id)
        engine._stop_event.set()  # 첫 대상 처리 중 stop 요청 도착을 흉내

    engine = MCPSyncEngine(meta_store, run_target)
    attempted = await engine.run_once()

    assert attempted == 1
    assert len(calls) == 1
