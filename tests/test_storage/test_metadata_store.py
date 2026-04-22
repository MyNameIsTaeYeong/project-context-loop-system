"""MetadataStore 테스트."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from context_loop.storage.metadata_store import MetadataStore, classify_staleness


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

        # 기존 row는 빈 문자열로 채워짐 (DEFAULT '')
        cursor = await store.db.execute(
            "SELECT section_path, section_anchor, embed_text FROM chunks WHERE id = ?",
            ("legacy-c1",),
        )
        row = await cursor.fetchone()
        assert row is not None
        assert row["section_path"] == ""
        assert row["section_anchor"] == ""
        assert row["embed_text"] == ""
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
    assert chunks[1]["chunk_index"] == 1
    # 선택 인자 생략 시 기본값 ''
    assert chunks[1]["section_path"] == ""
    assert chunks[1]["section_anchor"] == ""
    assert chunks[1]["embed_text"] == ""

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


async def test_log_search_persists_query_and_citations(store: MetadataStore) -> None:
    doc_id = await store.create_document(
        source_type="confluence", title="doc", original_content="x", content_hash="h"
    )
    log_id = await store.log_search(
        query="인증 플로우",
        source="mcp",
        result_count=1,
        latency_ms=42,
        citations=[
            {"document_id": doc_id, "rank": 0, "similarity": 0.87, "retrieval": "vector"},
        ],
    )

    logs = await store.get_search_logs()
    assert len(logs) == 1
    assert logs[0]["id"] == log_id
    assert logs[0]["query"] == "인증 플로우"
    assert logs[0]["source"] == "mcp"
    assert logs[0]["result_count"] == 1
    assert logs[0]["latency_ms"] == 42

    citations = await store.get_search_citations(log_id)
    assert len(citations) == 1
    assert citations[0]["document_id"] == doc_id
    assert citations[0]["rank"] == 0
    assert citations[0]["similarity"] == 0.87
    assert citations[0]["retrieval"] == "vector"


async def test_log_search_without_citations(store: MetadataStore) -> None:
    log_id = await store.log_search(query="empty", source="web")
    citations = await store.get_search_citations(log_id)
    assert citations == []


async def test_get_search_logs_filters_by_source(store: MetadataStore) -> None:
    await store.log_search(query="q1", source="mcp")
    await store.log_search(query="q2", source="web")
    await store.log_search(query="q3", source="web")

    web_logs = await store.get_search_logs(source="web")
    assert len(web_logs) == 2
    assert {log["query"] for log in web_logs} == {"q2", "q3"}


async def test_get_document_citation_counts_ranks_by_frequency(
    store: MetadataStore,
) -> None:
    doc_a = await store.create_document(
        source_type="confluence", title="A", original_content="a", content_hash="ha"
    )
    doc_b = await store.create_document(
        source_type="confluence", title="B", original_content="b", content_hash="hb"
    )

    # doc_a 2회 인용, doc_b 1회 인용
    for _ in range(2):
        await store.log_search(
            query="q", source="mcp",
            citations=[{"document_id": doc_a, "rank": 0, "similarity": 0.9}],
        )
    await store.log_search(
        query="q2", source="mcp",
        citations=[{"document_id": doc_b, "rank": 0, "similarity": 0.8}],
    )

    counts = await store.get_document_citation_counts()
    assert counts[0]["document_id"] == doc_a
    assert counts[0]["citation_count"] == 2
    assert counts[1]["document_id"] == doc_b
    assert counts[1]["citation_count"] == 1


async def test_create_document_persists_owner_id(store: MetadataStore) -> None:
    doc_id = await store.create_document(
        source_type="confluence",
        source_id="p1",
        title="문서",
        original_content="x",
        content_hash="h",
        author="editor-42",
        owner_id="owner-7",
    )
    doc = await store.get_document(doc_id)
    assert doc is not None
    assert doc["author"] == "editor-42"
    assert doc["owner_id"] == "owner-7"


async def test_create_document_persists_source_updated_at(
    store: MetadataStore,
) -> None:
    doc_id = await store.create_document(
        source_type="confluence",
        source_id="p1",
        title="문서",
        original_content="x",
        content_hash="h",
        source_updated_at="2026-01-15T10:00:00Z",
    )
    doc = await store.get_document(doc_id)
    assert doc is not None
    assert doc["source_updated_at"] == "2026-01-15T10:00:00Z"


async def test_update_document_content_updates_source_updated_at(
    store: MetadataStore,
) -> None:
    doc_id = await store.create_document(
        source_type="confluence",
        source_id="p1",
        title="문서",
        original_content="old",
        content_hash="h1",
        source_updated_at="2026-01-01T00:00:00Z",
    )
    await store.update_document_content(
        doc_id,
        "new",
        "h2",
        source_updated_at="2026-04-01T00:00:00Z",
    )
    doc = await store.get_document(doc_id)
    assert doc is not None
    assert doc["source_updated_at"] == "2026-04-01T00:00:00Z"


async def test_update_document_content_preserves_source_updated_at_when_omitted(
    store: MetadataStore,
) -> None:
    doc_id = await store.create_document(
        source_type="confluence",
        source_id="p1",
        title="문서",
        original_content="old",
        content_hash="h1",
        source_updated_at="2026-01-01T00:00:00Z",
    )
    await store.update_document_content(doc_id, "new", "h2")
    doc = await store.get_document(doc_id)
    assert doc is not None
    assert doc["source_updated_at"] == "2026-01-01T00:00:00Z"


def test_classify_staleness_fresh() -> None:
    now = datetime(2026, 4, 22, tzinfo=timezone.utc)
    result = classify_staleness(
        (now - timedelta(days=10)).isoformat(),
        now=now,
    )
    assert result["bucket"] == "fresh"
    assert result["age_days"] == 10


def test_classify_staleness_aging() -> None:
    now = datetime(2026, 4, 22, tzinfo=timezone.utc)
    result = classify_staleness(
        (now - timedelta(days=120)).isoformat(),
        now=now,
    )
    assert result["bucket"] == "aging"
    assert result["age_days"] == 120


def test_classify_staleness_stale() -> None:
    now = datetime(2026, 4, 22, tzinfo=timezone.utc)
    result = classify_staleness(
        (now - timedelta(days=400)).isoformat(),
        now=now,
    )
    assert result["bucket"] == "stale"
    assert result["age_days"] == 400


def test_classify_staleness_unknown_when_missing() -> None:
    assert classify_staleness(None) == {"bucket": "unknown", "age_days": None}


def test_classify_staleness_unknown_when_unparseable() -> None:
    assert classify_staleness("not-a-date") == {"bucket": "unknown", "age_days": None}


def test_classify_staleness_handles_z_suffix() -> None:
    now = datetime(2026, 4, 22, tzinfo=timezone.utc)
    result = classify_staleness("2026-04-12T00:00:00Z", now=now)
    assert result["bucket"] == "fresh"
    assert result["age_days"] == 10


async def test_record_sync_run_persists_summary(store: MetadataStore) -> None:
    started = datetime(2026, 4, 22, 10, 0, tzinfo=timezone.utc)
    completed = datetime(2026, 4, 22, 10, 5, tzinfo=timezone.utc)
    run_id = await store.record_sync_run(
        source_type="confluence",
        space_id="SPC",
        started_at=started,
        completed_at=completed,
        created_count=2,
        updated_count=3,
        unchanged_count=10,
        error_count=1,
        errors='[{"page_id": "p1", "error": "timeout"}]',
    )
    assert run_id is not None

    runs = await store.get_recent_sync_runs()
    assert len(runs) == 1
    assert runs[0]["source_type"] == "confluence"
    assert runs[0]["space_id"] == "SPC"
    assert runs[0]["created_count"] == 2
    assert runs[0]["error_count"] == 1


async def test_get_last_sync_run_filters_by_source_and_space(
    store: MetadataStore,
) -> None:
    now = datetime(2026, 4, 22, tzinfo=timezone.utc)
    await store.record_sync_run(
        source_type="confluence", space_id="A",
        started_at=now, completed_at=now,
        created_count=1, updated_count=0, unchanged_count=0, error_count=0,
    )
    await store.record_sync_run(
        source_type="confluence", space_id="B",
        started_at=now + timedelta(minutes=5), completed_at=now + timedelta(minutes=5),
        created_count=5, updated_count=0, unchanged_count=0, error_count=0,
    )

    last_any = await store.get_last_sync_run("confluence")
    assert last_any is not None
    assert last_any["space_id"] == "B"
    assert last_any["created_count"] == 5

    last_a = await store.get_last_sync_run("confluence", space_id="A")
    assert last_a is not None
    assert last_a["space_id"] == "A"


async def test_log_search_cascades_on_log_deletion(store: MetadataStore) -> None:
    """search_logs 삭제 시 citations도 CASCADE 삭제된다."""
    doc_id = await store.create_document(
        source_type="manual", title="d", original_content="x", content_hash="h"
    )
    log_id = await store.log_search(
        query="q", source="mcp",
        citations=[{"document_id": doc_id, "rank": 0, "similarity": 0.5}],
    )
    await store.db.execute("DELETE FROM search_logs WHERE id = ?", (log_id,))
    await store.db.commit()

    citations = await store.get_search_citations(log_id)
    assert citations == []
