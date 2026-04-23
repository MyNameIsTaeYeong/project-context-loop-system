"""Confluence 싱크 대상/멤버십 테이블 스키마 테스트.

``confluence_sync_targets`` / ``confluence_sync_membership`` 의 CHECK,
UNIQUE, FK CASCADE 제약을 DB 레벨에서 검증한다. CRUD 메서드는 후속
단계에서 추가되므로 여기서는 raw SQL로만 테스트한다.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from context_loop.storage.metadata_store import MetadataStore


@pytest.fixture
async def store(tmp_path: Path) -> MetadataStore:  # type: ignore[misc]
    s = MetadataStore(tmp_path / "test.db")
    await s.initialize()
    yield s
    await s.close()


# --- 테이블 존재 ---


async def test_sync_targets_table_exists(store: MetadataStore) -> None:
    cur = await store.db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='confluence_sync_targets'",
    )
    row = await cur.fetchone()
    assert row is not None


async def test_sync_membership_table_exists(store: MetadataStore) -> None:
    cur = await store.db.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name='confluence_sync_membership'",
    )
    row = await cur.fetchone()
    assert row is not None


# --- scope CHECK 제약 ---


async def test_scope_check_allows_valid_values(store: MetadataStore) -> None:
    for scope in ("page", "subtree", "space"):
        await store.db.execute(
            "INSERT INTO confluence_sync_targets (scope, space_key, page_id, name) "
            "VALUES (?, ?, ?, ?)",
            (scope, "ENG", f"pid-{scope}" if scope != "space" else None, f"N-{scope}"),
        )
    await store.db.commit()


async def test_scope_check_rejects_invalid_value(store: MetadataStore) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        await store.db.execute(
            "INSERT INTO confluence_sync_targets (scope, space_key, page_id, name) "
            "VALUES ('invalid', 'ENG', NULL, 'name')",
        )


# --- UNIQUE(scope, space_key, COALESCE(page_id, '')) ---


async def test_unique_rejects_duplicate_space_scope(store: MetadataStore) -> None:
    """같은 공간에 space scope 를 두 번 등록할 수 없다 (page_id가 NULL 인 경우 포함)."""
    await store.db.execute(
        "INSERT INTO confluence_sync_targets (scope, space_key, page_id, name) "
        "VALUES ('space', 'ENG', NULL, 'First')",
    )
    await store.db.commit()

    with pytest.raises(sqlite3.IntegrityError):
        await store.db.execute(
            "INSERT INTO confluence_sync_targets (scope, space_key, page_id, name) "
            "VALUES ('space', 'ENG', NULL, 'Second')",
        )


async def test_unique_rejects_duplicate_subtree_target(store: MetadataStore) -> None:
    """같은 (scope, space_key, page_id) subtree 를 두 번 등록할 수 없다."""
    await store.db.execute(
        "INSERT INTO confluence_sync_targets (scope, space_key, page_id, name) "
        "VALUES ('subtree', 'ENG', '100', 'First')",
    )
    await store.db.commit()

    with pytest.raises(sqlite3.IntegrityError):
        await store.db.execute(
            "INSERT INTO confluence_sync_targets (scope, space_key, page_id, name) "
            "VALUES ('subtree', 'ENG', '100', 'Second')",
        )


async def test_unique_allows_same_space_different_scope(store: MetadataStore) -> None:
    """같은 공간에 subtree + space 는 동시 등록 가능 (허용 정책)."""
    await store.db.execute(
        "INSERT INTO confluence_sync_targets (scope, space_key, page_id, name) "
        "VALUES ('space', 'ENG', NULL, 'Space target')",
    )
    await store.db.execute(
        "INSERT INTO confluence_sync_targets (scope, space_key, page_id, name) "
        "VALUES ('subtree', 'ENG', '100', 'Subtree target')",
    )
    await store.db.execute(
        "INSERT INTO confluence_sync_targets (scope, space_key, page_id, name) "
        "VALUES ('page', 'ENG', '200', 'Page target')",
    )
    await store.db.commit()

    cur = await store.db.execute(
        "SELECT COUNT(*) FROM confluence_sync_targets WHERE space_key = 'ENG'",
    )
    row = await cur.fetchone()
    assert row[0] == 3


async def test_unique_allows_different_space_keys(store: MetadataStore) -> None:
    await store.db.execute(
        "INSERT INTO confluence_sync_targets (scope, space_key, page_id, name) "
        "VALUES ('space', 'ENG', NULL, 'ENG')",
    )
    await store.db.execute(
        "INSERT INTO confluence_sync_targets (scope, space_key, page_id, name) "
        "VALUES ('space', 'OPS', NULL, 'OPS')",
    )
    await store.db.commit()


async def test_unique_allows_different_subtree_roots_in_same_space(
    store: MetadataStore,
) -> None:
    await store.db.execute(
        "INSERT INTO confluence_sync_targets (scope, space_key, page_id, name) "
        "VALUES ('subtree', 'ENG', '100', 'Root A')",
    )
    await store.db.execute(
        "INSERT INTO confluence_sync_targets (scope, space_key, page_id, name) "
        "VALUES ('subtree', 'ENG', '200', 'Root B')",
    )
    await store.db.commit()


# --- membership FK CASCADE ---


async def test_membership_cascade_on_target_delete(store: MetadataStore) -> None:
    cur = await store.db.execute(
        "INSERT INTO confluence_sync_targets (scope, space_key, page_id, name) "
        "VALUES ('subtree', 'ENG', '100', 'Root')",
    )
    target_id = cur.lastrowid

    await store.db.execute(
        "INSERT INTO confluence_sync_membership "
        "(target_id, page_id, space_key, parent_page_id, depth) "
        "VALUES (?, ?, ?, ?, ?)",
        (target_id, "100", "ENG", None, 0),
    )
    await store.db.execute(
        "INSERT INTO confluence_sync_membership "
        "(target_id, page_id, space_key, parent_page_id, depth) "
        "VALUES (?, ?, ?, ?, ?)",
        (target_id, "101", "ENG", "100", 1),
    )
    await store.db.commit()

    cur = await store.db.execute(
        "SELECT COUNT(*) FROM confluence_sync_membership WHERE target_id = ?",
        (target_id,),
    )
    assert (await cur.fetchone())[0] == 2

    await store.db.execute(
        "DELETE FROM confluence_sync_targets WHERE id = ?", (target_id,),
    )
    await store.db.commit()

    cur = await store.db.execute(
        "SELECT COUNT(*) FROM confluence_sync_membership WHERE target_id = ?",
        (target_id,),
    )
    assert (await cur.fetchone())[0] == 0


async def test_membership_primary_key_rejects_duplicate_pair(
    store: MetadataStore,
) -> None:
    cur = await store.db.execute(
        "INSERT INTO confluence_sync_targets (scope, space_key, page_id, name) "
        "VALUES ('subtree', 'ENG', '100', 'Root')",
    )
    target_id = cur.lastrowid

    await store.db.execute(
        "INSERT INTO confluence_sync_membership "
        "(target_id, page_id, space_key) VALUES (?, ?, ?)",
        (target_id, "200", "ENG"),
    )
    await store.db.commit()

    with pytest.raises(sqlite3.IntegrityError):
        await store.db.execute(
            "INSERT INTO confluence_sync_membership "
            "(target_id, page_id, space_key) VALUES (?, ?, ?)",
            (target_id, "200", "ENG"),
        )


async def test_membership_same_page_multiple_targets_allowed(
    store: MetadataStore,
) -> None:
    """같은 page_id가 여러 target에 속할 수 있어야 한다 (참조 카운팅 전제)."""
    cur = await store.db.execute(
        "INSERT INTO confluence_sync_targets (scope, space_key, page_id, name) "
        "VALUES ('subtree', 'ENG', '100', 'Root')",
    )
    subtree_id = cur.lastrowid

    cur = await store.db.execute(
        "INSERT INTO confluence_sync_targets (scope, space_key, page_id, name) "
        "VALUES ('space', 'ENG', NULL, 'All')",
    )
    space_id = cur.lastrowid

    await store.db.execute(
        "INSERT INTO confluence_sync_membership (target_id, page_id, space_key) "
        "VALUES (?, ?, ?)",
        (subtree_id, "200", "ENG"),
    )
    await store.db.execute(
        "INSERT INTO confluence_sync_membership (target_id, page_id, space_key) "
        "VALUES (?, ?, ?)",
        (space_id, "200", "ENG"),
    )
    await store.db.commit()

    cur = await store.db.execute(
        "SELECT COUNT(*) FROM confluence_sync_membership WHERE page_id = ?",
        ("200",),
    )
    assert (await cur.fetchone())[0] == 2


# --- 재초기화 idempotency ---


async def test_reinitialize_is_idempotent(tmp_path: Path) -> None:
    """동일 DB 파일로 두 번 initialize해도 에러가 없고 기존 데이터가 보존된다."""
    db_path = tmp_path / "test.db"

    s1 = MetadataStore(db_path)
    await s1.initialize()

    cur = await s1.db.execute(
        "INSERT INTO confluence_sync_targets (scope, space_key, page_id, name) "
        "VALUES ('subtree', 'ENG', '100', 'Root')",
    )
    target_id = cur.lastrowid
    await s1.db.commit()
    await s1.close()

    s2 = MetadataStore(db_path)
    await s2.initialize()

    cur = await s2.db.execute(
        "SELECT id, name FROM confluence_sync_targets WHERE id = ?", (target_id,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row["name"] == "Root"
    await s2.close()


# --- sync_targets CRUD ---


async def test_upsert_sync_target_inserts_and_returns_full_row(
    store: MetadataStore,
) -> None:
    t = await store.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    assert t["id"] > 0
    assert t["scope"] == "subtree"
    assert t["space_key"] == "ENG"
    assert t["page_id"] == "100"
    assert t["name"] == "Root"
    assert t["created_at"] is not None
    assert t["last_sync_at"] is None


async def test_upsert_sync_target_updates_name_when_exists(
    store: MetadataStore,
) -> None:
    first = await store.upsert_sync_target(
        scope="space", space_key="ENG", page_id=None, name="Old",
    )
    second = await store.upsert_sync_target(
        scope="space", space_key="ENG", page_id=None, name="New",
    )
    assert first["id"] == second["id"]
    assert second["name"] == "New"

    # 중복 레코드가 생기지 않았는지 확인
    all_targets = await store.list_sync_targets()
    assert len(all_targets) == 1


async def test_get_sync_target_found_and_not_found(store: MetadataStore) -> None:
    t = await store.upsert_sync_target(
        scope="page", space_key="ENG", page_id="200", name="P",
    )
    got = await store.get_sync_target(t["id"])
    assert got is not None and got["id"] == t["id"]

    missing = await store.get_sync_target(9999)
    assert missing is None


async def test_list_sync_targets_orders_newest_first(
    store: MetadataStore,
) -> None:
    """같은 초에 삽입되어 created_at 이 동일해도 id 로 tie-break 되어
    나중에 추가된 것이 먼저 온다."""
    await store.upsert_sync_target(
        scope="page", space_key="ENG", page_id="1", name="First",
    )
    await store.upsert_sync_target(
        scope="page", space_key="ENG", page_id="2", name="Second",
    )
    targets = await store.list_sync_targets()
    assert [t["name"] for t in targets] == ["Second", "First"]


async def test_update_sync_result_sets_timestamp_and_json(
    store: MetadataStore,
) -> None:
    t = await store.upsert_sync_target(
        scope="page", space_key="ENG", page_id="1", name="P",
    )
    assert t["last_sync_at"] is None

    await store.update_sync_result(t["id"], '{"created": 1}')

    refreshed = await store.get_sync_target(t["id"])
    assert refreshed is not None
    assert refreshed["last_sync_at"] is not None
    assert refreshed["last_result_json"] == '{"created": 1}'


async def test_delete_sync_target_returns_true_and_empty_orphans_when_no_docs(
    store: MetadataStore,
) -> None:
    t = await store.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    deleted, orphans = await store.delete_sync_target(t["id"])
    assert deleted is True
    assert orphans == []
    assert await store.get_sync_target(t["id"]) is None


async def test_delete_sync_target_returns_false_for_missing(
    store: MetadataStore,
) -> None:
    deleted, orphans = await store.delete_sync_target(9999)
    assert deleted is False
    assert orphans == []


async def test_delete_sync_target_cascades_membership(
    store: MetadataStore,
) -> None:
    t = await store.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    await store.upsert_membership(
        target_id=t["id"], page_id="100", space_key="ENG",
    )
    await store.upsert_membership(
        target_id=t["id"], page_id="101", space_key="ENG", depth=1,
    )
    assert await store.list_membership_page_ids(t["id"]) == {"100", "101"}

    await store.delete_sync_target(t["id"])

    assert await store.list_membership_page_ids(t["id"]) == set()


# --- membership CRUD ---


async def test_upsert_membership_single_insert_and_update(
    store: MetadataStore,
) -> None:
    t = await store.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    await store.upsert_membership(
        target_id=t["id"], page_id="200", space_key="ENG",
        parent_page_id="100", depth=1,
    )
    # 같은 (target_id, page_id) 재호출로 parent/depth 를 변경
    await store.upsert_membership(
        target_id=t["id"], page_id="200", space_key="ENG",
        parent_page_id="999", depth=5,
    )

    cur = await store.db.execute(
        "SELECT parent_page_id, depth FROM confluence_sync_membership "
        "WHERE target_id = ? AND page_id = ?",
        (t["id"], "200"),
    )
    row = await cur.fetchone()
    assert row["parent_page_id"] == "999"
    assert row["depth"] == 5


async def test_upsert_membership_batch_inserts_multiple_and_skips_missing_id(
    store: MetadataStore,
) -> None:
    t = await store.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    await store.upsert_membership_batch(
        t["id"],
        "ENG",
        [
            {"id": "100", "parent_id": None, "depth": 0},
            {"id": "200", "parent_id": "100", "depth": 1},
            {"id": "", "parent_id": "100", "depth": 1},      # 스킵
            {"parent_id": "100", "depth": 1},                # 스킵 (id 없음)
            {"id": "300", "parent_id": "100", "depth": 1},
        ],
    )
    assert await store.list_membership_page_ids(t["id"]) == {"100", "200", "300"}


async def test_upsert_membership_batch_empty_does_nothing(
    store: MetadataStore,
) -> None:
    t = await store.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    await store.upsert_membership_batch(t["id"], "ENG", [])
    assert await store.list_membership_page_ids(t["id"]) == set()


async def test_list_membership_page_ids_scoped_to_target(
    store: MetadataStore,
) -> None:
    t1 = await store.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="A",
    )
    t2 = await store.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="200", name="B",
    )
    await store.upsert_membership(target_id=t1["id"], page_id="1", space_key="ENG")
    await store.upsert_membership(target_id=t1["id"], page_id="2", space_key="ENG")
    await store.upsert_membership(target_id=t2["id"], page_id="2", space_key="ENG")

    assert await store.list_membership_page_ids(t1["id"]) == {"1", "2"}
    assert await store.list_membership_page_ids(t2["id"]) == {"2"}


# --- orphan detection via remove_memberships / delete_sync_target ---


async def _create_mcp_doc(store: MetadataStore, page_id: str) -> int:
    return await store.create_document(
        source_type="confluence_mcp",
        source_id=page_id,
        title=f"Page {page_id}",
        original_content="content",
        content_hash=f"hash-{page_id}",
    )


async def test_remove_memberships_flags_orphan_docs(
    store: MetadataStore,
) -> None:
    """다른 target이 소유하지 않는 페이지의 문서 ID가 반환된다."""
    t = await store.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    doc_id = await _create_mcp_doc(store, "200")
    await store.upsert_membership(target_id=t["id"], page_id="200", space_key="ENG")

    orphans = await store.remove_memberships(t["id"], ["200"])

    assert orphans == [doc_id]
    # membership row가 실제로 사라졌는지 확인
    assert await store.list_membership_page_ids(t["id"]) == set()


async def test_remove_memberships_keeps_doc_when_other_target_owns(
    store: MetadataStore,
) -> None:
    """다른 target이 동일 page_id를 소유하면 orphan으로 분류되지 않는다."""
    t_subtree = await store.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Subtree",
    )
    t_space = await store.upsert_sync_target(
        scope="space", space_key="ENG", page_id=None, name="Space",
    )
    await _create_mcp_doc(store, "200")
    await store.upsert_membership(
        target_id=t_subtree["id"], page_id="200", space_key="ENG",
    )
    await store.upsert_membership(
        target_id=t_space["id"], page_id="200", space_key="ENG",
    )

    orphans = await store.remove_memberships(t_subtree["id"], ["200"])
    assert orphans == []

    # subtree의 membership은 제거됐지만 space의 것은 남음
    assert await store.list_membership_page_ids(t_subtree["id"]) == set()
    assert await store.list_membership_page_ids(t_space["id"]) == {"200"}


async def test_remove_memberships_ignores_pages_without_documents(
    store: MetadataStore,
) -> None:
    """페이지에 대응하는 confluence_mcp 문서가 없으면 orphan 목록에 포함되지 않는다."""
    t = await store.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    await store.upsert_membership(target_id=t["id"], page_id="999", space_key="ENG")

    orphans = await store.remove_memberships(t["id"], ["999"])
    assert orphans == []


async def test_remove_memberships_empty_page_list_is_noop(
    store: MetadataStore,
) -> None:
    t = await store.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    await store.upsert_membership(target_id=t["id"], page_id="200", space_key="ENG")

    orphans = await store.remove_memberships(t["id"], [])
    assert orphans == []
    assert await store.list_membership_page_ids(t["id"]) == {"200"}


async def test_delete_sync_target_returns_orphan_doc_ids(
    store: MetadataStore,
) -> None:
    """해제 시, 이 target 에만 소속된 page의 문서 id가 반환된다."""
    t = await store.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Root",
    )
    doc_100 = await _create_mcp_doc(store, "100")
    doc_101 = await _create_mcp_doc(store, "101")
    await store.upsert_membership(target_id=t["id"], page_id="100", space_key="ENG")
    await store.upsert_membership(target_id=t["id"], page_id="101", space_key="ENG")

    deleted, orphans = await store.delete_sync_target(t["id"])
    assert deleted is True
    assert sorted(orphans) == sorted([doc_100, doc_101])


async def test_delete_sync_target_skips_orphans_shared_with_other_target(
    store: MetadataStore,
) -> None:
    """공간 전체와 서브트리가 공유하는 페이지는 해제 시 orphan이 아니다."""
    t_subtree = await store.upsert_sync_target(
        scope="subtree", space_key="ENG", page_id="100", name="Subtree",
    )
    t_space = await store.upsert_sync_target(
        scope="space", space_key="ENG", page_id=None, name="Space",
    )
    shared_doc = await _create_mcp_doc(store, "200")
    private_doc = await _create_mcp_doc(store, "201")

    for pid in ("200", "201"):
        await store.upsert_membership(
            target_id=t_subtree["id"], page_id=pid, space_key="ENG",
        )
    await store.upsert_membership(
        target_id=t_space["id"], page_id="200", space_key="ENG",
    )

    deleted, orphans = await store.delete_sync_target(t_subtree["id"])
    assert deleted is True
    assert orphans == [private_doc]
    # space target의 membership은 유지
    assert await store.list_membership_page_ids(t_space["id"]) == {"200"}
    # 문서 본체는 remove_memberships/delete_sync_target가 삭제하지 않는다
    # (caller의 책임). 존재 여부 확인:
    assert await store.get_document(shared_doc) is not None
    assert await store.get_document(private_doc) is not None
