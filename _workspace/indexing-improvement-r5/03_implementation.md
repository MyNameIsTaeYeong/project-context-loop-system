# R5 구현 — 저장 측 stem dedup (옵션 3 — 백필 + strict stem SQL + ORDER BY id ASC)

designer 의 6 단계 적용 순서대로 코드에 옮긴 결과. 새 설계 결정은 만들지 않았다.

---

## 1. 변경 파일 목록 + 요약

| 파일 | 변경 요약 |
|---|---|
| `src/context_loop/storage/metadata_store.py` | (a) `_SCHEMA_SQL` 의 `graph_nodes` 에 `name_stem TEXT` (NULL 허용) 추가. (b) `_migrate_schema` 에 graph_nodes 분기 — `PRAGMA table_info` 가드 → `ALTER TABLE graph_nodes ADD COLUMN name_stem TEXT` → `WHERE name_stem IS NULL` 백필 (Python 측 `normalize_name_stem` + `executemany` UPDATE) → `CREATE INDEX IF NOT EXISTS idx_graph_nodes_stem_type ON graph_nodes(name_stem, entity_type)` 까지 한 분기에서 처리. (c) `find_graph_node_by_entity` 가 내부에서 `normalize_name_stem` 호출 후 `WHERE name_stem = ? AND entity_type = ? ORDER BY id ASC LIMIT 1` 매칭. (d) `create_graph_node_with_link` 에 `name_stem: str` 필수 키워드 인자 + INSERT 컬럼 추가, 단일-commit 보장 유지. (e) `normalize_name_stem` import 추가. |
| `src/context_loop/storage/graph_store.py` | (a) `normalize_name_stem` import 추가. (b) `save_graph_data` 엔티티 루프에서 한 번 stem 계산 → `create_graph_node_with_link(..., name_stem=stem)` 으로 전달, `_graph.add_node(..., name_stem=stem)` 도 동기 노출. (c) `load_from_db` 의 `_graph.add_node(..., name_stem=node.get("name_stem"))` 로 백필 이전·이후·신규 모두 NULL 안전 처리. |
| `tests/test_storage/test_metadata_store.py` | (a) 기존 호출 2 곳 (`create_graph_node_with_link` at L341, L372) 에 `name_stem=` 키워드 추가 — 필수 키워드 인자 변경에 따른 갱신. (b) 신규 테스트 8 개: 스키마/INDEX 검증, stem 매칭 4 케이스 (hit 표기변형 / hit case / miss different root / miss plural), `ORDER BY id ASC` 결정성, 레거시 DB 백필 (R5 이전 스키마 위조 → initialize → 모든 행 stem 채워졌는지), `_migrate_schema` 멱등성. |
| `tests/test_storage/test_graph_store.py` | 신규 테스트 2 개: 문서 간 stem dedup 통합 (`AuthService` (D1) ↔ `Auth Service` (D2) → 단일 노드 + 양쪽 `document_ids`), `load_from_db` 의 `name_stem` NetworkX 속성 노출 검증. |

운영 코드 변경: **2 파일** (`metadata_store.py`, `graph_store.py`). 테스트: 2 파일.

> 주: `_SCHEMA_SQL` 의 INDEX 라인은 designer §2.1.1 가 권고한 위치(`_SCHEMA_SQL` 내) 가 R5 이전 스키마 DB 에서 `executescript` 단계에서 "no such column: name_stem" 으로 실패 — initialize() 의 `executescript → _migrate_schema → commit` 순서상 executescript 가 ALTER 보다 먼저 실행되므로 기존 DB 에서는 INDEX 생성 시점에 컬럼이 아직 없다. 이를 발견한 뒤 INDEX 생성을 `_migrate_schema` 분기 *내부*, ALTER + 백필 직후로 옮겼다 (`CREATE INDEX IF NOT EXISTS` 로 멱등성 유지). designer 의도 (멱등 + 단일 트랜잭션) 는 보존하면서 실행 시점만 마이그레이션 분기 안쪽으로 좁힌 조정. 산출은 동일.

---

## 2. 단계별 회귀 결과

각 단계 적용 후 `pytest tests/test_storage/ -v` 실행 결과.

| 단계 | 적용 내용 | 회귀 결과 |
|---|---|---|
| 1 | `_SCHEMA_SQL` 컬럼 + INDEX (위 메모대로 후속 단계 6 회귀에서 INDEX 위치 조정) | 20 PASS / 0 FAIL (test_metadata_store.py 단독) |
| 2 | `_migrate_schema` graph_nodes 분기 + `normalize_name_stem` import | 20 PASS / 0 FAIL |
| 3 | `find_graph_node_by_entity` 내부 stem 계산 + strict SQL + `ORDER BY id ASC` | 20 PASS / 0 FAIL |
| 4 | `create_graph_node_with_link` `name_stem: str` 필수 키워드 + INSERT 컬럼 추가, 기존 테스트 호출 2곳 (`test_metadata_store.py:341, 372`) 의 `name_stem=` 추가 | `test_storage/test_metadata_store.py` 20 PASS / 0 FAIL. 다만 `test_storage/test_graph_store.py` 및 `test_cascade.py` 가 36 FAIL (TypeError: missing required keyword 'name_stem') — 단계 5 의 `save_graph_data` 호출 갱신 *전* 의 의도된 일시적 RED. |
| 5 | `save_graph_data` 에서 stem 계산·전달, `load_from_db` 에 `name_stem` 속성 노출, `normalize_name_stem` import 추가 | 105 PASS / 0 FAIL (`tests/test_storage/` 전체). 단계 4 의 RED 해소. |
| 6 | 신규 테스트 11 개 추가 (스키마/INDEX, 매칭 4 케이스, ORDER BY id ASC, 레거시 백필, 멱등성, 문서 간 dedup, name_stem 속성 노출) | 첫 시도 1 FAIL (`test_migrate_schema_backfills_legacy_graph_nodes_name_stem`) — `_SCHEMA_SQL` 의 INDEX 생성이 `executescript` 단계에서 R5 이전 스키마 DB 에 대해 "no such column" 실패. INDEX 생성을 `_migrate_schema` 분기 안으로 이동 후 **115 PASS / 0 FAIL**. |

### 최종 회귀

```
$ uv run pytest tests/test_storage/ tests/test_processor/ -q
410 passed, 2 failed in 13.23s
```

- `tests/test_storage/` — **115 PASS / 0 FAIL** (신규 11 + 기존 104 모두 GREEN)
- `tests/test_processor/` — **297 PASS / 2 FAIL**
  - 실패 2 건 모두 R5 변경 *이전* 부터 동일하게 실패하는 사전 회귀로 확인 (`git stash` 후 동일 2 건 FAIL 재현 확인).
    - `test_short_parent_body_absorbed_into_first_child`
    - `test_long_parent_body_emitted_as_standalone_unit`
  - 두 테스트 모두 `extraction_unit.py` (스코프 가드 범위) — R5 가 건드리지 않은 영역. 본 라운드와 무관.

### 정적 검증

```
$ uv run ruff check src/context_loop/storage/
Found 8 errors.
```

- 8 errors 모두 **R5 변경 *이전*** 부터 존재하는 기존 코드의 E501 (line too long).
- `git stash` 후 동일 8 errors 확인 — R5 가 추가한 violation 0 건.
- R5 신규 라인은 모두 ≤ 100 자.

---

## 3. 설계 deviation

- **INDEX 생성 위치**: designer §2.1.1 은 `_SCHEMA_SQL` 의 INDEX 블록에서 `CREATE INDEX IF NOT EXISTS idx_graph_nodes_stem_type ...` 을 권고했다. 신규 DB 에서는 의도대로 동작하지만, **R5 이전 스키마 DB** 에서는 `executescript` 가 ALTER 보다 먼저 실행되어 INDEX 생성 시점에 `name_stem` 컬럼이 없는 상태가 됨 → `sqlite3.OperationalError: no such column: name_stem` 으로 부팅 자체가 실패. designer 의 멱등성 가정 (§2.1.5 의 "INDEX 생성은 백필 직전 단계에서 끝나 있다") 이 신규 DB 시나리오에만 성립하고 마이그레이션 시나리오를 놓친 것.

  해결: `_SCHEMA_SQL` 에서 stem INDEX 라인을 *제거* 하고, `_migrate_schema` 의 graph_nodes 분기 안 (ALTER + 백필 직후) 에서 `CREATE INDEX IF NOT EXISTS` 로 명시 실행. `IF NOT EXISTS` 로 멱등성 유지, 한 번도 안 만들어진 신규 DB · 이미 R5 마이그레이션을 받은 DB · 첫 R5 마이그레이션을 받는 레거시 DB 세 시나리오 모두 안전.

  **설계 의도 (멱등 + 단일 트랜잭션 + 신구 DB 통합 경로) 는 보존**. 변경은 실행 시점만 조정. 테스트 `test_graph_nodes_has_name_stem_column_and_index` 가 신규 DB 에서 INDEX 존재를 그대로 검증하므로 외부 동작 동일.

- 그 외: designer 의 6 단계 순서, 시그니처 결정 (`name_stem: str` 필수 키워드), find SQL (내부 stem 계산 + `ORDER BY id ASC`), `create_graph_node` 단독 함수 비변경 결정, `load_from_db` 의 `name_stem` 노출, 모두 그대로 적용.

---

## 4. 다음 단계 (verifier) 에 전달할 사항

### 실행 명령

```bash
uv run pytest tests/test_storage/ tests/test_processor/ -v --tb=short
uv run ruff check src/context_loop/storage/
```

기대 결과:
- `tests/test_storage/`: **115 PASS / 0 FAIL** (스코프 내).
- `tests/test_processor/`: **297 PASS / 2 FAIL** — 2 FAIL 은 R5 와 무관한 사전 회귀 (`test_extraction_unit.py::test_short_parent_body_absorbed_into_first_child`, `test_long_parent_body_emitted_as_standalone_unit`). `git stash` 시 동일 재현.
- `ruff`: **8 errors 모두 사전 존재 violation**. R5 신규 라인은 0 건 추가.

### 의미적 회귀 가드 (verifier 가 추가 검증해야 할 시나리오)

1. **신규 DB 부팅**: `MetadataStore(empty_path).initialize()` → `PRAGMA table_info(graph_nodes)` 에 `name_stem` 컬럼 존재 + `PRAGMA index_list(graph_nodes)` 에 `idx_graph_nodes_stem_type` 존재. (covered by `test_graph_nodes_has_name_stem_column_and_index`)

2. **레거시 DB 마이그레이션**: R5 이전 스키마의 `graph_nodes` 테이블에 노드 N 개 있는 DB → `initialize()` → 모든 행의 `name_stem` 이 `normalize_name_stem(entity_name)` 으로 채워짐. (covered by `test_migrate_schema_backfills_legacy_graph_nodes_name_stem`)

3. **멱등 부팅**: `initialize()` 두 번 호출 (혹은 `_migrate_schema` 직접 두 번 호출) → 두 번째는 ALTER skip, 백필 SELECT 가 빈 결과, INDEX 도 `IF NOT EXISTS` 로 무해. (covered by `test_migrate_schema_is_idempotent_for_name_stem`)

4. **문서 간 stem dedup**: `AuthService` (D1) + `Auth Service` (D2) → 단일 노드, `document_ids = {D1, D2}`. (covered by `test_save_graph_data_dedups_across_documents_by_stem`)

5. **stem 매칭 보수성**: `User` vs `Users` 분리 유지 (형태론 변형 통합 안 함), `AuthService` vs `AuthorizationService` 분리 유지 (어근 다름). (covered by miss 케이스 2 개)

6. **결정성**: 동일 stem 다중 노드 시 `ORDER BY id ASC` 로 항상 최초 생성 노드 winner. (covered by `test_find_graph_node_by_entity_stem_match_order_by_id_asc`)

### 관찰 사항

- `_create_doc_with_title` 픽스처가 `test_graph_store.py` 안에 이미 정의되어 있어 신규 테스트가 그대로 재사용.
- `find_graph_node_by_entity` 호출 시그니처는 *변경 없음* (raw `entity_name` 그대로 받음) — 호출자가 stem 을 미리 계산할 필요 없음. 운영 호출자(`save_graph_data`) 도 *내부에서 한 번* stem 을 계산해 INSERT 인자로만 넘긴다.
- `_graph.add_node(..., name_stem=...)` 는 NetworkX 노드 속성으로 추가됐지만, 본 라운드의 검색 측 코드 (`get_neighbors`, `search_entities_by_embedding`) 는 이 속성을 *읽지 않는다*. R6 이후 검색 측 stem 매칭이 도입될 때 데이터 기반으로 사용됨 — 본 라운드 스코프 밖.

### 결론

설계 deviation 1 건 (INDEX 생성 위치 조정) 외 designer 설계대로 적용. 모든 R5 스코프 회귀 GREEN.

산출 파일: `/home/user/project-context-loop-system/_workspace/indexing-improvement-r5/03_implementation.md`
