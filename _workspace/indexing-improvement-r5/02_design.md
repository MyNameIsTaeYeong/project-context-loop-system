# R5 설계 — 저장 측 stem dedup (옵션 3: 백필 + strict stem SQL + ORDER BY id ASC)

분석가 권고를 그대로 구현하기 위한 패치 설계서. 새 결정은 만들지 않는다.

---

## 1. 변경 파일 전체 목록 + 변경 라인 수 추정

| 파일 | 변경 내용 | 추정 라인 수 |
|---|---|---|
| `src/context_loop/storage/metadata_store.py` | `_SCHEMA_SQL` 에 `name_stem` 컬럼 + INDEX 추가 / `_migrate_schema` 에 ALTER + 백필 분기 / `find_graph_node_by_entity` SQL 교체 / `create_graph_node_with_link` 시그니처·INSERT 확장 / `normalize_name_stem` import | +50 (NET +35) |
| `src/context_loop/storage/graph_store.py` | `normalize_name_stem` import / `save_graph_data` 에서 stem 계산 → `find_graph_node_by_entity` / `create_graph_node_with_link` 에 전달 / `load_from_db` 에서 NetworkX 노드에 `name_stem` 속성 노출 | +10 |
| `src/context_loop/processor/graph_vocabulary.py` | (변경 없음) | 0 |
| `tests/test_storage/test_metadata_store.py` | 스키마·INDEX 검증 + stem 매칭 4 케이스 + 백필 검증 + 멱등성 검증 | +120 |
| `tests/test_storage/test_graph_store.py` | 문서 간 stem dedup 통합 시나리오 (D1 `AuthService` → D2 `Auth Service`) | +30 |

합계: 운영 코드 ~45줄, 테스트 ~150줄. 운영 5파일 이내 (실제 2 파일).

---

## 2. 패치별 상세 설계

### 2.1 스키마 ALTER + 백필 (`metadata_store.py`)

#### 2.1.1 `_SCHEMA_SQL` 변경 — `metadata_store.py:47-53`

```sql
CREATE TABLE IF NOT EXISTS graph_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    entity_name TEXT NOT NULL,
    entity_type TEXT,
    name_stem TEXT,                 -- 신규 (NULL 허용; 백필로 채움)
    properties TEXT
);
```

그리고 INDEX 블록 (`L120-131`) 끝에 추가:

```sql
CREATE INDEX IF NOT EXISTS idx_graph_nodes_stem_type
    ON graph_nodes(name_stem, entity_type);
```

`CREATE TABLE IF NOT EXISTS` 와 `CREATE INDEX IF NOT EXISTS` 모두 멱등이므로 신규 DB / 기존 DB 양쪽 안전.

#### 2.1.2 `_migrate_schema` 분기 추가 — `metadata_store.py:157-181` 끝에 부착

```python
async def _migrate_schema(self) -> None:
    """기존 DB에 누락된 컬럼을 idempotent하게 추가한다."""
    # ... 기존 documents/chunks 분기 그대로 ...

    # graph_nodes — R5: name_stem 컬럼 + 백필
    cursor = await self.db.execute("PRAGMA table_info(graph_nodes)")
    node_cols = {row["name"] for row in await cursor.fetchall()}
    if "name_stem" not in node_cols:
        # 1) 컬럼 추가 — SQLite 의 ALTER TABLE ADD COLUMN 은 기본값 없는
        #    TEXT 컬럼을 허용 (모든 기존 행에 NULL 채움)
        await self.db.execute(
            "ALTER TABLE graph_nodes ADD COLUMN name_stem TEXT"
        )

    # 2) NULL 백필 — name_stem 이 비어 있는 행에만 적용 (멱등)
    cursor = await self.db.execute(
        "SELECT id, entity_name FROM graph_nodes WHERE name_stem IS NULL"
    )
    rows = await cursor.fetchall()
    if rows:
        # Python 측에서 normalize_name_stem 을 호출해 row 단위 UPDATE
        # SQLite 사용자 함수 등록은 aiosqlite 추상화·테스트 격리 비용이
        # 크고, 6000 노드 기준 UPDATE 시간이 < 1초로 무시 가능하므로
        # 단순 루프를 채택.
        updates = [
            (normalize_name_stem(row["entity_name"]), row["id"])
            for row in rows
        ]
        await self.db.executemany(
            "UPDATE graph_nodes SET name_stem = ? WHERE id = ?",
            updates,
        )
    # INDEX 는 _SCHEMA_SQL 의 CREATE INDEX IF NOT EXISTS 가 이미 처리
    # (initialize() 가 executescript → _migrate_schema → commit 순서이므로
    # INDEX 생성은 백필 직전 단계에서 끝나 있다)
```

#### 2.1.3 백필 동작 방식 결정 근거

세 후보:
- **Python 측 row-loop UPDATE** *(채택)* — `normalize_name_stem` 단일 출처 보장, 의존 추가 없음, 테스트 격리 용이.
- SQLite 사용자 함수 등록 — `aiosqlite` 에서 가능하지만 동기 함수만 가능, 그리고 `_SCHEMA_SQL` 의 declarative 스타일을 깬다.
- `REPLACE` 누적 SQL — `LOWER(REPLACE(REPLACE(REPLACE(entity_name,'-',''),'_',''),' ',''))` 식으로 가능하지만 `normalize_name_stem` 과 *항상 동일* 함을 컴파일타임 보증 불가 — 알리아싱 위험.

#### 2.1.4 import 추가 — `metadata_store.py:14` 다음

```python
from context_loop.processor.graph_vocabulary import normalize_name_stem
```

분석가가 1.4 절에서 확인: `processor.graph_vocabulary` 는 `storage` import 가 없어 순환 없음.

#### 2.1.5 멱등성 보장

- 컬럼 존재 시: `PRAGMA table_info` 가드로 ALTER 건너뜀.
- 백필 NULL 가드: `WHERE name_stem IS NULL` 로 이미 채워진 행 건너뜀. 두 번 호출해도 zero UPDATE.
- INDEX: `IF NOT EXISTS` 로 중복 생성 무해.

전부 `initialize()` 의 단일 트랜잭션 (executescript → migrate → commit) 안에서 처리되어 외부 가시성 일관.

---

### 2.2 `find_graph_node_by_entity` 변경

#### 2.2.1 시그니처 결정 — **내부 stem 계산 방식 권고**

두 안 비교:

| 안 | 시그니처 | 호출자 책임 | 장점 | 단점 |
|---|---|---|---|---|
| A | `find_graph_node_by_entity(entity_name, entity_type)` 유지, 함수 *내부* 에서 stem 계산 | 호출자는 R4 와 동일 | 호출자 무수정, 회귀 위험 0, stem 정의 단일 진입점 | 호출자가 이미 stem 을 들고 있어도 재계산 (저비용) |
| B | `find_graph_node_by_entity_stem(name_stem, entity_type)` 신규, 호출자가 stem 전달 | 호출자가 매번 `normalize_name_stem` 호출 | stem 미사용 호출자 식별 명시화 | 운영 호출자 1곳뿐인데 시그니처 변경 비용. 테스트 호출자도 모두 갱신. 호출자가 `normalize_name_stem` 우회 시 silent dedup 손실 |

**권고: 안 A.** 회귀 위험을 최소화하면서 stem 단일 출처(`normalize_name_stem`) 를 함수 *내부* 에 묶어 호출자가 잘못 계산할 여지를 차단. 운영 호출자(`save_graph_data`) 는 별도로 stem 을 *INSERT* 에 넘겨야 하므로 거기서만 stem 을 계산하면 된다 — `find` 호출 측에서 같은 stem 을 재계산하더라도 비용 무시.

#### 2.2.2 새 SQL — `metadata_store.py:447-460` 전체 교체

```python
async def find_graph_node_by_entity(
    self,
    entity_name: str,
    entity_type: str,
) -> dict[str, Any] | None:
    """엔티티 이름+타입으로 기존 정규 노드를 stem 매칭으로 검색한다.

    `name_stem` (`normalize_name_stem(entity_name)`) 과 `entity_type` 의
    정확 매칭. R4 에서 추출 측이 표기 변형을 흡수했지만, 문서 간 동일
    엔티티가 표기만 다르게 들어오는 경우 (`AuthService` vs `Auth Service`)
    저장 측에서 한 번 더 통합한다.

    동등 stem 의 다중 노드 시 `id ASC` 로 결정성 부여 — 마이그레이션 백필
    이전 데이터의 분리된 노드 여러 개가 동일 stem 으로 채워질 때 항상
    *최초 생성된* 노드를 winner 로 선택.
    """
    stem = normalize_name_stem(entity_name)
    cursor = await self.db.execute(
        """SELECT * FROM graph_nodes
           WHERE name_stem = ? AND entity_type = ?
           ORDER BY id ASC
           LIMIT 1""",
        (stem, entity_type),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None
```

`(name_stem, entity_type)` 복합 INDEX 가 `WHERE` 절을 그대로 커버. `ORDER BY id ASC` 는 PK 의 자연 순서이므로 추가 sort 비용 없음 (혹은 옵티마이저가 sort skip).

#### 2.2.3 docstring 갱신

기존 "(대소문자 무시)" 표현 제거 — stem 매칭은 대소문자 무시를 *포함* 하지만 더 넓다 (공백/하이픈/언더스코어).

---

### 2.3 `create_graph_node_with_link` 변경

#### 2.3.1 시그니처 변경 — `metadata_store.py:389-396`

```python
async def create_graph_node_with_link(
    self,
    *,
    document_id: int,
    entity_name: str,
    name_stem: str,                       # 신규 (필수)
    entity_type: str | None = None,
    properties: str | None = None,
) -> int:
```

`name_stem` 을 **필수 키워드 인자** 로 정의 — 호출자가 잊으면 즉시 `TypeError` 가 나서 silent dedup 손실을 차단한다. 분석가가 5.1 절에서 *기본값 None* 도 고려했으나 silent 손실 위험이 더 크다고 판단되므로 필수로 격상.

> 운영 호출자는 `graph_store.py:199` 1 곳뿐이므로 시그니처 깨짐의 회귀 표면적이 작다. 테스트 호출자 2 곳(`tests/test_storage/test_metadata_store.py:341, 372`) 은 R5 에서 같이 갱신한다 (4 절 적용 순서 참고).

#### 2.3.2 INSERT 확장 — `metadata_store.py:409-413`

```python
cursor = await self.db.execute(
    "INSERT INTO graph_nodes "
    "(document_id, entity_name, entity_type, name_stem, properties) "
    "VALUES (?, ?, ?, ?, ?)",
    (document_id, entity_name, entity_type, name_stem, properties),
)
```

이후 단일-commit 보장 (`metadata_store.py:418-423` 의 link INSERT + `commit`) 은 *그대로 유지*. 분석가 5.3 절: 트랜잭션 모양 변경 아님.

#### 2.3.3 `create_graph_node` (단독 함수) 처리

분석가 2.5 절: 테스트만 사용. R5 스코프 결정 — **변경 없음**. 단독 함수가 stem 을 비워두면 백필이 후속에 자동으로 채울 수 있지만, 본 라운드에선 운영 경로(`with_link`)만 stem 을 채우면 dedup 효과는 달성. 시그니처 변경 비용 ↑ 대비 효과 ↓.

다만 `create_graph_node` 가 stem NULL 로 신규 행을 만들면 R6 측 stem 검색에서 빈틈이 생긴다 — 그러나 *현재 단독 호출자는 모두 테스트* 라 운영 데이터엔 영향 없다. 이 결정은 5절 리스크 가드에 명시.

---

### 2.4 `save_graph_data` 변경 (`graph_store.py`)

#### 2.4.1 import 추가 — `graph_store.py:19` 다음

```python
from context_loop.processor.graph_vocabulary import normalize_name_stem
```

분석가 4 절: 이미 `from context_loop.processor.graph_extractor import GraphData` 로 동일 방향 import 존재 — 신규 import 안전.

#### 2.4.2 엔티티 루프 갱신 — `graph_store.py:160-214`

```python
for entity in graph_data.entities:
    props = {"description": entity.description} if entity.description else {}
    entity_stem = normalize_name_stem(entity.name)        # 신규

    existing = await self._store.find_graph_node_by_entity(
        entity.name, entity.entity_type,                  # (A안: stem 내부 계산)
    )

    if existing:
        # ... (기존 분기 그대로) ...
    else:
        node_id = await self._store.create_graph_node_with_link(
            document_id=document_id,
            entity_name=entity.name,
            name_stem=entity_stem,                        # 신규 — 필수
            entity_type=entity.entity_type,
            properties=json.dumps(props, ensure_ascii=False),
        )
        # ... (이후 NetworkX add_node 부분에 name_stem 속성 노출) ...
        self._graph.add_node(
            node_id,
            entity_name=entity.name,
            entity_type=entity.entity_type,
            name_stem=entity_stem,                        # 신규 (옵션이지만 권고)
            document_ids={document_id},
            properties=props,
        )

    name_to_node_id[entity.name] = node_id
```

병합 분기 (`L186-192`) 의 NetworkX `add_node` 도 동일하게 `name_stem` 속성 추가. 기존 `existing` 분기는 SQLite 의 stem 이 이미 채워져 있으므로 그 값을 사용해도 되지만, 일관성 위해 `entity_stem` 으로 갱신해도 무방 (같은 값).

#### 2.4.3 `load_from_db` 의 NetworkX 노드 속성 — `graph_store.py:101-112`

```python
for node in nodes:
    doc_ids = set(node_doc_links.get(node["id"], []))
    if not doc_ids and node.get("document_id"):
        doc_ids = {node["document_id"]}
    self._graph.add_node(
        node["id"],
        entity_name=node["entity_name"],
        entity_type=node.get("entity_type", "other"),
        name_stem=node.get("name_stem"),                  # 신규 — NULL 도 허용
        document_ids=doc_ids,
        properties=json.loads(node["properties"] or "{}"),
    )
```

`node.get("name_stem")` 으로 NULL 안전 — 백필 전 / 백필 직후 / 정상 신규 노드 모두 한 코드 경로로 처리. 회귀 위험 0 (NetworkX 가 None 속성을 허용).

---

## 3. 단위 테스트 설계

### 3.1 새 스키마 + INDEX 검증 (`tests/test_storage/test_metadata_store.py`)

```python
async def test_graph_nodes_has_name_stem_column_and_index(store):
    """R5 스키마 마이그레이션 — name_stem 컬럼과 (name_stem, entity_type)
    복합 INDEX 가 신규/기존 DB 양쪽에서 존재한다."""
    cursor = await store.db.execute("PRAGMA table_info(graph_nodes)")
    cols = {row["name"] for row in await cursor.fetchall()}
    assert "name_stem" in cols
    cursor = await store.db.execute("PRAGMA index_list(graph_nodes)")
    indexes = {row["name"] for row in await cursor.fetchall()}
    assert "idx_graph_nodes_stem_type" in indexes
```

### 3.2 `find_graph_node_by_entity` stem 매칭 4 케이스

```python
async def test_find_graph_node_by_entity_stem_match_hit_surface_variant(store):
    """AuthService 저장 + 'Auth Service' 조회 → hit."""
    doc = await store.create_document(...)
    nid = await store.create_graph_node_with_link(
        document_id=doc, entity_name="AuthService",
        name_stem="authservice", entity_type="system",
    )
    found = await store.find_graph_node_by_entity("Auth Service", "system")
    assert found is not None and found["id"] == nid

async def test_find_graph_node_by_entity_stem_match_hit_exact(store):
    """AuthService 저장 + 'AuthService' 정확 일치 → hit."""
    # ... 동일 ...

async def test_find_graph_node_by_entity_stem_match_miss_different_root(store):
    """AuthService 저장 + 'AuthorizationService' → miss (stem 다름)."""
    # ... AuthService 저장 후 AuthorizationService 조회, None ...

async def test_find_graph_node_by_entity_stem_match_miss_plural(store):
    """User 저장 + 'Users' → miss (형태론 보존)."""
    # ... User 저장 (name_stem='user') 후 Users 조회 (stem='users'), None ...
```

### 3.3 `save_graph_data` 문서 간 통합 (`tests/test_storage/test_graph_store.py`)

```python
async def test_save_graph_data_dedups_across_documents_by_stem(
    graph_store, meta_store
):
    """D1 'AuthService' → D2 'Auth Service' → 단일 노드, document_ids=={D1, D2}."""
    d1 = await meta_store.create_document(source_type="manual", title="d1",
        original_content="x", content_hash="h1")
    d2 = await meta_store.create_document(source_type="manual", title="d2",
        original_content="y", content_hash="h2")

    await graph_store.save_graph_data(d1, GraphData(
        entities=[Entity(name="AuthService", entity_type="system")],
        relations=[],
    ))
    await graph_store.save_graph_data(d2, GraphData(
        entities=[Entity(name="Auth Service", entity_type="system")],
        relations=[],
    ))

    all_nodes = await meta_store.get_all_graph_nodes()
    assert len([n for n in all_nodes if n["entity_type"] == "system"]) == 1
    node = next(n for n in all_nodes if n["entity_type"] == "system")
    links = await meta_store.get_node_document_ids(node["id"])
    assert set(links) == {d1, d2}
```

### 3.4 백필 동작 + 멱등성

```python
async def test_migrate_schema_backfills_existing_name_stem(tmp_path):
    """기존 DB (name_stem 컬럼 없음) 에 노드를 만들어두고 마이그레이션 후
    모든 노드의 name_stem 이 normalize_name_stem(entity_name) 으로 채워진다."""
    # 1) 임시 DB 에 R5 이전 스키마로 노드 INSERT (SQL 직접 실행으로 모사)
    # 2) MetadataStore.initialize() 호출 — _migrate_schema 실행
    # 3) SELECT entity_name, name_stem FROM graph_nodes
    # 4) 모든 행에 대해 normalize_name_stem(entity_name) == name_stem 검증

async def test_migrate_schema_is_idempotent(store):
    """두 번 호출해도 안전 — ALTER 가드 + WHERE name_stem IS NULL 가드."""
    await store._migrate_schema()      # 두 번째 호출
    cursor = await store.db.execute(
        "SELECT COUNT(*) FROM graph_nodes WHERE name_stem IS NULL"
    )
    row = await cursor.fetchone()
    assert row[0] == 0
```

`tmp_path` 픽스처로 *R5 이전 스키마* DB 를 위조하는 방식: `aiosqlite.connect` 직접 열어 `CREATE TABLE graph_nodes ...` 를 `name_stem` 없이 실행 → 더미 행 몇 개 INSERT → close → `MetadataStore(...)` + `initialize()` 호출. 분석가 5.6 절의 호출 위치와 정합.

### 3.5 기존 테스트 회귀 점검

| 기존 테스트 | 영향 | 조치 |
|---|---|---|
| `tests/test_storage/test_metadata_store.py::test_create_graph_node_with_link_atomic` (L332) | `create_graph_node_with_link` 의 시그니처에 `name_stem` 필수 키워드 추가 → 호출이 `TypeError` 발생 | 호출에 `name_stem="x"` 추가 |
| `tests/test_storage/test_metadata_store.py::test_delete_graph_data_by_document_narrow_orphan_cleanup` (L351) | 동일 — `create_graph_node_with_link` 사용 | 호출에 `name_stem="x"` 추가 |
| `tests/test_storage/test_graph_store.py` 의 모든 `save_graph_data` 테스트 | `save_graph_data` 내부 호출이 갱신됨 — 시그니처는 외부 동일, 동작은 stem 매칭으로 변함. 기존 테스트가 동일 문서 내 *표기 변형* 을 명시 검증하는 케이스가 있다면 단일 노드로 dedup 되어 카운트 변할 가능성. | grep 후 단건 검토 — `test_save_graph_data` (L61) 류는 `entity_name` 이 완전 다른 이름이라 영향 없음으로 *추측*. 회귀 실행 시 확인 |
| `tests/test_processor/test_reprocessor.py:75` 의 `create_graph_node` | 단독 함수는 R5 변경 없음 | 무영향 |
| `tests/test_web/`, `tests/test_mcp/` | `get_graph_nodes_by_document` 응답 dict 에 `name_stem` 키 추가됨. 응답 스키마가 strict 검증이 아니면 무해 | 분석가 5.5 절의 *추측* 확인 — pytest 실행으로 검증 |

---

## 4. 적용 순서 권장

각 단계 직후 회귀 테스트 시점 명시 (점진적 적용):

1. **스키마 + 마이그레이션**
   - `_SCHEMA_SQL` 에 `name_stem` 컬럼 + INDEX 추가
   - `_migrate_schema` 에 graph_nodes 분기 추가 + `normalize_name_stem` import
   - 회귀: `pytest tests/test_storage/test_metadata_store.py -k "not name_stem"` — 기존 통과 확인 (실패 0)

2. **`find_graph_node_by_entity` SQL 교체** (안 A: 내부 stem 계산)
   - 회귀: 동일 `pytest tests/test_storage/test_metadata_store.py` — 호출 시그니처 무변동이므로 통과

3. **`create_graph_node_with_link` 시그니처 + INSERT 변경**
   - 기존 테스트 2 곳 (`L341, L372`) 의 호출에 `name_stem=...` 추가
   - 회귀: `pytest tests/test_storage/test_metadata_store.py` — `TypeError` 없음 확인

4. **`save_graph_data` 호출 갱신 + `load_from_db` 의 `name_stem` 속성 노출**
   - 회귀: `pytest tests/test_storage/test_graph_store.py` — 기존 통과 + 신규 dedup 시나리오 RED → GREEN

5. **신규 단위 테스트 추가** (3.1 ~ 3.4)
   - 회귀: `pytest tests/test_storage/` 전체 GREEN

6. **통합 회귀**
   - `pytest tests/test_processor/` (R4 결과 보존 확인)
   - `pytest tests/test_web/`, `pytest tests/test_mcp/` (응답 dict 의 `name_stem` 키 영향 점검)
   - 전체 `pytest`

---

## 5. 리스크 및 가드

### 5.1 백필 UPDATE 의 성능

6000 노드 기준 `executemany("UPDATE ... WHERE id = ?", updates)` 는 WAL 모드 SQLite 에서 *추측* 100~500ms. 사내 환경의 `initialize()` 호출이 부팅 / 테스트 fixture 당 1회뿐이므로 누적 비용 무시 가능. 가드: 처음 마이그레이션 후 `name_stem IS NULL` 행이 0 이므로 후속 부팅의 비용은 SELECT 1 회 (NULL 0 행).

### 5.2 `name_stem` 컬럼이 NULL 인 신규 INSERT 방어

NOT NULL 제약 불가 (ALTER 호환성). 따라서 운영 INSERT 경로에서 누락 시 *조용한* dedup 손실. 대책:
- `create_graph_node_with_link` 의 `name_stem: str` *필수* 키워드 — TypeError 로 빠른 실패.
- 단독 `create_graph_node` 는 R5 스코프 밖이므로 변경 없음 — 단독 호출자가 모두 테스트뿐이라 운영 데이터에 NULL 신규 행이 들어오지 않음.

### 5.3 호출자가 stem 을 잘못 계산하는 위험

가드: `normalize_name_stem` 단일 import 출처 — `graph_vocabulary.py:260`. `metadata_store.py` 와 `graph_store.py` 가 같은 함수를 import 하므로 stem 정의 변경 시 자동 전파. 호출자가 직접 stem 문자열 리터럴을 만들지 않도록 코드 리뷰 가드 + 테스트 (`test_save_graph_data_dedups_across_documents_by_stem`) 로 행위 보증.

### 5.4 동시성 / 트랜잭션 경계

`create_graph_node_with_link` 의 단일-commit 보장 (분석가 1.3 / 5.3) 은 INSERT 컬럼 1개 추가만으로 변하지 않는다. `_migrate_schema` 의 ALTER + 백필 + INDEX 가 `initialize()` 의 단일 `commit` 안에서 끝나므로 외부 코루틴은 일관 상태만 본다.

### 5.5 `find_graph_node_by_entity` 후 `create_graph_node_with_link` 사이 race

기존부터 존재하는 race window — `await` 양보 시점에 다른 코루틴이 같은 stem 으로 INSERT 가능. R5 가 *새로* 만드는 위험 아님 (분석가 5.3). R5 스코프 밖.

### 5.6 데이터 보존

기존 분리된 두 노드 (예: `AuthService` id=1, `Auth Service` id=7) 는 백필 후에도 *분리 보존* — 통합 SQL 미사용 (사용자 결정). 신규 문서의 같은 stem 엔티티는 `ORDER BY id ASC LIMIT 1` 로 항상 *최초 생성된* id=1 노드와만 link 추가됨 → 결정성·재현성 보장. id=7 노드는 그대로 남아 후속 사용자 재인덱싱 시점에 자연 정리.

### 5.7 `get_graph_nodes_by_document` 응답에 새 키

`SELECT gn.*` 이라 자동 노출. 분석가 5.5 가드: pytest 의 `tests/test_web/`, `tests/test_mcp/` 통과로 응답 schema 영향 확인.

---

## 한 줄 요약

옵션 3 채택: `_SCHEMA_SQL` 컬럼 + INDEX 추가, `_migrate_schema` 에 ALTER + Python 루프 백필, `find_graph_node_by_entity` 는 내부 stem 계산 + strict SQL + `ORDER BY id ASC`, `create_graph_node_with_link` 는 `name_stem` 필수 키워드 추가, `save_graph_data` + `load_from_db` 가 stem 을 전달·노출. 운영 2파일 ~45줄, 테스트 ~150줄.

산출 파일: `/home/user/project-context-loop-system/_workspace/indexing-improvement-r5/02_design.md`
