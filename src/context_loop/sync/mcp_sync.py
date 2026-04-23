"""Confluence MCP 기반 3-scope 싱크 실행 로직.

``confluence_sync_targets`` 행(scope=page/subtree/space)에 대응하는 sync 경로를
:func:`execute_sync_target` 디스패처가 선택해 실행한다.

- ``page`` scope: 단건 :func:`import_page_via_mcp`, diff 없음.
- ``subtree`` scope: :func:`walk_subtree` 로 루트 아래 모든 페이지를
  평탄화한 뒤 각 페이지를 임포트하고, 이전 membership 과의 차집합으로
  제거된 페이지를 식별해 cascade 삭제한다.
- ``space`` scope: :func:`enumerate_space_pages` 로 공간 전체 페이지를
  CQL 페이지네이션으로 나열한 뒤 동일한 증분 로직을 적용한다.

membership 반영은 **임포트 성공 시에만** 수행된다. 열거 자체가 실패하면
membership을 건드리지 않고 반환 — 일시적 Confluence 장애가 기존 문서를
삭제시키지 않도록 하기 위함.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession

from context_loop.ingestion.mcp_confluence import (
    enumerate_space_pages,
    import_page_via_mcp,
    walk_subtree,
)
from context_loop.storage.cascade import delete_document_cascade
from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """MCP 싱크 실행 결과 집계.

    Attributes:
        created: 새로 임포트된 문서 ID.
        updated: 내용이 변경된 문서 ID.
        unchanged: 해시 동일로 건너뛴 문서 ID.
        errors: 개별 페이지 처리 실패 ``{"page_id", "error"}`` 목록.
        removed: 스코프에서 사라져 cascade 삭제된 문서 ID.
    """

    created: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    unchanged: list[int] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (
            len(self.created)
            + len(self.updated)
            + len(self.unchanged)
            + len(self.errors)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "errors": self.errors,
            "removed": self.removed,
            "summary": {
                "created": len(self.created),
                "updated": len(self.updated),
                "unchanged": len(self.unchanged),
                "errors": len(self.errors),
                "removed": len(self.removed),
                "total": self.total,
            },
        }


async def execute_sync_target(
    session: ClientSession,
    target: dict[str, Any],
    *,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
) -> SyncResult:
    """싱크 대상 행의 scope 에 따라 올바른 sync 경로로 위임한다.

    Args:
        session: 초기화된 MCP ClientSession.
        target: ``confluence_sync_targets`` 한 행 (dict). 최소 ``id``,
            ``scope``, ``space_key``, ``page_id`` 키가 필요하다.
        meta_store: 초기화된 MetadataStore.
        vector_store: 초기화된 VectorStore.
        graph_store: 초기화된 GraphStore.

    Returns:
        :class:`SyncResult` 집계.

    Raises:
        ValueError: ``scope`` 가 알려진 값(page/subtree/space)이 아닐 때.
    """
    scope = target.get("scope")
    if scope == "page":
        return await _sync_page(
            session, target,
            meta_store=meta_store,
            vector_store=vector_store,
            graph_store=graph_store,
        )
    if scope == "subtree":
        return await _sync_subtree(
            session, target,
            meta_store=meta_store,
            vector_store=vector_store,
            graph_store=graph_store,
        )
    if scope == "space":
        return await _sync_space(
            session, target,
            meta_store=meta_store,
            vector_store=vector_store,
            graph_store=graph_store,
        )
    raise ValueError(f"Unknown sync target scope: {scope!r}")


async def _sync_page(
    session: ClientSession,
    target: dict[str, Any],
    *,
    meta_store: MetadataStore,
    vector_store: VectorStore,   # noqa: ARG001  # 인터페이스 통일용, 사용 안 함
    graph_store: GraphStore,     # noqa: ARG001  # 인터페이스 통일용, 사용 안 함
) -> SyncResult:
    """단건 페이지 싱크."""
    result = SyncResult()
    page_id = target["page_id"]
    space_key = target["space_key"]
    target_id = target["id"]

    try:
        r = await import_page_via_mcp(session, meta_store, page_id)
        _classify_import_result(result, r)
        await meta_store.upsert_membership(
            target_id=target_id,
            page_id=str(page_id),
            space_key=space_key,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "페이지 임포트 실패 target_id=%s page_id=%s: %s",
            target_id, page_id, exc,
        )
        result.errors.append({"page_id": str(page_id), "error": str(exc)})

    return result


async def _sync_subtree(
    session: ClientSession,
    target: dict[str, Any],
    *,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
) -> SyncResult:
    """서브트리 BFS + 증분 동기화."""
    result = SyncResult()
    root_id = str(target["page_id"])
    space_key = target["space_key"]
    target_id = target["id"]

    try:
        nodes = await walk_subtree(session, root_id)
    except Exception as exc:  # noqa: BLE001
        # walker 전면 실패 시 membership 은 건드리지 않는다.
        logger.warning(
            "서브트리 walker 실패 target_id=%s root=%s: %s",
            target_id, root_id, exc,
        )
        result.errors.append({"page_id": root_id, "error": f"walk_subtree: {exc}"})
        return result

    previous_ids = await meta_store.list_membership_page_ids(target_id)
    current_ids: set[str] = set()

    await _import_nodes_and_upsert(
        session, nodes, space_key, target_id, result, current_ids,
        meta_store=meta_store, with_hierarchy=True,
    )

    await _prune_stale_memberships(
        target_id, previous_ids, current_ids, result,
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
    )
    return result


async def _sync_space(
    session: ClientSession,
    target: dict[str, Any],
    *,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
) -> SyncResult:
    """공간 전체 페이지 열거 + 증분 동기화."""
    result = SyncResult()
    space_key = target["space_key"]
    target_id = target["id"]

    try:
        enumerated: list[dict[str, Any]] = [
            p async for p in enumerate_space_pages(session, space_key)
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "공간 페이지 열거 실패 target_id=%s space=%s: %s",
            target_id, space_key, exc,
        )
        result.errors.append(
            {"page_id": space_key, "error": f"enumerate_space_pages: {exc}"},
        )
        return result

    # enumerate 는 searchContent 결과라 {id, title, ...} 형태 — walker 와 달리
    # parent_id/depth 는 없으므로 hierarchy 저장하지 않는다.
    nodes = [{"id": str(p["id"])} for p in enumerated if p.get("id")]

    previous_ids = await meta_store.list_membership_page_ids(target_id)
    current_ids: set[str] = set()

    await _import_nodes_and_upsert(
        session, nodes, space_key, target_id, result, current_ids,
        meta_store=meta_store, with_hierarchy=False,
    )

    await _prune_stale_memberships(
        target_id, previous_ids, current_ids, result,
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
    )
    return result


async def _import_nodes_and_upsert(
    session: ClientSession,
    nodes: list[dict[str, Any]],
    space_key: str,
    target_id: int,
    result: SyncResult,
    current_ids: set[str],
    *,
    meta_store: MetadataStore,
    with_hierarchy: bool,
) -> None:
    """각 노드를 임포트하고 membership 을 upsert 한다.

    ``current_ids`` 에는 **열거 단계에서 존재가 확인된 모든 페이지**를 포함한다.
    임포트 단계의 일시적 실패가 stale 삭제로 번지지 않도록, 임포트 성공
    여부와 무관하게 walker/enumerate 결과에 포함된 페이지는 current_ids 에 넣는다.
    이후 이전 membership 과의 diff는 "Confluence 쪽에서 사라진 페이지"만 식별한다.
    """
    for node in nodes:
        page_id = str(node["id"])
        # 열거 확인된 페이지는 임포트 성공과 무관하게 "현재 존재"로 간주.
        current_ids.add(page_id)
        try:
            r = await import_page_via_mcp(session, meta_store, page_id)
            _classify_import_result(result, r)
            await meta_store.upsert_membership(
                target_id=target_id,
                page_id=page_id,
                space_key=space_key,
                parent_page_id=(
                    node.get("parent_id") if with_hierarchy else None
                ),
                depth=node.get("depth") if with_hierarchy else None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "페이지 임포트 실패 target_id=%s page_id=%s: %s",
                target_id, page_id, exc,
            )
            result.errors.append({"page_id": page_id, "error": str(exc)})


async def _prune_stale_memberships(
    target_id: int,
    previous_ids: set[str],
    current_ids: set[str],
    result: SyncResult,
    *,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
) -> None:
    """이번 sync에서 사라진 페이지의 membership 을 제거하고 고아 문서를 cascade 삭제."""
    removed_page_ids = previous_ids - current_ids
    if not removed_page_ids:
        return

    orphan_doc_ids = await meta_store.remove_memberships(
        target_id, removed_page_ids,
    )
    for doc_id in orphan_doc_ids:
        await delete_document_cascade(
            doc_id,
            meta_store=meta_store,
            vector_store=vector_store,
            graph_store=graph_store,
        )
        result.removed.append(doc_id)


def _classify_import_result(
    result: SyncResult, import_result: dict[str, Any],
) -> None:
    """``import_page_via_mcp`` 반환값을 created/updated/unchanged 버킷에 분류."""
    doc_id = import_result["id"]
    if import_result.get("created"):
        result.created.append(doc_id)
    elif import_result.get("changed"):
        result.updated.append(doc_id)
    else:
        result.unchanged.append(doc_id)
