# 엔티티 통합 품질 진단 보고서

> 생성일: 2026-05-26
> 분석 대상: `~/.context-loop/data/metadata.db` (SQLite)
> 재현 스크립트: `_workspace/graph-overview-merge-quality/scripts/diagnose.py`
> 원시 결과: `_workspace/graph-overview-merge-quality/scripts/diagnose_output.json`

---

## 1. 현재 병합 정책 요약

### 1.1 정규 노드 병합 흐름 (`storage/graph_store.py:160` `save_graph_data`)

`GraphStore.save_graph_data(document_id, graph_data)` 는 새 문서의 엔티티를 저장할 때 다음 순서로 동작한다:

1. 각 `entity` 마다 `MetadataStore.find_graph_node_by_entity(name, type)` 호출 (`metadata_store.py:447`).
2. **기존 정규 노드가 있으면** → 재사용. `graph_node_documents` 링크 테이블에 `(node_id, document_id)` 추가, NetworkX 노드의 `document_ids` 집합에 `document_id` 추가. (`merged_count` 증가)
3. **없으면** → `create_graph_node_with_link` 로 새 `graph_nodes` 행 + 링크를 단일 트랜잭션으로 INSERT. (`new_count` 증가)

### 1.2 매칭 기준 (`metadata_store.py:447` `find_graph_node_by_entity`)

```sql
SELECT * FROM graph_nodes
 WHERE LOWER(entity_name) = LOWER(?) AND entity_type = ?
 LIMIT 1
```

- **포함**: 대소문자 무시 (`LOWER()`).
- **요구**: `entity_type` 은 **정확 일치**(대소문자 구분).
- **놓치는 패턴**: 공백/하이픈/언더스코어 변형, 다국어 동치, 단/복수, 약어, 타입 충돌, FQN ↔ 짧은 이름.

### 1.3 검색 시 임베딩 fallback 은 별개 경로

`GraphStore.get_neighbors(entity_name, ..., embedding_fallback=...)` (`graph_store.py:339`) 에서 표면 매칭(완전 일치 → scoped → short)이 모두 실패한 경우에만 `search_entities_by_embedding` 으로 cosine 유사도 기반 시드를 찾는다. 이는 **저장 시 병합이 아니라 조회 시 broaden**이다. 즉:

- 임베딩 fallback 은 의미적으로 같은 엔티티가 여러 노드로 저장된 *이후* 적절히 합쳐서 보여주는 게 아니라, 사용자의 질의 표현 차이를 흡수하는 용도. 저장된 두 노드(`Auth Service` vs `AuthService`)는 임베딩 fallback 으로도 통합되지 않고 여전히 별도 노드로 잔존한다.

### 1.4 정책 한계 요약 (기준선)

| 한계 | 영향 |
|------|------|
| `entity_type` 정확 일치 요구 | `Kafka(system)` vs `Kafka(component)` 가 별도 노드로 잔존 |
| 표면 변형 무시 | `Auth Service` vs `AuthService` vs `auth-service` 가 모두 별도 |
| 다국어 미처리 | `인증 서비스` vs `Auth Service` 가 별도 |
| 단/복수·약어 미처리 | `User` vs `Users`, `DB` vs `Database` 가 별도 |
| 선두/말미 공백 미정규화 | ` 결제 DB (PostgreSQL)` 가 `결제 DB (PostgreSQL)` 와 별도 (현 인덱스에 실제 사례 존재) |
| FQN ↔ 짧은 이름 | 저장은 FQN 유지, 조회에서만 `_extract_short_name`/`_extract_scoped_name` fallback (조회 path-only) |

---

## 2. 인덱스 현황 통계

### 2.1 스키마 상태 (중요 발견)

현재 DB 는 **레거시 스키마**다 — `graph_node_documents` 링크 테이블이 존재하지 않는다 (`sqlite> .tables` 결과: `chunks, documents, graph_edges, graph_nodes, processing_history`).

| 항목 | 값 |
|------|-----|
| `graph_node_documents` 테이블 존재 여부 | **없음** |
| 의미 | 앱이 한 번도 신스키마(`_SCHEMA_SQL` v2)로 초기화/마이그레이션 되지 않았다. `MetadataStore.initialize()` 가 다음 실행될 때 `CREATE TABLE IF NOT EXISTS` 로 자동 생성된다. |
| 분석 영향 | **`cross_document_node_ratio` 측정 불가** — 모든 노드는 단일 문서(`graph_nodes.document_id`)에만 종속되므로 "병합이 실제로 일어났는지" 의 운영지표 산출이 의미 없음. 신스키마로 재인덱싱한 뒤 재측정 필요. |

### 2.2 노드 / 엣지 / 문서 통계

| 항목 | 값 |
|------|-----|
| 총 문서 수 | 4 |
| 그래프 노드를 가진 문서 수 | 1 (`document_id=5` "백엔드 시스템 아키텍처 및 서비스 의존성 맵") |
| 총 노드 수 | 21 |
| 총 엣지 수 | 16 |
| 그래프 보유 문서당 평균 노드 수 | 21.0 |

### 2.3 `entity_type` 분포

| type | count |
|------|------:|
| service | 6 |
| team | 6 |
| component | 5 |
| system | 4 |

### 2.4 `relation_type` 분포

| relation_type | count |
|---------------|------:|
| uses | 7 |
| depends_on | 6 |
| publishes_to | 2 |
| consumes_from | 1 |

### 2.5 인덱스 규모상의 한계

본 인덱스는 단일 문서·21 노드 규모로 매우 작다. 메트릭의 "통계적" 유의성은 낮으나, **알고리즘과 메트릭 정의는 규모에 무관하게 동작**해야 하므로 본 보고서는 규모에 의존하지 않는 사양을 제공한다.

---

## 3. 잠재 중복 그룹 탐지 결과

### 3.1 공백/구분자 정규화 기반 (Category A)

**정규화 정의**: `re.sub(r"[\s_\-.]+", "", name).lower()` — 모든 공백·하이픈·언더스코어·마침표를 제거하고 lower 변환. (다국어 보존)

**탐지 결과**:

| 메트릭 | 값 |
|--------|----:|
| 정규화 후 중복 그룹 수 (`group_count`) | **0** |
| 영향 노드 수 (`affected_nodes`) | **0** |
| `duplication_ratio_surface` | **0.0000** |

**보조 발견**: 공백/구분자 변형으로 인한 중복 그룹은 0이지만, **선두/말미 공백을 가진 노드가 1건** 존재한다:

| id | entity_name (대괄호로 경계 표시) | type |
|----|-----------------------------------|------|
| 12 | `[ 결제 DB (PostgreSQL)]` | component |

→ 본 노드는 선두에 공백이 있는 상태로 저장되어 있다. 같은 이름의 trimmed 버전 (`결제 DB (PostgreSQL)`) 이 향후 다른 문서에서 추출되면 `find_graph_node_by_entity` 의 `LOWER(entity_name) = LOWER(?)` 비교가 공백 차이로 실패해 별도 노드로 분기될 위험이 있다. → **저장 시 `entity_name.strip()` 보강을 권고** (자동 병합과 무관한 안전 정규화).

### 3.2 임베딩 유사도 기반 (Category B, cosine ≥ 0.85)

**현재 상태**: `GraphStore._entity_embeddings` 는 런타임 인메모리 캐시이며, **SQLite 에는 영속화되지 않음**. 또한 `build_entity_embeddings` 는 호출 시점에 임베딩 클라이언트가 필요하다(현재 DB 단독 조회로는 불가).

| 메트릭 | 값 |
|--------|----|
| `duplication_ratio_semantic` | **측정 불가 (caching not active, no client available)** |

**대체 진단(가벼운 표면 fuzzy)**:
SequenceMatcher ratio ≥ 0.85 + 동일 `entity_type` 으로 단/복수·약어·오타류 의심 쌍을 표면 분석으로 탐지.

| 메트릭 | 값 |
|--------|----:|
| fuzzy 후보 쌍 수 | **0** |

**의미상 의심 1건 (수동 검토)**:
- id=3 `PostgreSQL DB` (component) ↔ id=12 ` 결제 DB (PostgreSQL)` (component)
  - 표면 ratio: 약 0.51 → 0.85 임계 미달이라 자동 탐지 안 됨.
  - 의미: 둘 다 PostgreSQL DBMS. 다만 후자는 *결제 서비스 전용 DB* 라는 도메인 분리를 의도한 것일 수 있어 자동 머지 후보는 아니고 **LLM/사용자 판단 후보**.
  - 본 보고서는 자동 병합을 권고하지 않으므로, UI 의 "유사 후보 노드" 섹션 (5절 참조)에서 운영자에게 노출하는 것을 권장한다.

### 3.3 타입 충돌 (Category C)

**정의**: `LOWER(entity_name)` 은 같으나 `entity_type` 이 다른 그룹.

| 메트릭 | 값 |
|--------|----:|
| `type_conflict_count` (lower-name 동일) | **0** |
| `type_conflict_count` (surface-normalized 동일) | **0** |

**보조 발견 — 타입 의심 1건 (수동 검토)**:
| id | entity_name | 현재 type | 직관적 type | 근거 |
|----|-------------|-----------|-------------|------|
| 10 | KakaoPay | team | **service** 또는 **system** | 외부 PG/결제 서비스. 그러나 `Payment Service --depends_on--> KakaoPay` 관계 그래프에서 `team` 으로 잘못 라벨링됨. |
| 11 | Toss PG사 | team | **service** 또는 **system** | 동일 사유. |

→ 이는 LLM Classifier 가 "PG사" 토큰을 보고 "회사 = 팀" 으로 잘못 일반화했을 가능성. **본 보고서 범위(병합 정책 진단) 밖**이지만, 향후 같은 이름이 다른 문서에서 올바른 type 으로 추출될 경우 자동으로 별도 노드로 분기될 위험이 있어 *분기 가능성*을 운영자가 인지하도록 UI 에 노출하는 것을 권고한다 (5절 type_conflict 패널).

### 3.4 FQN 처리 확인 (Category D — 코드 심볼)

**현재 인덱스 상태**: `entity_name` 에 `::` 를 포함한 FQN 노드가 0건. → 본 인덱스에는 코드 심볼(git_code 소스)이 포함되지 않음. (`document_id=5` 는 manual 마크다운 한 건뿐.)

| 메트릭 | 값 |
|--------|----:|
| FQN 노드 수 | **0** |
| short_name collision 그룹 수 | **0** |
| scoped_name collision 그룹 수 | **0** |

**기준선 동작 확인** (코드 리뷰만): `graph_store.py:35-65` `_extract_short_name` / `_extract_scoped_name` 은 **조회시점에만** 동작하며, 저장 시에는 FQN 원본을 그대로 보존한다. 즉:
- `user_service.py::UserService.create` 와 `auth_service.py::UserService.create` 는 별도 노드로 저장됨 (의도된 동작 — 서로 다른 심볼).
- 그러나 `UserService.create` 같은 짧은 이름으로 직접 저장된 노드가 동일 인덱스에 함께 존재하면, 조회 fallback 에서는 둘 다 매칭되지만 저장에서는 별도로 분기. → 향후 git_code 인덱싱 후 재진단 필요 (현재는 측정 불가).

### 3.5 정합성 (Integrity)

| 메트릭 | 값 |
|--------|----:|
| `orphan_edge_count` (양쪽 노드가 `graph_nodes` 에 없음) | **0** |

---

## 4. 통합 품질 메트릭 정의

본 절은 backend builder 가 **API 응답으로 노출해야 할 메트릭 사양**이다. 각 메트릭에 `(이름, 정의, 계산식, 의미)` 4 필드를 적는다.

### 4.1 `duplication_ratio_surface`

- **이름**: `duplication_ratio_surface`
- **정의**: 공백/하이픈/언더스코어/마침표를 제거하고 lower-case 한 키 기준으로, **2 이상의 노드가 같은 키를 공유하는 그룹**에 속한 노드의 비율.
- **계산식**:
  - `key(node) = re.sub(r"[\s_\-.]+", "", node.entity_name).lower()`
  - `groups = {k: [n for n in nodes if key(n) == k]}`
  - `dup_nodes = sum(len(v) for k, v in groups.items() if len(v) >= 2)`
  - `duplication_ratio_surface = dup_nodes / max(len(nodes), 1)`
- **의미**: 표면 정규화만으로 통합되지 않은 노드 비율. **0 에 가까울수록 양호**. 현재 인덱스 값: **0.0000**.

### 4.2 `duplication_ratio_surface_same_type`

- **이름**: `duplication_ratio_surface_same_type`
- **정의**: 4.1 과 동일하되, 그룹 키를 `(surface_key, entity_type)` 튜플로 사용. 자동 병합의 안전한 후보군(타입까지 일치)에 한정한 비율.
- **계산식**:
  - `key(node) = (re.sub(r"[\s_\-.]+", "", node.entity_name).lower(), node.entity_type or "")`
  - `duplication_ratio_surface_same_type = (sum of |v| for groups with |v|>=2) / max(len(nodes), 1)`
- **의미**: "수동 머지 UI" 단계에서 사용자에게 추천할 수 있는 *비교적 안전한* 머지 후보의 비율. 현재 값: **0.0000**.

### 4.3 `duplication_ratio_semantic`

- **이름**: `duplication_ratio_semantic`
- **정의**: 엔티티 이름 임베딩 cosine 유사도 ≥ `θ` (권장 `θ=0.85`) 인 노드 쌍을 union-find 로 클러스터링한 후, 크기 ≥ 2 인 클러스터에 속한 노드의 비율.
- **계산식**:
  - `cluster` ← 모든 노드 쌍 `(i, j)` 에 대해 `cosine(emb_i, emb_j) >= θ` 이면 union(i, j).
  - `dup_nodes = sum(|C| for C in clusters if |C| >= 2)`
  - `duplication_ratio_semantic = dup_nodes / max(len(nodes), 1)`
- **의미**: 표면이 달라도 의미가 같은 엔티티(다국어, 별칭 등)의 비율. **임베딩 캐시가 활성화되어 있어야 산출 가능**. 활성화 절차: backend 가 `/api/graph/merge-quality?include_semantic=true` 호출 시 `GraphStore.build_entity_embeddings(embedding_client)` 를 idempotent 하게 호출.
- 현재 인덱스 값: **측정 불가 (캐시 비어있음)**. UI 는 "임베딩 메트릭 미계산" 배지 + "지금 계산" 버튼을 노출.

### 4.4 `type_conflict_count`

- **이름**: `type_conflict_count`
- **정의**: `LOWER(entity_name)` 이 동일하지만 `entity_type` 이 2종 이상으로 분기된 그룹의 수.
- **계산식**:
  - `by_lower[LOWER(name)] = list of nodes`
  - `type_conflict_count = |{ k : len({n.type for n in by_lower[k]}) >= 2 }|`
- **의미**: 동일 엔티티가 LLM Classifier 의 라벨 흔들림으로 분기된 사례. **0 에 가까울수록 양호**. 현재 값: **0**.

### 4.5 `cross_document_node_ratio`

- **이름**: `cross_document_node_ratio`
- **정의**: `graph_node_documents` 에서 2개 이상의 `document_id` 와 연결된 노드의 비율 — 즉, **병합이 실제로 일어난 노드의 비율**.
- **계산식**:
  - `doc_count[node_id] = |{d : (node_id, d) in graph_node_documents}|`
  - `cross_doc_nodes = |{n : doc_count[n] >= 2}|`
  - `cross_document_node_ratio = cross_doc_nodes / max(len(nodes), 1)`
- **의미**: 병합 정책의 *실제 효과*를 보여주는 운영 지표. 0 이면 모든 노드가 단일 문서 소속(병합 미발생). 현재 값: **0.0000** (단, `graph_node_documents` 테이블 자체가 부재하므로 `legacy schema — measurement unavailable` 로 표기).

### 4.6 `orphan_edge_count`

- **이름**: `orphan_edge_count`
- **정의**: 양쪽 끝(또는 한쪽 끝)이 `graph_nodes` 에 존재하지 않는 엣지의 수.
- **계산식**:
  - `node_ids = {n.id for n in graph_nodes}`
  - `orphan_edge_count = |{e in graph_edges : e.source_node_id not in node_ids or e.target_node_id not in node_ids}|`
- **의미**: 정합성 지표. 0 이 아니면 노드 삭제 후 엣지 잔존(GC 누락) 가능성을 시사. **0 이 정상**. 현재 값: **0**.

### 4.7 (참고용) `leading_trailing_whitespace_node_count`

- **이름**: `leading_trailing_whitespace_node_count`
- **정의**: `entity_name.strip() != entity_name` 인 노드의 수.
- **계산식**: `count(n for n in nodes if n.entity_name.strip() != n.entity_name)`
- **의미**: 정규화 누락으로 인한 분기 위험 노드 수. **0 이 권장**. 현재 값: **1** (id=12).

---

## 5. UI 표시 권고

본 절은 frontend builder 가 `/graph` 페이지에서 메트릭과 잠재 중복 그룹을 어떻게 노출할지에 대한 사양이다.

### 5.1 페이지 상단: 전역 통합 품질 카드 (Header KPI Bar)

`/graph` 페이지 최상단에 다음 KPI 박스 5개를 가로로 배치:

| 카드 | 표시 메트릭 | 값 형식 | 색상 규칙 |
|------|-------------|---------|-----------|
| **표면 중복** | `duplication_ratio_surface` | `X.XX% (N 노드)` | 0% 녹색, < 5% 노랑, ≥ 5% 빨강 |
| **의미 중복** | `duplication_ratio_semantic` | `X.XX% (N 노드)` 또는 "미계산" 배지 | 동일 규칙. 미계산 시 회색 + "계산하기" 버튼 |
| **타입 충돌** | `type_conflict_count` | `N 그룹` | 0 녹색, > 0 빨강 |
| **크로스-문서 병합** | `cross_document_node_ratio` | `X.XX% (N/M 노드)` | 정보(informational), 색상 없음 |
| **고아 엣지** | `orphan_edge_count` | `N` | 0 녹색, > 0 빨강 |

각 카드 우측 상단에 (?) 아이콘 → tooltip 으로 4절의 (이름, 정의, 의미)를 표시.

### 5.2 타입별 클러스터 카드 영역 (메인 뷰)

사용자 결정 사항: "타입별 클러스터 요약 뷰 (entity_type별 클러스터 → 클릭 시 펼침)"

각 type 클러스터 카드(예: `service (6 nodes)`) 우측에 다음 *클러스터 단위* 통합 품질 뱃지를 표시:

| 뱃지 | 노출 조건 | 표시 |
|------|----------|------|
| `surface-dup: N groups` | 본 type 내부에 표면 중복 그룹이 있을 때 | 빨강 배지 |
| `fuzzy-candidates: N pairs` | 본 type 내부에 SequenceMatcher ≥ 0.85 또는 cosine ≥ 0.85 쌍이 있을 때 | 노랑 배지 |
| (없으면 표시하지 않음) | 클린 클러스터 | — |

카드 펼침 시 노드 리스트 우측에 각 노드별 *문서 개수 뱃지* (`X docs`) 와, 본 노드가 어떤 잠재 중복 그룹에 속해 있다면 그룹 ID 배지 (`#dup-A3`) 를 표시. 그룹 ID 배지를 클릭하면 5.4 의 모달이 열린다.

### 5.3 노드 상세 (Side Panel 또는 Modal)

노드를 클릭하면 우측 패널에 다음 섹션 노출 (위→아래 순):

1. **기본 정보**: id, entity_name, entity_type, properties.description.
2. **연결 문서**: 본 노드와 연결된 `document_id` 리스트 (제목 + 링크).
3. **연결 엣지**: incoming / outgoing 엣지 리스트.
4. **유사 후보 노드** (병합 진단 섹션):
   - **표면 정규화 동일**: 같은 `surface_key` 를 가진 다른 노드 (있을 때만)
   - **표면 정규화 동일 + 동일 type**: 위와 동일 + 같은 `entity_type` (자동 머지 후보로 가장 안전)
   - **임베딩 유사도 ≥ 0.85**: 캐시가 있을 때만, 상위 5개 (이름, type, similarity).
   - **타입 충돌**: `LOWER(name)` 동일하지만 type 이 다른 노드 (있을 때만 — 분기 분석용)
   - 각 항목 우측에 "그래프로 이동" 버튼만 노출. **"병합" 버튼은 본 라운드에서 비활성화** (자동 병합 금지 정책).

### 5.4 별도 패널: 머지 후보 인벤토리 (`/graph` 페이지 하단 또는 토글)

`잠재 중복 그룹 인벤토리` 라는 접이식 섹션에 다음 4개 탭:

| 탭 | 내용 | 데이터 출처 |
|----|------|-------------|
| **표면 중복** | 각 그룹: 정규화 키, 멤버 노드 리스트(id, name, type), 그룹 크기 | `duplication_surface.groups` |
| **타입 충돌** | 각 그룹: lower(name), 멤버(id, name, type 다름) | `type_conflicts.exact_lower_match_with_diff_type.groups` |
| **Fuzzy 후보** | 각 쌍: a, b, similarity | `fuzzy_candidates.pairs` (표면 fuzzy 또는 임베딩) |
| **공백/정규화 위험** | trim 불일치 노드 리스트 | `stats.leading_or_trailing_whitespace_nodes` |

각 행에 "그래프로 강조" 액션. 자동 병합 UI 는 **본 라운드 범위 외**.

### 5.5 미계산 상태 UX

`duplication_ratio_semantic` 처럼 임베딩이 필요한 메트릭은:
- 초기 로드 시 "미계산" 회색 배지 + `[계산하기]` 버튼.
- 클릭 시 backend `/api/graph/merge-quality/build-embeddings` 호출 → 진행률 표시 → 완료 시 자동 새로고침.
- 완료 결과는 메모리 캐시이므로 페이지 리로드 시 다시 계산 필요 — UI 에 "마지막 계산: HH:MM:SS, 인덱스 변경 시 재계산 권장" 안내.

---

## 6. 권고 — 다음 라운드 이후 작업

### 6.1 자동 병합 정책 변경 시 위험

- **잘못된 머지의 복구가 어렵다**: `graph_nodes` 행을 합치면 양쪽 문서의 `properties.description` 중 하나를 선택/병합해야 하고, 엣지의 `source_node_id`/`target_node_id` 를 재포인팅해야 한다. 잘못된 머지를 되돌리려면 양쪽 문서를 모두 재처리(`delete_document_graph` 후 재추출)해야 하므로 비싸다.
- **LLM 분류 흔들림과 결합되면 cascade 오류**: 한 번 잘못 머지된 노드가 새 문서 인덱싱 시 시드로 재사용되어, 부정확한 type/description 이 전파된다.

### 6.2 권장 단계 (보수적 → 공격적)

1. **현 라운드**: 진단 도구 노출 (메트릭 카드 + 머지 후보 인벤토리). 자동 병합 없음. 사용자가 데이터 상태를 *볼 수 있게* 만든다.
2. **다음 라운드 A**: 운영자 수동 머지 UI (안전한 surface-same-type 후보만). 머지 액션 시 `processing_history` 에 `action="merged"` 기록 + undo 30일 윈도우 제공.
3. **다음 라운드 B**: LLM 검증 보조 머지 — 시스템이 후보 쌍을 LLM 에게 "이 두 엔티티는 같은가?" 묻고, "예/아니오/모름" 의 응답을 사용자에게 *제안*만 한다 (자동 머지 아님).
4. **장기 (가장 위험)**: 임베딩 + LLM 검증을 결합한 자동 머지. 단, undo 윈도우, 처리 이력 추적, 사용자 audit log 가 모두 갖춰진 후에만.

### 6.3 부수 권고 (본 라운드와 무관하게 처리 가능)

- **A. 저장 시 `entity_name.strip()` 보강** (`save_graph_data` / `find_graph_node_by_entity` 양쪽): 선두/말미 공백으로 인한 분기 방지. 현 인덱스에 1건 사례 존재(id=12). **자동 병합이 아니라 안전 정규화이므로 위험 낮음**.
- **B. 신스키마 마이그레이션**: 현 DB 는 `graph_node_documents` 가 없는 레거시 상태. 다음 앱 실행 시 자동 생성되나, *기존 21개 노드의 링크 백필*은 별도 마이그레이션이 필요할 수 있다 (`for n in graph_nodes: INSERT INTO graph_node_documents (n.id, n.document_id)`). builder/QA 가 이를 확인할 것.
- **C. LLM Classifier 의 type 라벨 검증**: `KakaoPay`/`Toss PG사` 가 `team` 으로 잘못 분류된 사례 확인. 본 하네스(병합 진단) 범위 밖이므로 `indexing-improvement` 하네스에 전달 권고.
