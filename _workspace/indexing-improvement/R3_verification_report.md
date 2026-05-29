# R3 — Semantic Entity Merge (후보 D) 검증 보고서

> **검증 대상**: `_workspace/indexing-improvement/R3_implementation_report.md`
> **설계서**: `_workspace/indexing-improvement/R2_semantic_merge_review.md` §5.1
> **브랜치**: `claude/confluence-mcp-graph-extraction-zxCwy`
> **검증 시각**: 2026-05-28

---

## 한 줄 결론

**PASS** — 설계서 §5.1 의 5개 항목 모두 정확히 반영, 신규 33 테스트 모두 통과, 회귀 0건, 범위 가드(A/B/C/E/F·괄호 제거·LLM 호출) 준수.

---

## 변경 요약

검증 도중 R3 변경이 자동으로 commit 되었음 (HEAD = `63e1fd3 feat(graph-store): D 룰 기반 entity 정규화 + graph_merge_log 도입`). 즉 R3 구현이 정식 commit 으로 들어간 상태.

- 변경 파일: 6개 (신규 4, 수정 2)
  - 신규: `src/context_loop/storage/entity_normalizer.py` (+82)
  - 신규: `tests/test_storage/test_entity_normalizer.py` (+105)
  - 신규: `tests/test_storage/test_graph_normalization.py` (+438)
  - 신규: `_workspace/indexing-improvement/R3_implementation_report.md` (+256)
  - 수정: `src/context_loop/storage/metadata_store.py` (+191)
  - 수정: `src/context_loop/storage/graph_store.py` (+67/-10)
- 총: 1129 insertions, 10 deletions

범위 가드 침범 없음 — `src/context_loop/storage/` 외 영역(processor/, ingestion/, eval/, web/, mcp/, processor.link_graph_builder, llm_body_extractor 등) 손대지 않음. graph_vocabulary, eval 시스템도 변경 없음.

---

## 계획 vs 구현 매트릭스 (설계서 §5.1)

| ID | 계획 동작 | 실제 변경 | 일치도 |
|----|----------|-----------|--------|
| 1 | `normalize_entity_name` (NFKC + lower + 공백/하이픈/언더스코어 제거, 괄호 제거 미채택) | `src/context_loop/storage/entity_normalizer.py:35-82` — NFKC → strip → squeeze → `[\s\-_]+` 제거 → lower. 괄호 제거 코드 **부재**. | ✅ |
| 2 | `graph_nodes.normalized_name` 컬럼 + 인덱스 | `metadata_store.py:55` (스키마), `:145-146` (`idx_graph_nodes_normalized(normalized_name, entity_type)`). | ✅ |
| 3 | 일회성 백필 마이그레이션 (idempotent) | `metadata_store.py:208-230` — `PRAGMA table_info` 로 컬럼 존재 확인 후 ALTER, `_backfill_normalized_names` 는 `WHERE normalized_name = ''` 필터로 idempotent. | ✅ |
| 4 | `find_graph_node_by_entity` 가 정규화 키로 매칭 | `metadata_store.py:539-569` — `WHERE normalized_name = ? AND entity_type = ?` 로 변경. `LOWER()` 함수 호출 제거 (인덱스 적용 가능 형태). | ✅ |
| 5 | `graph_merge_log` 테이블 + 머지/신규 INSERT | `metadata_store.py:80-89` (스키마 — 설계서 §4.3 와 완전 일치), `:650-689` (`record_graph_merge`). `graph_store.py:202-213` (머지 hit `exact`/`normalized`), `:236-242` (신규 `new`). | ✅ |

---

## 코드 정합성 spot check

### `entity_normalizer.py`
- NFKC 호출 ✅ (`unicodedata.normalize("NFKC", name)`, L67)
- `.lower()` 호출 ✅ (L82)
- 공백 / `-` / `_` 제거 ✅ (`_STRIP_CHARS_PATTERN = re.compile(r"[\s\-_]+")`, L35)
- 괄호 제거 코드 부재 ✅ (소스 grep 결과 `(`/`)` 처리 없음)
- 빈/None 안전 ✅ (`if not name: return ""`)
- 멱등성 ✅ (deterministic 정규식 + casefold)

### `metadata_store.py`
- `_migrate_schema` 가 `PRAGMA table_info(graph_nodes)` 로 컬럼 확인 후 ALTER ✅ (L211-220)
- 인덱스 `idx_graph_nodes_normalized` 가 `(normalized_name, entity_type)` ✅ (L145-146)
- `find_graph_node_by_entity` SQL 이 `normalized_name = ?` ✅ (L562-566) — LOWER() 제거 확인
- `create_graph_node_with_link` 가 `normalized_name` 받아 INSERT ✅ (L478, L495-504)
- `create_graph_node` 도 동일 처리 ✅ (L442, L457-466) — 책임 분리: caller 가 미지정 시 내부 fallback 으로 `normalize_entity_name`
- `record_graph_merge` 가 `graph_merge_log` INSERT ✅ (L650-689)
- `graph_merge_log` 스키마가 설계서 §4.3 와 일치 ✅:
  - `canonical_node_id INTEGER NOT NULL` ✓
  - `raw_entity_name TEXT NOT NULL` ✓
  - `raw_entity_type TEXT NOT NULL` ✓
  - `source_document_id INTEGER NOT NULL` ✓
  - `merge_method TEXT NOT NULL` ✓ (`'exact'|'normalized'|'new'`)
  - `similarity_score REAL` ✓ (D 단계 NULL)
  - `created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP` ✓

### `graph_store.py`
- `save_graph_data` 가 `normalize_entity_name(entity.name)` 호출 ✅ (L168)
- 결과를 `find_graph_node_by_entity` (L171-175) / `create_graph_node_with_link` (L220-226) 양쪽에 전달 ✅
- 머지 hit / 신규 두 경로 모두 merge log INSERT 호출 ✅ (L207-213, L236-242)
- `_record_merge_safely` 가 INSERT 실패를 swallow + warn 처리 ✅ (L298-329, `try/except + logger.warning`) — 그래프 저장 critical path 보호
- `entity_normalizer` import 정상 (L20)

---

## 테스트 결과

| 명령 | 결과 |
|------|------|
| `pytest tests/test_storage/test_entity_normalizer.py tests/test_storage/test_graph_normalization.py -v` | **33 passed in 0.90s** |
| `pytest tests/test_storage/ -q` | **138 passed in 6.94s** (기존 65 + 신규 33 + 다른 storage 40) |

### 요청된 핵심 케이스 커버리지

- 한국어 변형 4종 (`결제 시스템` / `결제시스템` / `결제-시스템` / `결제_시스템`) 같은 노드 매칭 ✅ (`test_korean_whitespace_variants_match`)
- 영문 케이스/구분자 변형 (`Payment Service` / `payment service` / `PAYMENT SERVICE` / `Payment-Service` / `payment_service` / `PaymentService`) 같은 노드 매칭 ✅ (`test_english_case_and_separator_variants_match`)
- `결제 시스템` vs `결제 시스템(v2)` vs `결제 시스템 (legacy)` 별개 노드 유지 ✅ (`test_parentheses_preserved_as_different_nodes`, `test_version_parentheses_preserved`, `test_legacy_parentheses_preserved`)
- merge_log 가 머지/신규마다 정확히 1행 INSERT ✅ (`test_one_log_row_per_entity`, `test_new_node_logs_method_new`, `test_exact_repeat_logs_method_exact`, `test_variant_repeat_logs_method_normalized`)
- 마이그레이션 idempotency (`initialize` 두 번 호출 안전) ✅ (`test_migration_runs_twice_safely`)
- entity_type 격리 (정규화 키 같아도 type 다르면 분리) ✅ (`test_entity_type_mismatch_isolates_nodes`)

---

## 회귀 위험 평가

### pre-existing failures spot check (HEAD~1 비교)

implementer 가 보고한 "본 변경과 무관한 영역의 사전 실패" 가 정말 R3 이전부터 존재하는지 직접 검증:

| 실패 케이스 | HEAD (R3 포함) | HEAD~1 (R3 이전) | 판정 |
|-----------|--------------|------------------|------|
| `test_extraction_unit.py::test_short_parent_body_absorbed_into_first_child` | FAIL | FAIL | pre-existing ✅ |
| `test_extraction_unit.py::test_long_parent_body_emitted_as_standalone_unit` | FAIL | FAIL | pre-existing ✅ |
| `test_build_synthetic_gold_set.py::test_fetch_source_text_anchor_match` | FAIL | FAIL | pre-existing ✅ |
| `test_build_synthetic_gold_set.py::test_fetch_source_text_legacy_chunk_id_fallback` | FAIL | FAIL | pre-existing ✅ |
| `test_build_synthetic_gold_set.py::test_make_graph_gold_item_falls_back_to_node_description` | FAIL | FAIL | pre-existing ✅ |

→ R3 가 신규 회귀를 만들지 않았음.

### DB 마이그레이션 race / 중복 실행

- `_migrate_schema` 가 `PRAGMA table_info` 로 컬럼 존재 확인 후만 ALTER → 중복 실행 안전.
- `CREATE INDEX IF NOT EXISTS` 사용 → 인덱스 idempotent.
- `_backfill_normalized_names` 가 `WHERE normalized_name = ''` 만 대상 → 멱등.
- `test_migration_runs_twice_safely` 로 명시 가드.

### lint 회귀

- 신규 파일 (`entity_normalizer.py`, 신규 테스트 2개): `ruff check` All clean.
- 수정 파일 (`metadata_store.py`, `graph_store.py`) 의 잔존 E501 6건은 모두 R3 이전 commit (`39193b6c`, `^935d923`) 책임 라인 — R3 변경이 신규로 만든 ruff 위반 없음.

### 다운스트림 영향

- `find_graph_node_by_entity` 시그니처: 기존 위치 인자 두 개(`entity_name`, `entity_type`) 호환 유지 + `normalized_name` 은 keyword-only 옵션 → 기존 호출자 호환.
- `create_graph_node` / `create_graph_node_with_link` 도 `normalized_name` 옵션 추가 (None 시 내부 정규화) → 기존 호출자 호환.
- 운영 critical path: `_record_merge_safely` 의 swallow 가드로 merge_log INSERT 실패가 그래프 저장을 막지 않음.

---

## 산출물 검증

- `_workspace/indexing-improvement/R3_implementation_report.md` 작성 ✅
- 보고서 섹션 완비 ✅:
  - 변경 파일 목록 (§1)
  - 핵심 결정 (§2: 정규화 위치 책임 분리, 규칙 채택 범위, 마이그레이션 idempotency, merge_method 분류)
  - 추가된 테스트 + 결과 (§3)
  - 회귀 테스트 결과 (§4)
  - 알려진 한계 / 후속 작업 (§5: D 본질적 한계, 사례 1/4/5/6 미해결, R4+ 후보)
  - 추천 커밋 메시지 (§6)
  - 보류 항목 (§7)

---

## 발견된 이슈

### 메이저: 없음

### 마이너 / 메타

1. **자동 commit 발생**: 검증 도중 implementer 단계의 변경(modified + untracked)이 `63e1fd3` 으로 자동 commit 되어, verifier 단계의 `git stash` 가 stash 할 대상이 없는 상태가 됨. 검증은 `git checkout HEAD~1 -- src tests` 로 R3 이전 상태로 되돌려 pre-existing failure 비교를 수행한 뒤 다시 `git checkout HEAD -- src tests` 로 복원. 영향은 없으나, 향후 implementer→verifier 핸드오프에서는 commit 시점을 명시하는 게 좋음.

2. **`record_graph_merge` 의 commit 횟수**: 매 entity 마다 `await self.db.commit()` 호출 (`metadata_store.py:688-689`). 한 문서당 entity 수가 수십~수백이면 commit 오버헤드가 누적될 수 있음. 현 단계에선 storage 회귀 통과 + critical path 가드 (`_record_merge_safely`) 가 있어 안전하지만, 후속 라운드에서 batch INSERT 로 최적화 여지. **PASS 등급 영향 없음 — 권고 수준**.

3. **`entity_type=None` 케이스**: implementer 가 §5 "신규 발견" 에서 명시한 대로 `find_graph_node_by_entity` 의 `entity_type=''` 케이스는 본 변경 전후 동일 동작 (LOWER 시절에도 AND 비교). 회귀 아님. 별도 라운드 후보.

---

## 후속 권고

1. **eval 측정 (별도 하네스)**: D 도입 후 cross-document 머지 recall 이 실제로 +30~40%p 향상했는지 측정. `graph_merge_log` 의 `merge_method = 'normalized'` 행 수가 baseline 데이터.
2. **`record_graph_merge` batch 화**: entity 수가 많은 문서 인덱싱 시 commit 횟수 절감.
3. **A/E PoC 라운드 진입 기준 가시화**: `graph_merge_log` 가 baseline 으로 사용될 수 있도록 골든셋 라벨링 도구 설계.
4. **R4 후보로 R3 보고서 §5 의 5개 항목 (PoC A, 하이브리드 E, 머지 로그 가시화, rollback split 도구, FQN 정책 호환성) 트래킹**.

---

## 결과 등급

**PASS**

- 설계서 §5.1 5개 항목 모두 정확히 구현 (계획 vs 구현 매트릭스 모두 ✅).
- 신규 테스트 33 케이스 100% 통과 + 전체 storage 138 케이스 통과.
- 신규 회귀 0건 (pre-existing failures 는 HEAD~1 비교로 검증 완료).
- 범위 가드(A/B/C/E/F, 괄호 제거, 한자 변환, eval/processor 영역) 모두 준수.
- 코드 정합성 spot check 전 항목 통과.
- 산출물 보고서 §1~§7 완비.
