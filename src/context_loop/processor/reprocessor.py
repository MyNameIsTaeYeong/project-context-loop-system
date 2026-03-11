"""문서 변경 감지 및 재처리 파이프라인.

문서의 content_hash를 비교하여 변경을 감지하고,
Delete & Recreate 전략으로 파생 데이터를 재생성한다.

현재 Phase 2에서는 파생 데이터(청크, 그래프) 삭제 및 status 갱신까지만 처리하며,
실제 LLM 처리(Phase 3)는 추후 파이프라인 단계에서 수행된다.
"""

from __future__ import annotations

import logging
from typing import Any

from context_loop.ingestion.uploader import compute_content_hash
from context_loop.storage.metadata_store import MetadataStore

logger = logging.getLogger(__name__)


class DocumentNotFoundError(Exception):
    """지정한 문서가 없을 때 발생한다."""


async def check_and_mark_changed(
    store: MetadataStore,
    document_id: int,
    new_content: str,
) -> bool:
    """문서의 content_hash를 비교하여 변경 여부를 확인하고, 변경 시 마킹한다.

    변경이 감지되면:
    1. original_content, content_hash 갱신
    2. status → 'changed'
    3. processing_history에 'updated' 기록 추가

    Args:
        store: 초기화된 MetadataStore 인스턴스.
        document_id: 확인할 문서 ID.
        new_content: 새 원본 내용.

    Returns:
        True면 변경됨, False면 변경 없음.

    Raises:
        DocumentNotFoundError: 해당 문서가 없는 경우.
    """
    doc = await store.get_document(document_id)
    if doc is None:
        raise DocumentNotFoundError(f"문서를 찾을 수 없습니다: document_id={document_id}")

    new_hash = compute_content_hash(new_content)
    if doc["content_hash"] == new_hash:
        return False

    await store.update_document_content(document_id, new_content, new_hash)
    await store.update_document_status(document_id, status="changed")
    await store.add_processing_history(
        document_id=document_id,
        action="updated",
        prev_storage_method=doc.get("storage_method"),
        status="started",
    )
    return True


async def delete_derived_data(
    store: MetadataStore,
    document_id: int,
) -> None:
    """문서의 파생 데이터(청크, 그래프 노드/엣지)를 모두 삭제한다.

    Delete & Recreate 전략의 첫 번째 단계.

    Args:
        store: 초기화된 MetadataStore 인스턴스.
        document_id: 파생 데이터를 삭제할 문서 ID.
    """
    await store.delete_chunks_by_document(document_id)
    await store.delete_graph_data_by_document(document_id)
    logger.debug("파생 데이터 삭제 완료: document_id=%d", document_id)


async def start_reprocessing(
    store: MetadataStore,
    document_id: int,
) -> int:
    """재처리 파이프라인을 시작한다.

    기존 파생 데이터를 삭제하고 status를 'processing'으로 설정한다.
    실제 LLM 처리(청킹, 임베딩, 그래프 추출)는 Phase 3에서 구현된다.

    Args:
        store: 초기화된 MetadataStore 인스턴스.
        document_id: 재처리할 문서 ID.

    Returns:
        생성된 processing_history ID.

    Raises:
        DocumentNotFoundError: 해당 문서가 없는 경우.
    """
    doc = await store.get_document(document_id)
    if doc is None:
        raise DocumentNotFoundError(f"문서를 찾을 수 없습니다: document_id={document_id}")

    # 기존 파생 데이터 삭제
    await delete_derived_data(store, document_id)

    # 상태를 processing으로 전환
    await store.update_document_status(document_id, status="processing")

    # 처리 이력 기록
    history_id = await store.add_processing_history(
        document_id=document_id,
        action="reprocessed",
        prev_storage_method=doc.get("storage_method"),
        status="started",
    )
    logger.info("재처리 시작: document_id=%d, history_id=%d", document_id, history_id)
    return history_id


async def complete_reprocessing(
    store: MetadataStore,
    document_id: int,
    history_id: int,
    new_storage_method: str,
    *,
    error_message: str | None = None,
) -> None:
    """재처리 완료를 기록한다.

    Args:
        store: 초기화된 MetadataStore 인스턴스.
        document_id: 완료된 문서 ID.
        history_id: 완료할 processing_history ID.
        new_storage_method: 재처리 후 결정된 저장 방식 ("chunk", "graph", "hybrid").
        error_message: 오류 메시지. None이면 성공으로 처리.
    """
    if error_message:
        await store.update_document_status(document_id, status="failed")
        await store.complete_processing_history(
            history_id, status="failed", error_message=error_message
        )
        logger.error("재처리 실패: document_id=%d, error=%s", document_id, error_message)
    else:
        await store.update_document_status(
            document_id, status="completed", storage_method=new_storage_method
        )
        await store.complete_processing_history(history_id, status="completed")
        logger.info(
            "재처리 완료: document_id=%d, storage_method=%s",
            document_id,
            new_storage_method,
        )


async def get_pending_documents(store: MetadataStore) -> list[dict[str, Any]]:
    """처리 대기 중인 문서 목록을 반환한다.

    status가 'pending', 'changed', 'processing' 인 문서를 반환한다.

    Args:
        store: 초기화된 MetadataStore 인스턴스.

    Returns:
        처리 대기 문서 dict 목록.
    """
    results: list[dict[str, Any]] = []
    for status in ("pending", "changed"):
        docs = await store.list_documents(status=status)
        results.extend(docs)
    return results
