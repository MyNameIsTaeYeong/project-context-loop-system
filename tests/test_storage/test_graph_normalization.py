"""R3 그래프 노드 정규화 머지 + graph_merge_log 통합 테스트.

- ``graph_nodes.normalized_name`` 백필 마이그레이션의 idempotency
- ``find_graph_node_by_entity`` 가 표기 변형을 같은 노드로 매칭
- ``graph_merge_log`` 가 머지/신규마다 정확히 한 행씩 기록
"""

from __future__ import annotations

from pathlib import Path

import aiosqlite
import pytest

from context_loop.processor.graph_extractor import Entity, GraphData
from context_loop.storage.entity_normalizer import normalize_entity_name
from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore


@pytest.fixture
async def meta_store(tmp_path: Path) -> MetadataStore:  # type: ignore[misc]
    s = MetadataStore(tmp_path / "test.db")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
async def graph_store(meta_store: MetadataStore) -> GraphStore:  # type: ignore[misc]
    return GraphStore(meta_store)


async def _create_doc(store: MetadataStore, title: str = "Test") -> int:
    return await store.create_document(
        source_type="manual",
        title=title,
        original_content="content",
        content_hash=f"hash-{title}",
    )


# =============================================================================
# 1. 마이그레이션 idempotency
# =============================================================================


class TestBackfillMigration:
    """``_backfill_normalized_names`` 가 idempotent하게 동작한다."""

    async def test_backfill_populates_existing_rows(self, tmp_path: Path) -> None:
        """legacy DB (normalized_name 컬럼이 추가되기 전 데이터) 시나리오를
        에뮬레이션 — normalized_name 이 '' 인 행이 있을 때 백필이 실제 정규화
        값을 채운다.
        """
        db_path = tmp_path / "legacy.db"
        store = MetadataStore(db_path)
        await store.initialize()
        try:
            # 노드 생성 (정규화 채워짐) — 일부러 normalized_name 을 ''로 되돌려서
            # legacy 상태를 흉내낸다.
            doc_id = await _create_doc(store)
            node_id = await store.create_graph_node(
                document_id=doc_id, entity_name="결제 시스템", entity_type="system",
            )
            await store.db.execute(
                "UPDATE graph_nodes SET normalized_name = '' WHERE id = ?",
                (node_id,),
            )
            await store.db.commit()

            # 백필 실행
            await store._backfill_normalized_names()

            # 검증: 정규화 키가 채워졌다
            cursor = await store.db.execute(
                "SELECT normalized_name FROM graph_nodes WHERE id = ?", (node_id,),
            )
            row = await cursor.fetchone()
            assert row is not None
            assert row["normalized_name"] == normalize_entity_name("결제 시스템")
            assert row["normalized_name"] == "결제시스템"
        finally:
            await store.close()

    async def test_backfill_is_idempotent(self, tmp_path: Path) -> None:
        """이미 정규화된 행은 백필이 건드리지 않는다 — 사용자가 별도 정규화
        정책으로 채운 값이 덮어쓰이지 않도록 ('' 가 아니면 skip)."""
        db_path = tmp_path / "idem.db"
        store = MetadataStore(db_path)
        await store.initialize()
        try:
            doc_id = await _create_doc(store)
            node_id = await store.create_graph_node(
                document_id=doc_id, entity_name="X", entity_type="system",
            )
            # 임의 값으로 강제 설정
            await store.db.execute(
                "UPDATE graph_nodes SET normalized_name = 'manual-override' WHERE id = ?",
                (node_id,),
            )
            await store.db.commit()

            await store._backfill_normalized_names()

            cursor = await store.db.execute(
                "SELECT normalized_name FROM graph_nodes WHERE id = ?", (node_id,),
            )
            row = await cursor.fetchone()
            assert row is not None
            # 비어있지 않으므로 건드리지 않음
            assert row["normalized_name"] == "manual-override"
        finally:
            await store.close()

    async def test_migration_runs_twice_safely(self, tmp_path: Path) -> None:
        """``initialize`` 가 두 번 호출되어도 ALTER/CREATE INDEX/백필 모두 안전."""
        db_path = tmp_path / "twice.db"
        store = MetadataStore(db_path)
        await store.initialize()
        try:
            doc_id = await _create_doc(store)
            await store.create_graph_node(
                document_id=doc_id, entity_name="A", entity_type="t",
            )
        finally:
            await store.close()

        # 두 번째 initialize — 컬럼 존재 / 인덱스 존재 / 백필 no-op
        store2 = MetadataStore(db_path)
        await store2.initialize()
        try:
            # 컬럼이 여전히 존재하는지 확인
            cursor = await store2.db.execute("PRAGMA table_info(graph_nodes)")
            cols = {row["name"] for row in await cursor.fetchall()}
            assert "normalized_name" in cols
            # 기존 노드의 normalized_name 이 보존
            cursor = await store2.db.execute(
                "SELECT entity_name, normalized_name FROM graph_nodes",
            )
            rows = await cursor.fetchall()
            assert len(rows) == 1
            assert rows[0]["normalized_name"] == normalize_entity_name(
                rows[0]["entity_name"],
            )
        finally:
            await store2.close()


# =============================================================================
# 2. find_graph_node_by_entity 의 정규화 매칭
# =============================================================================


class TestFindGraphNodeMatching:
    """정규화 키로 표기 변형을 같은 노드로 잡는다."""

    async def test_korean_whitespace_variants_match(
        self, meta_store: MetadataStore,
    ) -> None:
        doc_id = await _create_doc(meta_store)
        # 첫 노드를 "결제 시스템" 으로 등록
        node_id = await meta_store.create_graph_node_with_link(
            document_id=doc_id, entity_name="결제 시스템", entity_type="system",
        )
        # 공백 없는 표기 / dash 표기 모두 같은 노드로 찾아짐
        for variant in ("결제 시스템", "결제시스템", "결제-시스템", "결제_시스템"):
            found = await meta_store.find_graph_node_by_entity(
                variant, "system",
            )
            assert found is not None, f"표기 변형 {variant!r} 매칭 실패"
            assert found["id"] == node_id

    async def test_english_case_and_separator_variants_match(
        self, meta_store: MetadataStore,
    ) -> None:
        doc_id = await _create_doc(meta_store)
        node_id = await meta_store.create_graph_node_with_link(
            document_id=doc_id, entity_name="Payment Service", entity_type="service",
        )
        for variant in (
            "payment service",
            "PAYMENT SERVICE",
            "Payment-Service",
            "payment_service",
            "PaymentService",
        ):
            found = await meta_store.find_graph_node_by_entity(variant, "service")
            assert found is not None, f"표기 변형 {variant!r} 매칭 실패"
            assert found["id"] == node_id

    async def test_entity_type_mismatch_isolates_nodes(
        self, meta_store: MetadataStore,
    ) -> None:
        """정규화 키가 같아도 entity_type 이 다르면 분리 유지."""
        doc_id = await _create_doc(meta_store)
        n1 = await meta_store.create_graph_node_with_link(
            document_id=doc_id, entity_name="API", entity_type="system",
        )
        n2 = await meta_store.create_graph_node_with_link(
            document_id=doc_id, entity_name="API", entity_type="concept",
        )
        assert n1 != n2
        found_sys = await meta_store.find_graph_node_by_entity("api", "system")
        found_con = await meta_store.find_graph_node_by_entity("api", "concept")
        assert found_sys is not None and found_sys["id"] == n1
        assert found_con is not None and found_con["id"] == n2

    async def test_parentheses_preserved_as_different_nodes(
        self, meta_store: MetadataStore,
    ) -> None:
        """`(v2)`, `(legacy)` 같은 부가 표기는 R3 D 에서 정규화 대상 아님 —
        별도 노드로 유지되어야 한다 (설계서 §5.3, §8 사례 3)."""
        doc_id = await _create_doc(meta_store)
        base = await meta_store.create_graph_node_with_link(
            document_id=doc_id, entity_name="결제 시스템", entity_type="system",
        )
        v2 = await meta_store.create_graph_node_with_link(
            document_id=doc_id, entity_name="결제 시스템(v2)", entity_type="system",
        )
        legacy = await meta_store.create_graph_node_with_link(
            document_id=doc_id, entity_name="결제 시스템 (legacy)", entity_type="system",
        )
        assert base != v2 != legacy
        assert base != legacy

    async def test_explicit_normalized_key_takes_precedence(
        self, meta_store: MetadataStore,
    ) -> None:
        """호출자가 ``normalized_name`` 을 직접 전달하면 그 키로 검색
        (entity_name 인자는 무시되어 키 충돌 가능). 책임 분리 가드."""
        doc_id = await _create_doc(meta_store)
        node_id = await meta_store.create_graph_node_with_link(
            document_id=doc_id, entity_name="결제 시스템", entity_type="system",
        )
        # 일부러 다른 raw name 을 전달하되 정규화 키만 일치시키면 매칭
        found = await meta_store.find_graph_node_by_entity(
            "전혀 다른 이름", "system", normalized_name="결제시스템",
        )
        assert found is not None and found["id"] == node_id


# =============================================================================
# 3. graph_merge_log 기록
# =============================================================================


class TestGraphMergeLog:
    """머지/신규 결정마다 정확히 한 행씩 기록된다."""

    async def test_new_node_logs_method_new(
        self,
        graph_store: GraphStore,
        meta_store: MetadataStore,
    ) -> None:
        doc_id = await _create_doc(meta_store)
        await graph_store.save_graph_data(
            doc_id,
            GraphData(
                entities=[Entity(name="X", entity_type="system")],
                relations=[],
            ),
        )
        log = await meta_store.get_graph_merge_log(source_document_id=doc_id)
        assert len(log) == 1
        assert log[0]["merge_method"] == "new"
        assert log[0]["raw_entity_name"] == "X"
        assert log[0]["raw_entity_type"] == "system"
        assert log[0]["similarity_score"] is None

    async def test_exact_repeat_logs_method_exact(
        self,
        graph_store: GraphStore,
        meta_store: MetadataStore,
    ) -> None:
        """동일 raw 표기로 다시 들어오면 'exact'."""
        doc1 = await _create_doc(meta_store, "doc1")
        doc2 = await _create_doc(meta_store, "doc2")
        await graph_store.save_graph_data(
            doc1,
            GraphData(
                entities=[Entity(name="Payment Service", entity_type="service")],
                relations=[],
            ),
        )
        await graph_store.save_graph_data(
            doc2,
            GraphData(
                entities=[Entity(name="Payment Service", entity_type="service")],
                relations=[],
            ),
        )
        log = await meta_store.get_graph_merge_log()
        assert len(log) == 2
        assert log[0]["merge_method"] == "new"
        assert log[1]["merge_method"] == "exact"
        assert log[1]["source_document_id"] == doc2

    async def test_variant_repeat_logs_method_normalized(
        self,
        graph_store: GraphStore,
        meta_store: MetadataStore,
    ) -> None:
        """표기 변형으로 들어오면 'normalized'."""
        doc1 = await _create_doc(meta_store, "doc1")
        doc2 = await _create_doc(meta_store, "doc2")
        await graph_store.save_graph_data(
            doc1,
            GraphData(
                entities=[Entity(name="Payment Service", entity_type="service")],
                relations=[],
            ),
        )
        await graph_store.save_graph_data(
            doc2,
            GraphData(
                entities=[Entity(name="payment-service", entity_type="service")],
                relations=[],
            ),
        )
        log = await meta_store.get_graph_merge_log()
        assert len(log) == 2
        assert log[1]["merge_method"] == "normalized"
        assert log[1]["raw_entity_name"] == "payment-service"

    async def test_one_log_row_per_entity(
        self,
        graph_store: GraphStore,
        meta_store: MetadataStore,
    ) -> None:
        """N 개 엔티티 입력 → 정확히 N 행 머지 로그."""
        doc_id = await _create_doc(meta_store)
        entities = [
            Entity(name=f"E{i}", entity_type="system") for i in range(7)
        ]
        await graph_store.save_graph_data(
            doc_id,
            GraphData(entities=entities, relations=[]),
        )
        log = await meta_store.get_graph_merge_log(source_document_id=doc_id)
        assert len(log) == 7
        assert all(row["merge_method"] == "new" for row in log)

    async def test_canonical_node_id_lookup(
        self,
        graph_store: GraphStore,
        meta_store: MetadataStore,
    ) -> None:
        """``canonical_node_id`` 필터로 한 노드의 머지 이력 추적."""
        doc1 = await _create_doc(meta_store, "doc1")
        doc2 = await _create_doc(meta_store, "doc2")
        doc3 = await _create_doc(meta_store, "doc3")
        await graph_store.save_graph_data(
            doc1,
            GraphData(
                entities=[Entity(name="결제 시스템", entity_type="system")],
                relations=[],
            ),
        )
        # doc2 도 같은 entity (표기 변형) — normalized 머지
        await graph_store.save_graph_data(
            doc2,
            GraphData(
                entities=[Entity(name="결제시스템", entity_type="system")],
                relations=[],
            ),
        )
        await graph_store.save_graph_data(
            doc3,
            GraphData(
                entities=[Entity(name="결제-시스템", entity_type="system")],
                relations=[],
            ),
        )
        # canonical node 의 id 찾기
        found = await meta_store.find_graph_node_by_entity("결제 시스템", "system")
        assert found is not None
        node_history = await meta_store.get_graph_merge_log(
            canonical_node_id=found["id"],
        )
        assert len(node_history) == 3
        methods = [row["merge_method"] for row in node_history]
        assert methods == ["new", "normalized", "normalized"]


# =============================================================================
# 4. save_graph_data 가 신규 노드도 정규화 키와 함께 저장
# =============================================================================


async def test_save_graph_data_persists_normalized_name(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """신규 노드 생성 경로가 ``normalized_name`` 도 INSERT 한다."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(
        doc_id,
        GraphData(
            entities=[Entity(name="Auth Service", entity_type="service")],
            relations=[],
        ),
    )
    async with aiosqlite.connect(meta_store._db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT entity_name, normalized_name FROM graph_nodes",
        )
        rows = await cursor.fetchall()
    assert len(rows) == 1
    assert rows[0]["entity_name"] == "Auth Service"
    assert rows[0]["normalized_name"] == "authservice"


async def test_cross_document_merge_via_normalization(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """다른 문서가 표기 변형으로 들어와도 정규화 머지로 한 노드 유지."""
    doc1 = await _create_doc(meta_store, "doc1")
    doc2 = await _create_doc(meta_store, "doc2")
    r1 = await graph_store.save_graph_data(
        doc1,
        GraphData(
            entities=[Entity(name="Auth-Service", entity_type="service")],
            relations=[],
        ),
    )
    r2 = await graph_store.save_graph_data(
        doc2,
        GraphData(
            entities=[Entity(name="auth_service", entity_type="service")],
            relations=[],
        ),
    )
    assert r1["nodes"] == 1 and r1["merged"] == 0
    assert r2["nodes"] == 0 and r2["merged"] == 1

    all_nodes = await meta_store.get_all_graph_nodes()
    assert len(all_nodes) == 1, "표기 변형이 별개 노드로 만들어졌다"
