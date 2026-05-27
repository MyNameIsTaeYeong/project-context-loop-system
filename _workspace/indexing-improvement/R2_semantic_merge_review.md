# R2 — Semantic Entity Merge 설계 검토 (Design-only)

> **범위**: confluence_mcp 그래프 추출에서 만들어진 entity 를 `graph_store` 의 기존 노드들과 **의미론적으로(semantically)** 병합하는 방안의 사전 검토.
> **상태**: design-only. 본 문서는 비교/권고 문서이며 **코드 변경 결정을 포함하지 않는다**.
> **선행 자료**: `_workspace/indexing-improvement/03_confluence_graph_findings.md`(R1 F-CG-10 에서 graph_store 영역으로 스코프 아웃됐던 항목).

---

## 0. 요약 (TL;DR)

- 현재 머지는 **`LOWER(entity_name) = LOWER(?) AND entity_type = ?` 정확 매칭** 뿐 (`metadata_store.py:447-460`). 표기 변형/약어/다국어 한 글자 차이만으로도 노드가 분리된다.
- **단기 권고 (recommend)**: **후보 D (룰 기반 정규화)** — 정규화된 키 컬럼을 추가하고 `find_graph_node_by_entity` 가 정규화 키로 일치 검색. false-merge 위험 거의 0, ROI 가장 큼.
- **장기 권고**: **후보 E (룰 D → 임베딩 A 후보 추리기 → LLM B 판정)** 하이브리드. 단 임베딩/LLM 두 단계 모두 신중한 PoC 후 도입.
- **지금은 하지 말 것**: **풀 LLM 판정 (B 단독)** — 인덱싱 throughput 파괴, 비용 큼.
- **PoC 의사결정 기준**: pair-wise true-merge **precision ≥ 0.98** AND **recall ≥ 0.70** 만족 시 다음 단계로 진입.

한 줄 결정 권고: **D 를 R2 라운드 후속 작업으로 진행하고, A/E 는 PoC 골드셋 라벨링 후 별도 라운드에서 평가한다.**

---

## 1. 문제 정의

### 1.1 현재 동작 (재확인)

```python
# src/context_loop/storage/metadata_store.py:447-460
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

호출 경로: `graph_store.save_graph_data` (`graph_store.py:160-214`) 가 entity 마다 호출 → 히트 시 `merged_count += 1` + `add_node_document_link`(다문서 연결), 미스 시 `create_graph_node_with_link`(신규).

특성:
- 매칭 키 = `(LOWER(entity_name), entity_type)` 만.
- 공백/하이픈/언더스코어/한영병기/괄호/약어/오타 — **모두 다른 노드로 간주**.
- 임베딩 컬럼/ANN 인덱스 없음. (graph_store 안에는 in-memory `_entity_embeddings` 캐시가 있지만 이는 검색 쿼리용이지 머지용이 아님 — `graph_store.py:85, 699-726`)

### 1.2 한계 사례 (구체 예시)

| # | 사례 유형 | 예시 (모두 같은 실세계 개념) | 현재 동작 | 정답 |
|---|----------|------------------------------|----------|------|
| 1 | 다국어 (한↔영) | `결제 서비스` / `Payment Service` / `PaymentService` | 3개 별개 노드 | 1개 노드 (alias 3개) |
| 2 | 공백·구분자 변형 | `결제시스템` / `결제 시스템` / `결제-시스템` | 3개 별개 노드 | 1개 노드 |
| 3 | 부가 표기 (괄호/버전) | `결제 시스템` / `결제 시스템(v2)` / `결제 시스템 (legacy)` | 3개 별개 노드 | 합치는 게 옳은지 **불확실** — v2/legacy 는 별개 노드여야 할 수도 |
| 4 | 약어 ↔ 풀네임 | `PG` / `Payment Gateway` / `결제 게이트웨이` | 3개 별개 노드 | 1개 노드 (약어 사전 필요) |
| 5 | 동음이의어 (서로 다른 노드여야 함) | 도메인 A 의 `API` (결제 SDK) vs 도메인 B 의 `API` (인증 API) | 1개 노드로 잘못 합쳐짐 (현재도) | 2개 노드 |
| 6 | 폴백 표기 vs 표제 (link_graph_builder ↔ body_extractor) | link path 의 `page:12345` (제목 부재 폴백, `link_graph_builder.py:131-134`) vs body 의 `결제 시스템` (실제 표제) | 2개 별개 노드 | 1개 노드 (page_id 메타로 식별 가능) |
| 7 | 대소문자·복수형 | `auth-service` / `Auth-Service` / `Auth-Services` | 1+2 (lower 일치 1쌍) / 별개 1 | 1개 노드 |

→ **사례 5 가 가장 위험**: 머지 정책을 너무 공격적으로 만들면 동음이의어가 강제 통합되어 그래프 검색 품질이 깨진다. 이 위험이 후보 비교의 핵심 축이다.

### 1.3 현재 부분 보정의 한계

- `llm_body_extractor._canonical_name` (R1 보고서 F-CG2-09): 한 호출 안에서만 entity 표기 통일. **문서 간/세션 간 통일 안 됨**.
- `llm_body_extractor` 의 `(name.lower(), entity_type)` dedup (`llm_body_extractor.py:217-222`): 한 문서 내 unit 응답 통합용. **다른 문서가 만든 노드와는 분리**.
- 결과적으로 같은 confluence space 의 두 문서가 같은 entity 를 약간 다른 표기로 만들면 **항상 분리**된다.

---

## 2. 목표 정의 — 무엇이 "성공"인가

### 2.1 정량 목표 (제안)

| 지표 | 현재 (baseline 가정) | 단기 목표 (D 도입 후) | 장기 목표 (E 도입 후) |
|------|---------------------|----------------------|---------------------|
| **true-merge recall** (같아야 할 쌍 중 머지 비율) | ~30% (추정, 공백·대소문자 정도만) | ≥ 70% | ≥ 85% |
| **false-merge rate** (머지된 쌍 중 잘못 합쳐진 비율) | ~0.5% (추정, 동음이의 충돌) | **< 1%** | **< 2%** |
| **idempotency**: 같은 문서를 두 번 인덱싱 → 그래프 동일 | OK (정확 매칭은 deterministic) | **OK 유지** (필수) | OK 유지 (임베딩 모델 버전 고정 필요) |
| **인덱싱 throughput 영향** | baseline | ≤ 5% degradation | ≤ 20% degradation |
| **저장소 비용 증가** | baseline | ≤ +10% (정규화 컬럼 1개) | ≤ +30% (임베딩 컬럼) |

### 2.2 정성 목표

- **롤백 가능성**: 머지 결정을 되돌릴 수 있어야 한다. 즉 머지 정보(어떤 raw entity 가 어떤 canonical node 로 갔는지)를 보존.
- **explainability**: 검색·디버깅 시 "왜 이 두 entity 가 같은 노드로 합쳐졌는지" 추적 가능해야 한다.
- **cold start 안전**: 첫 노드부터 마지막 노드까지 일관된 정책. 임베딩 ANN 같은 동적 인덱스에 의존 시 부트스트랩 케이스를 명시.

### 2.3 비목표 (out of scope)

- entity description / properties 머지 정책 (현재 빈 description 만 보강하는 정책 유지).
- relation/edge 머지 (entity 머지에 자동으로 따라오는 부수효과는 인정하되, edge dedup 강화는 별도 작업).
- self_entity vs 일반 entity 충돌 (R1 F-CG-10) — 본 검토와 무관.

---

## 3. 후보 비교

### 3.1 후보 카탈로그

| ID | 후보 | 핵심 아이디어 |
|----|------|-------------|
| **A** | 임베딩 코사인 유사도 | entity_name 을 임베딩 후 ANN/threshold 검색으로 유사 노드 찾기 |
| **B** | LLM 판정 | 후보 K 개 (또는 모든 노드) 에 대해 LLM 이 "동일 entity 인가?" 판정 |
| **C** | 별칭 사전 (alias_dict) | 수동/반자동으로 구축된 alias 매핑 테이블 |
| **D** | 룰 기반 정규화 | 공백/대소문자/하이픈/괄호/한영병기 등 결정론적 normalization |
| **E** | 하이브리드 (D → A → B) | 정규화로 빠르게 후보 추림 + 임베딩으로 의미적 후보 + LLM 으로 최종 판정 |
| **F** | 명시적 alias 엣지 | 머지하지 않고 `alias_of` 관계로 표현 — 분리 노드 유지 |

### 3.2 후보별 상세

#### A. 임베딩 코사인 유사도

- **동작 위치**: `graph_store.save_graph_data` 안의 `find_graph_node_by_entity` 호출 전에 추가 lookup, 또는 별도 백그라운드 merge job 으로 분리.
- **신규 인프라**:
  - `graph_nodes` 에 `name_embedding BLOB` 컬럼 추가 (또는 별도 `graph_node_embeddings` 테이블).
  - ANN 인덱스 (작은 규모 ~10K 노드는 brute-force 도 가능, 100K+ 면 HNSW/IVF 등).
  - 임베딩 클라이언트 의존 — 현재 `graph_store.build_entity_embeddings` 가 `embedding_client.aembed_documents` 호출 가능 (`graph_store.py:687-688`).
- **threshold 결정**: cosine ≥ 0.92 정도 시작. 도메인별 튜닝 필요.
- **latency overhead per entity**:
  - 임베딩 생성: ~50ms (배치 시 더 빠름)
  - ANN 검색: brute-force <10K 노드 ~5ms, ANN ≤1ms.
  - **합산 ≈ +50ms/entity** (입출력 한 번씩).
- **비용**: 임베딩 모델이 self-host (예: bge-m3) 면 ≈ 0. 외부 API 면 1K entity ≈ $0.01~0.05.
- **실패 시 영향**:
  - false-merge: threshold 너무 낮으면 동음이의어 강제 통합 (사례 5).
  - idempotency: 임베딩 모델 버전 바뀌면 같은 입력 → 다른 벡터 → 다른 머지 결정. 버전 고정 필요.
  - cold start: 노드 0~수십 개 단계에서는 threshold 가 noise 에 민감.
- **장점**: 다국어 (한↔영) 와 약어 (사례 4) 를 잘 잡아낸다. 학습 데이터 무관.
- **약점**: threshold 단일 값으로 모든 entity_type 을 처리하기 어려움. 동음이의어 위험 가장 큼.

#### B. LLM 판정

- **동작 위치**: 룰/임베딩이 K 개의 후보를 추린 후 LLM 에 "후보 중 같은 entity 가 있는가?" 질의.
- **신규 인프라**: LLM 호출 큐, 결과 캐싱(같은 쌍을 두 번 묻지 않도록 `(name_a, name_b, type) → verdict` 캐시).
- **latency overhead per entity**: 후보 K=5 면 LLM 호출 1회 ≈ 500ms~5s (qwen2.5:7b @ Ollama 기준).
- **비용**: 1K entity × 1 call ≈ 토큰 1K × 1K = 1M token. self-host 면 시간 비용 위주, 외부 API 면 1K entity ≈ $0.5~5.
- **실패 시 영향**:
  - false-merge: LLM hallucination 으로 잘못된 동일성 판정 가능.
  - idempotency: LLM 자체가 비결정적 (temperature > 0). temperature=0 + 결과 캐싱으로 완화 가능하지만 모델 버전 바뀌면 결과 변동.
  - 인덱싱 throughput: LLM 호출이 인덱싱 critical path 에 들어가면 큰 폭 저하.
- **장점**: 약어/문맥 의존 동일성 (사례 3, 4) 을 가장 잘 판정.
- **약점**: 비용·지연 가장 큼. 단독 사용 비추천.

#### C. 별칭 사전 (alias_dict)

- **동작 위치**: 정규화 단계 안의 lookup table.
- **신규 인프라**: `entity_aliases` 테이블 또는 YAML 파일. `(canonical_name, entity_type, [aliases...])`.
- **latency overhead per entity**: 해시 lookup ~0.01ms. 무시 가능.
- **비용**: 구축/유지 인건비. 1K alias 수동 구축 ≈ 8h.
- **실패 시 영향**:
  - false-merge: 사전이 잘못 작성되면 그대로 잘못 머지.
  - 누락: 사전에 없는 변형은 못 잡음 (recall 한계).
  - 유지보수성: 사내 용어 사전을 누가 유지할 것인가? — 거버넌스 문제.
- **장점**: 정확도 높음 (사람이 검토함). 도메인 지식 정확히 반영.
- **약점**: 확장성 없음. 새 entity 등장 시 사전 갱신 필요.

#### D. 룰 기반 정규화 (recommended for short-term)

- **동작 위치**: `find_graph_node_by_entity` 의 매칭 키를 `entity_name` 원본 → 정규화된 키로 교체. `graph_nodes` 테이블에 `normalized_name TEXT` 컬럼 추가하고 인덱스 생성.
- **정규화 규칙 (제안)**:
  1. Unicode NFKC 정규화.
  2. 양끝 공백 제거.
  3. 연속 공백 → 단일 공백.
  4. 모든 공백 / `-` / `_` 제거 (또는 `_` 로 통일).
  5. 양 끝 괄호 묶음 제거 (예: `(v2)`, `(legacy)`). **이건 옵션** — 사례 3 의 v2/legacy 가 별개 노드여야 한다면 제거하지 말 것.
  6. 케이스 폴딩 (lower).
  7. (선택) 한자/일본어 제거나 변환은 하지 않음 (false-merge 위험).
- **latency overhead per entity**: 정규식 1~3회 ≈ <0.1ms. 무시 가능.
- **비용**: 0 (self-contained).
- **실패 시 영향**:
  - false-merge: 규칙 5(괄호 제거) 채택 시 사례 3 에서 잘못 머지 가능. → **규칙 5 는 R2 단기에서 채택하지 않음** 권고.
  - idempotency: 완벽히 deterministic. 마이그레이션 시점에 전체 노드 재정규화 1회.
- **장점**: 지연·비용·복잡도 모두 최저. R2 가장 빠른 win.
- **약점**: 다국어 (사례 1), 약어 (사례 4) 못 잡음. recall 한계.

#### E. 하이브리드 (D → A → B)

- **동작 위치**:
  - Stage 1 (D, 동기): 정규화 키로 정확 매치 시도. 히트면 즉시 머지 — fast path.
  - Stage 2 (A, 동기 또는 비동기): D 미스 시 임베딩 ANN top-K. cosine 이 매우 높음 (≥ 0.97) 이면 즉시 머지, 중간 (0.92~0.97) 이면 Stage 3 로 위임.
  - Stage 3 (B, 비동기 큐): LLM 판정. 결정 전까지 신규 노드로 생성하고, 나중에 `merge_node(src_id → tgt_id)` 마이그레이션.
- **신규 인프라**: D + A + B 합산. + 비동기 머지 큐 + `merge_node` 마이그레이션 함수 (graph_edges/graph_node_documents 재배선).
- **latency overhead per entity**: critical path 는 D+A ≈ 50ms. B 는 비동기.
- **비용**: A+B 비용 합산. 캐싱이 핵심 — `(normalized_a, normalized_b, type) → verdict` 캐시로 재호출 최소화.
- **실패 시 영향**: 단계가 많아 디버깅 어려움. rollback 도 여러 단계 영향.
- **장점**: 정확도·recall 둘 다 챙김. 단계별 차단 가능.
- **약점**: 구현 난이도 최고. 단계별로 PoC 검증 필요.

#### F. 명시적 alias 엣지

- **동작 위치**: 머지하지 않음. 대신 추정 유사 노드 사이에 `alias_of` 관계 신설.
- **신규 인프라**: `graph_edges.relation_type = 'alias_of'` 추가. 검색 시 alias_of 엣지를 따라 expand 하는 로직.
- **latency overhead per entity**: A 와 동일 (후보 탐지 비용).
- **비용**: A 와 동일.
- **실패 시 영향**:
  - false-alias: alias 엣지 잘못 만들어져도 노드는 분리 유지 → **rollback 안전**.
  - 검색 시 expand 비용: alias 엣지를 따라가는 추가 그래프 traversal 필요.
- **장점**: 비파괴적. 사례 5 같은 동음이의어 위험 회피.
- **약점**: 그래프가 커지고 노드 수 줄지 않음. 통계/시각화/UI 에서 "사실상 같은 entity" 처리는 별도 로직 필요.

### 3.3 비교표

| 후보 | 정확도 (P/R) | 지연 | 비용 (1K entity) | 유지보수성 | false-merge 위험 | 구현 난이도 | rollback |
|------|-------------|------|------------------|----------|-----------------|-----------|---------|
| **A 임베딩** | 중/중 | ~50ms | $0~0.05 (self-host 시 0) | 중 (threshold 튜닝) | **중** (threshold 의존) | M | 어려움 (병합 후) |
| **B LLM** | 고/고 | ~1s | $0.5~5 | 낮음 (모델 의존) | **중~높음** (hallucination) | M | 어려움 |
| **C alias 사전** | 매우 고/낮음 | <0.01ms | 인건비 (8h/1K) | **매우 낮음** (사람 의존) | 낮음 | S | 쉬움 |
| **D 룰 정규화** | 중/중 | <0.1ms | 0 | 높음 | **매우 낮음** | **S** | 쉬움 |
| **E 하이브리드** | 고/고 | ~50ms (critical) | A+B | 중 | 중 | **L** | 매우 어려움 |
| **F alias 엣지** | A 수준 + 비파괴 | ~50ms | $0~0.05 | 중 | **0 (비파괴)** | M | **매우 쉬움** |

ROI 정성 평가:

```
ROI = (true-merge gain × 그래프 검색 품질 향상) / (구현 공수 + 운영 risk)

D: 높은 ROI — 공수 S, recall +30~40%p, 위험 거의 0
F: 중간 ROI — 공수 M, 비파괴라 안전하지만 검색 측 추가 작업 필요
A: 중간 ROI — 공수 M, recall 큼 but 동음이의 위험
E: 장기 ROI — 공수 L, 단기 진입 부담 큼
B 단독: 낮은 ROI — 비용·지연 큼
C: 낮은 ROI — 확장성 없음, 인력 소모
```

---

## 4. 위험 분석

### 4.1 false-merge 가 그래프 검색 품질에 미치는 영향

- 한 노드가 무관한 N 개 문서와 연결되면, 그 노드를 거치는 모든 그래프 traversal 이 **noise 를 증폭**시킨다.
- 예: 사례 5 처럼 도메인 A 의 `API` 와 도메인 B 의 `API` 가 합쳐지면, "API 사용 방법" 질의에 두 도메인 문서가 동시 노출 → ranker 가 분리 못 하면 답변 품질 큰 손상.
- 임팩트 정량: 1% false-merge 가 검색 top-5 noise 를 ~5% 증가시킬 수 있다는 보수적 추정 (정량 측정 필요).
- 방어: **threshold 보수적 + 동음이의 후보는 비파괴 후보 F 로 우회** 가 안전.

### 4.2 idempotency 손상

| 후보 | idempotency 보장 조건 |
|------|---------------------|
| D | **항상 보장** (deterministic 정규화) |
| C | alias_dict 변경 없으면 보장 |
| A | 임베딩 모델 버전 고정 시 보장. 모델 업데이트 시 재정규화 필요 |
| B | temperature=0 + 모델 버전 고정 + 결과 캐싱 시 보장 |
| F | A/B 의 비결정성을 이어받음 — 단, 비파괴라 영향이 작음 |
| E | D 단계만 deterministic. A/B 단계는 외부 의존 |

→ **재인덱싱 시 그래프 동일성**은 D 만 자명 보장. A/B/E 도입 시 임베딩/LLM 버전 메타데이터를 `graph_nodes.properties` 에 기록하고 변경 감지 정책 필요.

### 4.3 rollback 가능성

머지를 되돌리려면 다음이 모두 가능해야 한다:

1. 어느 raw entity 가 어느 canonical node 로 갔는지 추적 (현재 코드는 **추적 안 함** — `merged_count` 만 통계로 +1).
2. 머지된 노드를 분리할 때 `graph_node_documents` / `graph_edges` 를 어떻게 재배선할지 결정.

영향 범위:

- `graph_nodes`: 분리 시 새 row 생성.
- `graph_node_documents`: 어느 link 가 어느 noun 의 것이었는지 모르면 rollback 불가능.
- `graph_edges`: source/target 가 머지된 노드면 split 시 어느 쪽으로 갈지 결정 어려움.

→ **rollback 가능성 확보 조건**: 머지 결정 시 **머지 이벤트 로그**를 별도 테이블에 저장. 예:

```sql
CREATE TABLE graph_merge_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_node_id INTEGER NOT NULL,
    raw_entity_name TEXT NOT NULL,
    raw_entity_type TEXT NOT NULL,
    source_document_id INTEGER NOT NULL,
    merge_method TEXT NOT NULL,         -- 'exact' | 'normalized' | 'embedding' | 'llm' | 'alias'
    similarity_score REAL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

D 만 도입해도 머지 로그를 같이 도입할 가치가 크다 (관측성).

### 4.4 cold start / scale 영향

| 단계 | D | A | B | E | F |
|------|---|---|---|---|---|
| 노드 0~수십 개 | OK | threshold noise 큼 | 비용 vs 효익 안 맞음 | D 만 동작 | A 와 같음 |
| 100~수천 개 | OK | OK | 가능 (캐시 hit 늘어남) | OK | OK |
| 100K+ | OK | ANN 인덱스 필수 | 비용 폭증 | OK (B 비동기) | OK (ANN 필수) |

→ D 는 어떤 스케일에서도 안전. A 도입 시 점진 (수천 노드 이후) 권고.

### 4.5 confluence_mcp 의 폴백 표기 (사례 6) 의 특수성

`link_graph_builder._target_name`(`link_graph_builder.py:123-136`) 이 `page:{target_id}` 폴백을 만든다. body 추출 측은 동일 페이지의 실제 표제를 사용한다. 두 출처가 같은 페이지를 다른 이름으로 등록할 수 있다.

→ 정규화 D 만으로는 못 잡는다 (`page:12345` vs `결제 시스템` 은 어떤 정규화로도 동일해지지 않음).

해결 옵션:
- (i) `properties.page_id` 메타를 entity 에 부여하고 머지 시 동일 page_id 면 canonical 표제를 우선.
- (ii) link_graph_builder 가 page_id 를 미리 표제로 lookup (별도 작업, R1 F-CG-05/06 영역).

본 라운드(R2) 권고: (i) 의 page_id 메타를 정규화 키의 보조 신호로 추가. **단 confluence_mcp 외 출처 (git_code 등) 와 충돌 없는 namespace 사용 필요**.

---

## 5. 권고 (Decision)

### 5.1 단기 (R2 → R3 진입 가능 수준)

**채택 후보: D (룰 기반 정규화) + 머지 로그 도입**

근거:
- 위험 거의 0 (false-merge 매우 낮음).
- 공수 S (정규화 함수 + DB 컬럼 + 마이그레이션 + find_graph_node_by_entity 수정).
- recall 즉시 +30~40%p 추정 (사례 2, 7 즉시 해결).
- idempotency 자명 보장.
- 머지 로그가 함께 들어가면 향후 A/E 도입 시 정량 평가의 baseline 데이터로 활용.

구체적 범위(설계 수준 — 본 라운드 implementer 미실행):
1. `entity_name` → `normalize_entity_name(name) -> str` 함수 (NFKC + lower + 공백/하이픈/언더스코어 정규화). **괄호 제거는 R2 에서 채택 안 함** (사례 3 false-merge 위험).
2. `graph_nodes.normalized_name` 컬럼 추가 + 인덱스.
3. 기존 노드 일회성 백필 마이그레이션.
4. `find_graph_node_by_entity` 가 정규화 키로 매칭.
5. `graph_merge_log` 테이블 도입 (관측성 / 후속 평가용).

### 5.2 장기 (R3 이후 PoC 후 결정)

**조건부 채택: E (하이브리드)**

- D 도입 후에도 사례 1 (다국어), 사례 4 (약어) 가 남는다.
- PoC 결과 임베딩이 precision ≥ 0.98 / recall ≥ 0.70 만족하면 A 단계 도입.
- A 만으로 false-merge 가 1% 초과하면 B 단계 추가 (LLM 후순위 판정).
- 어떤 단계든 **머지 로그 + version stamp** 가 운영 안전망.

### 5.3 지금은 하지 말 것

- **B 단독**: 인덱싱 critical path 에 LLM 호출 → throughput 파괴. R1 F-CG2-07 에서 본 LLM 호출 시간 비용을 머지에도 추가하면 손익 안 맞음.
- **C alias_dict 단독**: 확장성·거버넌스 문제. 단 D 의 보조로 entity_type 별 약어 사전을 부분적으로 활용하는 건 OK.
- **D 의 괄호 제거 규칙**: 사례 3 false-merge 위험. 사내 데이터 분포 측정 후 결정.
- **F 단독**: 정작 그래프 검색 측이 alias_of 를 따라가도록 수정해야 함 → 검색 측 범위까지 늘어남. R2 라운드 가드 침범.

### 5.4 결정 다이어그램

```
                  +-----------------+
                  | save_graph_data |
                  |  per entity     |
                  +--------+--------+
                           |
                  normalize_entity_name(e)
                           |
                  +--------v--------+
                  |  D: exact match |
                  |  on normalized  |
                  +--------+--------+
                     hit / miss
              hit -----+    +----- miss
                      |          |
              merge   |          | (R2 단기: 새 노드 생성 + 머지 로그)
                      |          |
                      |          v
                      |   [R3 이후 E 도입 시]
                      |   임베딩 ANN top-K → threshold
                      |       0.97+ → merge
                      |       0.92~0.97 → 비동기 LLM 큐
                      |       <0.92 → 새 노드
                      v
              graph_merge_log INSERT
```

---

## 6. PoC 경로 (실험 설계 — 구현 아님)

### 6.1 데이터 준비

- 기존 인덱싱된 confluence_mcp 그래프 노드를 dump.
- 추출 명령: `get_all_graph_nodes` (`metadata_store.py:441-445`) + `get_all_node_document_links` (`metadata_store.py:495-504`) 결과를 JSON 으로 export.
- 노드 수가 적으면 (수백) 전체 사용. 많으면 entity_type 별 stratified sampling (각 type 200~500 노드).

### 6.2 골드셋 구성

- 수동 라벨링: 노드 풀에서 pairwise 후보 추출. 단순 brute-force pair 는 O(N^2) — 노드 1000 개면 50만 쌍. 너무 많음.
- 줄이기: 다음 후보군만 라벨링.
  1. 정규화 후 동일: D 의 정답 (가짜 양성 검증용).
  2. 임베딩 cosine ≥ 0.85 인 쌍: A 의 후보군.
  3. 같은 entity_type 안의 무작위 N=500 쌍: baseline negative.
- 라벨링자: 도메인 SME 1명 + 리뷰어 1명. 합의 안 되는 쌍은 별도 표시.
- 목표 골드셋 크기: 1000~3000 쌍.
- 라벨: `same` / `different` / `unclear`.

### 6.3 측정 지표

각 후보에 대해:
- **precision** = TP / (TP + FP) — 머지/alias 판정한 쌍 중 정답이 same 인 비율.
- **recall** = TP / (TP + FN) — 정답 same 쌍 중 잡은 비율.
- **F1** = 2PR / (P+R).
- **per entity_type breakdown**: system, module, policy, team, concept 등별로 분리 측정.

부가 지표:
- **idempotency**: 같은 입력 두 번 실행 → 결과 동일성.
- **latency p50, p95**: per entity, batch 1000.
- **cost**: 외부 API 사용 시 1K entity 비용.

### 6.4 의사결정 기준

| 단계 | 진입 조건 |
|------|----------|
| D 만 (단기) 진입 | 별도 PoC 없이 진입 가능. 마이그레이션 회귀 테스트만. |
| A 추가 (E 의 중간) | golden set 에서 precision ≥ 0.98 **AND** recall ≥ 0.70 **AND** latency p95 ≤ 100ms |
| B 추가 (E 완성) | A 결과로 false-merge 가 목표(<1%) 초과할 때만 보강 |
| F 만 (보수 옵션) | 사례 5 (동음이의) 비율이 데이터에서 5% 초과면 F 우선 검토 |
| 진행 중단 | 어느 후보든 precision < 0.95 → 사내 데이터로는 시기상조, R3 보류 |

### 6.5 PoC 산출물 (가상)

```
_workspace/indexing-improvement/R3_semantic_merge_poc/
├── golden_pairs.jsonl          # 라벨링 결과
├── results_D.json              # D precision/recall
├── results_A_threshold_sweep.json
├── results_E.json
└── decision.md
```

본 PoC 자체는 **별도 라운드**에서 실행.

---

## 7. 범위 가드 (본 검토 밖)

- **eval 시스템**: 검색 품질 e2e 평가는 별도 워크플로우. 본 검토는 머지 직전/직후의 pairwise precision/recall 만.
- **LLM body extractor 자체 동작**: R1 F-CG2-* 항목들 (호출 단위 전환, vocab alias 등) 은 별도 라운드 의제.
- **graph_vocabulary**: relation_type / entity_type 어휘 alias 는 별도 작업 (R1 F-CG2-08).
- **검색 단계 (graph_search_planner, ranker)**: 머지 후의 검색 측 expansion / re-ranking 은 본 검토 밖.
- **non-confluence 출처**: git_code 그래프와의 cross-source 머지는 별도 검토. 본 검토는 confluence_mcp 한정 가설로 진행 (단 D 의 정규화 함수는 source-agnostic 하게 설계).

---

## 8. 부록 — 후보 D 정규화 사례 표

(설계 검증용. 구현하지 않음.)

| 입력 1 | 입력 2 | NFKC | lower | trim/squeeze ws | strip dash/underscore | **D 키 일치?** | 사례 |
|--------|--------|------|-------|-----------------|----------------------|----------------|------|
| `Payment Service` | `payment service` | 동일 | 동일 | 동일 | 동일 | YES | 7 |
| `결제 시스템` | `결제시스템` | 동일 | 동일 | (공백 squeeze 만) → 다름 | (공백 strip 후) → 동일 | YES (옵션 4 채택 시) | 2 |
| `결제 시스템` | `결제-시스템` | 동일 | 동일 | (구분자 차이) | (dash strip 후) → 동일 | YES | 2 |
| `PG` | `Payment Gateway` | 동일 | `pg` vs `payment gateway` | 다름 | 다름 | **NO** | 4 (D 만으로 불가) |
| `결제 서비스` | `Payment Service` | 다름 | 다름 | 다름 | 다름 | **NO** | 1 (D 만으로 불가) |
| `결제 시스템` | `결제 시스템(v2)` | 동일 | 동일 | 다름 (괄호 포함) | 다름 (D 의 괄호 제거 미채택) | NO | 3 (의도) |
| `API` (도메인 A) | `API` (도메인 B) | 동일 | 동일 | 동일 | 동일 | YES (잘못된 머지!) | 5 (D 의 한계 — entity_type 같으면 분리 불가) |
| `page:12345` | `결제 시스템` | 다름 | 다름 | 다름 | 다름 | NO | 6 (D 만으로 불가) |

→ 사례 1, 4, 5, 6 은 D 만으로 미해결. E 도입 시 해결 가능성.

---

## 9. 핵심 인용 (코드/문서 라인 참조)

- `src/context_loop/storage/metadata_store.py:47-53` — `graph_nodes` 스키마.
- `src/context_loop/storage/metadata_store.py:447-460` — `find_graph_node_by_entity` 정확 매칭.
- `src/context_loop/storage/graph_store.py:160-214` — `save_graph_data` 머지 흐름.
- `src/context_loop/storage/graph_store.py:674-726` — 기존 entity 임베딩 (검색 쿼리용, 머지용 아님).
- `src/context_loop/processor/link_graph_builder.py:123-136` — `page:{id}` 폴백 표기.
- `_workspace/indexing-improvement/03_confluence_graph_findings.md` F-CG-10, F-CG2-06, F-CG2-09 — 본 검토 진입의 컨텍스트.

---

## 10. 한 줄 결정 권고

**R2 단기는 D (룰 기반 정규화) + 머지 로그 도입만 채택한다. A/E 는 별도 PoC 라운드에서 golden set 기반 precision/recall 측정 후 결정한다. B 단독·F 단독·D 의 괄호 제거는 본 라운드에서 채택하지 않는다.**
