"""문서 삭제 cascade 유틸.

VectorStore, GraphStore, MetadataStore 세 저장소에서 일관된 순서로
문서 관련 데이터를 제거한다. 문서 상세 API의 삭제 경로와
Confluence 싱크 대상 해제 / orphan GC 양쪽이 공유 사용한다.
"""

from __future__ import annotations

import logging

from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore

logger = logging.getLogger(__name__)


async def delete_document_cascade(
    document_id: int,
    *,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
) -> bool:
    """문서와 연관된 모든 데이터를 제거한다.

    제거 범위:
      - ChromaDB 벡터 (청크 임베딩)
      - 그래프 엣지 · 노드-문서 연결 · 고아 노드 · NetworkX 메모리 그래프
      - SQLite ``documents`` 행 (chunks 등은 FK CASCADE로 자동 정리)

    삭제 순서는 ``vector → graph → meta`` 로, 실패 시 재생성 비용이
    낮은 쪽부터 제거한다. ``documents`` 행이 마지막에 삭제되므로
    중간에 실패해도 dangling 참조가 남지 않는다.

    문서가 존재하지 않으면 다른 스토어는 건드리지 않고 ``False``를 반환한다.

    Args:
        document_id: 삭제할 문서 ID.
        meta_store: 초기화된 MetadataStore.
        vector_store: 초기화된 VectorStore.
        graph_store: 초기화된 GraphStore.

    Returns:
        실제로 삭제되었으면 ``True``, 문서가 없어서 건너뛰었으면 ``False``.
    """
    doc = await meta_store.get_document(document_id)
    if doc is None:
        logger.debug("cascade 삭제 건너뜀 — 문서 없음: %d", document_id)
        return False

    vector_store.delete_by_document(document_id)
    await graph_store.delete_document_graph(document_id)
    await meta_store.delete_document(document_id)

    logger.info("문서 cascade 삭제 완료 — document_id=%d", document_id)
    return True
