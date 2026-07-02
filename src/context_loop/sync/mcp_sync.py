"""Confluence MCP 기반 3-scope 싱크 실행 로직.

``confluence_sync_targets`` 행(scope=page/subtree/space)에 대응하는 sync 경로를
:func:`execute_sync_target` 디스패처가 선택해 실행한다.

- ``page`` scope: 단건 :func:`import_page_via_mcp`, diff 없음.
- ``subtree`` scope: :func:`enumerate_subtree_pages` 로 루트 아래 모든 depth
  의 페이지를 CQL 로 평탄 열거한 뒤 각 페이지를 임포트하고, 이전 membership
  과의 차집합으로 제거된 페이지를 cascade 삭제한다.
- ``space`` scope: :func:`enumerate_space_pages` 로 공간 전체 페이지를
  CQL 페이지네이션으로 나열한 뒤 동일한 증분 로직을 적용한다.

membership 반영은 **임포트 성공 시에만** 수행된다. 열거 자체가 실패하면
membership을 건드리지 않고 반환 — 일시적 Confluence 장애가 기존 문서를
삭제시키지 않도록 하기 위함.

증분 fetch (워터마크):
  subtree/space scope 는 매 싱크마다 전체 페이지 본문을 다시 받아오지 않는다.
  열거는 **ID 목록**만 얻는 작업(페이지당 본문 없음)이라 싸고, 본문 fetch
  (``getPageByID``)는 아래 대상으로 한정한다:

  - **신규**: 열거됐지만 이전 membership 에 없는 페이지. 다른 공간에서
    이동해 오거나 복원된 페이지(lastModified 가 과거일 수 있음)도 이 경로로
    잡힌다.
  - **변경 후보**: ``lastModified >= (워터마크 − 마진)`` CQL 로 서버 측에서
    걸러진 페이지. CQL 필터는 서버에서 적용되므로 searchContent 응답에
    lastModified 필드가 없어도 동작한다.
  - **루트**(subtree 한정): ``ancestor`` CQL 은 루트 자신을 포함하지 않아
    변경 감지가 불가능하므로 항상 fetch 한다.

  워터마크는 Phase 1 임포트가 **오류 없이 완주**했을 때만 싱크 시작 시각으로
  전진한다 — 임포트에 실패한 변경 페이지가 다음 싱크의 변경 조회에서
  누락되지 않도록. 워터마크가 없거나(첫 싱크) 변경 후보 조회가 실패하면
  전체 fetch 로 폴백한다. fetch 여부와 무관하게 최종 재인덱싱 판정은
  기존처럼 content hash 가 담당한다.

stale prune 가드:
  "이전 membership 에 있는데 이번 열거에 없음 = 삭제됨" 추론은 열거가
  완전할 때만 성립한다. 열거가 ``max_pages`` 상한에 걸려 잘렸거나 서버
  ``totalSize`` 보다 적게 돌아온 경우, prune 을 건너뛰고 경고만 남긴다 —
  잘린 열거로 prune 하면 멀쩡한 문서가 대량 삭제/재인덱싱 진동에 빠진다.
  실제 삭제 반영은 다음 완전한 열거 때 이루어진다.

2 단계 구조 (Phase 1 + Phase 2):
  1. 임포트 단계(Phase 1): MCP 에서 본문을 받아 meta 에 저장. 해시 기반 중복
     제거로 ``created``/``updated``/``unchanged`` 분류.
  2. 인덱싱 단계(Phase 2): ``execute_sync_target`` 에 ``embedding_client``/
     ``pipeline_config`` 가 주입된 경우에만 실행. ``created``/``updated``
     문서를 :func:`process_document` 로 처리(청크 → 임베딩 → 그래프). 인덱싱
     실패는 ``processing_errors`` 에 격리되어 Phase 1 결과에 영향 없음.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.embeddings import Embeddings
from mcp import ClientSession

from context_loop.ingestion.mcp_confluence import (
    DEFAULT_ENUMERATION_MAX_PAGES,
    enumerate_space_pages,
    enumerate_subtree_pages,
    estimate_space_page_count,
    estimate_subtree_page_count,
    import_page_via_mcp,
)
from context_loop.processor.llm_client import LLMClient
from context_loop.processor.pipeline import PipelineConfig, process_document
from context_loop.storage.cascade import delete_document_cascade
from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore

logger = logging.getLogger(__name__)


@dataclass
class SyncResult:
    """MCP 싱크 실행 결과 집계.

    Attributes:
        created: Phase 1 — 새로 임포트된 문서 ID.
        updated: Phase 1 — 내용이 변경된 문서 ID.
        unchanged: Phase 1 — fetch 했으나 해시 동일로 건너뛴 문서 ID.
        errors: Phase 1 — 개별 페이지 import 실패 ``{"page_id", "error"}`` 목록.
        removed: Phase 1 — 스코프에서 사라져 cascade 삭제된 문서 ID.
        skipped: Phase 1 — 워터마크 증분 판정으로 **fetch 자체를 생략**한
            페이지 수. ``unchanged`` (fetch 후 해시 동일) 와 달리 MCP 왕복이
            발생하지 않은 건수다.
        processed: Phase 2 — 인덱싱 완료된 문서 ID (created+updated 중 성공한 것).
        processing_errors: Phase 2 — 인덱싱 실패 ``{"doc_id", "error"}`` 목록.
    """

    created: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    unchanged: list[int] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)
    skipped: int = 0
    processed: list[int] = field(default_factory=list)
    processing_errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (
            len(self.created)
            + len(self.updated)
            + len(self.unchanged)
            + len(self.errors)
            + self.skipped
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "errors": self.errors,
            "removed": self.removed,
            "processed": self.processed,
            "processing_errors": self.processing_errors,
            "summary": {
                "created": len(self.created),
                "updated": len(self.updated),
                "unchanged": len(self.unchanged),
                "errors": len(self.errors),
                "removed": len(self.removed),
                "skipped": self.skipped,
                "processed": len(self.processed),
                "processing_errors": len(self.processing_errors),
                "total": self.total,
            },
        }


DEFAULT_PHASE2_CONCURRENCY = 5

DEFAULT_WATERMARK_MARGIN_MINUTES = 24 * 60
"""워터마크 변경 조회에 적용하는 여유 마진(분).

CQL 날짜 리터럴은 Confluence 가 **인증 사용자의 타임존**으로 해석하므로,
우리가 저장한 워터마크(UTC)와 최대 ±14시간 어긋날 수 있다. 기본 24시간
마진은 어떤 타임존 조합에서도 변경 누락이 없도록 하는 안전값이다. 마진
때문에 중복 조회된 페이지는 해시 비교 단계에서 걸러지므로 비용은 하루
변경량만큼의 ``getPageByID`` 재호출뿐이다.
"""

_WATERMARK_FORMAT = "%Y-%m-%d %H:%M"


def _format_watermark(dt: datetime) -> str:
    """datetime 을 CQL 날짜 리터럴 겸 저장 형식으로 변환한다."""
    return dt.strftime(_WATERMARK_FORMAT)


def _watermark_query_since(
    watermark: str | None, margin_minutes: int,
) -> str | None:
    """저장된 워터마크에서 변경 조회용 ``lastModified`` 하한 문자열을 만든다.

    ``None`` 반환은 "증분 조회 불가 — 전체 fetch" 를 뜻한다: 워터마크가
    없거나(첫 싱크) 저장값이 파싱되지 않을 때.
    """
    if not watermark:
        return None
    try:
        parsed = datetime.strptime(watermark, _WATERMARK_FORMAT)
    except ValueError:
        logger.warning("워터마크 파싱 실패 — 전체 fetch 로 폴백: %r", watermark)
        return None
    return _format_watermark(parsed - timedelta(minutes=max(0, margin_minutes)))


async def execute_sync_target(
    session: ClientSession,
    target: dict[str, Any],
    *,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
    embedding_client: Embeddings | None = None,
    llm_client: LLMClient | None = None,
    pipeline_config: PipelineConfig | None = None,
    phase2_concurrency: int = DEFAULT_PHASE2_CONCURRENCY,
    watermark_margin_minutes: int = DEFAULT_WATERMARK_MARGIN_MINUTES,
    enumeration_max_pages: int = DEFAULT_ENUMERATION_MAX_PAGES,
) -> SyncResult:
    """싱크 대상 행의 scope 에 따라 올바른 sync 경로로 위임한다.

    Args:
        session: 초기화된 MCP ClientSession.
        target: ``confluence_sync_targets`` 한 행 (dict). 최소 ``id``,
            ``scope``, ``space_key``, ``page_id`` 키가 필요하다. 선택적으로
            ``last_watermark`` (증분 fetch 기준) 를 읽는다.
        meta_store: 초기화된 MetadataStore.
        vector_store: 초기화된 VectorStore.
        graph_store: 초기화된 GraphStore.
        embedding_client: 선택적. 주입되면 Phase 2(인덱싱)가 자동 실행된다.
            주입되지 않으면 Phase 1(임포트) 까지만 수행하고 반환 — 기존 동작.
        pipeline_config: 선택적. Phase 2 에서 :func:`process_document` 에
            전달할 설정. 기본값은 :class:`PipelineConfig` 기본값.
        phase2_concurrency: Phase 2 동시 처리 문서 수 상한. 기본 5 —
            일반 OpenAI 계정 rate limit 에 여유 있으면서 직렬 대비 약 5배
            단축. 1 로 설정하면 완전 직렬.
        watermark_margin_minutes: 워터마크 변경 조회 마진(분). 모듈 상수
            :data:`DEFAULT_WATERMARK_MARGIN_MINUTES` 참조.
        enumeration_max_pages: 열거 안전 상한. 이 값에 걸려 열거가 잘리면
            해당 싱크의 stale prune 은 생략된다.

    Returns:
        :class:`SyncResult` 집계. ``embedding_client`` 가 주입된 경우
        ``processed``/``processing_errors`` 가 채워진다.

    Raises:
        ValueError: ``scope`` 가 알려진 값(page/subtree/space)이 아닐 때.
    """
    scope = target.get("scope")
    if scope == "page":
        result = await _sync_page(
            session, target,
            meta_store=meta_store,
            vector_store=vector_store,
            graph_store=graph_store,
        )
    elif scope == "subtree":
        result = await _sync_subtree(
            session, target,
            meta_store=meta_store,
            vector_store=vector_store,
            graph_store=graph_store,
            watermark_margin_minutes=watermark_margin_minutes,
            enumeration_max_pages=enumeration_max_pages,
        )
    elif scope == "space":
        result = await _sync_space(
            session, target,
            meta_store=meta_store,
            vector_store=vector_store,
            graph_store=graph_store,
            watermark_margin_minutes=watermark_margin_minutes,
            enumeration_max_pages=enumeration_max_pages,
        )
    else:
        raise ValueError(f"Unknown sync target scope: {scope!r}")

    # Phase 2: 인덱싱. embedding_client 가 주입된 경우에만 실행.
    if embedding_client is not None:
        await _run_processing_phase(
            result,
            target_id=int(target["id"]),
            meta_store=meta_store,
            vector_store=vector_store,
            graph_store=graph_store,
            embedding_client=embedding_client,
            llm_client=llm_client,
            pipeline_config=pipeline_config,
            concurrency=phase2_concurrency,
        )

    return result


async def _run_processing_phase(
    result: SyncResult,
    *,
    target_id: int,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
    embedding_client: Embeddings,
    llm_client: LLMClient | None,
    pipeline_config: PipelineConfig | None,
    concurrency: int,
) -> None:
    """Phase 1 결과의 created/updated + 기존 failed/degraded 문서를 인덱싱.

    처리 대상:
      - ``result.created`` + ``result.updated`` — 이번 싱크에서 신규·변경 감지된 문서
      - Target 의 membership 에 속한 ``status='failed'`` 기존 문서 — 지난 번
        인덱싱 실패를 재싱크 시 자동 재시도 (본문 해시는 그대로라도 인덱싱만
        다시 시도). :meth:`MetadataStore.list_failed_member_doc_ids` 로 식별.
      - Target 의 membership 에 속한 ``llm_degraded=1`` 기존 문서 — 생성형 LLM
        단계(가상 질문/본문 그래프) 결손으로 검색 품질이 저하된 채
        ``status='completed'`` 로 마감된 문서. 재싱크 시 자동으로 재인덱싱을
        시도해 그래프·질문 view 를 복구한다.
        :meth:`MetadataStore.list_degraded_member_doc_ids` 로 식별.

    처리 제외:
      - ``result.unchanged`` — 내용이 그대로면 재임베딩은 낭비.

    **동시성**: ``asyncio.Semaphore(concurrency)`` 로 바운드된 병렬 실행. 문서별
    실패는 격리되어 ``result.processing_errors`` 에 누적되고 다른 문서 인덱싱을
    막지 않는다. 실패 문서는 ``meta.status='failed'`` 로 마킹되어 다음 재싱크에
    자동 재시도 대상이 된다.

    결과 리스트(``result.processed``, ``result.processing_errors``) 에 append
    되는 순서는 **완료 순** 이므로 created/updated 입력 순서와 다를 수 있다.
    """
    primary = list(result.created) + list(result.updated)
    try:
        failed_retries = await meta_store.list_failed_member_doc_ids(target_id)
    except Exception:  # noqa: BLE001
        # failed 재시도 식별이 실패해도 primary 는 계속 처리.
        logger.debug(
            "list_failed_member_doc_ids 실패 target_id=%s", target_id, exc_info=True,
        )
        failed_retries = []

    try:
        degraded_retries = await meta_store.list_degraded_member_doc_ids(target_id)
    except Exception:  # noqa: BLE001
        # degraded 재시도 식별이 실패해도 primary/failed 는 계속 처리.
        logger.debug(
            "list_degraded_member_doc_ids 실패 target_id=%s", target_id, exc_info=True,
        )
        degraded_retries = []

    # 중복 제거 (created/updated 와 겹치면 primary 우선 — 위치는 크게 중요치 않음).
    seen: set[int] = set()
    to_process: list[int] = []
    for doc_id in primary + failed_retries + degraded_retries:
        if doc_id in seen:
            continue
        seen.add(doc_id)
        to_process.append(doc_id)

    if not to_process:
        return

    config = pipeline_config or PipelineConfig()
    effective_concurrency = max(1, concurrency)
    sem = asyncio.Semaphore(effective_concurrency)

    async def _process_one(doc_id: int) -> None:
        async with sem:
            try:
                await process_document(
                    doc_id,
                    meta_store=meta_store,
                    vector_store=vector_store,
                    graph_store=graph_store,
                    embedding_client=embedding_client,
                    config=config,
                    llm_client=llm_client,
                )
                result.processed.append(doc_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("문서 인덱싱 실패 doc_id=%s: %s", doc_id, exc)
                result.processing_errors.append({
                    "doc_id": doc_id, "error": str(exc),
                })
                # 실패 표식 — 다음 재싱크에서 자동 재시도 대상.
                try:
                    await meta_store.update_document_status(doc_id, "failed")
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "status=failed 업데이트 실패 doc_id=%s",
                        doc_id, exc_info=True,
                    )

    await asyncio.gather(*(_process_one(d) for d in to_process))


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


def _dedupe_ids(pages: list[dict[str, Any]]) -> list[str]:
    """열거 결과에서 순서를 보존하며 중복 없는 page_id 문자열 목록을 만든다."""
    ids: list[str] = []
    seen: set[str] = set()
    for page in pages:
        pid = page.get("id")
        if not pid:
            continue
        pid_str = str(pid)
        if pid_str in seen:
            continue
        seen.add(pid_str)
        ids.append(pid_str)
    return ids


async def _sync_subtree(
    session: ClientSession,
    target: dict[str, Any],
    *,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
    watermark_margin_minutes: int = DEFAULT_WATERMARK_MARGIN_MINUTES,
    enumeration_max_pages: int = DEFAULT_ENUMERATION_MAX_PAGES,
) -> SyncResult:
    """서브트리 전체(루트 + 모든 후손)를 CQL 평탄 열거로 증분 동기화.

    CQL ``ancestor = ROOT AND type = "page"`` 로 depth 제한 없이 모든 후손
    페이지를 서버 측 권위로 나열한다. per-parent ``getChild`` BFS 를 거치는
    기존 ``walk_subtree`` 경로와 달리 자식 페이지네이션 오류·중간 노드
    예외·``type`` 필드 누락 등에 의한 누락이 없다. ``ancestor`` 는 루트 자신을
    포함하지 않으므로 첫 노드로 수동 추가한다.

    본문 fetch 는 증분 (모듈 docstring "증분 fetch" 참조): 루트(변경 감지
    불가라 항상) + 신규 + ``lastModified`` 변경 후보만 ``getPageByID`` 한다.
    """
    result = SyncResult()
    root_id = str(target["page_id"])
    space_key = target["space_key"]
    target_id = target["id"]
    sync_started_at = datetime.now(tz=UTC)

    try:
        expected_descendants: int | None = await estimate_subtree_page_count(
            session, root_id,
        )
    except Exception as exc:  # noqa: BLE001
        # 예상치 확인은 정보성이므로 실패해도 본 열거를 계속 시도한다.
        logger.debug(
            "estimate_subtree_page_count 실패 target_id=%s root=%s: %s",
            target_id, root_id, exc,
        )
        expected_descendants = None

    try:
        descendants: list[dict[str, Any]] = [
            p async for p in enumerate_subtree_pages(
                session, root_id, max_pages=enumeration_max_pages,
            )
        ]
    except Exception as exc:  # noqa: BLE001
        # 열거 전면 실패 시 membership 은 건드리지 않는다.
        logger.warning(
            "서브트리 열거 실패 target_id=%s root=%s: %s",
            target_id, root_id, exc,
        )
        result.errors.append(
            {"page_id": root_id, "error": f"enumerate_subtree_pages: {exc}"},
        )
        return result

    # 루트 + 후손 (dedupe, 루트 우선). CQL 결과에는 parent_id/depth 가 없으므로
    # hierarchy 저장은 안 함.
    descendant_ids = [
        pid for pid in _dedupe_ids(descendants) if pid != root_id
    ]
    all_ids = [root_id, *descendant_ids]

    # 서버 totalSize 대비 실제 열거 수 비교 — 누락 탐지 관측성.
    if (
        expected_descendants is not None
        and len(descendant_ids) != expected_descendants
    ):
        logger.warning(
            "서브트리 열거 개수 불일치 target_id=%s root=%s: "
            "expected=%d actual=%d",
            target_id, root_id, expected_descendants, len(descendant_ids),
        )

    enumeration_complete = (
        len(descendants) < enumeration_max_pages
        and (
            expected_descendants is None
            or len(descendant_ids) >= expected_descendants
        )
    )

    async def _changed_descendants(since: str) -> set[str]:
        pages = [
            p async for p in enumerate_subtree_pages(
                session, root_id,
                max_pages=enumeration_max_pages, modified_since=since,
            )
        ]
        return set(_dedupe_ids(pages))

    await _import_incremental_and_prune(
        session, result,
        target_id=target_id,
        space_key=space_key,
        all_ids=all_ids,
        # ancestor CQL 은 루트를 포함하지 않아 루트의 변경이 변경 후보 조회에
        # 잡히지 않는다 — 루트는 항상 fetch (싱크당 getPageByID 1회 추가).
        always_fetch_ids={root_id},
        changed_enumerator=_changed_descendants,
        enumeration_complete=enumeration_complete,
        watermark=target.get("last_watermark"),
        watermark_margin_minutes=watermark_margin_minutes,
        sync_started_at=sync_started_at,
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
    watermark_margin_minutes: int = DEFAULT_WATERMARK_MARGIN_MINUTES,
    enumeration_max_pages: int = DEFAULT_ENUMERATION_MAX_PAGES,
) -> SyncResult:
    """공간 전체 페이지 열거 + 증분 동기화.

    본문 fetch 는 증분 (모듈 docstring "증분 fetch" 참조): 신규 +
    ``lastModified`` 변경 후보만 ``getPageByID`` 한다.
    """
    result = SyncResult()
    space_key = target["space_key"]
    target_id = target["id"]
    sync_started_at = datetime.now(tz=UTC)

    try:
        expected_total: int | None = await estimate_space_page_count(
            session, space_key,
        )
    except Exception as exc:  # noqa: BLE001
        # 예상치 확인은 정보성 + prune 가드용이므로 실패해도 열거는 계속한다.
        logger.debug(
            "estimate_space_page_count 실패 target_id=%s space=%s: %s",
            target_id, space_key, exc,
        )
        expected_total = None

    try:
        enumerated: list[dict[str, Any]] = [
            p async for p in enumerate_space_pages(
                session, space_key, max_pages=enumeration_max_pages,
            )
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
    all_ids = _dedupe_ids(enumerated)

    if expected_total is not None and len(all_ids) != expected_total:
        logger.warning(
            "공간 열거 개수 불일치 target_id=%s space=%s: expected=%d actual=%d",
            target_id, space_key, expected_total, len(all_ids),
        )

    enumeration_complete = (
        len(enumerated) < enumeration_max_pages
        and (expected_total is None or len(all_ids) >= expected_total)
    )

    async def _changed_pages(since: str) -> set[str]:
        pages = [
            p async for p in enumerate_space_pages(
                session, space_key,
                max_pages=enumeration_max_pages, modified_since=since,
            )
        ]
        return set(_dedupe_ids(pages))

    await _import_incremental_and_prune(
        session, result,
        target_id=target_id,
        space_key=space_key,
        all_ids=all_ids,
        always_fetch_ids=set(),
        changed_enumerator=_changed_pages,
        enumeration_complete=enumeration_complete,
        watermark=target.get("last_watermark"),
        watermark_margin_minutes=watermark_margin_minutes,
        sync_started_at=sync_started_at,
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
    )
    return result


async def _import_incremental_and_prune(
    session: ClientSession,
    result: SyncResult,
    *,
    target_id: int,
    space_key: str,
    all_ids: list[str],
    always_fetch_ids: set[str],
    changed_enumerator: Callable[[str], Awaitable[set[str]]],
    enumeration_complete: bool,
    watermark: str | None,
    watermark_margin_minutes: int,
    sync_started_at: datetime,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
) -> None:
    """subtree/space 공통: 증분 fetch 대상 선정 → 임포트 → prune → 워터마크.

    fetch 대상 = ``always_fetch_ids`` ∪ 신규(이전 membership 에 없음) ∪
    변경 후보(``changed_enumerator``). 워터마크가 없거나 변경 후보 조회가
    실패하면 전체 fetch 로 폴백한다.

    fetch 를 생략한 페이지도 membership ``last_seen_at`` 은 배치로 갱신해
    "이번 열거에서 존재 확인됨" 상태를 유지한다.

    prune 은 ``enumeration_complete`` 일 때만 수행 — 잘린 열거로 diff 하면
    멀쩡한 문서가 삭제되기 때문 (모듈 docstring "stale prune 가드" 참조).

    워터마크는 Phase 1 임포트가 오류 없이 끝난 경우에만 ``sync_started_at``
    으로 전진한다. 임포트 오류가 있으면 전진하지 않는다 — 변경 감지가
    membership 부재(신규 경로)로 커버되지 않는 "기존 문서의 변경" 이 임포트
    실패 시 다음 싱크에서 누락되는 것을 막기 위함.
    """
    previous_ids = await meta_store.list_membership_page_ids(target_id)
    # 열거 단계에서 존재가 확인된 모든 페이지 — 임포트/스킵 여부와 무관하게
    # "현재 존재" 로 간주해 stale diff 가 임포트 실패에 오염되지 않도록 한다.
    current_ids = set(all_ids)

    changed_ids: set[str] | None = None
    since = _watermark_query_since(watermark, watermark_margin_minutes)
    if since is not None:
        try:
            changed_ids = await changed_enumerator(since)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "변경 후보 조회 실패 — 전체 fetch 로 폴백 target_id=%s: %s",
                target_id, exc,
            )
            changed_ids = None

    if changed_ids is None:
        fetch_ids = list(all_ids)
    else:
        fetch_ids = [
            pid for pid in all_ids
            if pid in always_fetch_ids
            or pid not in previous_ids
            or pid in changed_ids
        ]

    fetch_set = set(fetch_ids)
    skipped_ids = [pid for pid in all_ids if pid not in fetch_set]
    result.skipped = len(skipped_ids)
    if skipped_ids:
        logger.info(
            "증분 fetch target_id=%s: %d/%d 페이지 fetch 생략 (워터마크 %s)",
            target_id, len(skipped_ids), len(all_ids), watermark,
        )

    await _import_nodes_and_upsert(
        session, [{"id": pid} for pid in fetch_ids], space_key, target_id,
        result, current_ids,
        meta_store=meta_store, with_hierarchy=False,
    )

    # fetch 생략 페이지의 membership last_seen_at 갱신 — 존재 확인 기록.
    if skipped_ids:
        try:
            await meta_store.upsert_membership_batch(
                target_id, space_key, [{"id": pid} for pid in skipped_ids],
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "skipped membership 갱신 실패 target_id=%s",
                target_id, exc_info=True,
            )

    if enumeration_complete:
        await _prune_stale_memberships(
            target_id, previous_ids, current_ids, result,
            meta_store=meta_store,
            vector_store=vector_store,
            graph_store=graph_store,
        )
    else:
        logger.warning(
            "열거 불완전 — stale prune 생략 target_id=%s enumerated=%d "
            "(잘린 열거로 diff 하면 존재하는 문서가 삭제될 수 있음)",
            target_id, len(current_ids),
        )

    if not result.errors:
        try:
            await meta_store.update_sync_watermark(
                target_id, _format_watermark(sync_started_at),
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "워터마크 갱신 실패 target_id=%s", target_id, exc_info=True,
            )
    elif watermark is not None:
        logger.info(
            "임포트 오류 %d건 — 워터마크 유지 target_id=%s (다음 싱크에서 재조회)",
            len(result.errors), target_id,
        )


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
