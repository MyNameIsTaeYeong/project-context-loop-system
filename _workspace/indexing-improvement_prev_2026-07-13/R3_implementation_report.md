# R3 — Semantic Entity Merge (후보 D) 구현 보고서

> **범위**: `_workspace/indexing-improvement/R2_semantic_merge_review.md` §5.1
> **단기 채택 항목만** 구현. 후보 D (룰 기반 정규화) + `graph_merge_log` 도입.
> 후보 A/B/C/E/F, D 의 괄호 제거 규칙, 한자 변환은 본 라운드 범위 밖.

---

## 1. 변경 파일 목록

### 신규 파일

| 파일 | 역할 |
|------|------|
| `src/context_loop/storage/entity_normalizer.py` | source-agnostic `normalize_entity_name(name)` 함수 |
| `tests/test_storage/test_entity_normalizer.py` | 정규화 사례표 검증 (18 케이스) |
| `tests/test_storage/test_graph_normalization.py` | 마이그레이션 idempotency / find 매칭 / merge log 통합 테스트 (15 케이스) |
| `_workspace/indexing-improvement/R3_implementation_report.md` | 본 보고서 |

### 수정 파일

| 파일 | 변경 요약 |
|------|----------|
| `src/context_loop/storage/metadata_store.py` | 스키마: `graph_nodes.normalized_name` 컬럼 + 인덱스, `graph_merge_log` 테이블 + 인덱스. 마이그레이션: ALTER + 백필 (`_backfill_normalized_names`). `find_graph_node_by_entity` 가 `normalized_name = ?` 로 매칭 (LOWER() 제거). `create_graph_node` / `create_graph_node_with_link` 가 `normalized_name` 도 INSERT. 신규 `record_graph_merge` / `get_graph_merge_log` 메서드. |
| `src/context_loop/storage/graph_store.py` | `save_graph_data` 가 `normalize_entity_name` 호출 후 정규화 키를 `find_graph_node_by_entity` / `create_graph_node_with_link` 양쪽에 전달. 머지/신규 결정마다 `_record_merge_safely` 로 `graph_merge_log` 한 행 INSERT (실패 시 그래프 저장은 진행). |

---

## 2. 핵심 결정

### 2.1 정규화 위치 — graph_store 가 정규화하여 metadata_store 에 전달

- 책임 분리: storage 레이어(`metadata_store`)는 입력 키를 그대로 신뢰. 정책 결정은
  caller(`graph_store`).
- `find_graph_node_by_entity` 와 `create_graph_node*` 는 `normalized_name` 인자를
  옵션으로 받되, 미지정 시 안전 fallback 으로 내부에서 `normalize_entity_name` 호출.
  → 기존 직접 호출자(테스트/마이그레이션 경로) 가 깨지지 않는다.
- 같은 entity 의 lookup ↔ insert 사이에 키가 재계산되지 않아 일관성 보장 +
  중복 정규화 비용 절약.

### 2.2 정규화 규칙 — 설계서 §3.2 의 1~4, 6 만 채택

```
1. Unicode NFKC 정규화
2. 양끝 공백 strip
3. 연속 공백 → 단일 공백
4. 공백 / `-` / `_` 모두 제거 (빈 문자열로 join)
5. 케이스 폴딩 (lower)
```

- 설계서의 5번 (양 끝 괄호 제거) 은 제외 — 사례 3 (`결제 시스템(v2)`,
  `결제 시스템 (legacy)`) 의 false-merge 위험. 테스트
  (`test_parentheses_preserved_as_different_nodes`) 로 별개 노드 유지 가드.
- 설계서의 7번 (한자/일본어 변환) 도 제외.

### 2.3 마이그레이션 idempotency

- `PRAGMA table_info(graph_nodes)` 로 컬럼 존재 확인 후 ALTER. 두 번째
  `initialize` 호출 시 ALTER 스킵.
- 인덱스는 `CREATE INDEX IF NOT EXISTS` 로 자명 idempotent.
- 백필은 `WHERE normalized_name = ''` 필터 + executemany — 이미 채워진 행은
  건드리지 않는다. 사용자가 임의 값으로 override 한 경우에도 보존되도록 설계
  (`test_backfill_is_idempotent` 로 가드).

### 2.4 merge_method 분류

| 값 | 조건 |
|----|------|
| `'new'` | 정규화 매칭 실패 → 신규 노드 생성 |
| `'exact'` | 매칭 성공 + canonical 노드의 `entity_name` 이 입력 raw 와 완전 동일 (즉 표기 변형 없음) |
| `'normalized'` | 매칭 성공 + 표기 변형이 정규화 키로 흡수됨 |

- `similarity_score` 는 D 단계 binary 매칭이므로 항상 `NULL`. 향후 A/B 도입 시
  사용할 슬롯.
- `_record_merge_safely` 가 INSERT 실패를 swallow + warn — 그래프 저장
  critical path 가 로그 실패로 멈추지 않는다.

---

## 3. 추가된 테스트 + 결과

### `tests/test_storage/test_entity_normalizer.py` — 18 케이스 (모두 PASS)

설계서 §8 부록의 8개 사례 + 빈 입력 안전성 + idempotency.

- `test_case_folding_simple_english` — Payment Service / payment service
- `test_korean_whitespace_squeeze` — 결제 시스템 / 결제시스템
- `test_korean_dash_separator` — 결제 시스템 / 결제-시스템
- `test_abbrev_vs_fullname_not_merged` — PG ≠ Payment Gateway (D 한계)
- `test_multilingual_not_merged` — 결제 서비스 ≠ Payment Service (D 한계)
- `test_version_parentheses_preserved` — `결제 시스템(v2)` 별개 유지
- `test_legacy_parentheses_preserved` — `결제 시스템 (legacy)` 별개 유지
- `test_same_entity_type_homonym_still_collides` — 사례 5 D 한계 명시
- `test_underscore_separator` — auth_service / auth-service / Auth Service
- `test_nfkc_fullwidth_normalization` — `ＰＡＹＭＥＮＴ` → `payment`
- `test_squeeze_consecutive_whitespace` — 연속 공백
- `test_strip_outer_whitespace` — 양끝 공백
- `test_empty_inputs[None/""/"   "/"\t\n"/"  --  __  "]` — 5 케이스
- `test_deterministic_idempotent` — 같은 입력 → 같은 출력 + 멱등성

### `tests/test_storage/test_graph_normalization.py` — 15 케이스 (모두 PASS)

#### 마이그레이션 (3)
- `test_backfill_populates_existing_rows` — legacy 시뮬레이션 후 백필
- `test_backfill_is_idempotent` — 비어있지 않은 행은 보존
- `test_migration_runs_twice_safely` — `initialize` 두 번 호출 안전

#### `find_graph_node_by_entity` 정규화 매칭 (5)
- `test_korean_whitespace_variants_match` — 결제 시스템 / 결제시스템 / 결제-시스템 / 결제_시스템
- `test_english_case_and_separator_variants_match` — Payment Service / payment service / PAYMENT SERVICE / Payment-Service / payment_service / PaymentService
- `test_entity_type_mismatch_isolates_nodes` — 같은 정규화 키여도 entity_type 다르면 분리
- `test_parentheses_preserved_as_different_nodes` — `(v2)`, `(legacy)` 별개 유지
- `test_explicit_normalized_key_takes_precedence` — `normalized_name` 인자 우선

#### `graph_merge_log` 기록 (5)
- `test_new_node_logs_method_new` — 신규 노드 1 행
- `test_exact_repeat_logs_method_exact` — 동일 표기 재등록은 'exact'
- `test_variant_repeat_logs_method_normalized` — 표기 변형 재등록은 'normalized'
- `test_one_log_row_per_entity` — N 엔티티 입력 → 정확히 N 행
- `test_canonical_node_id_lookup` — `canonical_node_id` 필터로 노드별 머지 이력

#### 신규 노드 normalized_name 영속화 + cross-document 머지 (2)
- `test_save_graph_data_persists_normalized_name` — 신규 노드 INSERT 가
  `normalized_name` 채움
- `test_cross_document_merge_via_normalization` — 다른 문서가 `Auth-Service` /
  `auth_service` 로 들어와도 같은 노드 (사례 2, 7 해결 가드)

### 즉시 실행 결과

```
$ .venv/bin/python -m pytest tests/test_storage/test_entity_normalizer.py \
    tests/test_storage/test_graph_normalization.py -v
============================== 33 passed in 1.05s ==============================
```

---

## 4. 회귀 테스트 결과

### `tests/test_storage/` (전체) — 138 passed
```
$ .venv/bin/python -m pytest tests/test_storage/ -q
138 passed in 7.33s
```

- 기존 65 개 + 신규 33 개 + 다른 모듈 40 개 모두 통과.
- 특히 `test_graph_store.py::test_save_graph_data` / `test_get_neighbors` /
  cross-document 머지 테스트 / FK 위반 race 가드 테스트가 정상 통과.

### `tests/test_processor/`, `tests/test_ingestion/` — 본 변경과 무관 영역
```
$ .venv/bin/python -m pytest tests/test_processor/ tests/test_ingestion/ -q
2 failed, 578 passed
```
- 실패한 2개 (`test_extraction_unit.py::test_short_parent_body_absorbed_into_first_child`,
  `test_long_parent_body_emitted_as_standalone_unit`) 는 **R3 변경 이전부터
  존재하던 회귀** (git stash 로 변경 제거 후에도 동일 실패 확인). 청킹/추출
  로직 영역으로, R3 범위 밖.

### `tests/test_eval/test_build_synthetic_gold_set.py` — 본 변경과 무관 영역
```
$ .venv/bin/python -m pytest tests/test_eval/test_build_synthetic_gold_set.py -q
3 failed, 35 passed
```
- 실패한 3개 (`test_fetch_source_text_anchor_match`,
  `test_fetch_source_text_legacy_chunk_id_fallback`,
  `test_make_graph_gold_item_falls_back_to_node_description`) 역시 **R3 이전부터
  존재** (git stash 확인). 골드셋 생성기 영역으로, R3 범위 밖.

### `tests/test_web/`, `tests/test_mcp/` — 본 변경과 무관 영역
```
$ .venv/bin/python -m pytest tests/test_mcp/ tests/test_web/ -q
17 failed, 82 passed
```
- 17 개 실패 모두 R3 이전부터 존재 (git stash 확인). 웹 템플릿/세션 관련.

### Lint
- 신규 파일 (`entity_normalizer.py`, 신규 테스트 2개) — `ruff check` All clean.
- 수정 파일의 ruff 에러는 모두 R3 이전부터 존재한 라인.

---

## 5. 알려진 한계 / 후속 작업

### D 의 본질적 한계 (설계서 §3.2 D)

- **사례 1 (다국어)**: `결제 서비스` ↔ `Payment Service` — 매칭 불가.
- **사례 4 (약어)**: `PG` ↔ `Payment Gateway` — 매칭 불가.
- **사례 5 (동음이의어)**: `API` (도메인 A) 와 `API` (도메인 B) 가 정규화 키 +
  entity_type 같으면 **잘못 머지**. 설계서 §5.3 에 따라 D 라운드에서 회피 코드
  추가 금지. → 후속 라운드에서 A/E (임베딩 + LLM) PoC 로 검증.
- **사례 6 (link 폴백 표기)**: `page:12345` ↔ `결제 시스템` — D 만으로 불가.
  설계서 §4.5 의 `page_id` 메타 보조 신호 도입이 별도 라운드 필요.

### 후속 라운드 후보 (R4+ 제안)

1. **PoC for A (임베딩 머지)**: 설계서 §6 의 골든셋 라벨링 후 precision/recall
   측정. `graph_merge_log` 가 baseline 데이터 제공 가능 — D 가 잡은 머지 중
   사람이 검증한 ground truth 와의 일치율 계산.
2. **하이브리드 E**: PoC 결과가 §6.4 의 기준 충족 시 동기 D → 비동기 A → LLM B
   3 단계 큐 도입.
3. **머지 로그 가시화**: 운영 대시보드에 `graph_merge_log` 그래프 추가
   (방법별 분포, 노드별 head/tail 표기 다양성).
4. **rollback 메커니즘**: `graph_merge_log` 가 있으니 노드 분리 (split) 도구
   별도 라운드에서 구현 가능. 현재는 로그만 있고 split 자체는 미구현.
5. **non-confluence 출처 영향 평가**: `normalize_entity_name` 은
   source-agnostic 이지만, git_code 의 FQN (`file.py::Class.method`) 에서
   `.`/`::` 가 제거되지 않는다. FQN 정책과의 호환성은 별도 검토 필요.

### 신규 발견 (구현 중 — 다음 라운드/세션에서 다룰 후보)

- **graph_store 의 `find_graph_node_by_entity` `entity_type=None` 케이스**:
  현재 시그니처는 `entity_type: str` (non-Optional). 일부 LLM 추출 결과가
  entity_type 빈 문자열을 보내면 정규화 키 매칭이 entity_type 까지 정확
  비교라 안 잡힐 수 있음. 본 라운드 변경 전후 동일 동작 (LOWER 시절에도
  AND entity_type = ?) — 회귀 아니지만 별도 라운드에서 검토할 가치.

---

## 6. 추천 커밋 메시지

```
feat(storage): D - normalize entity names for graph node merging

R3: 후보 D (룰 기반 정규화) 채택 — 표기 변형(공백/하이픈/언더스코어/케이스/
전각문자) 을 흡수해 cross-document entity 머지 recall 을 끌어올린다.
설계서: _workspace/indexing-improvement/R2_semantic_merge_review.md §5.1.

- src/context_loop/storage/entity_normalizer.py: source-agnostic
  normalize_entity_name(name) — NFKC + strip + 공백/-/_ 제거 + lower.
  괄호 제거(false-merge 위험)와 한자 변환은 채택 안 함.
- src/context_loop/storage/metadata_store.py:
  - graph_nodes.normalized_name TEXT NOT NULL DEFAULT '' + index 추가
  - graph_merge_log 테이블 도입 (canonical_node_id, raw_entity_name,
    merge_method, similarity_score) — 머지/신규 결정마다 한 행 기록
  - 마이그레이션 idempotent: ALTER + 백필 (normalized_name='' 인 행만)
  - find_graph_node_by_entity: LOWER(entity_name)=LOWER(?) → normalized_name=?
  - create_graph_node*: normalized_name 도 INSERT
- src/context_loop/storage/graph_store.py:
  - save_graph_data 가 normalize_entity_name 호출 → 정규화 키를 lookup/insert
    양쪽에 전달 (책임 분리). 신규/머지 결정마다 graph_merge_log INSERT
    (실패 시 그래프 저장은 진행).
- tests/test_storage/test_entity_normalizer.py: 18 케이스 (설계서 §8)
- tests/test_storage/test_graph_normalization.py: 마이그레이션 idempotency,
  find 매칭, merge log 기록 검증 15 케이스

회귀: tests/test_storage/ 138 passed. 사례 5 (동음이의어), 사례 1/4 (다국어/
약어), 괄호 표기 보존은 D 의 한계로 명시 — 별도 PoC 라운드 (A/E) 의제.
```

---

## 7. 보류 항목 (없음)

본 라운드는 설계서 §5.1 의 5개 항목을 **모두 구현**. 명시적으로 범위에서 제외된
A/B/C/E/F, 괄호 제거, 한자 변환은 의도된 보류 (설계서 §5.3, R3 의 제약 조건).
