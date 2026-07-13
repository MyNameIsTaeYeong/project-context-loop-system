"""PeriodicSyncEngine 공용 주기 엔진 베이스 테스트.

start/stop 라이프사이클·stop 즉시성·멱등성은 서브클래스인 MCPSyncEngine
테스트(test_mcp_engine.py)가 커버하므로, 여기서는 베이스 고유 동작
(run_cycle 주입, 미주입 오류, 사이클 실패 격리)만 검증한다.
"""

from __future__ import annotations

import asyncio

import pytest

from context_loop.sync.periodic import PeriodicSyncEngine


async def test_run_once_calls_injected_cycle():
    calls: list[int] = []

    async def run_cycle() -> str:
        calls.append(1)
        return "ok"

    engine = PeriodicSyncEngine(run_cycle, name="test")
    result = await engine.run_once()

    assert result == "ok"
    assert calls == [1]
    assert engine.last_cycle_at is not None
    assert engine.name == "test"


async def test_run_once_without_cycle_raises():
    engine = PeriodicSyncEngine()
    with pytest.raises(NotImplementedError):
        await engine.run_once()


async def test_loop_survives_cycle_failure():
    """사이클 예외가 루프를 죽이지 않고 다음 주기에 재시도된다."""
    attempts: list[int] = []
    second_cycle = asyncio.Event()

    async def run_cycle() -> None:
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("simulated cycle failure")
        second_cycle.set()

    engine = PeriodicSyncEngine(
        run_cycle, name="test",
        interval_minutes=0.001,       # 60ms 주기
        initial_delay_seconds=0,
    )
    engine.start()
    await asyncio.wait_for(second_cycle.wait(), timeout=5)
    await engine.stop()

    assert len(attempts) >= 2
    assert not engine.is_running
