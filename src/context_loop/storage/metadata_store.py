"""SQLite 메타데이터 저장소.

documents, chunks, graph_nodes, graph_edges, processing_history,
document_sources 테이블을 관리한다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

# staleness 기본 임계값 (일 단위). 90일 미만은 fresh, 180일 미만은 aging, 그 외 stale.
FRESH_THRESHOLD_DAYS = 90
AGING_THRESHOLD_DAYS = 180


def classify_staleness(
    source_updated_at: str | None,
    *,
    now: datetime | None = None,
    fresh_days: int = FRESH_THRESHOLD_DAYS,
    aging_days: int = AGING_THRESHOLD_DAYS,
) -> dict[str, Any]:
    """원본 마지막 수정 시각으로 staleness 등급을 판정한다.

    Returns:
        ``{"bucket": "fresh"|"aging"|"stale"|"unknown", "age_days": int|None}``.
    """
    if not source_updated_at:
        return {"bucket": "unknown", "age_days": None}
    try:
        ts = datetime.fromisoformat(source_updated_at.replace("Z", "+00:00"))
    except ValueError:
        return {"bucket": "unknown", "age_days": None}
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    current = now or datetime.now(tz=timezone.utc)
    age_days = max((current - ts).days, 0)
    if age_days < fresh_days:
        bucket = "fresh"
    elif age_days < aging_days:
        bucket = "aging"
    else:
        bucket = "stale"
    return {"bucket": bucket, "age_days": age_days}

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id TEXT,
    title TEXT NOT NULL,
    original_content TEXT,
    raw_content TEXT,
    content_hash TEXT,
    storage_method TEXT,
    status TEXT DEFAULT 'pending',
    version INTEGER DEFAULT 1,
    url TEXT,
    author TEXT,
    owner_id TEXT,
    source_updated_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_type, source_id)
);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    chunk_index INTEGER,
    content TEXT,
    token_count INTEGER,
    section_path TEXT DEFAULT '',
    section_anchor TEXT DEFAULT '',
    embed_text TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS graph_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    entity_name TEXT NOT NULL,
    entity_type TEXT,
    properties TEXT
);

CREATE TABLE IF NOT EXISTS graph_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    source_node_id INTEGER REFERENCES graph_nodes(id) ON DELETE CASCADE,
    target_node_id INTEGER REFERENCES graph_nodes(id) ON DELETE CASCADE,
    relation_type TEXT,
    properties TEXT
);

CREATE TABLE IF NOT EXISTS graph_node_documents (
    node_id INTEGER REFERENCES graph_nodes(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    PRIMARY KEY (node_id, document_id)
);

CREATE TABLE IF NOT EXISTS processing_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    action TEXT,
    prev_storage_method TEXT,
    new_storage_method TEXT,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    status TEXT,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS document_sources (
    doc_id        INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    source_doc_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    file_path     TEXT,
    PRIMARY KEY (doc_id, source_doc_id)
);

CREATE TABLE IF NOT EXISTS search_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    source TEXT NOT NULL,
    result_count INTEGER DEFAULT 0,
    latency_ms INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS search_citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    search_log_id INTEGER NOT NULL REFERENCES search_logs(id) ON DELETE CASCADE,
    document_id INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    similarity REAL,
    retrieval TEXT NOT NULL DEFAULT 'vector'
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    space_id TEXT,
    created_count INTEGER DEFAULT 0,
    updated_count INTEGER DEFAULT 0,
    unchanged_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    errors TEXT,
    started_at TIMESTAMP NOT NULL,
    completed_at TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'completed'
);

CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_document ON graph_nodes(document_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_document ON graph_edges(document_id);
CREATE INDEX IF NOT EXISTS idx_graph_node_documents_node ON graph_node_documents(node_id);
CREATE INDEX IF NOT EXISTS idx_graph_node_documents_document ON graph_node_documents(document_id);
CREATE INDEX IF NOT EXISTS idx_processing_history_document ON processing_history(document_id);
CREATE INDEX IF NOT EXISTS idx_document_sources_doc ON document_sources(doc_id);
CREATE INDEX IF NOT EXISTS idx_document_sources_source ON document_sources(source_doc_id);
CREATE INDEX IF NOT EXISTS idx_search_logs_created ON search_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_search_citations_log ON search_citations(search_log_id);
CREATE INDEX IF NOT EXISTS idx_search_citations_document ON search_citations(document_id);
CREATE INDEX IF NOT EXISTS idx_sync_runs_source ON sync_runs(source_type, started_at);
"""


class MetadataStore:
    """SQLite 기반 메타데이터 저장소.

    Args:
        db_path: SQLite 데이터베이스 파일 경로.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        """DB 연결을 열고 스키마를 생성한다."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._db_path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        await self._db.executescript(_SCHEMA_SQL)
        await self._migrate_schema()
        await self._db.commit()

    async def _migrate_schema(self) -> None:
        """기존 DB에 누락된 컬럼을 idempotent하게 추가한다."""
        cursor = await self.db.execute("PRAGMA table_info(documents)")
        existing_columns = {row["name"] for row in await cursor.fetchall()}
        if "raw_content" not in existing_columns:
            await self.db.execute("ALTER TABLE documents ADD COLUMN raw_content TEXT")
        if "source_updated_at" not in existing_columns:
            await self.db.execute(
                "ALTER TABLE documents ADD COLUMN source_updated_at TIMESTAMP"
            )
        if "owner_id" not in existing_columns:
            await self.db.execute("ALTER TABLE documents ADD COLUMN owner_id TEXT")

        cursor = await self.db.execute("PRAGMA table_info(chunks)")
        chunk_columns = {row["name"] for row in await cursor.fetchall()}
        if "section_path" not in chunk_columns:
            await self.db.execute(
                "ALTER TABLE chunks ADD COLUMN section_path TEXT DEFAULT ''",
            )
        if "section_anchor" not in chunk_columns:
            await self.db.execute(
                "ALTER TABLE chunks ADD COLUMN section_anchor TEXT DEFAULT ''",
            )
        if "embed_text" not in chunk_columns:
            await self.db.execute(
                "ALTER TABLE chunks ADD COLUMN embed_text TEXT DEFAULT ''",
            )

    async def close(self) -> None:
        """DB 연결을 닫는다."""
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("MetadataStore가 초기화되지 않았습니다. initialize()를 먼저 호출하세요.")
        return self._db

    # --- Documents ---

    async def create_document(
        self,
        *,
        source_type: str,
        title: str,
        original_content: str,
        content_hash: str,
        source_id: str | None = None,
        url: str | None = None,
        author: str | None = None,
        raw_content: str | None = None,
        source_updated_at: str | None = None,
        owner_id: str | None = None,
    ) -> int:
        """문서를 생성하고 ID를 반환한다.

        ``raw_content``는 소스 원본 (예: Confluence Storage Format HTML).
        하류에서 구조화 추출기가 재파싱할 수 있도록 보존한다. 없으면 NULL.

        ``source_updated_at``은 원본 시스템(Confluence 등)의 마지막 수정 시각
        ISO-8601 문자열. ``updated_at``(시스템 반영 시각)과 구분하여 staleness
        판단에 사용한다.

        ``author``는 마지막 수정자 ID, ``owner_id``는 원본 시스템의 문서 소유자
        ID로 의미가 다르다. stale 알림·리뷰 요청의 수신자 경로로 활용한다.
        """
        cursor = await self.db.execute(
            """INSERT INTO documents
               (source_type, source_id, title, original_content, raw_content,
                content_hash, url, author, owner_id, source_updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_type, source_id, title, original_content, raw_content,
                content_hash, url, author, owner_id, source_updated_at,
            ),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_document(self, document_id: int) -> dict[str, Any] | None:
        """ID로 문서를 조회한다."""
        cursor = await self.db.execute("SELECT * FROM documents WHERE id = ?", (document_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_documents(
        self,
        source_type: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """문서 목록을 조회한다."""
        query = "SELECT * FROM documents WHERE 1=1"
        params: list[Any] = []
        if source_type:
            query += " AND source_type = ?"
            params.append(source_type)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY updated_at DESC"
        cursor = await self.db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def update_document_status(
        self,
        document_id: int,
        status: str,
        storage_method: str | None = None,
    ) -> None:
        """문서 상태를 업데이트한다."""
        if storage_method:
            await self.db.execute(
                """UPDATE documents
                   SET status = ?, storage_method = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (status, storage_method, document_id),
            )
        else:
            await self.db.execute(
                "UPDATE documents SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, document_id),
            )
        await self.db.commit()

    async def update_document_content(
        self,
        document_id: int,
        original_content: str,
        content_hash: str,
        raw_content: str | None = None,
        source_updated_at: str | None = None,
    ) -> None:
        """문서 원본 내용과 해시를 갱신한다.

        ``raw_content``가 ``None``이 아니면 함께 갱신한다. ``None``이면
        기존 ``raw_content`` 값을 유지한다 (마크다운만 수정되는 케이스 지원).

        ``source_updated_at``이 주어지면 원본 시스템의 마지막 수정 시각도
        함께 갱신한다 (staleness 관측용).
        """
        sets = ["original_content = ?", "content_hash = ?",
                "version = version + 1", "updated_at = CURRENT_TIMESTAMP"]
        params: list[Any] = [original_content, content_hash]
        if raw_content is not None:
            sets.insert(1, "raw_content = ?")
            params.insert(1, raw_content)
        if source_updated_at is not None:
            sets.append("source_updated_at = ?")
            params.append(source_updated_at)
        params.append(document_id)
        await self.db.execute(
            f"UPDATE documents SET {', '.join(sets)} WHERE id = ?",  # noqa: S608
            params,
        )
        await self.db.commit()

    async def delete_document(self, document_id: int) -> None:
        """문서와 관련 데이터를 모두 삭제한다 (CASCADE)."""
        await self.db.execute("DELETE FROM documents WHERE id = ?", (document_id,))
        await self.db.commit()

    # --- Chunks ---

    async def create_chunk(
        self,
        *,
        chunk_id: str,
        document_id: int,
        chunk_index: int,
        content: str,
        token_count: int,
        section_path: str = "",
        section_anchor: str = "",
        embed_text: str = "",
    ) -> None:
        """청크를 저장한다.

        ``embed_text`` 는 git_code 분기처럼 임베딩 입력이 본문(``content``)과
        다른 경우(이름+시그니처+docstring)에 채운다. 일반 분기는 본문 자체가
        임베딩 입력이므로 빈 문자열로 둔다 — 대시보드/감사 시점에 ChromaDB
        엔트리의 임베딩 입력을 그대로 보여주기 위한 영속화 용도.
        """
        await self.db.execute(
            "INSERT INTO chunks "
            "(id, document_id, chunk_index, content, token_count, "
            " section_path, section_anchor, embed_text) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chunk_id, document_id, chunk_index, content, token_count,
                section_path, section_anchor, embed_text,
            ),
        )
        await self.db.commit()

    async def get_chunks_by_document(self, document_id: int) -> list[dict[str, Any]]:
        """문서의 청크 목록을 조회한다."""
        cursor = await self.db.execute(
            "SELECT * FROM chunks WHERE document_id = ? ORDER BY chunk_index",
            (document_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def delete_chunks_by_document(self, document_id: int) -> None:
        """문서의 모든 청크를 삭제한다."""
        await self.db.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
        await self.db.commit()

    # --- Graph Nodes ---

    async def create_graph_node(
        self,
        *,
        document_id: int,
        entity_name: str,
        entity_type: str | None = None,
        properties: str | None = None,
    ) -> int:
        """그래프 노드를 생성하고 ID를 반환한다."""
        cursor = await self.db.execute(
            "INSERT INTO graph_nodes (document_id, entity_name, entity_type, properties) VALUES (?, ?, ?, ?)",
            (document_id, entity_name, entity_type, properties),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_graph_nodes_by_document(self, document_id: int) -> list[dict[str, Any]]:
        """문서의 그래프 노드 목록을 조회한다.

        canonical 병합으로 다른 문서에서 먼저 생성된 노드도 `graph_node_documents`
        링크 테이블을 통해 포함한다 (`graph_nodes.document_id`는 최초 생성자만 기록).
        """
        cursor = await self.db.execute(
            """SELECT gn.* FROM graph_nodes gn
               INNER JOIN graph_node_documents gnd ON gn.id = gnd.node_id
               WHERE gnd.document_id = ?""",
            (document_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_all_graph_nodes(self) -> list[dict[str, Any]]:
        """전체 그래프 노드 목록을 조회한다."""
        cursor = await self.db.execute("SELECT * FROM graph_nodes")
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def find_graph_node_by_entity(
        self,
        entity_name: str,
        entity_type: str,
    ) -> dict[str, Any] | None:
        """엔티티 이름+타입으로 기존 정규 노드를 검색한다 (대소문자 무시)."""
        cursor = await self.db.execute(
            """SELECT * FROM graph_nodes
               WHERE LOWER(entity_name) = LOWER(?) AND entity_type = ?
               LIMIT 1""",
            (entity_name, entity_type),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def update_graph_node_properties(
        self,
        node_id: int,
        properties: str,
    ) -> None:
        """그래프 노드의 속성을 업데이트한다."""
        await self.db.execute(
            "UPDATE graph_nodes SET properties = ? WHERE id = ?",
            (properties, node_id),
        )
        await self.db.commit()

    async def add_node_document_link(
        self,
        node_id: int,
        document_id: int,
    ) -> None:
        """노드-문서 연결을 추가한다 (이미 존재하면 무시)."""
        await self.db.execute(
            "INSERT OR IGNORE INTO graph_node_documents (node_id, document_id) VALUES (?, ?)",
            (node_id, document_id),
        )
        await self.db.commit()

    async def get_node_document_ids(self, node_id: int) -> list[int]:
        """노드에 연결된 문서 ID 목록을 반환한다."""
        cursor = await self.db.execute(
            "SELECT document_id FROM graph_node_documents WHERE node_id = ?",
            (node_id,),
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def get_all_node_document_links(self) -> dict[int, list[int]]:
        """전체 노드-문서 연결을 반환한다. {node_id: [doc_id, ...]}"""
        cursor = await self.db.execute(
            "SELECT node_id, document_id FROM graph_node_documents"
        )
        rows = await cursor.fetchall()
        links: dict[int, list[int]] = {}
        for row in rows:
            links.setdefault(row[0], []).append(row[1])
        return links

    async def unlink_node_from_document(
        self,
        node_id: int,
        document_id: int,
    ) -> None:
        """노드에서 특정 문서 연결을 제거한다."""
        await self.db.execute(
            "DELETE FROM graph_node_documents WHERE node_id = ? AND document_id = ?",
            (node_id, document_id),
        )
        await self.db.commit()

    async def get_orphan_node_ids(self) -> list[int]:
        """어떤 문서에도 연결되지 않은 고아 노드 ID를 반환한다."""
        cursor = await self.db.execute(
            """SELECT gn.id FROM graph_nodes gn
               LEFT JOIN graph_node_documents gnd ON gn.id = gnd.node_id
               WHERE gnd.node_id IS NULL"""
        )
        rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def delete_graph_nodes_by_ids(self, node_ids: list[int]) -> None:
        """노드 ID 목록으로 노드를 삭제한다 (CASCADE로 엣지도 삭제)."""
        if not node_ids:
            return
        placeholders = ",".join("?" for _ in node_ids)
        await self.db.execute(
            f"DELETE FROM graph_nodes WHERE id IN ({placeholders})",  # noqa: S608
            node_ids,
        )
        await self.db.commit()

    async def delete_graph_data_by_document(self, document_id: int) -> None:
        """문서의 그래프 엣지를 삭제하고, 노드-문서 연결을 해제한다.

        고아 노드(어떤 문서에도 연결되지 않은 노드)도 정리한다.
        """
        # 1. 이 문서에서 생성된 엣지 삭제
        await self.db.execute(
            "DELETE FROM graph_edges WHERE document_id = ?", (document_id,)
        )
        # 2. 노드-문서 연결 해제
        await self.db.execute(
            "DELETE FROM graph_node_documents WHERE document_id = ?", (document_id,)
        )
        # 3. 고아 노드 삭제 (어떤 문서에도 연결되지 않은 노드)
        await self.db.execute(
            """DELETE FROM graph_nodes WHERE id NOT IN (
                SELECT DISTINCT node_id FROM graph_node_documents
            )"""
        )
        await self.db.commit()

    # --- Graph Edges ---

    async def create_graph_edge(
        self,
        *,
        document_id: int,
        source_node_id: int,
        target_node_id: int,
        relation_type: str,
        properties: str | None = None,
    ) -> int:
        """그래프 엣지를 생성하고 ID를 반환한다."""
        cursor = await self.db.execute(
            """INSERT INTO graph_edges
               (document_id, source_node_id, target_node_id, relation_type, properties)
               VALUES (?, ?, ?, ?, ?)""",
            (document_id, source_node_id, target_node_id, relation_type, properties),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_graph_edges_by_document(self, document_id: int) -> list[dict[str, Any]]:
        """문서의 그래프 엣지 목록을 조회한다."""
        cursor = await self.db.execute(
            "SELECT * FROM graph_edges WHERE document_id = ?", (document_id,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # --- Processing History ---

    async def add_processing_history(
        self,
        *,
        document_id: int,
        action: str,
        new_storage_method: str | None = None,
        prev_storage_method: str | None = None,
        status: str = "started",
    ) -> int:
        """처리 이력을 추가하고 ID를 반환한다."""
        cursor = await self.db.execute(
            """INSERT INTO processing_history
               (document_id, action, prev_storage_method, new_storage_method, started_at, status)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (document_id, action, prev_storage_method, new_storage_method, datetime.now().isoformat(), status),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def complete_processing_history(
        self,
        history_id: int,
        status: str = "completed",
        error_message: str | None = None,
    ) -> None:
        """처리 이력을 완료 처리한다."""
        await self.db.execute(
            """UPDATE processing_history
               SET completed_at = ?, status = ?, error_message = ?
               WHERE id = ?""",
            (datetime.now().isoformat(), status, error_message, history_id),
        )
        await self.db.commit()

    async def get_processing_history(self, document_id: int) -> list[dict[str, Any]]:
        """문서의 처리 이력을 조회한다."""
        cursor = await self.db.execute(
            "SELECT * FROM processing_history WHERE document_id = ? ORDER BY started_at DESC",
            (document_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # --- Document Sources ---

    async def add_document_source(
        self,
        doc_id: int,
        source_doc_id: int,
        file_path: str | None = None,
    ) -> None:
        """문서 간 소스 연결을 추가한다 (code_doc ↔ git_code).

        Args:
            doc_id: LLM 생성 문서(code_doc) ID.
            source_doc_id: 원본 코드 문서(git_code) ID.
            file_path: 원본 코드의 파일 경로 (선택).
        """
        await self.db.execute(
            "INSERT OR IGNORE INTO document_sources (doc_id, source_doc_id, file_path) VALUES (?, ?, ?)",
            (doc_id, source_doc_id, file_path),
        )
        await self.db.commit()

    async def get_document_sources(self, doc_id: int) -> list[dict[str, Any]]:
        """문서의 소스 문서 목록을 조회한다 (code_doc → git_code 방향).

        Returns:
            소스 문서 정보 리스트. 각 항목은 source_doc_id, file_path,
            그리고 소스 문서의 전체 컬럼을 포함한다.
        """
        cursor = await self.db.execute(
            """SELECT ds.source_doc_id, ds.file_path, d.*
               FROM document_sources ds
               JOIN documents d ON ds.source_doc_id = d.id
               WHERE ds.doc_id = ?""",
            (doc_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_documents_by_source(self, source_doc_id: int) -> list[dict[str, Any]]:
        """원본 코드를 참조하는 문서 목록을 조회한다 (git_code → code_doc 역방향).

        Returns:
            참조 문서 정보 리스트. 각 항목은 doc_id, file_path,
            그리고 참조 문서의 전체 컬럼을 포함한다.
        """
        cursor = await self.db.execute(
            """SELECT ds.doc_id, ds.file_path, d.*
               FROM document_sources ds
               JOIN documents d ON ds.doc_id = d.id
               WHERE ds.source_doc_id = ?""",
            (source_doc_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def delete_document_sources(self, doc_id: int) -> None:
        """문서의 모든 소스 연결을 삭제한다."""
        await self.db.execute(
            "DELETE FROM document_sources WHERE doc_id = ?", (doc_id,)
        )
        await self.db.commit()

    # --- Search Logs ---

    async def log_search(
        self,
        *,
        query: str,
        source: str,
        result_count: int = 0,
        latency_ms: int | None = None,
        citations: list[dict[str, Any]] | None = None,
    ) -> int:
        """검색 질의와 인용된 문서 ID를 기록한다.

        SSOT로서 "어떤 문서가 실제로 사용되는가"를 관측하기 위한 신호.
        stale/owner 판단 루프의 기초 데이터로 활용한다.

        Args:
            query: 원본 질의 문자열.
            source: 호출 경로 ("mcp", "web" 등).
            result_count: 인용된 문서 수.
            latency_ms: 검색·조립 소요 시간(ms).
            citations: 인용 문서 리스트. 각 항목은
                ``{"document_id", "rank", "similarity", "retrieval"}``.

        Returns:
            생성된 search_logs 레코드 ID.
        """
        cursor = await self.db.execute(
            """INSERT INTO search_logs (query, source, result_count, latency_ms)
               VALUES (?, ?, ?, ?)""",
            (query, source, result_count, latency_ms),
        )
        log_id = cursor.lastrowid
        assert log_id is not None

        if citations:
            await self.db.executemany(
                """INSERT INTO search_citations
                   (search_log_id, document_id, rank, similarity, retrieval)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (
                        log_id,
                        c["document_id"],
                        c["rank"],
                        c.get("similarity"),
                        c.get("retrieval", "vector"),
                    )
                    for c in citations
                ],
            )
        await self.db.commit()
        return log_id

    async def get_search_logs(
        self,
        *,
        limit: int = 100,
        source: str | None = None,
    ) -> list[dict[str, Any]]:
        """최근 검색 로그를 조회한다."""
        query = "SELECT * FROM search_logs"
        params: list[Any] = []
        if source:
            query += " WHERE source = ?"
            params.append(source)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        cursor = await self.db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_search_citations(self, search_log_id: int) -> list[dict[str, Any]]:
        """특정 검색 로그의 인용 문서 목록을 조회한다."""
        cursor = await self.db.execute(
            """SELECT * FROM search_citations
               WHERE search_log_id = ? ORDER BY rank""",
            (search_log_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_document_citation_counts(
        self,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """문서별 인용 횟수를 내림차순으로 반환한다.

        hot/cold 문서 분류와 품질 리뷰 우선순위 지정을 위한 집계.

        Returns:
            ``{"document_id", "citation_count", "last_cited_at"}`` 목록.
        """
        cursor = await self.db.execute(
            """SELECT
                   sc.document_id,
                   COUNT(*) AS citation_count,
                   MAX(sl.created_at) AS last_cited_at
               FROM search_citations sc
               JOIN search_logs sl ON sc.search_log_id = sl.id
               GROUP BY sc.document_id
               ORDER BY citation_count DESC, last_cited_at DESC
               LIMIT ?""",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # --- Sync Runs ---

    async def record_sync_run(
        self,
        *,
        source_type: str,
        space_id: str | None,
        started_at: datetime,
        completed_at: datetime,
        created_count: int,
        updated_count: int,
        unchanged_count: int,
        error_count: int,
        errors: str | None = None,
        status: str = "completed",
    ) -> int:
        """단일 스페이스 동기화 결과를 기록한다.

        ``errors``는 직렬화된 JSON 문자열(선택). 대시보드에서 마지막 sync
        시각·실패 건수·변경 건수를 표시하는 신호 원천.
        """
        cursor = await self.db.execute(
            """INSERT INTO sync_runs
               (source_type, space_id, created_count, updated_count,
                unchanged_count, error_count, errors, started_at,
                completed_at, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_type, space_id,
                created_count, updated_count, unchanged_count, error_count,
                errors,
                started_at.isoformat(),
                completed_at.isoformat(),
                status,
            ),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_recent_sync_runs(
        self,
        *,
        limit: int = 20,
        source_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """최근 sync 실행 이력을 반환한다."""
        query = "SELECT * FROM sync_runs"
        params: list[Any] = []
        if source_type:
            query += " WHERE source_type = ?"
            params.append(source_type)
        query += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)
        cursor = await self.db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_last_sync_run(
        self,
        source_type: str,
        space_id: str | None = None,
    ) -> dict[str, Any] | None:
        """특정 소스(+스페이스)의 가장 최근 sync 실행을 반환한다."""
        if space_id is None:
            cursor = await self.db.execute(
                """SELECT * FROM sync_runs WHERE source_type = ?
                   ORDER BY started_at DESC LIMIT 1""",
                (source_type,),
            )
        else:
            cursor = await self.db.execute(
                """SELECT * FROM sync_runs
                   WHERE source_type = ? AND space_id = ?
                   ORDER BY started_at DESC LIMIT 1""",
                (source_type, space_id),
            )
        row = await cursor.fetchone()
        return dict(row) if row else None

    # --- Statistics ---

    async def get_stats(self) -> dict[str, int]:
        """전체 통계를 조회한다."""
        stats: dict[str, int] = {}
        for table, key in [
            ("documents", "document_count"),
            ("chunks", "chunk_count"),
            ("graph_nodes", "node_count"),
            ("graph_edges", "edge_count"),
        ]:
            cursor = await self.db.execute(f"SELECT COUNT(*) FROM {table}")  # noqa: S608
            row = await cursor.fetchone()
            stats[key] = row[0] if row else 0

        # Git 소스 타입별 문서 수
        for source_type in ("code_file_summary", "code_doc", "code_summary", "git_code"):
            cursor = await self.db.execute(
                "SELECT COUNT(*) FROM documents WHERE source_type = ?",
                (source_type,),
            )
            row = await cursor.fetchone()
            stats[f"{source_type}_count"] = row[0] if row else 0

        return stats
