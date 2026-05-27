# R5 분석 — 저장 측 stem dedup 영향·회귀·NULL safe 폴백

라운드 스코프(`_workspace/indexing-improvement-r5/00_round_scope.md`)에 명시된 변경(`graph_nodes.name_stem` 컬럼 추가, `find_graph_node_by_entity` stem 매칭 전환, `create_graph_node_with_link` 시그니처 확장)이 미치는 코드 영향과 회귀 위험만 정리한다. *수정은 하지 않았다.*

---

## 1. 현재 코드 상태 (file:line)

### 1.1 `find_graph_node_by_entity` 현재 SQL — `src/context_loop/storage/metadata_store.py:447-460`

```python
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
```

매칭 키: `LOWER(entity_name) + entity_type`. 인덱스 없음 — full scan 위에 `LOWER()` 적용.

### 1.2 `graph_nodes` 스키마 + INDEX — `metadata_store.py:47-53, 123-126`

컬럼 (`L47-53`):
- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE`
- `entity_name TEXT NOT NULL`
- `entity_type TEXT`
- `properties TEXT`

관련 INDEX (`L123-126`):
- `idx_graph_nodes_document ON graph_nodes(document_id)`
- `idx_graph_node_documents_node ON graph_node_documents(node_id)`
- `idx_graph_node_documents_document ON graph_node_documents(document_id)`

`entity_name` / `entity_type` / `(name_stem, entity_type)` 어느 것에도 인덱스 없음. 즉 현재 dedup 조회는 매번 full scan 이다(6000노드면 미세하지만 인덱싱-때마다 N 회 호출이므로 R5 가 인덱스 추가하면 부수 효과로 dedup 자체도 빨라진다 — *추측*).

마이그레이션 hook `_migrate_schema` (`L157-181`) 는 현재 `documents` / `chunks` 컬럼만 idempotent ALTER. `graph_nodes` 는 마이그레이션 분기가 아직 없다 → R5 에서 추가해야 할 위치는 명확하다 (`PRAGMA table_info(graph_nodes)` 결과에 `name_stem` 부재 시 `ALTER TABLE graph_nodes ADD COLUMN name_stem TEXT`).

### 1.3 `create_graph_node_with_link` 시그니처 — `metadata_store.py:389-424`

```python
async def create_graph_node_with_link(
    self,
    *,
    document_id: int,
    entity_name: str,
    entity_type: str | None = None,
    properties: str | None = None,
) -> int:
```

내부 동작 (`L409-423`): `graph_nodes` INSERT → `graph_node_documents` INSERT (`INSERT OR IGNORE`) → `commit` 1 회. 두 INSERT 가 단일 트랜잭션. race window 회피 의도가 주석에 박혀 있어 (`L400-404`), R5 에서 `name_stem` 컬럼이 추가되어도 *commit 경계는 유지되어야* 한다 (5절 참조).

호출자는 단 1 곳: `src/context_loop/storage/graph_store.py:199`. (단독 `create_graph_node` 는 별도 함수로 마이그레이션·테스트 전용으로 보존되었다 — `metadata_store.py:365-387`.)

### 1.4 `save_graph_data` 에서 dedup 호출 경로 — `graph_store.py:160-214`

```python
for entity in graph_data.entities:                                # L160
    ...
    existing = await self._store.find_graph_node_by_entity(       # L164
        entity.name, entity.entity_type,
    )
    if existing:                                                  # L168
        node_id = existing["id"]
        merged_count += 1
        ...
        await self._store.add_node_document_link(node_id, document_id)  # L179
        ...
    else:
        node_id = await self._store.create_graph_node_with_link(  # L199
            document_id=document_id,
            entity_name=entity.name,
            entity_type=entity.entity_type,
            properties=json.dumps(props, ensure_ascii=False),
        )
    name_to_node_id[entity.name] = node_id                        # L214
```

`name_to_node_id` 는 *그 문서 안에서만* 사용되는 로컬 dict 이고 키가 원본 `entity.name` 이므로, 한 문서 내 Layer 1 stem dedup 이 R4 에서 이미 끝났다면 이 dict 의 키 충돌은 사실상 없다. 하지만 R5 에서 *문서 간* stem 매칭이 켜지면 *같은 문서 내* 에서 표기만 다른 두 엔티티 (R4 Layer 1 이 어떤 이유로 놓친 경우) 가 들어왔을 때 두 번째가 `find_graph_node_by_entity` 로 첫 번째와 매칭되어 `name_to_node_id` 에 두 키가 같은 `node_id` 로 기록될 수 있다. 이는 의도된 동작이고 회귀가 아니다. 다만 `relation.source/target` 매핑(`L218-219`) 이 *raw 원본 이름* 으로 lookup 하므로 — R4 가 이미 처리한 사항이라 R5 가 새로 깨뜨릴 부분은 아니다.

### 1.5 `load_from_db` 의 노드 속성 — `graph_store.py:88-135`

L101-112 에서 노드에 넣는 속성:
- `entity_name = node["entity_name"]`
- `entity_type = node.get("entity_type", "other")`
- `document_ids = set(...)`
- `properties = json.loads(node["properties"] or "{}")`

`name_stem` 은 *현재 NetworkX 노드 속성으로 노출되지 않는다*. R5 가 이를 추가 노출할지 5.4 절에서 다룸.

---

## 2. 변경 표면적

### 2.1 `find_graph_node_by_entity` 호출자 — 전수

```
src/context_loop/storage/graph_store.py:164  (save_graph_data 내, 유일)
```

운영 코드 외부 호출자 없음. 즉 SQL 변경의 *직접* 영향은 `save_graph_data` 1 곳.

### 2.2 `create_graph_node_with_link` 호출자 — 전수

```
src/context_loop/storage/graph_store.py:199  (save_graph_data 내, 유일)
tests/test_storage/test_metadata_store.py:341  (atomic 검증)
tests/test_storage/test_metadata_store.py:372  (orphan 좁힘 검증)
```

운영 1 + 테스트 2. R5 가 `name_stem` 파라미터를 추가하면 두 테스트가 키워드 인자만 추가하든지, 기본값(`name_stem: str | None = None`) 으로 두면 테스트는 무수정 통과한다 — *권고: 기본값 None* (5.1 참조).

### 2.3 `graph_nodes` 테이블 INSERT/SELECT 다른 경로

INSERT:
- `metadata_store.py:383` — `create_graph_node` (단독, 테스트·마이그레이션 전용)
- `metadata_store.py:410-413` — `create_graph_node_with_link` (운영)

SELECT:
- `metadata_store.py:443` — `get_all_graph_nodes` (전체 스캔, `load_from_db` 가 사용)
- `metadata_store.py:433` — `get_graph_nodes_by_document` (link 조인)
- `metadata_store.py:454` — `find_graph_node_by_entity` (변경 대상)
- `metadata_store.py:521` — `get_orphan_node_ids` (cleanup 후보)
- `metadata_store.py:570` — `delete_graph_data_by_document` 내 좁힌 orphan delete

INSERT 경로에 `name_stem` 채움이 누락되면 신규 노드의 stem 이 NULL 로 들어가 옵션 1/2/3 의 의미가 또 깨진다 — 5.1 의 NOT NULL 정책 결정과 직결.

SELECT 경로 중 `get_all_graph_nodes` 는 `SELECT *` 이므로 `name_stem` 컬럼이 자동으로 dict 에 포함되어 `load_from_db` 가 받게 된다 (5.4 참조).

### 2.4 `cleanup_orphan_nodes` 류

`get_orphan_node_ids` (`L518-526`), `delete_graph_nodes_by_ids` (`L528-537`), `delete_graph_data_by_document` (`L539-574`) 셋 모두 *id 기준* 으로만 동작한다 — `entity_name` / `name_stem` 무관. R5 변경의 영향 없음.

### 2.5 테스트 픽스처가 `graph_nodes` 직접 채우는지

- `tests/test_storage/test_metadata_store.py:256, 305, 309, 501` — `create_graph_node` 사용 (link 없는 노드)
- `tests/test_storage/test_metadata_store.py:341, 372` — `create_graph_node_with_link` 사용
- `tests/test_processor/test_reprocessor.py:75` — `create_graph_node` 사용
- 다른 `INSERT INTO graph_nodes` 직접 호출 *없음* (grep 결과)

테스트가 `create_graph_node` 를 통해 들어가므로 *그 함수에도* `name_stem` 파라미터 추가가 일관성 차원에서 필요하다 (기본값 None 또는 동일한 계산). 그러나 R5 스코프는 운영 경로 (`create_graph_node_with_link`) 에 한정해도 무방 — 단독 `create_graph_node` 는 stem 미채움이라도 테스트가 깨지지 않는다(테스트가 stem 을 검증하지 않으면).

---

## 3. NULL safe 폴백 옵션 trade-off

기존 노드(스키마 ALTER 직후 `name_stem IS NULL` 상태)와의 dedup 처리.

### 옵션 1 — strict stem only

SQL:
```sql
SELECT * FROM graph_nodes WHERE name_stem = ? AND entity_type = ? LIMIT 1
```

- 코드 최단. INDEX `(name_stem, entity_type)` 1 개만 잘 잡으면 끝.
- 깨지는 시나리오: ALTER 직후 사용자가 *재인덱싱을 하지 않은 채* 새 문서를 추가하면 신규 문서의 `Auth Service` 엔티티가 기존 NULL stem 의 `AuthService` 노드와 매칭되지 않는다 → **새 노드로 INSERT** → 중복 ↑. R4 가 추출 측 dedup 을 향상시킨 결과가 무력화되고, *오히려* 노드 수가 늘어 R6 cross-doc 골드셋의 pivot 후보가 분산된다.
- 회귀 위험: 기능적 회귀는 없음(매칭이 *덜* 될 뿐 잘못 매칭되지는 않음). 데이터 회귀(노드 수 증가)는 존재.

### 옵션 2 — NULL fallback

SQL:
```sql
SELECT * FROM graph_nodes
WHERE (
  (name_stem IS NOT NULL AND name_stem = ?)
  OR
  (name_stem IS NULL AND LOWER(entity_name) = LOWER(?))
)
AND entity_type = ?
LIMIT 1
```

- 한 쿼리로 신/구 모두 매칭. 재인덱싱 안 해도 신규 노드가 기존 노드와 연결되어 dedup 효과를 즉시 본다.
- *의도 외 매칭* 위험: 기존 NULL stem 노드가 표기 변형(`AuthService` vs `Auth Service`)을 *서로 다른 노드로* 보존하고 있던 데이터를, 새 문서의 stem `authservice` 가 둘 중 **먼저 매치된 하나** 와만 연결한다. 결과는 `LIMIT 1` 의 비결정성 + 한쪽 노드 편향. 예: 기존에 `AuthService`(id=1) 와 `Auth Service`(id=7) 두 노드가 있고 신규로 `auth_service` 가 오면 옵션 2 의 NULL 분기는 `entity_name` LIKE 가 아닌 정확 매칭이므로 둘 다 hit 되지 않을 가능성도 있다 (`LOWER('AuthService')` ≠ `LOWER('auth_service')`). 즉 NULL fallback 은 *정확 동일 이름* 만 잡고 *변형* 은 못 잡는다 → 옵션 1 만큼은 아니지만 부분 dedup 만 됨. INDEX 활용도 또한 OR 분기로 인해 옵티마이저가 두 인덱스를 다 쓸 수 있을지 *추측: SQLite 는 OR 분기를 union 으로 풀 수 있어 (name_stem, entity_type) + (entity_name, entity_type) 두 인덱스가 있으면 가능*. 인덱스를 둘 다 만들지 결정 필요.
- 회귀 위험: SQL 복잡도 ↑, LIMIT 1 의 비결정성으로 *어느* 노드와 병합되는지 데이터의존. 같은 문서를 두 번 인덱싱하면 어떤 노드로 합쳐지는지가 달라질 수 있다(노드 INSERT 순서가 ROWID 결정). 사용자가 명시적으로 "기존 데이터 통합 안 한다" 결정을 했는데, 옵션 2 는 사실상 *암묵적으로* 통합이 일부 일어남 — 사용자 결정 정신과 미세 충돌.

### 옵션 3 — 백필

ALTER 직후 1회 (의사 코드):
```python
async def _migrate_graph_nodes_name_stem(self) -> None:
    cursor = await self.db.execute("PRAGMA table_info(graph_nodes)")
    cols = {row["name"] for row in await cursor.fetchall()}
    if "name_stem" not in cols:
        await self.db.execute("ALTER TABLE graph_nodes ADD COLUMN name_stem TEXT")
    # 백필 — name_stem 이 NULL 인 행만 채움 (idempotent)
    cursor = await self.db.execute(
        "SELECT id, entity_name FROM graph_nodes WHERE name_stem IS NULL"
    )
    rows = await cursor.fetchall()
    for row in rows:
        stem = normalize_name_stem(row["entity_name"])
        await self.db.execute(
            "UPDATE graph_nodes SET name_stem = ? WHERE id = ?",
            (stem, row["id"]),
        )
    # INDEX 도 이후 단계에서 생성 (백필 후가 더 빠름 — 행 수가 작으면 차이 없음)
```

- 데이터 *통합* 은 하지 않는다 — 기존 두 노드 `AuthService`(id=1), `Auth Service`(id=7) 는 그대로 둔다. 둘의 `name_stem` 만 `"authservice"` 로 동일하게 채워진다.
- 효과: 신규 문서의 `auth-service` 가 옵션 1 SQL 로 들어와도 LIMIT 1 으로 둘 중 하나와 매칭되어 `graph_node_documents` 에 link 가 추가됨. 즉 *기존 분리* 는 보존되지만 *신규 노드는 더 안 생긴다*. R6 pivot 양 관점에서는 옵션 1 보다 명백히 우월.
- 부작용 / 시간: `name_stem` 컬럼 UPDATE 만이라 cascade 없음. 6000 노드 기준 UPDATE 6000건 ≈ 1 초 미만 (*추측 — WAL 모드 PRAGMA 켜져 있음, L151*). `initialize()` 가 호출되는 시점(앱 부팅 / 테스트 fixture) 에 1회 실행되어 idempotent.
- 멱등성: `WHERE name_stem IS NULL` 가드로 두 번 실행되어도 안전. ALTER 자체도 `PRAGMA table_info` 가드로 idempotent.
- LIMIT 1 비결정성: 옵션 2 와 동일하게 존재(기존 2개의 동등 stem 노드 중 어느 쪽이 winner 가 되는지 ROWID 순서에 의존). 다만 옵션 3 에서는 LIMIT 1 이 *어디서나 일관* — `ORDER BY id ASC` 를 추가해 결정성 부여 가능 *(권고)*.
- 회귀 위험: 매우 낮음. 백필이 INSERT/DELETE 가 아닌 UPDATE 의 단일 컬럼만 건드리므로 FK / cascade 에 무관. 다만 ALTER 후 *백필 전* 에 다른 코루틴이 들어오면 NULL 행을 볼 수 있다 — `initialize()` 안에서 ALTER + 백필 + INDEX 생성을 단일 commit 으로 묶으면 외부에서는 일관 상태로 보인다(`L151-155` 의 pragma + executescript + migrate + commit 패턴과 동일 흐름).

### 권고

**옵션 3.** 사용자 결정("스키마만, 재인덱싱 일임")의 *정신* 은 "기존 데이터 노드 자체를 통합/병합하는 위험 SQL 회피". stem 컬럼 백필은 *통합* 이 아니라 *키 채움* 이므로 결정 정신을 침해하지 않는다. 동시에 옵션 1 의 데이터 회귀(중복 ↑)를 깨끗이 막는다. 옵션 2 는 SQL 복잡도와 LIMIT 1 비결정성, 인덱스 사용 모호성 측면에서 옵션 3 보다 열위.

옵션 3 의 추가 가드로 `find_graph_node_by_entity` 에 `ORDER BY id ASC` 또는 `MIN(id)` 를 넣어 동등 stem 다중 노드 시 *항상 최초 생성 노드* 가 winner 가 되도록 하면 멱등성·재현성이 보장된다.

---

## 4. 의존성 방향 점검

### 현재 import 패턴

- `storage/graph_store.py:19`: `from context_loop.processor.graph_extractor import GraphData` — **이미** `storage/` 가 `processor/` 를 import 하고 있다. 즉 R5 가 `storage/graph_store.py` 에서 `normalize_name_stem` 을 추가 import 해도 *방향 전환은 아니다* — 동일 방향 import 추가.
- `storage/metadata_store.py` 는 현재 `processor/` import 없음. R5 에서 `_migrate_schema` 가 백필을 수행하려면 `normalize_name_stem` 이 필요 → `metadata_store.py → processor.graph_vocabulary` 신규 import 발생.

### 순환 import 위험

`processor/` 측에서 `storage/` 를 import 하는 모듈:
- `processor/reprocessor.py:16` → `storage.metadata_store`
- `processor/graph_search_planner.py:26` → `storage.graph_store`
- `processor/pipeline.py:58-60` → `storage.graph_store / metadata_store / vector_store`

R5 신규 import 후보 (`metadata_store.py → processor.graph_vocabulary`) 와 위 세 모듈은 *서로 다른 processor 파일들* 이라 직접 순환은 안 만든다. 정확히 검증해야 할 단 한 가지: `processor.graph_vocabulary` 가 `storage` 를 import 하는가? — 본 파일 (`graph_vocabulary.py`) 전체를 읽었고 `storage` import 는 *없다* (`re`, `dataclasses` 만). 따라서 import 사이클은 발생하지 않는다.

### 대안 (필요 시)

순환이 발생할 가능성은 사실상 없지만 *방어적 옵션* 으로:
- (a) `normalize_name_stem` 을 `storage/_stem.py` 같은 storage-local 헬퍼로 복제 — 단점: R4 의 "어휘 단일 출처" 원칙과 분기 위험.
- (b) `src/context_loop/common/text_normalize.py` 같은 공용 모듈로 끌어내고 `processor`/`storage` 둘 다 import — 단점: 모듈 신설 비용. 장점: 명확한 단일 출처.

권고: 현재 import 가 깨끗하므로 (a)/(b) 둘 다 *필요 없다*. `storage/metadata_store.py` 와 `storage/graph_store.py` 모두 `from context_loop.processor.graph_vocabulary import normalize_name_stem` 를 직접 사용해도 안전. 후속 라운드에서 의존도가 더 깊어지면 그때 (b) 검토.

---

## 5. 회귀 위험 평가

### 5.1 INSERT 시 `name_stem` 누락 / NOT NULL 제약

신규 컬럼은 **NOT NULL 로 만들면 안 된다** — 기존 데이터의 ALTER 호환성을 위해 NULL 허용이 필수(SQLite 의 `ALTER TABLE ADD COLUMN` 은 기본값 없는 NOT NULL 컬럼 추가를 거부한다). 옵션 3 백필이 끝나면 사실상 NULL 행이 없게 되지만, 백필 후에도 컬럼 정의는 `name_stem TEXT` (NULL 허용) 로 두는 게 안전. 

INSERT 누락 위험: `create_graph_node_with_link` SQL 을 `(document_id, entity_name, entity_type, properties, name_stem) VALUES (?, ?, ?, ?, ?)` 로 확장할 때, 호출자가 새 키워드 인자 (`name_stem=...`) 를 빠뜨리면 None 이 들어간다. 단일 호출자 (`graph_store.py:199`) 만 수정하면 되지만, *기본값 `name_stem: str | None = None`* 으로 두고 호출자가 명시적으로 전달하게 강제하는 게 안전 (호출 누락 시 stem 매칭 미스로 *조용히* dedup 손실 → 발견 어려움). 단위 테스트 ((`tests/test_storage/test_metadata_store.py:341, 372`)) 는 키워드 누락하므로 None 으로 들어간다 — 테스트 의도(atomic 검증 / orphan 좁힘) 와 무관하니 OK.

### 5.2 INDEX 충돌 / 중복 정의

기존 INDEX 목록 (`L120-131`) 에 `name_stem` 관련 INDEX 없음. 신규 `CREATE INDEX IF NOT EXISTS idx_graph_nodes_stem ON graph_nodes(name_stem, entity_type)` 추가는 충돌 없음. 단 idx 이름 충돌만 피하면 됨(`idx_graph_nodes_*` 중 `idx_graph_nodes_document` 가 이미 있음 → 새 이름은 충분히 구체적이어야).

복합 INDEX `(name_stem, entity_type)` 가 옵션 3 SQL `WHERE name_stem = ? AND entity_type = ?` 와 정확히 정합. 순서도 stem 의 selectivity 가 높으니 `(name_stem, entity_type)` 가 자연스럽다.

### 5.3 동시성 / race window

`create_graph_node_with_link` 의 단일-commit 보장 (`metadata_store.py:400-404` 의 주석에서 명시) 은 R5 변경 후에도 유지되어야 한다. 즉 SQL 만 확장하고 commit 경계는 *그대로* — INSERT 컬럼 1 개 추가로 트랜잭션 모양이 바뀌지 않는다. 안전.

stem 매칭 자체의 race: `find_graph_node_by_entity` 후 `create_graph_node_with_link` 사이의 await 양보 시점에 *다른 코루틴이 같은 stem 으로 INSERT* 했다면 중복 노드가 생긴다 — 그러나 이는 **기존 SQL 에도 동일하게 존재하는** race 이며 R5 가 새로 만드는 위험이 아니다. (현재도 동일 entity_name 의 동시 INSERT 가능.) 본 라운드 스코프 밖.

### 5.4 `load_from_db` 의 `name_stem` 노출

`get_all_graph_nodes` 가 `SELECT *` 이므로 ALTER 후에는 dict 에 `name_stem` 키가 자동 포함된다. `graph_store.py:106-112` 는 명시 키만 NetworkX 노드 속성으로 옮기므로 `name_stem` 은 *암묵적으로 누락* 된다. 

결정 필요: NetworkX 노드에 `name_stem` 속성을 노출할지. 

- 노출 안 함 (현 코드 유지): 검색 측 (`get_neighbors`, `search_entities_by_embedding`) 이 stem 매칭을 필요로 한다면 추후 라운드에서 추가 필요. R5 의 검색 측 변경은 스코프 밖이므로 *기본은 비노출* 이 맞다.
- 노출 함: `entity_name=...` 옆에 `name_stem=node.get("name_stem")` 추가. 비용 0, 미래 검색 측 stem 매칭의 기반 데이터 준비. *권고: 노출* (한 줄 추가, 회귀 0).

### 5.5 `get_graph_nodes_by_document` 등 다른 SELECT

`get_graph_nodes_by_document` (`L433-437`) 은 `SELECT gn.*` 이라 자동으로 `name_stem` 포함. `web/api/documents.py:215` 와 `mcp/tools.py:137` 호출자가 받는 dict 에 새 키가 들어가지만, 이들은 dict 을 LLM 또는 응답 JSON 으로 직렬화하는 코드 — 새 키 노출이 *기능적 회귀* 는 아니다 (응답 schema 가 strict 가 아니라면). *추측: strict schema 검증 없음 — pydantic 기반 응답 모델이 명시 필드만 빼간다면 영향 0*. R5 verification 에서 `pytest tests/test_web/`, `pytest tests/test_mcp/` 회귀 통과 확인 필요.

### 5.6 마이그레이션 / 백필 호출 위치

`_migrate_schema` (`L157-181`) 는 `initialize()` 안에서 `executescript(_SCHEMA_SQL)` 직후, `commit` 직전에 호출된다. R5 의 ALTER + 백필 + INDEX 도 같은 위치에 두면 단일 commit 으로 외부 가시성 일관. 옵션 3 권고 시 `_migrate_schema` 에 `graph_nodes` 분기 추가가 자연스럽다.

---

## 한 줄 요약

R5 의 stem dedup 표면적은 SQL 1 함수 + INSERT 1 함수 + 운영 호출자 1 곳 (`save_graph_data`) 으로 매우 좁다. 의존성 방향은 이미 `storage → processor` 가 깔려 있어 신규 import 안전. NULL safe 폴백은 **옵션 3 (백필 + 옵션1 SQL + `ORDER BY id ASC`)** 권고 — 사용자 결정 정신과 부합하며 데이터 회귀 없음.

산출 파일: `/home/user/project-context-loop-system/_workspace/indexing-improvement-r5/01_analysis.md`
