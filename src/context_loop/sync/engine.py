"""Confluence 증분 동기화 엔진.

설정된 주기마다 Confluence에서 변경된 페이지를 감지하여
메타데이터 저장소를 갱신한다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from context_loop.ingestion.confluence import ConfluenceClient, import_page
from context_loop.storage.metadata_store import MetadataStore

logger = logging.getLogger(__name__)


class SyncResult:
    """동기화 결과를 담는 데이터 클래스."""

    def __init__(self) -> None:
        self.created: list[int] = []      # 새로 생성된 문서 ID 목록
        self.updated: list[int] = []      # 갱신된 문서 ID 목록
        self.unchanged: list[int] = []    # 변경 없는 문서 ID 목록
        self.errors: list[dict[str, Any]] = []  # 오류 목록 [{page_id, error}]

    @property
    def total(self) -> int:
        return len(self.created) + len(self.updated) + len(self.unchanged) + len(self.errors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "errors": self.errors,
            "summary": {
                "created": len(self.created),
                "updated": len(self.updated),
                "unchanged": len(self.unchanged),
                "errors": len(self.errors),
                "total": self.total,
            },
        }


async def sync_space(
    client: ConfluenceClient,
    store: MetadataStore,
    space_id: str,
    base_url: str,
    *,
    since: datetime | None = None,
) -> SyncResult:
    """Confluence 스페이스를 증분 동기화한다.

    since가 주어지면 해당 시각 이후 변경된 페이지만 가져온다.
    since가 None이면 전체 스페이스를 동기화한다.

    Args:
        client: ConfluenceClient 인스턴스.
        store: 초기화된 MetadataStore 인스턴스.
        space_id: 동기화할 스페이스 ID.
        base_url: Confluence 인스턴스 URL.
        since: 이 시각 이후 변경된 페이지만 대상. None이면 전체 동기화.

    Returns:
        SyncResult — 생성/갱신/변경없음/오류 집계.
    """
    result = SyncResult()
    pages = await client.list_pages(space_id)

    for page in pages:
        page_id = str(page["id"])

        # since 필터: 페이지의 마지막 수정 시각과 비교
        if since is not None:
            last_modified_str = (
                page.get("version", {}).get("createdAt")
                or page.get("lastModifiedDate")
            )
            if last_modified_str:
                try:
                    last_modified = datetime.fromisoformat(
                        last_modified_str.replace("Z", "+00:00")
                    )
                    if last_modified.tzinfo is None:
                        last_modified = last_modified.replace(tzinfo=timezone.utc)
                    since_aware = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
                    if last_modified <= since_aware:
                        # 변경되지 않은 페이지는 DB 조회로 확인
                        existing_docs = await store.list_documents(source_type="confluence")
                        existing = next(
                            (d for d in existing_docs if d.get("source_id") == page_id), None
                        )
                        if existing:
                            result.unchanged.append(existing["id"])
                            continue
                except ValueError:
                    pass  # 날짜 파싱 실패 시 무조건 처리

        try:
            doc = await import_page(client, store, page_id, base_url)
            if doc["created"]:
                result.created.append(doc["id"])
            elif doc["changed"]:
                result.updated.append(doc["id"])
            else:
                result.unchanged.append(doc["id"])
        except Exception as exc:  # noqa: BLE001
            logger.error("페이지 동기화 실패 page_id=%s: %s", page_id, exc)
            result.errors.append({"page_id": page_id, "error": str(exc)})

    return result


class SyncEngine:
    """Confluence 자동 증분 동기화 엔진.

    주기적으로 스페이스를 동기화하는 백그라운드 태스크를 관리한다.

    Args:
        client: ConfluenceClient 인스턴스.
        store: 초기화된 MetadataStore 인스턴스.
        base_url: Confluence 인스턴스 URL.
        space_ids: 동기화할 스페이스 ID 목록.
        interval_minutes: 동기화 주기(분).
    """

    def __init__(
        self,
        client: ConfluenceClient,
        store: MetadataStore,
        base_url: str,
        space_ids: list[str],
        interval_minutes: int = 30,
    ) -> None:
        self._client = client
        self._store = store
        self._base_url = base_url
        self._space_ids = space_ids
        self._interval_seconds = interval_minutes * 60
        self._task: asyncio.Task[None] | None = None
        self._last_sync: datetime | None = None
        self._running = False

    @property
    def last_sync(self) -> datetime | None:
        """마지막 동기화 시각."""
        return self._last_sync

    @property
    def is_running(self) -> bool:
        """자동 동기화 실행 중 여부."""
        return self._running

    async def sync_now(self) -> dict[str, SyncResult]:
        """모든 스페이스를 즉시 증분 동기화한다.

        Returns:
            {space_id: SyncResult} 매핑.
        """
        results: dict[str, SyncResult] = {}
        for space_id in self._space_ids:
            logger.info("스페이스 동기화 시작: %s", space_id)
            result = await sync_space(
                self._client,
                self._store,
                space_id,
                self._base_url,
                since=self._last_sync,
            )
            results[space_id] = result
            logger.info(
                "스페이스 동기화 완료: %s — %s",
                space_id,
                result.to_dict()["summary"],
            )
        self._last_sync = datetime.now(tz=timezone.utc)
        return results

    async def _loop(self) -> None:
        """주기적 동기화 루프."""
        self._running = True
        try:
            while self._running:
                try:
                    await self.sync_now()
                except Exception as exc:  # noqa: BLE001
                    logger.error("자동 동기화 중 오류 발생: %s", exc)
                await asyncio.sleep(self._interval_seconds)
        finally:
            self._running = False

    def start(self) -> None:
        """백그라운드 자동 동기화를 시작한다."""
        if self._task and not self._task.done():
            logger.warning("동기화 엔진이 이미 실행 중입니다.")
            return
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "자동 동기화 시작 — 스페이스: %s, 주기: %d분",
            self._space_ids,
            self._interval_seconds // 60,
        )

    async def stop(self) -> None:
        """백그라운드 자동 동기화를 중지한다."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("자동 동기화 중지됨.")
