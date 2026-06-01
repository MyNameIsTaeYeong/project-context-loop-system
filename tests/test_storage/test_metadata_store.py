"""MetadataStore 테스트."""

from pathlib import Path

import pytest

from context_loop.storage.metadata_store import MetadataStore


@pytest.fixture
async def store(tmp_path: Path) -> MetadataStore:
    s = MetadataStore(tmp_path / "test.db")
    await s.initialize()
    yield s  # type: ignore[misc]
    await s.close()


async def test_create_and_get_document(store: MetadataStore) -> None:
    doc_id = await store.create_document(
        source_type="manual",
        title="테스트 문서",
        original_content="# Hello\n테스트 내용",
        content_hash="abc123",
    )
    assert doc_id is not None

    doc = await store.get_document(doc_id)
    assert doc is not None
    assert doc["title"] == "테스트 문서"
    assert doc["source_type"] == "manual"
    assert doc["status"] == "pending"


async def test_list_documents_filter(store: MetadataStore) -> None:
    await store.create_document(
        source_type="manual", title="문서1", original_content="a", content_hash="h1"
    )
    await store.create_document(
        source_type="upload", title="문서2", original_content="b", content_hash="h2"
    )

    all_docs = await store.list_documents()
    assert len(all_docs) == 2

    manual_docs = await store.list_documents(source_type="manual")
    assert len(manual_docs) == 1
    assert manual_docs[0]["title"] == "문서1"


async def test_update_document_status(store: MetadataStore) -> None:
    doc_id = await store.create_document(
        source_type="manual", title="문서", original_content="x", content_hash="h"
    )
    await store.update_document_status(doc_id, "completed", storage_method="chunk")

    doc = await store.get_document(doc_id)
    assert doc is not None
    assert doc["status"] == "completed"
    assert doc["storage_method"] == "chunk"


async def test_update_document_content(store: MetadataStore) -> None:
    doc_id = await store.create_document(
        source_type="manual", title="문서", original_content="old", content_hash="h1"
    )
    await store.update_document_content(doc_id, "new content", "h2")

    doc = await store.get_document(doc_id)
    assert doc is not None
    assert doc["original_content"] == "new content"
    assert doc["content_hash"] == "h2"
    assert doc["version"] == 2
    assert doc["status"] == "pending"  # status는 호출자가 별도로 설정


async def test_create_document_persists_raw_content(store: MetadataStore) -> None:
    """``raw_content``를 지정해 생성하면 DB에 그대로 저장된다."""
    doc_id = await store.create_document(
        source_type="confluence",
        source_id="p1",
        title="페이지",
        original_content="# 페이지",
        content_hash="h",
        raw_content="<h1>페이지</h1>",
    )
    doc = await store.get_document(doc_id)
    assert doc is not None
    assert doc["raw_content"] == "<h1>페이지</h1>"


async def test_create_document_without_raw_content_is_null(store: MetadataStore) -> None:
    """``raw_content``를 생략하면 NULL."""
    doc_id = await store.create_document(
        source_type="manual",
        title="문서",
        original_content="x",
        content_hash="h",
    )
    doc = await store.get_document(doc_id)
    assert doc is not None
    assert doc["raw_content"] is None


async def test_update_document_content_updates_raw_content(store: MetadataStore) -> None:
    """``raw_content``를 넘기면 함께 갱신된다."""
    doc_id = await store.create_document(
        source_type="confluence",
        source_id="p1",
        title="문서",
        original_content="old",
        content_hash="h1",
        raw_content="<p>old</p>",
    )
    await store.update_document_content(
        doc_id,
        "new content",
        "h2",
        raw_content="<p>new</p>",
    )
    doc = await store.get_document(doc_id)
    assert doc is not None
    assert doc["raw_content"] == "<p>new</p>"
    assert doc["original_content"] == "new content"


async def test_update_document_content_preserves_raw_content_when_omitted(
    store: MetadataStore,
) -> None:
    """``raw_content=None``이면 기존 값을 유지한다."""
    doc_id = await store.create_document(
        source_type="confluence",
        source_id="p1",
        title="문서",
        original_content="old",
        content_hash="h1",
        raw_content="<p>original</p>",
    )
    await store.update_document_content(doc_id, "new content", "h2")
    doc = await store.get_document(doc_id)
    assert doc is not None
    assert doc["raw_content"] == "<p>original</p>"
    assert doc["original_content"] == "new content"


async def test_migration_adds_raw_content_to_legacy_db(tmp_path: Path) -> None:
    """구버전 스키마 DB(raw_content 컬럼 없음)를 열면 ALTER TABLE로 컬럼이 추가된다."""
    import aiosqlite

    db_path = tmp_path / "legacy.db"

    # 구버전 스키마: raw_content 없는 documents 테이블
    async with aiosqlite.connect(db_path) as legacy:
        await legacy.execute(
            """CREATE TABLE documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_type TEXT NOT NULL,
                source_id TEXT,
                title TEXT NOT NULL,
                original_content TEXT,
                content_hash TEXT,
                storage_method TEXT,
                status TEXT DEFAULT 'pending',
                version INTEGER DEFAULT 1,
                url TEXT,
                author TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source_type, source_id)
            )"""
        )
        await legacy.execute(
            "INSERT INTO documents (source_type, title, original_content, content_hash)"
            " VALUES (?, ?, ?, ?)",
            ("manual", "legacy", "legacy content", "h"),
        )
        await legacy.commit()

    # 새 MetadataStore로 열면 마이그레이션이 실행되어야 함
    store = MetadataStore(db_path)
    await store.initialize()
    try:
        cursor = await store.db.execute("PRAGMA table_info(documents)")
        columns = {row["name"] for row in await cursor.fetchall()}
        assert "raw_content" in columns

        # 기존 row는 raw_content가 NULL
        cursor = await store.db.execute(
            "SELECT raw_content FROM documents WHERE title = ?", ("legacy",)
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["raw_content"] is None
    finally:
        await store.close()


async def test_migration_adds_section_columns_to_legacy_chunks(
    tmp_path: Path,
) -> None:
    """구버전 chunks 테이블(section_path/section_anchor/embed_text 없음)을 열면 ALTER로 컬럼이 추가된다."""
    import aiosqlite

    db_path = tmp_path / "legacy_chunks.db"

    async with aiosqlite.connect(db_path) as legacy:
        await legacy.execute(
            """CREATE TABLE chunks (
                id TEXT PRIMARY KEY,
                document_id INTEGER,
                chunk_index INTEGER,
                content TEXT,
                token_count INTEGER
            )"""
        )
        await legacy.execute(
            "INSERT INTO chunks (id, document_id, chunk_index, content, token_count)"
            " VALUES (?, ?, ?, ?, ?)",
            ("legacy-c1", 1, 0, "구버전 청크", 5),
        )
        await legacy.commit()

    store = MetadataStore(db_path)
    await store.initialize()
    try:
        cursor = await store.db.execute("PRAGMA table_info(chunks)")
        columns = {row["name"] for row in await cursor.fetchall()}
        assert "section_path" in columns
        assert "section_anchor" in columns
        assert "embed_text" in columns
        assert "section_index" in columns

        # 기존 row는 빈 문자열로 채워짐 (DEFAULT '')
        cursor = await store.db.execute(
            "SELECT section_path, section_anchor, embed_text, section_index "
            "FROM chunks WHERE id = ?",
            ("legacy-c1",),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["section_path"] == ""
        assert row["section_anchor"] == ""
        assert row["embed_text"] == ""
        # section_index 는 ALTER 시 DEFAULT 가 없어 NULL
        assert row["section_index"] is None
    finally:
        await store.close()


async def test_delete_document_cascades(store: MetadataStore) -> None:
    doc_id = await store.create_document(
        source_type="manual", title="문서", original_content="x", content_hash="h"
    )
    await store.create_chunk(
        chunk_id="c1", document_id=doc_id, chunk_index=0, content="chunk", token_count=5
    )
    node_id = await store.create_graph_node(
        document_id=doc_id, entity_name="Entity1", entity_type="concept"
    )

    await store.delete_document(doc_id)

    assert await store.get_document(doc_id) is None
    assert await store.get_chunks_by_document(doc_id) == []
    assert await store.get_graph_nodes_by_document(doc_id) == []


async def test_chunks_crud(store: MetadataStore) -> None:
    doc_id = await store.create_document(
        source_type="manual", title="문서", original_content="x", content_hash="h"
    )
    await store.create_chunk(
        chunk_id="c1", document_id=doc_id, chunk_index=0,
        content="첫 번째 청크", token_count=10,
        section_path="문서 > 개요", section_anchor="개요",
        embed_text="hello\nfoo()\nDocstring",
        section_index=3,
    )
    await store.create_chunk(
        chunk_id="c2", document_id=doc_id, chunk_index=1,
        content="두 번째 청크", token_count=8,
    )

    chunks = await store.get_chunks_by_document(doc_id)
    assert len(chunks) == 2
    assert chunks[0]["chunk_index"] == 0
    assert chunks[0]["section_path"] == "문서 > 개요"
    assert chunks[0]["section_anchor"] == "개요"
    assert chunks[0]["embed_text"] == "hello\nfoo()\nDocstring"
    assert chunks[0]["section_index"] == 3
    assert chunks[1]["chunk_index"] == 1
    # 선택 인자 생략 시 기본값 (section_index 는 NULL)
    assert chunks[1]["section_path"] == ""
    assert chunks[1]["section_anchor"] == ""
    assert chunks[1]["embed_text"] == ""
    assert chunks[1]["section_index"] is None

    await store.delete_chunks_by_document(doc_id)
    assert await store.get_chunks_by_document(doc_id) == []


async def test_graph_nodes_and_edges(store: MetadataStore) -> None:
    doc_id = await store.create_document(
        source_type="manual", title="문서", original_content="x", content_hash="h"
    )
    node1 = await store.create_graph_node(
        document_id=doc_id, entity_name="서비스A", entity_type="system"
    )
    await store.add_node_document_link(node1, doc_id)
    node2 = await store.create_graph_node(
        document_id=doc_id, entity_name="서비스B", entity_type="system"
    )
    await store.add_node_document_link(node2, doc_id)
    edge_id = await store.create_graph_edge(
        document_id=doc_id,
        source_node_id=node1,
        target_node_id=node2,
        relation_type="depends_on",
    )

    nodes = await store.get_graph_nodes_by_document(doc_id)
    assert len(nodes) == 2

    edges = await store.get_graph_edges_by_document(doc_id)
    assert len(edges) == 1
    assert edges[0]["relation_type"] == "depends_on"

    await store.delete_graph_data_by_document(doc_id)
    assert await store.get_graph_nodes_by_document(doc_id) == []
    assert await store.get_graph_edges_by_document(doc_id) == []


async def test_create_graph_node_with_link_atomic(store: MetadataStore) -> None:
    """``create_graph_node_with_link`` 는 graph_nodes 와 graph_node_documents
    두 INSERT 를 같은 트랜잭션 한 번의 commit 으로 처리한다 — 두 INSERT 사이
    await 양보 시점에 다른 코루틴의 고아 노드 정리가 신규 노드를 잡아 FK
    위반을 일으키던 race window 를 제거한다.
    """
    doc_id = await store.create_document(
        source_type="manual", title="d", original_content="x", content_hash="h",
    )
    node_id = await store.create_graph_node_with_link(
        document_id=doc_id, entity_name="X", entity_type="system",
    )
    # 노드와 link 가 동시에 보인다 — 별도 add_node_document_link 호출 없이도
    # get_graph_nodes_by_document 에서 조회 가능.
    nodes = await store.get_graph_nodes_by_document(doc_id)
    assert len(nodes) == 1
    assert nodes[0]["id"] == node_id


async def test_delete_graph_data_by_document_narrow_orphan_cleanup(
    store: MetadataStore,
) -> None:
    """``delete_graph_data_by_document`` 의 고아 정리가 '이 문서가 unlink 한
    노드' 만 범위로 좁혀져, 동시 처리 중인 다른 문서의 신규 노드(아직 link 등록
    중인 노드)를 잘못 삭제하지 않는다 — FK 위반의 핵심 회귀 가드.

    시나리오 재구성:
    1. 문서 A 가 노드 X 를 생성 (link 까지 atomic) — 정상 등록 상태
    2. 문서 B 가 노드 Y 를 INSERT 만 한 직후 (link 등록 직전) — 모사를 위해
       create_graph_node 단독 호출로 'link 없는 신규 노드' 상태를 흉내냄
    3. A 의 delete_graph_data_by_document 호출 — Y 가 link 없지만 본 문서의
       unlink 범위가 아니므로 보존되어야 함
    """
    doc_a = await store.create_document(
        source_type="manual", title="A", original_content="x", content_hash="ha",
    )
    doc_b = await store.create_document(
        source_type="manual", title="B", original_content="y", content_hash="hb",
    )
    # A 의 노드 X (link 동반)
    x_id = await store.create_graph_node_with_link(
        document_id=doc_a, entity_name="X", entity_type="system",
    )
    # B 의 노드 Y — link 등록 직전 상태를 흉내내기 위해 단독 INSERT 만
    y_id = await store.create_graph_node(
        document_id=doc_b, entity_name="Y", entity_type="system",
    )

    # A 의 그래프 정리 — Y 가 보존되어야 함 (이전 구현은 전역 스캔으로 삭제했음)
    await store.delete_graph_data_by_document(doc_a)

    # 검증: Y 는 graph_nodes 에 남아있고, X 는 삭제됨.
    all_nodes = await store.get_all_graph_nodes()
    ids = {n["id"] for n in all_nodes}
    assert y_id in ids, "다른 문서의 link 등록 중 신규 노드가 잘못 삭제됨"
    assert x_id not in ids, "이번 문서가 unlink 한 노드는 정상 삭제되어야 함"


async def test_processing_history(store: MetadataStore) -> None:
    doc_id = await store.create_document(
        source_type="manual", title="문서", original_content="x", content_hash="h"
    )
    history_id = await store.add_processing_history(
        document_id=doc_id, action="created", new_storage_method="chunk"
    )
    await store.complete_processing_history(history_id, status="completed")

    history = await store.get_processing_history(doc_id)
    assert len(history) == 1
    assert history[0]["action"] == "created"
    assert history[0]["status"] == "completed"
    assert history[0]["completed_at"] is not None


async def test_document_sources_crud(store: MetadataStore) -> None:
    """document_sources 테이블 CRUD 테스트."""
    # code_doc 문서 생성
    code_doc_id = await store.create_document(
        source_type="code_doc",
        title="VPC 아키텍처 문서",
        original_content="# VPC\nVPC 관련 설명",
        content_hash="cd1",
        source_id="vpc:architecture",
    )
    # git_code 원본 코드 문서 생성
    git1_id = await store.create_document(
        source_type="git_code",
        title="vpc.tf",
        original_content='resource "aws_vpc" ...',
        content_hash="g1",
        source_id="vpc.tf",
    )
    git2_id = await store.create_document(
        source_type="git_code",
        title="subnets.tf",
        original_content='resource "aws_subnet" ...',
        content_hash="g2",
        source_id="subnets.tf",
    )

    # 소스 연결 추가 (code_doc → git_code N:M)
    await store.add_document_source(code_doc_id, git1_id, file_path="infra/vpc.tf")
    await store.add_document_source(code_doc_id, git2_id, file_path="infra/subnets.tf")

    # code_doc → git_code 조회
    sources = await store.get_document_sources(code_doc_id)
    assert len(sources) == 2
    source_paths = {s["file_path"] for s in sources}
    assert source_paths == {"infra/vpc.tf", "infra/subnets.tf"}

    # git_code → code_doc 역방향 조회
    referencing = await store.get_documents_by_source(git1_id)
    assert len(referencing) == 1
    assert referencing[0]["doc_id"] == code_doc_id

    # 중복 INSERT 무시 (INSERT OR IGNORE)
    await store.add_document_source(code_doc_id, git1_id, file_path="infra/vpc.tf")
    sources = await store.get_document_sources(code_doc_id)
    assert len(sources) == 2  # 여전히 2개

    # 소스 연결 삭제
    await store.delete_document_sources(code_doc_id)
    sources = await store.get_document_sources(code_doc_id)
    assert len(sources) == 0


async def test_document_sources_cascade_on_delete(store: MetadataStore) -> None:
    """문서 삭제 시 document_sources도 CASCADE 삭제되는지 확인."""
    doc_id = await store.create_document(
        source_type="code_doc", title="doc", original_content="x", content_hash="h1",
    )
    src_id = await store.create_document(
        source_type="git_code", title="src", original_content="y", content_hash="h2",
    )
    await store.add_document_source(doc_id, src_id, file_path="main.py")

    # doc_id 삭제 → document_sources에서 doc_id 행 CASCADE 삭제
    await store.delete_document(doc_id)
    sources = await store.get_document_sources(doc_id)
    assert len(sources) == 0

    # src_id 측에서도 역방향 조회 시 결과 없음
    referencing = await store.get_documents_by_source(src_id)
    assert len(referencing) == 0


async def test_document_sources_cascade_on_source_delete(store: MetadataStore) -> None:
    """소스 문서(git_code) 삭제 시 document_sources도 CASCADE 삭제되는지 확인."""
    doc_id = await store.create_document(
        source_type="code_doc", title="doc", original_content="x", content_hash="h1",
    )
    src_id = await store.create_document(
        source_type="git_code", title="src", original_content="y", content_hash="h2",
    )
    await store.add_document_source(doc_id, src_id, file_path="main.py")

    # source_doc_id 삭제 → CASCADE로 연결 제거
    await store.delete_document(src_id)
    sources = await store.get_document_sources(doc_id)
    assert len(sources) == 0


async def test_get_stats(store: MetadataStore) -> None:
    doc_id = await store.create_document(
        source_type="manual", title="문서", original_content="x", content_hash="h"
    )
    await store.create_chunk(
        chunk_id="c1", document_id=doc_id, chunk_index=0, content="chunk", token_count=5
    )
    await store.create_graph_node(
        document_id=doc_id, entity_name="E1", entity_type="concept"
    )

    stats = await store.get_stats()
    assert stats["document_count"] == 1
    assert stats["chunk_count"] == 1
    assert stats["node_count"] == 1
    assert stats["edge_count"] == 0


async def test_get_merged_node_groups(store: MetadataStore) -> None:
    """병합된 노드 그룹을 집계한다 — 2종 이상 표기 또는 2개 이상 문서 수렴."""
    doc1 = await store.create_document(
        source_type="manual", title="D1",
        original_content="c", content_hash="h1",
    )
    doc2 = await store.create_document(
        source_type="manual", title="D2",
        original_content="c", content_hash="h2",
    )
    node_id = await store.create_graph_node_with_link(
        document_id=doc1, entity_name="Gateway", entity_type="component",
    )
    # 같은 정규 노드로 두 표기·두 문서가 수렴한 머지 로그 기록
    await store.record_graph_merge(
        canonical_node_id=node_id, raw_entity_name="Gateway",
        raw_entity_type="component", source_document_id=doc1,
        merge_method="new",
    )
    await store.record_graph_merge(
        canonical_node_id=node_id, raw_entity_name="gateway",
        raw_entity_type="component", source_document_id=doc2,
        merge_method="normalized",
    )

    groups = await store.get_merged_node_groups(min_variants=2)
    assert len(groups) == 1
    g = groups[0]
    assert g["entity_name"] == "Gateway"
    assert set(g["variant_names"]) == {"Gateway", "gateway"}
    assert set(g["document_ids"]) == {doc1, doc2}
    assert set(g["methods"]) == {"new", "normalized"}


async def test_get_merged_node_groups_excludes_single_variant(
    store: MetadataStore,
) -> None:
    """단일 표기·단일 문서(신규 1건)는 병합으로 보지 않아 제외한다."""
    doc = await store.create_document(
        source_type="manual", title="D",
        original_content="c", content_hash="h",
    )
    node_id = await store.create_graph_node_with_link(
        document_id=doc, entity_name="Solo", entity_type="component",
    )
    await store.record_graph_merge(
        canonical_node_id=node_id, raw_entity_name="Solo",
        raw_entity_type="component", source_document_id=doc,
        merge_method="new",
    )
    assert await store.get_merged_node_groups(min_variants=2) == []
