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
