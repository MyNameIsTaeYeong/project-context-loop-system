"""소스 공용 주기 실행 엔진 베이스.

:class:`PeriodicSyncEngine` 은 "설정된 주기마다 사이클 1회 실행"만 아는
범용 백그라운드 루프다. 사이클이 실제로 무엇을 하는지는 두 방법 중 하나로
정의한다:

- 생성자에 ``run_cycle`` async 콜러블 주입 (git_code 자동 싱크가 이 방식 —
  :func:`context_loop.web.api.git_sync.run_sync_in_background` 를 그대로 넘긴다)
- 서브클래스에서 :meth:`run_once` 오버라이드 (Confluence MCP 의
  :class:`~context_loop.sync.mcp_engine.MCPSyncEngine` 이 이 방식 —
  싱크 대상 목록을 돌며 대상별 실패를 격리한다)

엔진은 스토어·클라이언트·세션을 직접 알지 않으므로 어떤 러너와도 조합된다.
stop 은 ``asyncio.Event`` 기반 협조적 종료 — sleep 중이면 즉시 깨어나고,
사이클 진행 중이면 그 사이클(또는 서브클래스가 정의한 중단 지점)까지만
마치고 멈춘다. 싱크 도중 강제 cancel 로 워터마크/membership 같은 진행
상태가 어중간하게 남지 않도록 하기 위함이다.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_MINUTES = 30
DEFAULT_INITIAL_DELAY_SECONDS = 60.0
"""기동 직후 첫 사이클까지의 지연(초).

서버 시작과 동시에 무거운 싱크가 몰리지 않도록 짧게 기다린다. 앱 초기화
(엔티티 임베딩 사전 구축 등)와 겹치지 않게 하려는 목적이다.
"""


class PeriodicSyncEngine:
    """사이클 콜러블을 설정된 주기마다 실행하는 백그라운드 엔진.

    Args:
        run_cycle: 사이클 1회를 수행하는 async 콜러블. 서브클래스가
            :meth:`run_once` 를 오버라이드하는 경우 생략한다.
        name: 로그·상태 표시용 엔진 이름 (예: "Confluence MCP", "git").
        interval_minutes: 사이클 주기(분).
        initial_delay_seconds: 기동 후 첫 사이클까지의 지연(초).
    """

    def __init__(
        self,
        run_cycle: Callable[[], Awaitable[Any]] | None = None,
        *,
        name: str = "sync",
        interval_minutes: float = DEFAULT_INTERVAL_MINUTES,
        initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS,
    ) -> None:
        self._run_cycle = run_cycle
        self._name = name
        self._interval_seconds = max(1.0, float(interval_minutes) * 60)
        self._initial_delay_seconds = max(0.0, float(initial_delay_seconds))
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._running = False
        self._last_cycle_at: datetime | None = None

    @property
    def name(self) -> str:
        """엔진 이름 — 로그·상태 표시용."""
        return self._name

    @property
    def is_running(self) -> bool:
        """주기 루프 실행 중 여부."""
        return self._running

    @property
    def last_cycle_at(self) -> datetime | None:
        """마지막으로 완료된 사이클의 종료 시각(UTC)."""
        return self._last_cycle_at

    @property
    def interval_minutes(self) -> float:
        """사이클 주기(분) — 상태 표시용."""
        return self._interval_seconds / 60

    async def run_once(self) -> Any:
        """사이클 1회를 실행한다.

        기본 구현은 생성자에 주입된 ``run_cycle`` 을 호출한다. 서브클래스는
        이 메서드를 오버라이드해 사이클 내부 구조(대상 순회, 부분 실패 격리
        등)를 정의할 수 있다 — 오버라이드 시 완료 후 ``self._last_cycle_at``
        갱신도 책임진다.
        """
        if self._run_cycle is None:
            raise NotImplementedError(
                "run_cycle 을 주입하거나 run_once 를 오버라이드해야 합니다.",
            )
        result = await self._run_cycle()
        self._last_cycle_at = datetime.now(tz=UTC)
        return result

    async def _sleep(self, seconds: float) -> None:
        """stop 요청에 즉시 깨어나는 sleep."""
        if seconds <= 0:
            return
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def _loop(self) -> None:
        self._running = True
        try:
            await self._sleep(self._initial_delay_seconds)
            while not self._stop_event.is_set():
                try:
                    result = await self.run_once()
                    logger.info(
                        "[%s] 자동 싱크 사이클 완료 — %s", self._name, result,
                    )
                except Exception:  # noqa: BLE001
                    # 사이클 전체 실패 — 루프는 다음 주기에 재시도.
                    logger.exception("[%s] 자동 싱크 사이클 실패", self._name)
                await self._sleep(self._interval_seconds)
        finally:
            self._running = False

    def start(self) -> None:
        """백그라운드 자동 싱크 루프를 시작한다 (이미 실행 중이면 무시)."""
        if self._task and not self._task.done():
            logger.warning("[%s] 자동 싱크 엔진이 이미 실행 중입니다.", self._name)
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "[%s] 자동 싱크 시작 — 주기: %.0f분, 첫 실행까지: %.0f초",
            self._name,
            self.interval_minutes,
            self._initial_delay_seconds,
        )

    async def stop(self) -> None:
        """루프를 중지하고 태스크 종료를 기다린다.

        sleep 중이면 즉시 깨워 종료하고, 사이클 진행 중이면 협조적으로
        마무리될 때까지 기다린다 (cancel 하지 않음).
        """
        self._stop_event.set()
        if self._task and not self._task.done():
            await self._task
        logger.info("[%s] 자동 싱크 중지됨.", self._name)
