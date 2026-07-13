"""Confluence MCP 싱크 자동 주기 실행 엔진.

:func:`~context_loop.sync.mcp_sync.execute_sync_target` 은 session/stores 만
받으면 돌아가는 "커널"이고, 이 모듈의 :class:`MCPSyncEngine` 은 그 커널을
설정된 주기마다 모든 싱크 대상에 대해 실행하는 "러너"다. 주기 루프 자체는
:class:`~context_loop.sync.periodic.PeriodicSyncEngine` 베이스가 담당하고,
이 클래스는 사이클 내부(대상 순회 + 대상 단위 실패 격리)만 정의한다.

엔진은 MCP 세션·스토어·임베딩 클라이언트를 직접 알지 않는다. 대상 1건을
싱크하는 ``run_target(target_id)`` 콜러블만 주입받아 호출한다 — 웹 앱에서는
:func:`context_loop.web.api.confluence_mcp.run_sync_in_background` 를 그대로
넘겨, 수동 버튼 싱크와 **동일한 target 단위 락/진행 상태**를 공유한다.
같은 대상이 이미 싱크 중이면 러너 쪽 락이 실행을 건너뛰므로 엔진은
중복 실행을 걱정하지 않는다.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from context_loop.storage.metadata_store import MetadataStore
from context_loop.sync.periodic import (
    DEFAULT_INITIAL_DELAY_SECONDS,
    DEFAULT_INTERVAL_MINUTES,
    PeriodicSyncEngine,
)

__all__ = [
    "DEFAULT_INITIAL_DELAY_SECONDS",
    "DEFAULT_INTERVAL_MINUTES",
    "MCPSyncEngine",
]

logger = logging.getLogger(__name__)


class MCPSyncEngine(PeriodicSyncEngine):
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
        super().__init__(
            name="Confluence MCP",
            interval_minutes=interval_minutes,
            initial_delay_seconds=initial_delay_seconds,
        )
        self._meta_store = meta_store
        self._run_target = run_target

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
