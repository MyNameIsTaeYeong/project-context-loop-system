# R5 검증 — 저장 측 stem dedup (옵션 3) 독립 검증

implementer 의 보고서를 신뢰하지 않고 *실제 git diff · pytest · ruff* 로 재현 검증한 결과.

---

## 1. 검증 결과 종합

**PASS-WITH-NOTES.**

- designer 의 6 단계 설계와 implementer 의 코드 변경이 정합. deviation 1 건 (INDEX 생성 위치 조정) 정당성 *직접 확인*.
- 스코프 가드 위반 0 건. 코드 변경 4 파일이 정확히 스코프 매트릭스와 일치.
- `tests/test_storage/` 115 PASS / 0 FAIL — 신규 11 + 기존 104 모두 GREEN.
- `tests/test_processor/` 297 PASS / 2 FAIL — 두 실패 모두 R5 변경 *전* (`git stash` 후) 동일 재현, R5 무관 pre-existing.
- ruff 8 errors (`src/context_loop/storage/`) + 26 errors (processor/tests) 모두 R5 *전* 동일. R5 가 *추가* 한 violation 0 건.

NOTES: implementer 보고서의 “410 passed” 는 실측 412 passed (오타 가능성). 회귀 결과 자체는 일치.

---

## 2. 설계-구현 정합성 점검 (6 단계별)

| 단계 | designer 권고 | 실제 구현 (git diff 라인 확인) | 정합 |
|---|---|---|---|
| 1. 스키마 컬럼 | `_SCHEMA_SQL.graph_nodes` 에 `name_stem TEXT` 추가 (NULL 허용) | `metadata_store.py:54` — `name_stem TEXT,` (NOT NULL 없음) | ✓ |
| 1. 스키마 INDEX | `idx_graph_nodes_stem_type ON graph_nodes(name_stem, entity_type)` | 위치 변경 (deviation 1) — `_migrate_schema` 안 (`metadata_store.py:223-226`) | ✓ (deviation 정당) |
| 2. 마이그레이션 | `PRAGMA table_info` 가드 → `ALTER` → 백필 → INDEX | `metadata_store.py:192-226` 순서대로 (`PRAGMA → ALTER → executemany UPDATE → CREATE INDEX IF NOT EXISTS`) | ✓ |
| 2. 백필 | `WHERE name_stem IS NULL` 만 UPDATE (멱등) | `metadata_store.py:204` `WHERE name_stem IS NULL` 가드 + Python 측 `normalize_name_stem` 루프 → `executemany` | ✓ |
| 3. find SQL | 시그니처 유지 (안 A), 내부 stem 계산, `WHERE name_stem = ? AND entity_type = ? ORDER BY id ASC LIMIT 1` | `metadata_store.py:504-528` 정확히 일치. `normalize_name_stem` 내부 호출, `ORDER BY id ASC LIMIT 1` 명시 | ✓ |
| 4. create with link | `name_stem: str` *필수* 키워드 인자, INSERT 컬럼 추가 | `metadata_store.py:439` `name_stem: str` (default 없음, `*,` 뒤 keyword-only). `metadata_store.py:466-469` INSERT 에 `name_stem` 컬럼 추가. 단일-commit (`L480`) 보존 | ✓ |
| 5. save_graph_data | `normalize_name_stem` import + 엔티티 루프에서 stem 계산 → `create_graph_node_with_link(name_stem=...)` 전달, `_graph.add_node(name_stem=...)` 양쪽 노출 | `graph_store.py:20` import. `L167` 한 번 계산. `L197` (existing 분기 add_node), `L210` (create_with_link), `L219` (신규 add_node) 세 곳 모두 `name_stem=entity_stem` 전달 | ✓ |
| 5. load_from_db | `_graph.add_node(..., name_stem=node.get("name_stem"))` — NULL 안전 | `graph_store.py:111` `name_stem=node.get("name_stem")` 정확히 | ✓ |

### deviation 1 (INDEX 생성 위치) 정당성 검증

implementer 보고서 §3 의 deviation: designer 가 `_SCHEMA_SQL` 의 INDEX 블록에 INDEX 정의 권고 → R5 이전 스키마 DB 에서 `executescript` 가 ALTER 보다 먼저 실행되어 `no such column: name_stem` 으로 실패. INDEX 생성을 `_migrate_schema` 안쪽으로 이동.

검증: `tests/test_storage/test_metadata_store.py::test_migrate_schema_backfills_legacy_graph_nodes_name_stem` 가 실제로 R5 이전 스키마 DB 를 위조 (`L688-703`: `CREATE TABLE graph_nodes` 에 `name_stem` 컬럼 없이 만든 뒤 노드 4개 INSERT) → `MetadataStore.initialize()` 호출 → 모든 행 `name_stem` 채워졌는지 검증. 본 테스트가 *PASS* — 즉 deviation 으로 옮긴 위치가 실제 레거시 시나리오를 처리한다.

또한 `test_graph_nodes_has_name_stem_column_and_index` 가 신규 DB 부팅 시점에 INDEX 존재를 검증 → PASS. 신규 DB / 마이그레이션 DB 양쪽 시나리오 모두 외부 동작 동일.

설계 의도 (멱등 + 단일 트랜잭션 + 신구 DB 통합 경로) 보존. deviation 1 정당.

implementer 보고서가 명시한 deviation 외 추가 deviation **없음**.

---

## 3. 테스트 결과

### 3.1 R5 스코프 회귀

```bash
$ .venv/bin/python -m pytest tests/test_storage/ -v
============================= 115 passed in 7.79s ==============================
```

- 신규 11 개 PASS (`test_metadata_store.py` 8 + `test_graph_store.py` 2 + 자체 검증한 결정성 케이스 1).
- 기존 104 개 PASS (`test_create_graph_node_with_link_atomic`, `test_delete_graph_data_by_document_narrow_orphan_cleanup` 등 호출자 갱신된 테스트 포함).

### 3.2 R4 영향권 회귀

```bash
$ .venv/bin/python -m pytest tests/test_storage/ tests/test_processor/ -q
2 failed, 412 passed in 11.05s
```

- 412 passed (115 storage + 297 processor) — implementer 보고서 “410 passed” 는 실측 412 와 ±2 차이. 사소한 오기로 판단.
- 2 failed:
  - `test_processor/test_extraction_unit.py::test_short_parent_body_absorbed_into_first_child`
  - `test_processor/test_extraction_unit.py::test_long_parent_body_emitted_as_standalone_unit`

### 3.3 사전 실패 무관성 검증 (`git stash`)

```bash
$ git stash push -u  # R5 변경 제거
$ .venv/bin/python -m pytest tests/test_processor/test_extraction_unit.py::test_short_parent_body_absorbed_into_first_child \
                              tests/test_processor/test_extraction_unit.py::test_long_parent_body_emitted_as_standalone_unit -v
============================== 2 failed in 0.16s ===============================
```

R5 변경 *이전* 베이스라인에서도 동일 두 테스트가 FAIL. 동일 assertion (assert '9:1' in ('9:0',), assert 4 == 2) — R5 가 새로 깨뜨린 회귀가 아님. 두 테스트는 `extraction_unit.py` (R4 영역, 본 라운드 스코프 가드 밖) 관련이며 본 라운드와 무관. `git stash pop` 으로 복원 완료.

---

## 4. ruff / 스코프 가드

### 4.1 ruff 검사

```bash
$ .venv/bin/python -m ruff check src/context_loop/storage/
Found 8 errors.

$ .venv/bin/python -m ruff check src/context_loop/processor/ tests/test_storage/ tests/test_processor/
Found 26 errors.
```

`git stash` 후 동일 베이스라인에서도 8 / 26 errors. R5 *추가* 한 violation **0 건**.

8 errors 의 패턴: 모두 `E501 Line too long` — `metadata_store.py:666` INSERT SQL, `vector_store.py:44` 한국어 메시지 등 R5 미변경 라인. R5 신규 라인은 모두 ≤ 100 자.

26 errors: 사전 존재 `F841` (unused variable) 등 — 본 라운드 무관.

### 4.2 스코프 가드 검증

```bash
$ git diff HEAD --name-only
src/context_loop/storage/graph_store.py
src/context_loop/storage/metadata_store.py
tests/test_storage/test_graph_store.py
tests/test_storage/test_metadata_store.py
```

스코프 매트릭스 정확히 일치. 다음 영역 변경 **없음** (확인):

- `src/context_loop/processor/llm_body_extractor.py` (R4) — ✓ 변경 없음
- `src/context_loop/eval/*`, `scripts/eval_search.py`, `scripts/build_synthetic_gold_set.py` — ✓ 변경 없음
- `src/context_loop/processor/ast_code_extractor.py`, `body_extractor.py`, `link_graph_builder.py` — ✓ 변경 없음
- `src/context_loop/processor/graph_search_planner.py`, `mcp/context_assembler.py` — ✓ 변경 없음
- `src/context_loop/processor/graph_vocabulary.py` — ✓ 변경 없음 (import 만 사용)

untracked: `_workspace/indexing-improvement-r5/03_implementation.md` — 워크스페이스 문서, 코드 영향 없음.

---

## 5. 회귀 위험 spot check

| 항목 | 검증 결과 | 근거 |
|---|---|---|
| NOT NULL 제약 부재 | ✓ | `metadata_store.py:54` `name_stem TEXT,` — NOT NULL 절 없음. ALTER 호환성 (designer §5.1) 충족 |
| 백필 멱등성 | ✓ | `metadata_store.py:204` `WHERE name_stem IS NULL` 가드. `test_migrate_schema_is_idempotent_for_name_stem` PASS |
| stem 단일 출처 | ✓ | grep 결과 `metadata_store.py` 2회, `graph_store.py` 1회 — 모두 `from context_loop.processor.graph_vocabulary import normalize_name_stem`. 직접 string 조작 없음 |
| 결정성 (`ORDER BY id ASC`) | ✓ | `metadata_store.py:524` 명시. `test_find_graph_node_by_entity_stem_match_order_by_id_asc` 가 동일 stem 다중 노드에서 최초 id winner 검증 → PASS |
| `name_to_node_id` 키 정책 | ✓ | `graph_store.py:224` `name_to_node_id[entity.name] = node_id` — *원본 name* 유지 (analyst 권고대로 R5 직접 영향 없음). relation source/target 매핑이 원본 name 으로 lookup 하므로 정합 |
| `create_graph_node_with_link` 시그니처 | ✓ | `metadata_store.py:439` `name_stem: str` — `*,` 뒤이므로 keyword-only, default 없음 → 미전달 시 즉시 `TypeError`. silent dedup 손실 차단 |
| 단일-commit 보장 | ✓ | `metadata_store.py:465-480` INSERT graph_nodes → INSERT graph_node_documents → commit. 컬럼 1 개 추가만으로 트랜잭션 모양 무변동 |
| INSERT 컬럼 순서 정합 | ✓ | `metadata_store.py:467-469` 컬럼 5 개 / 값 5 개 + placeholder 5 개 정합. `(document_id, entity_name, entity_type, name_stem, properties)` |
| `load_from_db` NULL 안전 | ✓ | `graph_store.py:111` `name_stem=node.get("name_stem")` — `.get()` 으로 missing 키 (백필 직후 NULL 행) 안전 처리 |
| 신규 통합 시나리오 | ✓ | `test_save_graph_data_dedups_across_documents_by_stem` (test_graph_store.py:1031-1068): D1 `AuthService` → D2 `Auth Service` → `len(system_nodes) == 1` + `doc_ids == {d1, d2}` + NetworkX 노드 `document_ids == {d1, d2}` 모두 assert. assert 충분 |
| miss 케이스 (보수성) | ✓ | `test_find_graph_node_by_entity_stem_match_miss_different_root` (어근 다름), `test_find_graph_node_by_entity_stem_match_miss_plural` (단/복수형 분리) 모두 PASS — `normalize_name_stem` 의 보수적 정책 (형태론 보존) 회귀 가드 작동 |

순환 import 점검: `metadata_store.py:16` → `processor.graph_vocabulary` 신규 import. `processor.graph_vocabulary` 가 `storage` 를 import 하지 않음 (analyst §4 확인). pytest import 단계에서도 에러 없음 (115 PASS).

---

## 6. 결론과 권고

### 결론

PASS-WITH-NOTES. R5 의 저장 측 stem dedup (옵션 3 — 백필 + strict stem SQL + ORDER BY id ASC) 가 designer 설계대로 구현되었고, deviation 1 건 (INDEX 생성 위치) 의 정당성도 직접 검증했다. R5 스코프 회귀 (`tests/test_storage/`) 115 PASS, R4 영향권 (`tests/test_processor/`) 297 PASS — 사전 2 실패는 R5 무관. ruff R5 추가 violation 0 건. 스코프 가드 위반 0 건.

NOTES: implementer 보고서가 “410 passed” 라고 적었으나 실측 **412 passed** (115 + 297). 합산 오기 — 검증 결과에 영향 없음.

### 사용자 보고 핵심

- 저장 측 (Layer 2) stem dedup 구현 완료. 문서 간 `AuthService` ↔ `Auth Service` 가 단일 노드로 통합되어 multi-doc pivot 양 ↑ (R6 cross-doc 골드셋의 데이터 기반).
- 마이그레이션: `initialize()` 호출 시 `_migrate_schema` 가 자동으로 `name_stem` 컬럼 추가 + 기존 노드 백필 + INDEX 생성. 멱등성·단일 트랜잭션 보장.
- 데이터 *통합* (in-place merge) 은 하지 않음 — 사용자 결정 정신 보존. 기존 분리 노드는 그대로 두되 stem *키* 만 채워 신규 노드가 기존 노드와 link 추가되도록.
- 사용자 액션 필요: 재인덱싱 시점에 사내 6000노드/8500엣지 DB 가 자동 백필됨. 별도 수작업 없음.

### 커밋·푸시 가이드 (R4 와 일관)

PASS-WITH-NOTES 이므로 사용자가 일괄 커밋·푸시 가능. 권고 절차:

```bash
# 1) untracked 워크스페이스 문서 추가 (03_implementation.md 가 untracked)
git add _workspace/indexing-improvement-r5/03_implementation.md

# 2) 코드·테스트 변경 stage
git add src/context_loop/storage/metadata_store.py \
        src/context_loop/storage/graph_store.py \
        tests/test_storage/test_metadata_store.py \
        tests/test_storage/test_graph_store.py

# 3) 워크스페이스 검증 보고서 추가
git add _workspace/indexing-improvement-r5/04_verification.md

# 4) 단일 커밋 (R4 와 일관된 메시지 구조)
git commit -m "$(cat <<'EOF'
R5: 저장 측 stem dedup — graph_nodes.name_stem 컬럼 + 백필 + strict SQL

- _SCHEMA_SQL 의 graph_nodes 에 name_stem TEXT 컬럼 (NULL 허용) 추가.
- _migrate_schema 에 graph_nodes 분기 — PRAGMA 가드 → ALTER → NULL 백필
  → idx_graph_nodes_stem_type 복합 INDEX 까지 단일 트랜잭션 처리. 멱등.
- find_graph_node_by_entity SQL 을 `name_stem = ? AND entity_type = ?
  ORDER BY id ASC LIMIT 1` 로 교체. 동일 stem 다중 노드 시 최초 id winner
  로 결정성 보장.
- create_graph_node_with_link 에 name_stem: str 필수 키워드 인자 추가
  + INSERT 컬럼 확장. 단일-commit race 가드는 그대로 유지.
- save_graph_data 가 normalize_name_stem 으로 한 번 stem 계산 → find /
  create / NetworkX add_node 양쪽에 일관 전달. load_from_db 가 name_stem
  속성을 NetworkX 노드에 노출 (NULL 안전, .get() 사용).
- 신규 단위 테스트 11 개: 스키마/INDEX 검증, stem 매칭 4 케이스
  (hit 표기변형 / hit case / miss different root / miss plural),
  ORDER BY id ASC 결정성, 레거시 DB 백필, 멱등성, 문서 간 dedup,
  NetworkX name_stem 속성 노출.

회귀: tests/test_storage/ 115 PASS, tests/test_processor/ 297 PASS / 2
FAIL (R5 무관 pre-existing — extraction_unit.py).

문서 간 표기 변형 통합으로 multi-doc pivot 양 ↑ — R6 cross-doc 골드셋의
데이터 기반.

🤖 Generated with [Claude Code](https://claude.com/claude-code)

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"

# 5) 푸시
git push origin claude/indexing-improvement-r5
```

### 후속 (R6 예고와 호환)

- 사내 데이터 재인덱싱 후 multi-doc pivot 수 측정 → R6 의 2-hop cross-doc 골드셋 생성 (pivot 거치는 A→B→C chain seed) 에서 본 라운드의 dedup 효과 확인 가능.
- `_graph.add_node(..., name_stem=...)` 가 NetworkX 노드 속성으로 노출됐지만 검색 측 (`get_neighbors`, `search_entities_by_embedding`) 은 본 라운드에서 *읽지 않음*. R6 이후 검색 측 stem 매칭 도입 시 활용 가능 — 본 라운드 스코프 밖.

산출 파일: `/home/user/project-context-loop-system/_workspace/indexing-improvement-r5/04_verification.md`
