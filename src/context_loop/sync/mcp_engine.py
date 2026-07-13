"""Confluence MCP 싱크 자동 주기 실행 엔진.

:func:`~context_loop.sync.mcp_sync.execute_sync_target` 은 session/stores 만
받으면 돌아가는 "커널"이고, 이 모듈의 :class:`MCPSyncEngine` 은 그 커널을
설정된 주기마다 모든 싱크 대상에 대해 실행하는 "러너"다.

엔진은 MCP 세션·스토어·임베딩 클라이언트를 직접 알지 않는다. 대상 1건을
싱크하는 ``run_target(target_id)`` 콜러블만 주입받아 호출한다 — 웹 앱에서는
:func:`context_loop.web.api.confluence_mcp.run_sync_in_background` 를 그대로
넘겨, 수동 버튼 싱크와 **동일한 target 단위 락/진행 상태**를 공유한다.
같은 대상이 이미 싱크 중이면 러너 쪽 락이 실행을 건너뛰므로 엔진은
중복 실행을 걱정하지 않는다.

기존 REST 기반 :class:`~context_loop.sync.engine.SyncEngine` 과 별개 클래스다
(D-분리 결정: 세션 수명·도구 셋·에러 타입이 달라 공통화 비용이 분리 비용보다 큼).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from context_loop.storage.metadata_store import MetadataStore

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_MINUTES = 30
DEFAULT_INITIAL_DELAY_SECONDS = 60.0
"""기동 직후 첫 사이클까지의 지연(초).

서버 시작과 동시에 무거운 싱크가 몰리지 않도록 짧게 기다린다. 증분 fetch
(워터마크) 덕에 첫 사이클 자체는 싸지만, 앱 초기화(엔티티 임베딩 사전 구축
등)와 겹치지 않게 하려는 목적이다.
"""


class MCPSyncEngine:
    """등록된 모든 싱크 대상을 주기적으로 재싱크하는 백그라운드 엔진.

    Args:
        meta_store: 초기화된 MetadataStore — 싱크 대상 목록 조회에만 사용.
        run_target: 대상 1건을 싱크하는 async 콜러블. 예외를 밖으로 던져도
            엔진이 격리하지만, 웹 러너처럼 내부에서 삼키는 구현이어도 된다.
        interval_minutes: 사이클 주기(분). ``sources.confluence_mcp.
            sync_interval_minutes`` 설정값이 여기로 들어온다.
        initial_delay_seconds: 기동 후 첫 사이클까지의 지연(초).
    """

    def __init__(
        self,
        meta_store: MetadataStore,
        run_target: Callable[[int], Awaitable[None]],
        *,
        interval_minutes: float = DEFAULT_INTERVAL_MINUTES,
        initial_delay_seconds: float = DEFAULT_INITIAL_DELAY_SECONDS,
    ) -> None:
        self._meta_store = meta_store
        self._run_target = run_target
        self._interval_seconds = max(1.0, float(interval_minutes) * 60)
        self._initial_delay_seconds = max(0.0, float(initial_delay_seconds))
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._running = False
        self._last_cycle_at: datetime | None = None

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

    async def run_once(self) -> int:
        """등록된 모든 싱크 대상을 순차로 1회 싱크한다.

        대상 단위 실패는 격리된다 — 한 대상의 예외가 나머지 대상 싱크를
        막지 않는다. 순차 실행인 이유: 대상들이 같은 MCP 서버·임베딩
        엔드포인트를 공유하므로, 대상 간 병렬화는 rate limit 만 압박한다
        (대상 내부는 이미 ``phase2_concurrency`` 로 병렬).

        Returns:
            싱크를 시도한 대상 수 (stop 요청으로 중단되면 그 시점까지의 수).
        """
        targets = await self._meta_store.list_sync_targets()
        attempted = 0
        for target in targets:
            if self._stop_event.is_set():
                break
            target_id = int(target["id"])
            try:
                await self._run_target(target_id)
            except Exception:  # noqa: BLE001
                logger.exception("자동 싱크 실패 target_id=%d", target_id)
            attempted += 1
        self._last_cycle_at = datetime.now(tz=UTC)
        return attempted

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
                    count = await self.run_once()
                    logger.info("자동 싱크 사이클 완료 — 대상 %d건", count)
                except Exception:  # noqa: BLE001
                    # 대상 목록 조회 실패 등 — 루프는 다음 주기에 재시도.
                    logger.exception("자동 싱크 사이클 실패")
                await self._sleep(self._interval_seconds)
        finally:
            self._running = False

    def start(self) -> None:
        """백그라운드 자동 싱크 루프를 시작한다 (이미 실행 중이면 무시)."""
        if self._task and not self._task.done():
            logger.warning("MCP 자동 싱크 엔진이 이미 실행 중입니다.")
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "MCP 자동 싱크 시작 — 주기: %.0f분, 첫 실행까지: %.0f초",
            self.interval_minutes,
            self._initial_delay_seconds,
        )

    async def stop(self) -> None:
        """루프를 중지하고 태스크 종료를 기다린다.

        sleep 중이면 즉시 깨워 종료하고, 대상 싱크가 진행 중이면 해당
        대상까지만 마치고 멈춘다 (싱크 도중 강제 취소로 워터마크/membership
        이 어중간하게 남지 않도록 cancel 대신 협조적 종료를 쓴다).
        """
        self._stop_event.set()
        if self._task and not self._task.done():
            await self._task
        logger.info("MCP 자동 싱크 중지됨.")
