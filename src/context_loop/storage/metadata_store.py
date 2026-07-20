"""SQLite 메타데이터 저장소.

documents, chunks, graph_nodes, graph_edges, processing_history,
document_sources 테이블을 관리한다.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from context_loop.storage.entity_normalizer import normalize_entity_name

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
    -- 생성형 LLM 인덱싱 단계(가상 질문 생성 / LLM 본문 그래프 추출) 의 호출이
    -- 실패해 그래프·질문 view 가 누락된 채 status='completed' 로 마감된 문서를
    -- 표시한다. status 는 '검색 가능' 의미를 보존(completed)하고, 품질 결손은
    -- 이 플래그로 분리 추적한다. 다음 sync 에서 자동 재인덱싱 대상이 된다.
    llm_degraded INTEGER DEFAULT 0,
    llm_degraded_detail TEXT,
    version INTEGER DEFAULT 1,
    -- 소스 시스템 측 리비전 (예: Confluence version.number). 내부 version 과
    -- 달리 서버가 부여한 값 그대로 저장 — "서버는 v5 인데 마지막 인덱싱은 v3"
    -- 같은 누락 진단과, 향후 열거 응답에 version 이 포함될 때 문서별 변경
    -- 비교로 전환하기 위한 기반 데이터.
    source_version INTEGER,
    url TEXT,
    author TEXT,
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
    embed_text TEXT DEFAULT '',
    section_index INTEGER
);

CREATE TABLE IF NOT EXISTS graph_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    entity_name TEXT NOT NULL,
    entity_type TEXT,
    properties TEXT,
    normalized_name TEXT NOT NULL DEFAULT ''
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

-- R3: 그래프 노드 머지/신규 결정의 관측성 로그.
-- save_graph_data 가 entity 마다 한 행 INSERT — 운영 디버깅과 향후 평가
-- (precision/recall 계산) 의 baseline 데이터로 활용. 본 단계는 binary 매칭
-- 이므로 similarity_score 는 NULL. merge_method:
--   'exact'      — 원본과 정규화 키가 동일했던 정확 매치 (즉 표기 변형 없음)
--   'normalized' — 정규화 키 매칭으로 표기 변형 흡수
--   'new'        — 매칭 실패, 신규 노드 생성
CREATE TABLE IF NOT EXISTS graph_merge_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_node_id INTEGER NOT NULL,
    raw_entity_name TEXT NOT NULL,
    raw_entity_type TEXT NOT NULL,
    source_document_id INTEGER NOT NULL,
    merge_method TEXT NOT NULL,
    similarity_score REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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

-- Confluence 싱크 대상 (page | subtree | space 3-scope)
CREATE TABLE IF NOT EXISTS confluence_sync_targets (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    scope             TEXT NOT NULL CHECK (scope IN ('page','subtree','space')),
    space_key         TEXT NOT NULL,
    page_id           TEXT,
    name              TEXT NOT NULL,
    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_sync_at      TIMESTAMP,
    last_result_json  TEXT,
    -- 증분 fetch 워터마크. CQL ``lastModified >= "..."`` 에 쓰는
    -- "YYYY-MM-DD HH:MM" 문자열. 임포트 실패 페이지가
    -- confluence_sync_fetch_retries 에 기록된 경우에만 전진한다.
    -- NULL 이면 전체 fetch (첫 싱크 또는 워터마크 리셋).
    last_watermark    TEXT
);

-- scope+space_key+page_id 조합의 유일성. page_id NULL(=space scope)도
-- 동일 space_key 에서 한 건만 허용되도록 COALESCE 로 collapse 한다.
CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_targets_unique
    ON confluence_sync_targets (scope, space_key, COALESCE(page_id, ''));

-- 싱크 대상 ↔ 페이지 소유권. documents 수명은 이 테이블의 행 수에
-- 의해 결정된다 (참조 카운트 0 시 cascade로 삭제). target 삭제 시
-- FK CASCADE 로 이 테이블의 행도 같이 사라진다.
CREATE TABLE IF NOT EXISTS confluence_sync_membership (
    target_id       INTEGER NOT NULL
                    REFERENCES confluence_sync_targets(id) ON DELETE CASCADE,
    page_id         TEXT NOT NULL,
    space_key       TEXT NOT NULL,
    parent_page_id  TEXT,
    depth           INTEGER,
    last_seen_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (target_id, page_id)
);

-- Phase 1 임포트가 실패한 페이지의 강제 재fetch 대기 목록. 이 목록이 있어야
-- 임포트 오류가 있어도 워터마크를 전진시킬 수 있다 — 실패 페이지는 다음
-- 싱크에서 변경 후보 조회와 무관하게 항상 fetch 되므로, 워터마크 전진으로
-- 변경 조회 범위에서 벗어나도 변경이 유실되지 않는다.
CREATE TABLE IF NOT EXISTS confluence_sync_fetch_retries (
    target_id   INTEGER NOT NULL
                REFERENCES confluence_sync_targets(id) ON DELETE CASCADE,
    page_id     TEXT NOT NULL,
    error       TEXT,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (target_id, page_id)
);

CREATE INDEX IF NOT EXISTS idx_documents_source ON documents(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_documents_status ON documents(status);
CREATE INDEX IF NOT EXISTS idx_chunks_document ON chunks(document_id);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_document ON graph_nodes(document_id);
-- idx_graph_nodes_normalized 는 _migrate_schema 에서 ALTER 이후 생성.
-- (executescript 는 단일 트랜잭션이라 이 시점에 normalized_name 컬럼이
-- 아직 없는 기존 DB 에서 CREATE INDEX 가 실패하기 때문.)
CREATE INDEX IF NOT EXISTS idx_graph_merge_log_canonical
    ON graph_merge_log(canonical_node_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_document ON graph_edges(document_id);
CREATE INDEX IF NOT EXISTS idx_graph_node_documents_node ON graph_node_documents(node_id);
CREATE INDEX IF NOT EXISTS idx_graph_node_documents_document ON graph_node_documents(document_id);
CREATE INDEX IF NOT EXISTS idx_processing_history_document ON processing_history(document_id);
CREATE INDEX IF NOT EXISTS idx_document_sources_doc ON document_sources(doc_id);
CREATE INDEX IF NOT EXISTS idx_document_sources_source ON document_sources(source_doc_id);
CREATE INDEX IF NOT EXISTS idx_sync_membership_page ON confluence_sync_membership(page_id);
CREATE INDEX IF NOT EXISTS idx_sync_membership_space ON confluence_sync_membership(space_key);
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
        if "llm_degraded" not in existing_columns:
            await self.db.execute(
                "ALTER TABLE documents ADD COLUMN llm_degraded INTEGER DEFAULT 0",
            )
        if "llm_degraded_detail" not in existing_columns:
            await self.db.execute(
                "ALTER TABLE documents ADD COLUMN llm_degraded_detail TEXT",
            )
        if "source_version" not in existing_columns:
            await self.db.execute(
                "ALTER TABLE documents ADD COLUMN source_version INTEGER",
            )

        cursor = await self.db.execute(
            "PRAGMA table_info(confluence_sync_targets)",
        )
        target_columns = {row["name"] for row in await cursor.fetchall()}
        if "last_watermark" not in target_columns:
            await self.db.execute(
                "ALTER TABLE confluence_sync_targets "
                "ADD COLUMN last_watermark TEXT",
            )

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
        if "section_index" not in chunk_columns:
            await self.db.execute(
                "ALTER TABLE chunks ADD COLUMN section_index INTEGER",
            )

        # R3: graph_nodes.normalized_name 컬럼 추가 + 백필. 정규화 키로
        # find_graph_node_by_entity 가 매칭하기 위한 컬럼 — 표기 변형
        # (공백/하이픈/언더스코어/케이스) 을 흡수해 머지 recall 을 끌어올린다.
        cursor = await self.db.execute("PRAGMA table_info(graph_nodes)")
        graph_node_columns = {row["name"] for row in await cursor.fetchall()}
        if "normalized_name" not in graph_node_columns:
            # SQLite ALTER TABLE 은 NOT NULL 컬럼을 DEFAULT 와 함께 추가하는
            # 패턴이 안전 — 모든 기존 행이 default 값('')로 채워진다. 이후
            # 백필 UPDATE 로 실제 정규화 값을 적용한다.
            await self.db.execute(
                "ALTER TABLE graph_nodes ADD COLUMN "
                "normalized_name TEXT NOT NULL DEFAULT ''",
            )
        # 인덱스는 컬럼 존재 여부와 무관하게 항상 보장 — _SCHEMA_SQL 에서
        # 빼냈기 때문에 신규 DB 든 마이그레이션된 DB 든 여기서 한 번 만든다.
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_graph_nodes_normalized "
            "ON graph_nodes(normalized_name, entity_type)",
        )

        # 백필: normalized_name 이 비어있는 행만 채운다 (idempotent — 이미
        # 정규화된 행은 skip). 신규 DB 면 행이 없어 no-op.
        await self._backfill_normalized_names()

    async def _backfill_normalized_names(self) -> None:
        """``graph_nodes.normalized_name`` 이 비어있는 행을 일회성 백필.

        Idempotent — 이미 비어있지 않은 행은 건드리지 않는다. 신규 DB 또는
        이전 백필이 완료된 DB 에서는 no-op.
        """
        cursor = await self.db.execute(
            "SELECT id, entity_name FROM graph_nodes WHERE normalized_name = ''",
        )
        rows = await cursor.fetchall()
        if not rows:
            return
        # 같은 트랜잭션에서 일괄 업데이트. executemany 로 row count 만큼 호출.
        updates = [
            (normalize_entity_name(row["entity_name"]), row["id"]) for row in rows
        ]
        await self.db.executemany(
            "UPDATE graph_nodes SET normalized_name = ? WHERE id = ?",
            updates,
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
        source_version: int | None = None,
    ) -> int:
        """문서를 생성하고 ID를 반환한다.

        ``raw_content``는 소스 원본 (예: Confluence Storage Format HTML).
        하류에서 구조화 추출기가 재파싱할 수 있도록 보존한다. 없으면 NULL.
        ``source_version`` 은 소스 시스템의 리비전 번호 (예: Confluence
        ``version.number``). 알 수 없으면 NULL.
        """
        cursor = await self.db.execute(
            """INSERT INTO documents
               (source_type, source_id, title, original_content, raw_content,
                content_hash, url, author, source_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_type, source_id, title, original_content, raw_content,
                content_hash, url, author, source_version,
            ),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def get_document_by_source(
        self, source_type: str, source_id: str,
    ) -> dict[str, Any] | None:
        """``(source_type, source_id)`` 로 문서 한 건을 조회한다.

        ``list_documents`` 전체 스캔 없이 ``idx_documents_source`` 인덱스를
        타는 단건 lookup — 대량 임포트 루프에서 기존 문서 확인용.
        """
        cursor = await self.db.execute(
            "SELECT * FROM documents WHERE source_type = ? AND source_id = ?",
            (source_type, source_id),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

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
        """문서 목록을 조회한다.

        각 문서에 파생 데이터 집계(``chunk_count``: 청크 수, ``node_count``:
        기여한 그래프 노드 수)를 상관 서브쿼리로 함께 반환한다.
        """
        query = (
            "SELECT d.*, "
            "(SELECT COUNT(*) FROM chunks c WHERE c.document_id = d.id) "
            "AS chunk_count, "
            "(SELECT COUNT(*) FROM graph_node_documents gnd "
            "WHERE gnd.document_id = d.id) AS node_count, "
            "(SELECT m.space_key FROM confluence_sync_membership m "
            "WHERE m.page_id = d.source_id LIMIT 1) AS space_key "
            "FROM documents d WHERE 1=1"
        )
        params: list[Any] = []
        if source_type:
            query += " AND d.source_type = ?"
            params.append(source_type)
        if status:
            query += " AND d.status = ?"
            params.append(status)
        query += " ORDER BY d.updated_at DESC"
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
        source_version: int | None = None,
    ) -> None:
        """문서 원본 내용과 해시를 갱신한다.

        ``raw_content``가 ``None``이 아니면 함께 갱신한다. ``None``이면
        기존 ``raw_content`` 값을 유지한다 (마크다운만 수정되는 케이스 지원).
        ``source_version`` 도 동일 규칙 — ``None`` 이면 기존 값 유지.
        """
        sets = [
            "original_content = ?", "content_hash = ?",
            "version = version + 1", "updated_at = CURRENT_TIMESTAMP",
        ]
        params: list[Any] = [original_content, content_hash]
        if raw_content is not None:
            sets.insert(1, "raw_content = ?")
            params.insert(1, raw_content)
        if source_version is not None:
            sets.append("source_version = ?")
            params.append(source_version)
        params.append(document_id)
        await self.db.execute(
            f"UPDATE documents SET {', '.join(sets)} WHERE id = ?",  # noqa: S608
            params,
        )
        await self.db.commit()

    async def update_document_title(self, document_id: int, title: str) -> None:
        """문서 제목만 갱신한다.

        Confluence 페이지 rename 반영 및 "Confluence Page {id}" 폴백 제목
        오염 문서의 치유용. 본문/해시는 건드리지 않는다.
        """
        await self.db.execute(
            "UPDATE documents SET title = ?, updated_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (title, document_id),
        )
        await self.db.commit()

    async def update_document_source_version(
        self, document_id: int, source_version: int,
    ) -> None:
        """소스 리비전 번호만 갱신한다.

        본문 해시는 동일한데 소스 측 리비전만 오른 경우 (예: Confluence
        메타데이터성 편집) 진단 데이터를 최신으로 유지하기 위한 경량 경로.
        ``updated_at``/``version`` 은 건드리지 않는다 — 내용 변경이 아니다.
        """
        await self.db.execute(
            "UPDATE documents SET source_version = ? WHERE id = ?",
            (source_version, document_id),
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
        section_index: int | None = None,
    ) -> None:
        """청크를 저장한다.

        ``embed_text`` 는 git_code 분기처럼 임베딩 입력이 본문(``content``)과
        다른 경우(이름+시그니처+docstring)에 채운다. 일반 분기는 본문 자체가
        임베딩 입력이므로 빈 문자열로 둔다 — 대시보드/감사 시점에 ChromaDB
        엔트리의 임베딩 입력을 그대로 보여주기 위한 영속화 용도.

        ``section_index`` 는 Confluence 구조화 추출 경로에서 청크가 유래한
        ``ExtractedDocument.sections`` 인덱스이다. ExtractionUnit 의
        ``section_ids`` 와 조인해 청크-unit 매핑을 복원하는 데 쓰인다.
        그 외 경로(일반 마크다운, AST 코드)에서는 ``None`` 으로 둔다.
        """
        await self.db.execute(
            "INSERT INTO chunks "
            "(id, document_id, chunk_index, content, token_count, "
            " section_path, section_anchor, embed_text, section_index) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                chunk_id, document_id, chunk_index, content, token_count,
                section_path, section_anchor, embed_text, section_index,
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
        normalized_name: str | None = None,
    ) -> int:
        """그래프 노드를 생성하고 ID를 반환한다.

        NOTE: 운영 경로(save_graph_data) 는 ``create_graph_node_with_link`` 를
        사용한다 — 신규 노드 INSERT 직후 link INSERT 가 별도 commit 으로
        분리되면, 두 commit 사이의 ``await`` 양보 시점에 다른 코루틴이 고아 노드
        정리 SQL 을 실행하여 방금 만든 노드가 잘못 삭제될 수 있다 (FK violation
        원인). 본 단독 메서드는 link 없는 노드 생성이 필요한 마이그레이션/
        테스트 전용으로 보존.

        Args:
            normalized_name: R3 정규화 키. 미지정 시 ``entity_name`` 으로부터
                내부 정규화한다.
        """
        key = (
            normalized_name
            if normalized_name is not None
            else normalize_entity_name(entity_name)
        )
        cursor = await self.db.execute(
            "INSERT INTO graph_nodes "
            "(document_id, entity_name, entity_type, properties, normalized_name) "
            "VALUES (?, ?, ?, ?, ?)",
            (document_id, entity_name, entity_type, properties, key),
        )
        await self.db.commit()
        return cursor.lastrowid  # type: ignore[return-value]

    async def create_graph_node_with_link(
        self,
        *,
        document_id: int,
        entity_name: str,
        entity_type: str | None = None,
        properties: str | None = None,
        normalized_name: str | None = None,
    ) -> int:
        """그래프 노드 INSERT 와 graph_node_documents link INSERT 를 같은
        트랜잭션에서 처리하고 한 번에 commit 한다.

        ``create_graph_node`` 후 ``add_node_document_link`` 를 분리 호출하면
        두 ``commit`` 사이의 ``await`` 양보 시점에 다른 코루틴의 고아 노드 정리
        SQL 이 link 없는 신규 노드를 삭제하여 후속 link INSERT 가 FK 위반을
        일으키는 race window 가 있었다 (재인덱싱 산발 실패의 근본 원인).
        본 메서드는 두 INSERT 를 단일 commit 으로 묶어 race window 를 제거한다.

        R3: ``normalized_name`` 도 함께 INSERT — 정규화 키 기반 머지가 신규
        노드부터 일관되게 동작하도록 한다.

        Returns:
            새로 생성된 ``graph_nodes.id``.
        """
        key = (
            normalized_name
            if normalized_name is not None
            else normalize_entity_name(entity_name)
        )
        cursor = await self.db.execute(
            "INSERT INTO graph_nodes "
            "(document_id, entity_name, entity_type, properties, normalized_name) "
            "VALUES (?, ?, ?, ?, ?)",
            (document_id, entity_name, entity_type, properties, key),
        )
        node_id = cursor.lastrowid
        assert node_id is not None
        # 같은 트랜잭션 안에서 link INSERT — 첫 INSERT 는 아직 commit 전이므로
        # 외부에서는 두 INSERT 가 모두 보이거나 모두 안 보인다.
        await self.db.execute(
            "INSERT OR IGNORE INTO graph_node_documents "
            "(node_id, document_id) VALUES (?, ?)",
            (node_id, document_id),
        )
        await self.db.commit()
        return node_id

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
        *,
        normalized_name: str | None = None,
    ) -> dict[str, Any] | None:
        """정규화 키로 기존 정규 노드를 검색한다.

        R3: 매칭 키를 ``LOWER(entity_name) = LOWER(?)`` → ``normalized_name = ?``
        로 변경. 공백/하이픈/언더스코어/케이스 표기 변형을 흡수해 머지 recall 을
        높인다. ``idx_graph_nodes_normalized(normalized_name, entity_type)`` 인덱스
        활용으로 LOWER() 함수 호출 제거 (인덱스 적용 가능 형태).

        Args:
            entity_name: 원본 엔티티 이름. ``normalized_name`` 미지정 시 내부에서
                정규화하여 사용한다.
            entity_type: 엔티티 타입 (정확 매치).
            normalized_name: 이미 정규화된 키. 호출자가 책임지고 정규화한 결과를
                전달하면 중복 정규화 비용을 절약한다. 권장: graph_store 가
                정규화 → 본 메서드에 전달.
        """
        key = normalized_name if normalized_name is not None else normalize_entity_name(entity_name)
        cursor = await self.db.execute(
            """SELECT * FROM graph_nodes
               WHERE normalized_name = ? AND entity_type = ?
               LIMIT 1""",
            (key, entity_type),
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

    # --- Graph Merge Log (R3) ---

    async def record_graph_merge(
        self,
        *,
        canonical_node_id: int,
        raw_entity_name: str,
        raw_entity_type: str,
        source_document_id: int,
        merge_method: str,
        similarity_score: float | None = None,
    ) -> None:
        """그래프 머지/신규 결정을 ``graph_merge_log`` 에 한 행 기록한다.

        R3: 정규화 머지 도입과 함께 도입된 관측성 로그. 머지/신규 결정마다
        한 행 INSERT. ``merge_method`` 값:

        - ``'exact'`` — 원본 ``entity_name`` 이 정규화 키와 동일했던 케이스
          (즉 표기 변형이 전혀 없었던 정확 매치)
        - ``'normalized'`` — 정규화 키 매칭으로 표기 변형 흡수
        - ``'new'`` — 매칭 실패, 신규 노드 생성

        Args:
            similarity_score: D 단계는 binary 매칭이므로 항상 ``None`` 전달.
                향후 임베딩(A)/LLM(B) 도입 시 cosine 점수 또는 LLM verdict
                점수 기록 슬롯.
        """
        await self.db.execute(
            """INSERT INTO graph_merge_log
               (canonical_node_id, raw_entity_name, raw_entity_type,
                source_document_id, merge_method, similarity_score)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                canonical_node_id,
                raw_entity_name,
                raw_entity_type,
                source_document_id,
                merge_method,
                similarity_score,
            ),
        )
        await self.db.commit()

    async def get_graph_merge_log(
        self,
        *,
        canonical_node_id: int | None = None,
        source_document_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """머지 로그를 조회한다 (디버깅·평가용).

        둘 다 ``None`` 이면 전체 로그를 반환. 필터를 결합하면 AND.
        """
        query = "SELECT * FROM graph_merge_log WHERE 1=1"
        params: list[Any] = []
        if canonical_node_id is not None:
            query += " AND canonical_node_id = ?"
            params.append(canonical_node_id)
        if source_document_id is not None:
            query += " AND source_document_id = ?"
            params.append(source_document_id)
        query += " ORDER BY id"
        cursor = await self.db.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_merged_node_groups(
        self,
        *,
        min_variants: int = 2,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        """크로스-문서 병합이 일어난 노드 그룹을 집계해 반환한다 (관측·디버깅용).

        ``graph_merge_log`` 의 행을 ``canonical_node_id`` 기준으로 묶어, 한
        정규 노드로 흡수된 원본 표기(raw_entity_name)들과 머지 방식을 요약한다.
        병합이 의미 있게 일어난 노드(서로 다른 원본 표기가 ``min_variants`` 종
        이상이거나, 여러 문서에서 수렴한 노드)만 노출한다.

        Args:
            min_variants: 그룹으로 노출할 최소 기준 (원본 표기 종류 수 또는
                기여 문서 수 중 큰 값). 기본 2 — 단일 문서에서 한 번만
                등장(신규 1건)한 노드는 병합이 아니므로 제외.
            include_deleted: 정규 노드가 이미 그래프에서 삭제되어
                ``graph_nodes`` 조인이 비는(병합 로그만 남은) 그룹을 포함할지
                여부. 기본 False — 삭제된 노드는 숨긴다. True 면 함께 노출하며
                해당 그룹은 ``entity_name`` 이 "(삭제된 노드)" 로 표시된다.

        Returns:
            각 그룹 dict (variant 수 → 문서 수 내림차순 정렬):
              - canonical_node_id: 정규 노드 ID
              - entity_name: 정규 노드의 현재 이름 (graph_nodes 조인)
              - entity_type: 정규 노드 타입
              - variant_names: 흡수된 원본 표기 목록 (중복 제거, 정렬)
              - document_ids: 기여한 문서 ID 목록 (중복 제거, 정렬)
              - methods: 등장한 merge_method 집합 (exact/normalized/new)
              - log_count: 이 노드에 기록된 총 로그 행 수
              - is_deleted: 정규 노드가 삭제되어 병합 로그만 남았는지 여부
        """
        cursor = await self.db.execute(
            """SELECT m.canonical_node_id      AS canonical_node_id,
                      m.raw_entity_name        AS raw_entity_name,
                      m.source_document_id     AS source_document_id,
                      m.merge_method           AS merge_method,
                      n.entity_name            AS entity_name,
                      n.entity_type            AS entity_type
               FROM graph_merge_log m
               LEFT JOIN graph_nodes n ON n.id = m.canonical_node_id
               ORDER BY m.canonical_node_id, m.id"""
        )
        rows = [dict(r) for r in await cursor.fetchall()]

        grouped: dict[int, dict[str, Any]] = {}
        for r in rows:
            cid = r["canonical_node_id"]
            g = grouped.get(cid)
            if g is None:
                g = {
                    "canonical_node_id": cid,
                    "entity_name": r.get("entity_name") or "(삭제된 노드)",
                    "entity_type": r.get("entity_type") or "other",
                    # graph_nodes 조인이 비면(LEFT JOIN 미매칭) 정규 노드가
                    # 삭제된 것. entity_name 은 NOT NULL 컬럼이므로 None 이면
                    # 노드가 사라진 경우로 판정한다.
                    "is_deleted": r.get("entity_name") is None,
                    "variant_names": set(),
                    "document_ids": set(),
                    "methods": set(),
                    "log_count": 0,
                }
                grouped[cid] = g
            g["log_count"] += 1
            if r.get("raw_entity_name"):
                g["variant_names"].add(r["raw_entity_name"])
            if r.get("source_document_id") is not None:
                g["document_ids"].add(r["source_document_id"])
            if r.get("merge_method"):
                g["methods"].add(r["merge_method"])

        result: list[dict[str, Any]] = []
        for g in grouped.values():
            variant_count = len(g["variant_names"])
            doc_count = len(g["document_ids"])
            # 병합으로 볼 수 있는 조건: 원본 표기가 2종 이상이거나
            # 2개 이상 문서가 같은 노드로 수렴.
            if max(variant_count, doc_count) < min_variants:
                continue
            # 기본적으로 삭제된(고아) 정규 노드는 숨긴다.
            if g["is_deleted"] and not include_deleted:
                continue
            result.append({
                "canonical_node_id": g["canonical_node_id"],
                "entity_name": g["entity_name"],
                "entity_type": g["entity_type"],
                "variant_names": sorted(g["variant_names"]),
                "document_ids": sorted(g["document_ids"]),
                "methods": sorted(g["methods"]),
                "log_count": g["log_count"],
                "is_deleted": g["is_deleted"],
            })

        result.sort(
            key=lambda x: (len(x["variant_names"]), len(x["document_ids"])),
            reverse=True,
        )
        return result

    async def delete_graph_data_by_document(self, document_id: int) -> None:
        """문서의 그래프 엣지를 삭제하고, 노드-문서 연결을 해제한다.

        이 문서의 unlink 결과 link 가 0 이 된 노드만 정리한다 — 전역
        ``WHERE id NOT IN (SELECT node_id FROM graph_node_documents)`` 스캔은
        ``save_graph_data`` 가 아직 link 를 추가하기 전인 신규 노드까지
        잘못 삭제하여 FK 위반을 일으켰다. 이번 문서가 실제로 unlink 한 노드만
        범위로 좁히면 동시 처리 중인 다른 문서의 신규 노드를 건드리지 않는다.
        """
        # 1. 이 문서에서 생성된 엣지 삭제
        await self.db.execute(
            "DELETE FROM graph_edges WHERE document_id = ?", (document_id,)
        )
        # 2. 이번 문서가 link 한 노드 ID 집합을 미리 채취 (3 단계에서 그 노드들
        #    만 고아 검사하기 위함).
        cursor = await self.db.execute(
            "SELECT DISTINCT node_id FROM graph_node_documents WHERE document_id = ?",
            (document_id,),
        )
        rows = await cursor.fetchall()
        candidate_node_ids: list[int] = [row[0] for row in rows]

        # 3. 노드-문서 연결 해제
        await self.db.execute(
            "DELETE FROM graph_node_documents WHERE document_id = ?", (document_id,)
        )

        # 4. 후보 노드들 중 link 가 0 인 것만 삭제 (좁힌 고아 정리).
        if candidate_node_ids:
            placeholders = ",".join("?" for _ in candidate_node_ids)
            await self.db.execute(
                f"DELETE FROM graph_nodes WHERE id IN ({placeholders}) "  # noqa: S608
                f"AND id NOT IN (SELECT DISTINCT node_id FROM graph_node_documents)",
                candidate_node_ids,
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

    # --- Confluence Sync Targets ---

    async def upsert_sync_target(
        self,
        *,
        scope: str,
        space_key: str,
        page_id: str | None,
        name: str,
    ) -> dict[str, Any]:
        """싱크 대상을 생성하거나 이름만 갱신하고 전체 행을 반환한다.

        동일 ``(scope, space_key, COALESCE(page_id, ''))`` 조합이 이미 있으면
        ``name`` 만 갱신하고, 없으면 새로 생성한다.
        """
        cursor = await self.db.execute(
            """SELECT * FROM confluence_sync_targets
               WHERE scope = ? AND space_key = ?
                 AND COALESCE(page_id, '') = COALESCE(?, '')""",
            (scope, space_key, page_id),
        )
        existing = await cursor.fetchone()
        if existing is not None:
            await self.db.execute(
                "UPDATE confluence_sync_targets SET name = ? WHERE id = ?",
                (name, existing["id"]),
            )
            await self.db.commit()
            target_id = existing["id"]
        else:
            cursor = await self.db.execute(
                """INSERT INTO confluence_sync_targets
                   (scope, space_key, page_id, name)
                   VALUES (?, ?, ?, ?)""",
                (scope, space_key, page_id, name),
            )
            await self.db.commit()
            target_id = cursor.lastrowid  # type: ignore[assignment]

        cursor = await self.db.execute(
            "SELECT * FROM confluence_sync_targets WHERE id = ?", (target_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else {}

    async def get_sync_target(self, target_id: int) -> dict[str, Any] | None:
        """ID로 싱크 대상을 조회한다."""
        cursor = await self.db.execute(
            "SELECT * FROM confluence_sync_targets WHERE id = ?", (target_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_sync_targets(self) -> list[dict[str, Any]]:
        """등록된 모든 싱크 대상을 최신순으로 반환한다.

        ``created_at`` 이 동일한 경우(SQLite CURRENT_TIMESTAMP는 초 단위)
        ``id`` 로 tie-break 하여 나중에 추가된 것이 먼저 오도록 한다.
        """
        cursor = await self.db.execute(
            "SELECT * FROM confluence_sync_targets "
            "ORDER BY created_at DESC, id DESC",
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def update_sync_result(
        self, target_id: int, result_json: str,
    ) -> None:
        """직전 싱크 결과와 ``last_sync_at`` 을 갱신한다."""
        await self.db.execute(
            """UPDATE confluence_sync_targets
               SET last_sync_at = CURRENT_TIMESTAMP, last_result_json = ?
               WHERE id = ?""",
            (result_json, target_id),
        )
        await self.db.commit()

    async def update_sync_watermark(
        self, target_id: int, watermark: str,
    ) -> None:
        """증분 fetch 워터마크를 갱신한다.

        임포트 실패 페이지가 :meth:`replace_fetch_retries` 로 기록된 뒤에만
        호출되어야 한다 — 실패분을 재시도 목록에 남기지 않고 전진시키면
        그 사이의 변경이 다음 싱크의 변경 조회에서 누락된다.
        """
        await self.db.execute(
            "UPDATE confluence_sync_targets SET last_watermark = ? WHERE id = ?",
            (watermark, target_id),
        )
        await self.db.commit()

    async def list_fetch_retry_page_ids(self, target_id: int) -> set[str]:
        """Target 의 강제 재fetch 대기 page_id 집합을 반환한다.

        Phase 1 임포트가 실패해 기록된 페이지들 — 다음 싱크에서 변경 후보
        조회 결과와 무관하게 항상 fetch 대상에 포함되어야 한다.
        """
        cursor = await self.db.execute(
            "SELECT page_id FROM confluence_sync_fetch_retries "
            "WHERE target_id = ?",
            (target_id,),
        )
        rows = await cursor.fetchall()
        return {row["page_id"] for row in rows}

    async def replace_fetch_retries(
        self, target_id: int, entries: Iterable[dict[str, Any]],
    ) -> None:
        """Target 의 강제 재fetch 대기 목록을 통째로 교체한다.

        싱크 1회가 끝날 때마다 그 실행의 실패분으로 갱신된다 — 이전 목록의
        페이지는 이번 실행에서 강제 fetch 됐으므로, 성공했으면 목록에서
        빠지고 또 실패했으면 ``entries`` 로 다시 들어온다.

        Args:
            target_id: 대상 싱크 target ID.
            entries: ``{"page_id": str, "error": str}`` 목록. 빈 목록이면
                대기 목록이 비워진다 (전부 성공).
        """
        await self.db.execute(
            "DELETE FROM confluence_sync_fetch_retries WHERE target_id = ?",
            (target_id,),
        )
        rows = [
            (target_id, str(e["page_id"]), str(e.get("error", "")))
            for e in entries
            if e.get("page_id")
        ]
        if rows:
            await self.db.executemany(
                """INSERT INTO confluence_sync_fetch_retries
                   (target_id, page_id, error, updated_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(target_id, page_id) DO UPDATE SET
                     error = excluded.error,
                     updated_at = CURRENT_TIMESTAMP""",
                rows,
            )
        await self.db.commit()

    async def delete_sync_target(
        self, target_id: int,
    ) -> tuple[bool, list[int]]:
        """싱크 대상을 삭제하고 고아가 될 문서 ID 목록을 함께 반환한다.

        FK CASCADE로 해당 target의 membership 행은 자동 제거된다. 반환된
        ``orphan_doc_ids`` 에는 이 target 제거 뒤 어떤 target 에도 속하지
        않게 된 문서의 ID가 들어간다. 실제 문서 본체(벡터/그래프/메타)의
        cascade 삭제는 호출측에서 :func:`delete_document_cascade` 로 수행한다.

        Returns:
            ``(deleted, orphan_doc_ids)``. ``deleted`` 는 target이 존재해
            실제로 지워졌는지 여부.
        """
        orphan_doc_ids = await self._find_orphans_if_membership_dropped(
            target_id, page_ids=None,
        )
        cursor = await self.db.execute(
            "DELETE FROM confluence_sync_targets WHERE id = ?", (target_id,),
        )
        await self.db.commit()
        return (cursor.rowcount > 0), orphan_doc_ids

    # --- Confluence Sync Membership ---

    async def upsert_membership(
        self,
        *,
        target_id: int,
        page_id: str,
        space_key: str,
        parent_page_id: str | None = None,
        depth: int | None = None,
    ) -> None:
        """단일 membership 행을 upsert 한다 (``last_seen_at`` 갱신)."""
        await self.db.execute(
            """INSERT INTO confluence_sync_membership
               (target_id, page_id, space_key, parent_page_id, depth, last_seen_at)
               VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(target_id, page_id) DO UPDATE SET
                 space_key = excluded.space_key,
                 parent_page_id = excluded.parent_page_id,
                 depth = excluded.depth,
                 last_seen_at = CURRENT_TIMESTAMP""",
            (target_id, page_id, space_key, parent_page_id, depth),
        )
        await self.db.commit()

    async def upsert_membership_batch(
        self,
        target_id: int,
        space_key: str,
        nodes: Iterable[dict[str, Any]],
    ) -> None:
        """여러 membership을 한 트랜잭션에서 upsert 한다.

        ``nodes`` 각 항목은 최소 ``id`` 필드가 있어야 하며, 선택적으로
        ``parent_id``, ``depth`` 를 포함할 수 있다. walker/enumerate 출력
        형태를 그대로 받을 수 있다.
        """
        rows = [
            (
                target_id,
                str(node["id"]),
                space_key,
                node.get("parent_id"),
                node.get("depth"),
            )
            for node in nodes
            if node.get("id")
        ]
        if not rows:
            return
        await self.db.executemany(
            """INSERT INTO confluence_sync_membership
               (target_id, page_id, space_key, parent_page_id, depth, last_seen_at)
               VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(target_id, page_id) DO UPDATE SET
                 space_key = excluded.space_key,
                 parent_page_id = excluded.parent_page_id,
                 depth = excluded.depth,
                 last_seen_at = CURRENT_TIMESTAMP""",
            rows,
        )
        await self.db.commit()

    async def list_membership_page_ids(self, target_id: int) -> set[str]:
        """Target이 소유하는 page_id 집합을 반환한다."""
        cursor = await self.db.execute(
            "SELECT page_id FROM confluence_sync_membership WHERE target_id = ?",
            (target_id,),
        )
        rows = await cursor.fetchall()
        return {row["page_id"] for row in rows}

    async def list_failed_member_doc_ids(self, target_id: int) -> list[int]:
        """Target 의 membership 에 속한 문서 중 ``status='failed'`` 인 doc_id 목록.

        재싱크 시 Phase 2 가 이전에 인덱싱 실패한 문서를 자동 재시도하도록
        식별하기 위한 헬퍼. Phase 1 이 해당 문서를 ``unchanged`` 로 분류해
        Phase 2 큐에서 누락되는 것을 보완한다.
        """
        cursor = await self.db.execute(
            """SELECT d.id FROM documents d
               INNER JOIN confluence_sync_membership m
                 ON d.source_id = m.page_id
                 AND d.source_type = 'confluence_mcp'
               WHERE m.target_id = ? AND d.status = 'failed'""",
            (target_id,),
        )
        rows = await cursor.fetchall()
        return [row["id"] for row in rows]

    async def list_fallback_title_page_ids(self, target_id: int) -> set[str]:
        """Target membership 중 폴백 제목("Confluence Page {id}")으로 저장된
        문서의 page_id 집합을 반환한다.

        과거 임포트에서 getPageByID 응답 파싱 실패로 제목이 오염된 문서를
        증분 fetch 대상에 강제 포함시키기 위한 헬퍼. 워터마크 이후 소스측
        변경이 없는 페이지는 증분 선정에서 제외되어 오염이 영구 잔존하는
        문제를 막는다 — 제목이 치유되면 자연히 이 집합에서 빠진다.
        """
        cursor = await self.db.execute(
            """SELECT m.page_id FROM confluence_sync_membership m
               INNER JOIN documents d
                 ON d.source_id = m.page_id
                 AND d.source_type = 'confluence_mcp'
               WHERE m.target_id = ?
                 AND d.title = 'Confluence Page ' || d.source_id""",
            (target_id,),
        )
        rows = await cursor.fetchall()
        return {row["page_id"] for row in rows}

    async def list_degraded_member_doc_ids(self, target_id: int) -> list[int]:
        """Target 의 membership 에 속한 문서 중 LLM 결손(``llm_degraded=1``) 목록.

        가상 질문 생성 / LLM 본문 그래프 추출 호출이 실패해 검색 품질이
        저하된 채 ``status='completed'`` 로 마감된 문서를 다음 재싱크 때
        자동으로 재인덱싱 큐에 포함시키기 위한 헬퍼. ``failed`` 자동 재시도
        (:meth:`list_failed_member_doc_ids`) 와 동일한 JOIN 구조를 쓴다.
        """
        cursor = await self.db.execute(
            """SELECT d.id FROM documents d
               INNER JOIN confluence_sync_membership m
                 ON d.source_id = m.page_id
                 AND d.source_type = 'confluence_mcp'
               WHERE m.target_id = ?
                 AND d.llm_degraded = 1
                 AND d.status = 'completed'""",
            (target_id,),
        )
        rows = await cursor.fetchall()
        return [row["id"] for row in rows]

    async def set_llm_degraded(
        self,
        document_id: int,
        *,
        degraded: bool,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """문서의 LLM 결손 플래그/상세를 기록한다.

        Args:
            document_id: 대상 문서 ID.
            degraded: 결손 여부. ``False`` 면 플래그를 해제하고 detail 을 비운다
                (이전에 degraded 였던 문서가 정상 재처리되면 자동 해제).
            detail: 결손 상세(질문/그래프 결손 수치). ``None`` 이거나 degraded
                 가 ``False`` 면 detail 컬럼은 NULL 로 저장된다.
        """
        detail_json = (
            json.dumps(detail, ensure_ascii=False)
            if degraded and detail is not None
            else None
        )
        await self.db.execute(
            """UPDATE documents
               SET llm_degraded = ?, llm_degraded_detail = ?,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (1 if degraded else 0, detail_json, document_id),
        )
        await self.db.commit()

    async def remove_memberships(
        self,
        target_id: int,
        page_ids: Iterable[str],
    ) -> list[int]:
        """주어진 페이지들의 membership을 제거하고 고아 문서 ID를 반환한다.

        고아 판정: 이 target에서 제거한 뒤 해당 page_id 에 대한 membership이
        하나도 남지 않는 경우, ``documents`` 테이블에서 ``source_type=
        'confluence_mcp' AND source_id=page_id`` 에 매치되는 문서 ID를 결과에
        포함시킨다. 실제 문서 cascade 삭제는 호출측의 책임이다.
        """
        page_ids_list = [str(pid) for pid in page_ids]
        if not page_ids_list:
            return []

        orphan_doc_ids = await self._find_orphans_if_membership_dropped(
            target_id, page_ids=page_ids_list,
        )

        placeholders = ",".join("?" * len(page_ids_list))
        await self.db.execute(
            f"DELETE FROM confluence_sync_membership "  # noqa: S608
            f"WHERE target_id = ? AND page_id IN ({placeholders})",
            [target_id, *page_ids_list],
        )
        await self.db.commit()
        return orphan_doc_ids

    async def _find_orphans_if_membership_dropped(
        self,
        target_id: int,
        page_ids: list[str] | None,
    ) -> list[int]:
        """해당 target의 ``page_ids`` membership을 삭제한다고 가정했을 때
        고아가 되는 ``documents.id`` 목록을 계산한다(실제 삭제는 하지 않음).

        ``page_ids`` 가 ``None`` 이면 이 target의 모든 membership을 대상으로 한다.
        """
        if page_ids is None:
            cursor = await self.db.execute(
                "SELECT page_id FROM confluence_sync_membership "
                "WHERE target_id = ?",
                (target_id,),
            )
            rows = await cursor.fetchall()
            target_pages = [row["page_id"] for row in rows]
        else:
            target_pages = list(page_ids)

        if not target_pages:
            return []

        placeholders = ",".join("?" * len(target_pages))
        # 이 target의 membership을 "제거한 뒤" 남는 membership 수를 센다.
        cursor = await self.db.execute(
            f"""SELECT page_id, COUNT(*) AS cnt
                FROM confluence_sync_membership
                WHERE page_id IN ({placeholders})
                  AND target_id != ?
                GROUP BY page_id""",  # noqa: S608
            [*target_pages, target_id],
        )
        rows = await cursor.fetchall()
        still_owned = {row["page_id"] for row in rows if row["cnt"] > 0}
        orphan_pages = [pid for pid in target_pages if pid not in still_owned]

        if not orphan_pages:
            return []

        placeholders = ",".join("?" * len(orphan_pages))
        cursor = await self.db.execute(
            f"""SELECT id FROM documents
                WHERE source_type = 'confluence_mcp'
                  AND source_id IN ({placeholders})""",  # noqa: S608
            orphan_pages,
        )
        rows = await cursor.fetchall()
        return [row["id"] for row in rows]

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
