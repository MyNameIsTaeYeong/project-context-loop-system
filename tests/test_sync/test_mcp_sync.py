"""mcp_sync.execute_sync_target + 3-scope 싱크 로직 테스트."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore
from context_loop.sync import mcp_sync
from context_loop.sync.mcp_sync import SyncResult, execute_sync_target


# --- Fixtures ---


@pytest.fixture
async def stores(tmp_path: Path):  # type: ignore[misc]
    meta = MetadataStore(tmp_path / "test.db")
    await meta.initialize()
    vec = VectorStore(tmp_path)
    vec.initialize()
    graph = GraphStore(meta)
    yield meta, vec, graph
    await meta.close()


# --- Fakes ---


def _make_fake_importer(fail_pages: set[str] | None = None):
    """``import_page_via_mcp`` 를 대체하는 async 함수.

    실제 ``confluence_mcp`` 문서를 DB에 생성/조회해 orphan GC 로직이 realistic 하게
    동작하도록 한다. ``fail_pages`` 에 포함된 page_id 는 예외를 던진다.
    """
    fail = fail_pages or set()

    async def fake(
        session: Any,
        store: MetadataStore,
        page_id: str,
    ) -> dict[str, Any]:
        pid = str(page_id)
        if pid in fail:
            raise RuntimeError(f"simulated import failure: {pid}")

        existing = await store.list_documents(source_type="confluence_mcp")
        match = next((d for d in existing if d.get("source_id") == pid), None)
        if match is not None:
            return {**match, "created": False, "changed": False}

        doc_id = await store.create_document(
            source_type="confluence_mcp",
            source_id=pid,
            title=f"Page {pid}",
            original_content="content",
            content_hash=f"hash-{pid}",
        )
        doc = await store.get_document(doc_id)
        assert doc is not None
        return {**doc, "created": True, "changed": True}

    return fake


def _make_fake_subtree_enum(descendants: list[dict[str, Any]]):
    """CQL ``ancestor`` 평탄 열거를 흉내내는 async generator fake.

    CQL 응답에는 루트 자신이 포함되지 않으므로 descendants 만 전달한다 —
    ``_sync_subtree`` 가 루트를 별도로 prepend 한다.
    """

    async def fake(session: Any, ancestor_page_id: str, **_: Any):
        for p in descendants:
            yield p

    return fake


def _make_failing_subtree_enum():
    async def fake(session: Any, ancestor_page_id: str, **_: Any):
        raise RuntimeError("enumerate blew up")
        yield  # 제너레이터 마커 — 실제로 도달 안 함

    return fake


def _make_fake_enumerate(pages: list[dict[str, Any]]):
    async def fake(session: Any, space_key: str, **_: Any):
        for p in pages:
            yield p
    return fake


def _make_failing_enumerate():
    async def fake(session: Any, space_key: str, **_: Any):
        raise RuntimeError("enumerate blew up")
        yield  # 제너레이터 마커 — 실제로 도달 안 함
    return fake


# --- Dispatcher ---


async def test_execute_sync_target_rejects_unknown_scope(stores) -> None:
    meta, vec, graph = stores
    with pytest.raises(ValueError, match="Unknown sync target scope"):
        await execute_sync_target(
            None,
            {"id": 1, "scope": "something", "space_key": "ENG", "page_id": None},
            meta_store=meta, vector_store=vec, graph_store=graph,
        )


# --- page scope ---


async def test_sync_page_creates_doc_and_membership(stores, monkeypatch) -> None:
    meta, vec, graph = stores
    monkeypatch.setattr(mcp_sync, "import_page_via_mcp", _make_fake_importer())

    t = await meta.upsert_sync_target(
        scope="page", space_key="ENG", page_id="100", name="P",
    )
    result = await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
    )

    assert len(result.created) == 1
    assert result.updated == []
    assert result.unchanged == []
    assert result.errors == []
    assert result.removed == []
    assert await meta.list_membership_page_ids(t["id"]) == {"100"}


async def test_sync_page_unchanged_on_second_call(stores, monkeypatch) -> None:
    meta, vec, graph = stores
    monkeypatch.setattr(mcp_sync, "import_page_via_mcp", _make_fake_importer())

    t = await meta.upsert_sync_target(
        scope="page", space_key="ENG", page_id="100", name="P",
    )
    await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
    )
    result = await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
    )

    assert result.created == []
    assert len(result.unchanged) == 1


async def test_sync_page_import_error_recorded_without_membership(
    stores, monkeypatch,
) -> None:
    meta, vec, graph = stores
    monkeypatch.setattr(
        mcp_sync, "import_page_via_mcp", _make_fake_importer(fail_pages={"100"}),
    )

    t = await meta.upsert_sync_target(
        scope="page", space_key="ENG", page_id="100", name="P",
    )
    result = await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
    )

    assert len(result.errors) == 1
    assert result.errors[0]["page_id"] == "100"
    assert await meta.list_membership_page_ids(t["id"]) == set()


# --- subtree scope ---


async def test_sync_subtree_imports_root_and_all_descendants_flat(
    stores, monkeypatch,
) -> None:
    """CQL ``ancestor`` 평탄 열거로 루트 + 모든 depth 후손이 임포트된다.

    CQL 결과에는 parent/depth 가 없으므로 membership 은 flat 으로 저장된다
    (``parent_page_id``/``depth`` 는 모두 ``NULL``). 이는 누락 제거를 위한
    의도된 trade-off 다.
    """
    meta, vec, graph = stores
    monkeypatch.setattr(mcp_sync, "import_page_via_mcp", _make_fake_importer())
    monkeypatch.setattr(
        mcp_sync, "enumerate_subtree_pages",
        _make_fake_subtree_enum([
            {"id": "200", "title": "A"},
            {"id": "201", "title": "B"},
            {"id": "300", "title": "A1"},
        ]),
    )

    t = await meta.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    result = await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
    )

    assert len(result.created) == 4   # root + 3 descendants
    assert await meta.list_membership_page_ids(t["id"]) == {
        "100", "200", "201", "300",
    }

    # CQL 경로에서는 hierarchy 를 저장하지 않는다 — 모두 NULL.
    cur = await meta.db.execute(
        "SELECT page_id, parent_page_id, depth FROM confluence_sync_membership "
        "WHERE target_id = ?",
        (t["id"],),
    )
    rows = {r["page_id"]: dict(r) for r in await cur.fetchall()}
    for pid in ("100", "200", "201", "300"):
        assert rows[pid]["parent_page_id"] is None
        assert rows[pid]["depth"] is None


async def test_sync_subtree_deduplicates_root_if_returned_by_cql(
    stores, monkeypatch,
) -> None:
    """만약 CQL 이 루트 자신을 결과에 섞어 돌려줘도 중복으로 임포트하지 않는다."""
    meta, vec, graph = stores
    monkeypatch.setattr(mcp_sync, "import_page_via_mcp", _make_fake_importer())
    monkeypatch.setattr(
        mcp_sync, "enumerate_subtree_pages",
        _make_fake_subtree_enum([
            {"id": "100"},  # 루트 자신 — 무시되어야 함
            {"id": "200"},
        ]),
    )

    t = await meta.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    result = await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
    )

    assert len(result.created) == 2   # 100, 200 만
    assert await meta.list_membership_page_ids(t["id"]) == {"100", "200"}


async def test_sync_subtree_enumerate_failure_preserves_membership(
    stores, monkeypatch,
) -> None:
    """CQL 열거 전체 실패 시 기존 membership 이 그대로 보존되어야 한다."""
    meta, vec, graph = stores
    monkeypatch.setattr(mcp_sync, "import_page_via_mcp", _make_fake_importer())

    t = await meta.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    # 이전 sync 에서 있었다고 가정하고 membership 을 미리 심는다.
    await meta.upsert_membership(
        target_id=t["id"], page_id="100", space_key="ENG",
    )
    await meta.upsert_membership(
        target_id=t["id"], page_id="200", space_key="ENG",
    )

    monkeypatch.setattr(
        mcp_sync, "enumerate_subtree_pages", _make_failing_subtree_enum(),
    )

    result = await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
    )

    assert len(result.errors) == 1
    assert "enumerate_subtree_pages" in result.errors[0]["error"]
    # 기존 membership 이 그대로 있는지 확인
    assert await meta.list_membership_page_ids(t["id"]) == {"100", "200"}
    assert result.removed == []


async def test_sync_subtree_stale_page_triggers_cascade_delete(
    stores, monkeypatch,
) -> None:
    """이전 sync 에 있었으나 이번에 없는 페이지는 cascade 삭제된다."""
    meta, vec, graph = stores
    monkeypatch.setattr(mcp_sync, "import_page_via_mcp", _make_fake_importer())

    t = await meta.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )

    # 첫 sync: 100 + 200 + 201 포함
    monkeypatch.setattr(
        mcp_sync, "enumerate_subtree_pages",
        _make_fake_subtree_enum([{"id": "200"}, {"id": "201"}]),
    )
    await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
    )
    assert await meta.list_membership_page_ids(t["id"]) == {"100", "200", "201"}

    # 두 번째 sync: 201 이 사라짐
    monkeypatch.setattr(
        mcp_sync, "enumerate_subtree_pages",
        _make_fake_subtree_enum([{"id": "200"}]),
    )
    result = await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
    )

    assert len(result.removed) == 1
    assert await meta.list_membership_page_ids(t["id"]) == {"100", "200"}
    # 문서 자체도 cascade 삭제됐는지 확인
    docs = await meta.list_documents(source_type="confluence_mcp")
    page_ids = {d["source_id"] for d in docs}
    assert page_ids == {"100", "200"}


async def test_sync_subtree_import_failure_does_not_cause_stale_deletion(
    stores, monkeypatch,
) -> None:
    """열거 단계에서 본 페이지의 import 가 실패해도 기존 membership 은 유지된다.

    일시적 import 실패가 cascade 삭제로 번지지 않는 안전 속성 검증.
    """
    meta, vec, graph = stores
    monkeypatch.setattr(mcp_sync, "import_page_via_mcp", _make_fake_importer())

    t = await meta.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    monkeypatch.setattr(
        mcp_sync, "enumerate_subtree_pages",
        _make_fake_subtree_enum([{"id": "200"}]),
    )
    await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
    )
    assert await meta.list_membership_page_ids(t["id"]) == {"100", "200"}

    # 두 번째 sync 에서 200 import 가 일시 실패
    monkeypatch.setattr(
        mcp_sync, "import_page_via_mcp",
        _make_fake_importer(fail_pages={"200"}),
    )
    result = await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
    )

    assert any(e["page_id"] == "200" for e in result.errors)
    # 200 은 기존 membership 이 유지되어야 함
    assert await meta.list_membership_page_ids(t["id"]) == {"100", "200"}
    assert result.removed == []
    # 문서도 그대로 존재
    docs = await meta.list_documents(source_type="confluence_mcp")
    assert {d["source_id"] for d in docs} == {"100", "200"}


# --- space scope ---


async def test_sync_space_imports_all_pages_without_hierarchy(
    stores, monkeypatch,
) -> None:
    meta, vec, graph = stores
    monkeypatch.setattr(mcp_sync, "import_page_via_mcp", _make_fake_importer())
    monkeypatch.setattr(
        mcp_sync, "enumerate_space_pages",
        _make_fake_enumerate([
            {"id": "1", "title": "A"},
            {"id": "2", "title": "B"},
            {"id": "3", "title": "C"},
        ]),
    )

    t = await meta.upsert_sync_target(
        scope="space", space_key="ENG", page_id=None, name="Engineering",
    )
    result = await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
    )

    assert len(result.created) == 3
    assert await meta.list_membership_page_ids(t["id"]) == {"1", "2", "3"}

    # space scope 는 hierarchy 를 저장하지 않는다 — parent/depth 모두 NULL
    cur = await meta.db.execute(
        "SELECT page_id, parent_page_id, depth FROM confluence_sync_membership "
        "WHERE target_id = ?",
        (t["id"],),
    )
    for row in await cur.fetchall():
        assert row["parent_page_id"] is None
        assert row["depth"] is None


async def test_sync_space_enumerate_failure_preserves_membership(
    stores, monkeypatch,
) -> None:
    meta, vec, graph = stores
    monkeypatch.setattr(mcp_sync, "import_page_via_mcp", _make_fake_importer())

    t = await meta.upsert_sync_target(
        scope="space", space_key="ENG", page_id=None, name="Engineering",
    )
    await meta.upsert_membership(
        target_id=t["id"], page_id="1", space_key="ENG",
    )

    monkeypatch.setattr(mcp_sync, "enumerate_space_pages", _make_failing_enumerate())

    result = await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
    )

    assert len(result.errors) == 1
    assert "enumerate_space_pages" in result.errors[0]["error"]
    assert await meta.list_membership_page_ids(t["id"]) == {"1"}


async def test_sync_space_stale_removal(stores, monkeypatch) -> None:
    meta, vec, graph = stores
    monkeypatch.setattr(mcp_sync, "import_page_via_mcp", _make_fake_importer())

    t = await meta.upsert_sync_target(
        scope="space", space_key="ENG", page_id=None, name="Engineering",
    )

    monkeypatch.setattr(
        mcp_sync, "enumerate_space_pages",
        _make_fake_enumerate([{"id": "1"}, {"id": "2"}, {"id": "3"}]),
    )
    await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
    )

    # 다음 sync에서 2가 사라짐
    monkeypatch.setattr(
        mcp_sync, "enumerate_space_pages",
        _make_fake_enumerate([{"id": "1"}, {"id": "3"}]),
    )
    result = await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
    )

    assert len(result.removed) == 1
    assert await meta.list_membership_page_ids(t["id"]) == {"1", "3"}


# --- 중복 소유 (shared ownership) 시나리오 ---


async def test_shared_page_kept_when_one_target_loses_it(
    stores, monkeypatch,
) -> None:
    """subtree 와 space 가 같은 페이지를 공유할 때, 한쪽 sync 에서
    사라지더라도 다른 쪽이 여전히 소유하므로 문서는 유지된다."""
    meta, vec, graph = stores
    monkeypatch.setattr(mcp_sync, "import_page_via_mcp", _make_fake_importer())

    t_space = await meta.upsert_sync_target(
        scope="space", space_key="ENG", page_id=None, name="Space",
    )
    t_sub = await meta.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )

    # space 와 subtree 모두 200 을 포함
    monkeypatch.setattr(
        mcp_sync, "enumerate_space_pages",
        _make_fake_enumerate([{"id": "100"}, {"id": "200"}]),
    )
    await execute_sync_target(
        None, t_space, meta_store=meta, vector_store=vec, graph_store=graph,
    )

    monkeypatch.setattr(
        mcp_sync, "enumerate_subtree_pages",
        _make_fake_subtree_enum([{"id": "200"}]),
    )
    await execute_sync_target(
        None, t_sub, meta_store=meta, vector_store=vec, graph_store=graph,
    )

    assert await meta.list_membership_page_ids(t_space["id"]) == {"100", "200"}
    assert await meta.list_membership_page_ids(t_sub["id"]) == {"100", "200"}

    # 이제 subtree 에서 200 이 사라짐 (CQL 결과에서 빠짐)
    monkeypatch.setattr(
        mcp_sync, "enumerate_subtree_pages",
        _make_fake_subtree_enum([]),
    )
    result = await execute_sync_target(
        None, t_sub, meta_store=meta, vector_store=vec, graph_store=graph,
    )

    # subtree 의 membership 에서는 200 이 제거됨
    assert await meta.list_membership_page_ids(t_sub["id"]) == {"100"}
    # 하지만 space 가 여전히 소유하므로 cascade 삭제되지 않음
    assert result.removed == []
    docs = await meta.list_documents(source_type="confluence_mcp")
    assert "200" in {d["source_id"] for d in docs}


async def test_shared_page_deleted_when_both_targets_lose_it(
    stores, monkeypatch,
) -> None:
    """공유되던 페이지를 모든 target 이 놓으면 문서가 삭제된다."""
    meta, vec, graph = stores
    monkeypatch.setattr(mcp_sync, "import_page_via_mcp", _make_fake_importer())

    t_space = await meta.upsert_sync_target(
        scope="space", space_key="ENG", page_id=None, name="Space",
    )
    t_sub = await meta.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )

    monkeypatch.setattr(
        mcp_sync, "enumerate_space_pages",
        _make_fake_enumerate([{"id": "100"}, {"id": "200"}]),
    )
    await execute_sync_target(
        None, t_space, meta_store=meta, vector_store=vec, graph_store=graph,
    )
    monkeypatch.setattr(
        mcp_sync, "enumerate_subtree_pages",
        _make_fake_subtree_enum([{"id": "200"}]),
    )
    await execute_sync_target(
        None, t_sub, meta_store=meta, vector_store=vec, graph_store=graph,
    )

    # space 와 subtree 양쪽 모두에서 200 제거
    monkeypatch.setattr(
        mcp_sync, "enumerate_space_pages",
        _make_fake_enumerate([{"id": "100"}]),
    )
    r_space = await execute_sync_target(
        None, t_space, meta_store=meta, vector_store=vec, graph_store=graph,
    )
    # space 가 먼저 놓았지만 subtree 가 아직 소유 → 미삭제
    assert r_space.removed == []

    monkeypatch.setattr(
        mcp_sync, "enumerate_subtree_pages",
        _make_fake_subtree_enum([]),
    )
    r_sub = await execute_sync_target(
        None, t_sub, meta_store=meta, vector_store=vec, graph_store=graph,
    )
    # subtree 까지 놓으면 이제 완전히 고아 → cascade 삭제
    assert len(r_sub.removed) == 1
    docs = await meta.list_documents(source_type="confluence_mcp")
    assert "200" not in {d["source_id"] for d in docs}


# --- SyncResult to_dict ---


def test_sync_result_to_dict_summary() -> None:
    r = SyncResult()
    r.created = [1, 2]
    r.updated = [3]
    r.unchanged = [4, 5, 6]
    r.errors = [{"page_id": "x", "error": "boom"}]
    r.removed = [7]
    r.processed = [1, 2, 3]
    r.processing_errors = [{"doc_id": 9, "error": "embed failed"}]

    d = r.to_dict()
    assert d["summary"] == {
        "created": 2, "updated": 1, "unchanged": 3,
        "errors": 1, "removed": 1,
        "processed": 3, "processing_errors": 1,
        "total": 7,
    }
    assert d["removed"] == [7]
    assert d["processed"] == [1, 2, 3]


# --- Phase 2 (인덱싱) ---


def _make_recording_process_document(
    fail_docs: set[int] | None = None,
) -> tuple[list[int], Any]:
    """``process_document`` 를 대체하는 기록용 fake + 호출된 doc_id 누적 리스트.

    ``fail_docs`` 의 doc_id 는 예외를 던지도록 설정해 Phase 2 실패 격리를 검증한다.
    """
    fail = fail_docs or set()
    called: list[int] = []

    async def fake(
        document_id: int,
        *,
        meta_store: Any,
        vector_store: Any,
        graph_store: Any,
        embedding_client: Any,
        config: Any = None,
    ) -> dict[str, Any]:
        called.append(document_id)
        if document_id in fail:
            raise RuntimeError(f"simulated indexing failure for {document_id}")
        return {"id": document_id, "status": "completed"}

    return called, fake


class _DummyEmbeddings:
    """인덱싱 브랜치를 켜기 위한 진짜가 아닌 placeholder. Phase 2 에선
    monkeypatch 된 process_document 가 실제로 호출되므로 이 객체 내용은 무관."""


async def test_phase2_indexes_created_and_updated_docs(stores, monkeypatch) -> None:
    """Phase 1 결과의 created + updated 가 인덱싱 대상. unchanged 는 제외."""
    meta, vec, graph = stores
    monkeypatch.setattr(mcp_sync, "import_page_via_mcp", _make_fake_importer())
    monkeypatch.setattr(
        mcp_sync, "enumerate_subtree_pages",
        _make_fake_subtree_enum([{"id": "200"}, {"id": "201"}]),
    )
    called, fake_proc = _make_recording_process_document()
    monkeypatch.setattr(mcp_sync, "process_document", fake_proc)

    t = await meta.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    result = await execute_sync_target(
        None, t,
        meta_store=meta, vector_store=vec, graph_store=graph,
        embedding_client=_DummyEmbeddings(),
    )

    # 첫 싱크 — 3건 모두 created → 3건 모두 인덱싱 대상
    assert set(called) == set(result.created)
    assert len(result.processed) == 3
    assert result.processing_errors == []


async def test_phase2_skips_unchanged(stores, monkeypatch) -> None:
    """해시 동일로 skip 된 문서는 재임베딩하지 않는다."""
    meta, vec, graph = stores
    monkeypatch.setattr(mcp_sync, "import_page_via_mcp", _make_fake_importer())
    monkeypatch.setattr(
        mcp_sync, "enumerate_subtree_pages",
        _make_fake_subtree_enum([{"id": "200"}]),
    )
    called, fake_proc = _make_recording_process_document()
    monkeypatch.setattr(mcp_sync, "process_document", fake_proc)

    t = await meta.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    # 1st: 모두 created → 인덱싱
    await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
        embedding_client=_DummyEmbeddings(),
    )
    first_call_count = len(called)

    # 2nd: hash 동일로 unchanged → Phase 2 는 아무것도 안 함
    result = await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
        embedding_client=_DummyEmbeddings(),
    )

    assert len(called) == first_call_count  # 추가 호출 없음
    assert result.processed == []
    assert len(result.unchanged) >= 1


async def test_phase2_failure_isolated_per_doc(stores, monkeypatch) -> None:
    """한 문서의 인덱싱 실패가 다른 문서 인덱싱을 막지 않는다.

    첫 싱크에서 3건 모두 created 로 임포트되고, 그중 한 건의 인덱싱만
    실패하도록 설정 → 나머지는 그대로 processed, 실패 건만 격리.
    """
    meta, vec, graph = stores
    monkeypatch.setattr(mcp_sync, "import_page_via_mcp", _make_fake_importer())
    monkeypatch.setattr(
        mcp_sync, "enumerate_subtree_pages",
        _make_fake_subtree_enum([{"id": "200"}, {"id": "201"}]),
    )

    # 첫 실행 시 생성될 doc_id 를 미리 알 수 없으므로, 실패 대상은
    # "두 번째 처리되는 문서" 라는 기준으로 실행 중에 결정한다.
    call_log: list[int] = []

    async def fake_proc(
        document_id: int, *, meta_store: Any, vector_store: Any,
        graph_store: Any, embedding_client: Any, config: Any = None,
    ) -> dict[str, Any]:
        call_log.append(document_id)
        if len(call_log) == 2:
            raise RuntimeError(f"simulated indexing failure for {document_id}")
        return {"id": document_id, "status": "completed"}

    monkeypatch.setattr(mcp_sync, "process_document", fake_proc)

    t = await meta.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    result = await execute_sync_target(
        None, t,
        meta_store=meta, vector_store=vec, graph_store=graph,
        embedding_client=_DummyEmbeddings(),
    )

    # 3 건 모두 process_document 호출 — 중간 실패가 다음 호출을 막지 않음
    assert len(call_log) == 3
    # 2번째 호출이 실패하므로 processed 2건, processing_errors 1건
    assert len(result.processed) == 2
    assert len(result.processing_errors) == 1
    failed_doc_id = call_log[1]  # 두 번째 처리 = 실패 대상
    assert result.processing_errors[0]["doc_id"] == failed_doc_id
    # 실패 문서는 status=failed 로 마킹
    failed_doc = await meta.get_document(failed_doc_id)
    assert failed_doc is not None
    assert failed_doc["status"] == "failed"


async def test_phase2_is_skipped_when_embedding_client_missing(stores, monkeypatch) -> None:
    """embedding_client 미주입 시 Phase 2 는 전혀 실행되지 않는다(기존 동작 유지)."""
    meta, vec, graph = stores
    monkeypatch.setattr(mcp_sync, "import_page_via_mcp", _make_fake_importer())
    monkeypatch.setattr(
        mcp_sync, "enumerate_subtree_pages",
        _make_fake_subtree_enum([{"id": "200"}]),
    )
    called, fake_proc = _make_recording_process_document()
    monkeypatch.setattr(mcp_sync, "process_document", fake_proc)

    t = await meta.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    result = await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
        # embedding_client 주입 안 함
    )

    assert called == []
    assert result.processed == []
    assert result.processing_errors == []


async def test_phase2_respects_concurrency_bound(stores, monkeypatch) -> None:
    """Semaphore 로 동시 in-flight 문서 수가 상한 이하로 유지된다."""
    import asyncio as _asyncio

    meta, vec, graph = stores
    monkeypatch.setattr(mcp_sync, "import_page_via_mcp", _make_fake_importer())
    # 10 건의 문서를 임포트
    monkeypatch.setattr(
        mcp_sync, "enumerate_subtree_pages",
        _make_fake_subtree_enum([{"id": f"d{i}"} for i in range(10)]),
    )

    in_flight = 0
    max_in_flight = 0
    overlap_observed = 0

    async def slow_proc(
        document_id: int, *, meta_store: Any, vector_store: Any,
        graph_store: Any, embedding_client: Any, config: Any = None,
    ) -> dict[str, Any]:
        nonlocal in_flight, max_in_flight, overlap_observed
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        if in_flight > 1:
            overlap_observed += 1
        # Phase 2 처리에 시간이 걸리는 것처럼 흉내
        await _asyncio.sleep(0.02)
        in_flight -= 1
        return {"id": document_id}

    monkeypatch.setattr(mcp_sync, "process_document", slow_proc)

    t = await meta.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    result = await execute_sync_target(
        None, t,
        meta_store=meta, vector_store=vec, graph_store=graph,
        embedding_client=_DummyEmbeddings(),
        phase2_concurrency=3,
    )

    assert len(result.processed) == 11  # root + 10 descendants
    # 동시 실행은 3 건을 넘지 못한다
    assert max_in_flight <= 3
    # 실제로 동시성이 작동해 겹침이 발생했는지 (직렬이면 0)
    assert overlap_observed > 0


async def test_phase2_retries_previously_failed_docs(stores, monkeypatch) -> None:
    """재싱크 시 이전에 인덱싱 실패한 문서가 unchanged 여도 Phase 2 에 포함된다."""
    meta, vec, graph = stores
    monkeypatch.setattr(mcp_sync, "import_page_via_mcp", _make_fake_importer())
    monkeypatch.setattr(
        mcp_sync, "enumerate_subtree_pages",
        _make_fake_subtree_enum([{"id": "200"}]),
    )

    # 1차: 200 만 실패하도록 설정
    call_log_1: list[int] = []

    async def fake_proc_1(
        document_id: int, *, meta_store: Any, vector_store: Any,
        graph_store: Any, embedding_client: Any, config: Any = None,
    ) -> dict[str, Any]:
        call_log_1.append(document_id)
        # 첫 실행에서 page_id=200 을 가진 doc (두 번째 처리) 실패
        if len(call_log_1) == 2:
            raise RuntimeError("indexing fails first time")
        return {"id": document_id}

    monkeypatch.setattr(mcp_sync, "process_document", fake_proc_1)

    t = await meta.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    result1 = await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
        embedding_client=_DummyEmbeddings(),
        phase2_concurrency=1,  # 순서 예측 가능하게 직렬
    )
    assert len(result1.processing_errors) == 1
    failed_doc_id = result1.processing_errors[0]["doc_id"]

    # 2차: 같은 페이지를 재싱크. hash 동일 → 모두 unchanged.
    # 하지만 failed 문서는 재시도되어야 함.
    call_log_2: list[int] = []

    async def fake_proc_2(
        document_id: int, *, meta_store: Any, vector_store: Any,
        graph_store: Any, embedding_client: Any, config: Any = None,
    ) -> dict[str, Any]:
        call_log_2.append(document_id)
        return {"id": document_id}

    monkeypatch.setattr(mcp_sync, "process_document", fake_proc_2)

    result2 = await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
        embedding_client=_DummyEmbeddings(),
        phase2_concurrency=1,
    )

    # 재싱크에서는 created/updated 가 없지만 failed 는 재시도 대상
    assert result2.created == []
    assert result2.updated == []
    assert failed_doc_id in call_log_2
    assert failed_doc_id in result2.processed
    assert result2.processing_errors == []


async def test_phase2_concurrency_zero_or_negative_defaults_to_1(
    stores, monkeypatch,
) -> None:
    """``phase2_concurrency=0`` 같은 잘못된 값이 들어와도 무한루프/데드락 없이 직렬 실행."""
    meta, vec, graph = stores
    monkeypatch.setattr(mcp_sync, "import_page_via_mcp", _make_fake_importer())
    monkeypatch.setattr(
        mcp_sync, "enumerate_subtree_pages",
        _make_fake_subtree_enum([{"id": "200"}]),
    )
    called, fake_proc = _make_recording_process_document()
    monkeypatch.setattr(mcp_sync, "process_document", fake_proc)

    t = await meta.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    result = await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
        embedding_client=_DummyEmbeddings(),
        phase2_concurrency=0,
    )
    assert len(called) == 2
    assert len(result.processed) == 2
