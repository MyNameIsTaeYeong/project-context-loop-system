"""Confluence 증분 동기화 엔진.

주기적으로 Confluence에서 변경된 페이지를 감지하고,
변경된 문서만 재임포트한다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from context_loop.ingestion.confluence import ConfluenceClient, ConfluenceImporter
from context_loop.processor.parser import compute_content_hash, html_to_markdown
from context_loop.storage.metadata_store import MetadataStore

logger = logging.getLogger(__name__)


class SyncEngine:
    """Confluence 증분 동기화 엔진.

    Args:
        client: Confluence API 클라이언트.
        store: 메타데이터 저장소.
        interval_minutes: 동기화 주기 (분).
    """

    def __init__(
        self,
        client: ConfluenceClient,
        store: MetadataStore,
        interval_minutes: int = 30,
    ) -> None:
        self._client = client
        self._store = store
        self._interval = interval_minutes * 60
        self._running = False
        self._task: asyncio.Task[None] | None = None

    async def sync_once(self) -> list[int]:
        """한 번 동기화를 수행한다.

        Returns:
            업데이트된 문서 ID 목록.
        """
        updated_ids: list[int] = []

        # 로컬에 저장된 Confluence 문서 목록 조회
        local_docs = await self._store.list_documents(source_type="confluence")
        local_map: dict[str, dict[str, Any]] = {
            doc["source_id"]: doc for doc in local_docs if doc["source_id"]
        }

        if not local_map:
            logger.info("동기화할 Confluence 문서가 없습니다.")
            return updated_ids

        # 변경된 페이지 조회
        for source_id, local_doc in local_map.items():
            try:
                page = await self._client.get_page(source_id)
                markdown_content = html_to_markdown(page.body_html)
                new_hash = compute_content_hash(markdown_content)

                if new_hash != local_doc["content_hash"]:
                    logger.info(
                        "변경 감지: '%s' (ID: %s)", local_doc["title"], local_doc["id"]
                    )
                    await self._store.update_document_content(
                        local_doc["id"], markdown_content, new_hash
                    )
                    updated_ids.append(local_doc["id"])
            except Exception:
                logger.exception("페이지 동기화 실패: source_id=%s", source_id)

        logger.info("동기화 완료: %d개 문서 업데이트", len(updated_ids))
        return updated_ids

    async def _run_loop(self) -> None:
        """주기적 동기화 루프."""
        while self._running:
            try:
                await self.sync_once()
            except Exception:
                logger.exception("동기화 루프 오류")
            await asyncio.sleep(self._interval)

    def start(self) -> None:
        """백그라운드 동기화를 시작한다."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("동기화 시작 (주기: %d분)", self._interval // 60)

    def stop(self) -> None:
        """백그라운드 동기화를 중지한다."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("동기화 중지")
