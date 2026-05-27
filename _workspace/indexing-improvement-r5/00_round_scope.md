# R5 Round Scope — 저장 측 (Layer 2) stem dedup

## 배경

R4 라운드에서 **추출 측 (Layer 1)** 의 stem 정규화를 적용했지만, **저장 측 (Layer 2)** 의 `find_graph_node_by_entity` SQL 은 여전히 `LOWER(entity_name)` 만으로 매칭. 결과적으로:

- 한 문서 내: `AuthService` ≡ `Auth Service` ≡ `auth-service` (R4 적용)
- 문서 간: `AuthService` ≡ `authservice` 만 (case-insensitive 만)
- **`AuthService` (문서 A) vs `Auth Service` (문서 B) → 별도 노드 2개** ← 빈틈

이 빈틈이 사내 6000노드/8500엣지 데이터에서 **multi-doc pivot 노드 양을 제한** 하고 있어, R6 의 cross-doc 골드셋 (2-hop chain seeds) 작업의 효과를 약화시킴.

R5 의 목표: Layer 2 동등성 판단을 stem 기반으로 확장하여 multi-doc pivot 양 ↑, 다음 라운드 (R6 cross-doc 골드셋) 의 기반 데이터 강화.

## 사용자 합의된 결정 (AskUserQuestion 답변)

| 결정 항목 | 선택 | 이유 |
|---|---|---|
| 마이그레이션 정책 | **스키마만, 재인덱싱 일임** | 기존 데이터 통합 SQL 위험 회피. 사용자가 재인덱싱 시점 통제 |
| stem 정의 | **R4 의 `normalize_name_stem` 재사용** | Layer 1 / Layer 2 일관성. 공백·하이픈·언더스코어·대소문자만 통합. 단·복수형 분리 유지 |

상위 plan: `/root/.claude/plans/pr66-snazzy-kitten.md` (R4 후속 트랙)

## R5 스코프

### 변경 대상

| 파일 | 변경 종류 |
|---|---|
| `src/context_loop/storage/metadata_store.py` | `graph_nodes` 테이블에 `name_stem TEXT` 컬럼 추가 + INDEX. `find_graph_node_by_entity` SQL 을 stem 매칭 우선으로 변경. `create_graph_node_with_link` 에 `name_stem` 파라미터 추가 |
| `src/context_loop/storage/graph_store.py` | `save_graph_data` 에서 `normalize_name_stem` 호출해 stem 컬럼 채움. `name_to_node_id` 로컬 dict 도 stem 기반 키 사용 가능 검토 |
| `src/context_loop/processor/graph_vocabulary.py` | (변경 없음) — `normalize_name_stem` import 만. storage → processor 의존 방향 점검 필요 |
| `tests/test_storage/test_metadata_store.py` | stem 매칭 케이스 + 스키마 컬럼 검증 |
| `tests/test_storage/test_graph_store.py` | 문서 간 stem dedup 통합 시나리오 |

### 마이그레이션 정책

- **NOT** in-place 데이터 통합 (사용자 결정)
- 스키마 변경만 — `ALTER TABLE graph_nodes ADD COLUMN name_stem TEXT`
- 기존 노드의 `name_stem` 은 `NULL` 로 남음 → 재인덱싱 시점부터 자연스럽게 채워짐

### NULL safe 폴백 정책 (디자이너 결정 필요)

기존 노드 (`name_stem IS NULL`) 와의 매칭 처리:
- 옵션 1 (strict): stem 매칭만 → 기존 노드와 매칭 안 됨 → 재인덱싱 전엔 *오히려 중복 ↑*
- 옵션 2 (NULL fallback): `name_stem = ? OR (name_stem IS NULL AND LOWER(entity_name) = LOWER(?))` → 재인덱싱 전에도 기존 노드와 매칭 가능
- 옵션 3 (백필): 스키마 ALTER 직후 `UPDATE graph_nodes SET name_stem = normalize_name_stem(entity_name) WHERE name_stem IS NULL` 1회 실행 — 데이터 통합은 안 하되 stem 만 채움

권고: **옵션 3** — 데이터 *통합* 은 안 하지만 stem *백필* 은 하여 신구 노드가 같은 stem 으로 매칭되면 새 link 만 추가. 사용자 결정 정신 ("스키마만") 과 부합하면서도 재인덱싱 전 dedup 효과 일부 확보. 디자이너가 옵션 1/2/3 중 최종 결정.

### 스코프 *밖*

- **기존 데이터 노드 통합 (in-place merge)** — 사용자 결정으로 제외
- entity_type alias 결합 (Q2 선택은 R4 stem 재사용만)
- relation_type alias 결합
- Git Code 측 AST 상속/호출 (별도 트랙)
- cross-doc 골드셋 생성 (R6 후속 라운드)

## 산출물 (5-파일 R5 구조)

- [x] `00_round_scope.md` — 본 문서
- [ ] `01_analysis.md` — 영향 분석 + NULL safe 폴백 옵션 trade-off
- [ ] `02_design.md` — 스키마 변경 + SQL 변경 + 의존성 방향 + 옵션 1/2/3 결정
- [ ] `03_implementation.md` — 변경 요약
- [ ] `04_verification.md` — pytest + 새/기존 데이터 흐름 검증

## 검증 요구사항

1. 새 스키마 + INDEX 생성 단위 테스트
2. `find_graph_node_by_entity` stem 매칭 시나리오:
   - `AuthService` 저장 + `Auth Service` 매칭 → hit
   - `AuthService` 저장 + `AuthService` (case 변형) 매칭 → hit
   - `AuthService` 저장 + `AuthorizationService` 매칭 → miss
   - `User` 저장 + `Users` 매칭 → miss (형태론 보존)
3. 문서 간 통합 통합 시나리오:
   - D1 에 `AuthService`, D2 에 `Auth Service` 저장 → 단일 노드, `document_ids = {D1, D2}`
4. 기존 노드 (`name_stem IS NULL`) 와의 동작 — 옵션 1/2/3 별 단위 테스트
5. `pytest tests/test_storage/` 전체 회귀 통과
6. `pytest tests/test_processor/` 회귀 통과 (R4 결과 보존)

## 운영 가드

- 커밋·푸시는 verifier PASS 후 사용자 확인 받고
- `LOWER(REPLACE(REPLACE(REPLACE(...))))` 같은 SQL 함수 사용 피하기 — 컬럼 + INDEX 가 더 빠르고 명확
- 의존성 방향: `storage/` → `processor/graph_vocabulary` import 가 가능한지 점검. 순환 의존 발생 시 stem 헬퍼를 `storage/` 측에 복제 또는 별도 공용 모듈로 이동

## 후속 (R6 예고)

R5 완료 → 사내 데이터 재인덱싱 → multi-doc pivot 수 측정 → R6 으로 **2-hop cross-doc 골드셋 생성** (이전 대화에서 합의된 알고리즘 — `pivot 노드를 거치는 A→B→C chain seed 생성, relevant_doc_ids=[D_AB, D_BC] AND 채점`).
