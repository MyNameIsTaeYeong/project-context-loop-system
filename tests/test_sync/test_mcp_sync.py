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


def _make_fake_walk_subtree(tree: dict[str, list[dict[str, Any]]]):
    """walker 결과를 하드코딩한 노드 목록으로 반환하는 fake."""

    async def fake(session: Any, root_page_id: str, **_: Any):
        nodes: list[dict[str, Any]] = [
            {"id": root_page_id, "parent_id": None, "depth": 0, "title": ""},
        ]
        queue = [(root_page_id, 0)]
        visited = {root_page_id}
        while queue:
            pid, depth = queue.pop(0)
            for child in tree.get(pid, []):
                cid = str(child["id"])
                if cid in visited:
                    continue
                visited.add(cid)
                nodes.append({
                    "id": cid,
                    "parent_id": pid,
                    "depth": depth + 1,
                    "title": child.get("title", ""),
                })
                queue.append((cid, depth + 1))
        return nodes

    return fake


def _make_failing_walker():
    async def fake(*_: Any, **__: Any):
        raise RuntimeError("walker blew up")
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


async def test_sync_subtree_imports_all_pages_and_saves_hierarchy(
    stores, monkeypatch,
) -> None:
    meta, vec, graph = stores
    monkeypatch.setattr(mcp_sync, "import_page_via_mcp", _make_fake_importer())
    monkeypatch.setattr(
        mcp_sync, "walk_subtree",
        _make_fake_walk_subtree({
            "100": [{"id": "200", "title": "A"}, {"id": "201", "title": "B"}],
            "200": [{"id": "300", "title": "A1"}],
        }),
    )

    t = await meta.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    result = await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
    )

    assert len(result.created) == 4   # root + 3 descendants
    assert await meta.list_membership_page_ids(t["id"]) == {"100", "200", "201", "300"}

    # hierarchy (parent/depth) 가 저장되는지 확인
    cur = await meta.db.execute(
        "SELECT page_id, parent_page_id, depth FROM confluence_sync_membership "
        "WHERE target_id = ?",
        (t["id"],),
    )
    rows = {r["page_id"]: dict(r) for r in await cur.fetchall()}
    assert rows["100"]["parent_page_id"] is None
    assert rows["100"]["depth"] == 0
    assert rows["200"]["parent_page_id"] == "100"
    assert rows["200"]["depth"] == 1
    assert rows["300"]["parent_page_id"] == "200"
    assert rows["300"]["depth"] == 2


async def test_sync_subtree_walker_failure_preserves_membership(
    stores, monkeypatch,
) -> None:
    """walker 전체 실패 시 기존 membership 이 그대로 보존되어야 한다."""
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

    monkeypatch.setattr(mcp_sync, "walk_subtree", _make_failing_walker())

    result = await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
    )

    assert len(result.errors) == 1
    assert "walk_subtree" in result.errors[0]["error"]
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
        mcp_sync, "walk_subtree",
        _make_fake_walk_subtree({
            "100": [{"id": "200"}, {"id": "201"}],
        }),
    )
    await execute_sync_target(
        None, t, meta_store=meta, vector_store=vec, graph_store=graph,
    )
    assert await meta.list_membership_page_ids(t["id"]) == {"100", "200", "201"}

    # 두 번째 sync: 201 이 사라짐
    monkeypatch.setattr(
        mcp_sync, "walk_subtree",
        _make_fake_walk_subtree({
            "100": [{"id": "200"}],
        }),
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
    """walker 는 페이지를 봤는데 import 가 실패해도 기존 membership 은 유지된다.

    일시적 import 실패가 cascade 삭제로 번지지 않는 안전 속성 검증.
    """
    meta, vec, graph = stores
    monkeypatch.setattr(mcp_sync, "import_page_via_mcp", _make_fake_importer())

    t = await meta.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    monkeypatch.setattr(
        mcp_sync, "walk_subtree",
        _make_fake_walk_subtree({"100": [{"id": "200"}]}),
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
        mcp_sync, "walk_subtree",
        _make_fake_walk_subtree({"100": [{"id": "200"}]}),
    )
    await execute_sync_target(
        None, t_sub, meta_store=meta, vector_store=vec, graph_store=graph,
    )

    assert await meta.list_membership_page_ids(t_space["id"]) == {"100", "200"}
    assert await meta.list_membership_page_ids(t_sub["id"]) == {"100", "200"}

    # 이제 subtree 에서 200 이 사라짐 (walker 결과에서 빠짐)
    monkeypatch.setattr(
        mcp_sync, "walk_subtree",
        _make_fake_walk_subtree({"100": []}),
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
        mcp_sync, "walk_subtree",
        _make_fake_walk_subtree({"100": [{"id": "200"}]}),
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
        mcp_sync, "walk_subtree",
        _make_fake_walk_subtree({"100": []}),
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

    d = r.to_dict()
    assert d["summary"] == {
        "created": 2, "updated": 1, "unchanged": 3,
        "errors": 1, "removed": 1, "total": 7,
    }
    assert d["removed"] == [7]
