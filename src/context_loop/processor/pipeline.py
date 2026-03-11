"""문서 처리 파이프라인.

문서 변경 감지 및 재처리(Delete & Recreate) 로직을 제공한다.
Phase 3에서 LLM Classifier, Chunker, GraphExtractor가 구현되면 연동된다.
"""

from __future__ import annotations

import logging

from context_loop.processor.parser import compute_content_hash
from context_loop.storage.metadata_store import MetadataStore

logger = logging.getLogger(__name__)


class ProcessingPipeline:
    """문서 처리 파이프라인.

    문서 입력 → 변경 감지 → 기존 데이터 삭제 → 재처리의 흐름을 관리한다.

    Args:
        store: 메타데이터 저장소.
    """

    def __init__(self, store: MetadataStore) -> None:
        self._store = store

    async def process_document(self, document_id: int) -> None:
        """문서를 처리한다.

        현재는 상태 변경만 수행하며, Phase 3에서 LLM 분류/청킹/그래프 추출이 추가된다.

        Args:
            document_id: 처리할 문서 ID.
        """
        doc = await self._store.get_document(document_id)
        if doc is None:
            raise ValueError(f"문서를 찾을 수 없습니다: ID {document_id}")

        # 처리 이력 시작
        history_id = await self._store.add_processing_history(
            document_id=document_id,
            action="created" if doc["version"] == 1 else "reprocessed",
            prev_storage_method=doc.get("storage_method"),
        )

        try:
            await self._store.update_document_status(document_id, "processing")

            # Phase 3에서 아래가 추가될 예정:
            # 1. LLM Classifier로 저장 방식 판단 (chunk/graph/hybrid)
            # 2. 판정에 따라 청킹 또는 그래프 추출
            # 3. 벡터DB/그래프DB에 저장

            # 현재는 pending 상태로 유지 (처리 모듈 미구현)
            await self._store.update_document_status(document_id, "completed")

            await self._store.complete_processing_history(history_id, status="completed")
            logger.info("문서 처리 완료: ID %d", document_id)

        except Exception as e:
            await self._store.complete_processing_history(
                history_id, status="failed", error_message=str(e)
            )
            await self._store.update_document_status(document_id, "failed")
            raise

    async def reprocess_document(self, document_id: int) -> None:
        """문서를 재처리한다 (Delete & Recreate).

        기존 파생 데이터(청크, 그래프)를 삭제한 후 재처리한다.

        Args:
            document_id: 재처리할 문서 ID.
        """
        doc = await self._store.get_document(document_id)
        if doc is None:
            raise ValueError(f"문서를 찾을 수 없습니다: ID {document_id}")

        logger.info("문서 재처리 시작 (Delete & Recreate): ID %d, '%s'", document_id, doc["title"])

        # 1. 기존 파생 데이터 삭제
        await self._store.delete_chunks_by_document(document_id)
        await self._store.delete_graph_data_by_document(document_id)

        # 2. 재처리
        await self.process_document(document_id)

    async def check_and_reprocess(self, document_id: int, new_content: str) -> bool:
        """콘텐츠 변경을 확인하고 필요시 재처리한다.

        Args:
            document_id: 문서 ID.
            new_content: 새 콘텐츠.

        Returns:
            변경이 있었으면 True, 없으면 False.
        """
        doc = await self._store.get_document(document_id)
        if doc is None:
            raise ValueError(f"문서를 찾을 수 없습니다: ID {document_id}")

        new_hash = compute_content_hash(new_content)
        if new_hash == doc["content_hash"]:
            return False

        # 콘텐츠 업데이트
        await self._store.update_document_content(document_id, new_content, new_hash)

        # 재처리
        await self.reprocess_document(document_id)
        return True
