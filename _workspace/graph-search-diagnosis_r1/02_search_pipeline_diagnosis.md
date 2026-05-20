# Graph Search Pipeline Diagnosis

> 정적 코드 분석 기준 진단. `graph_hit/precision/recall<10%`, MRR/NDCG=0.065의 핵심 원인은 검색 측 funnel에서 발견됨.

## 검색 funnel (정적 추정)

```
쿼리
  │
  ├─ [Stage 1] _embed_query (단순 호출, 실패 시 None)
  │
  ├─ [Stage 2] plan_graph_search
  │     │
  │     ├─ if entity_embeddings 비어있음 → format_schema_for_llm (전체 dump)
  │     ├─ if entity_embeddings 있음     → format_query_relevant_schema_for_llm
  │     │     └─ search_entities_by_embedding(threshold=0.5, top_k=10)
  │     │           ↑ 임베딩 캐시 누락 시 빈 결과 (F-IDX-02 영향)
  │     ↓
  │     LLM 응답 (search_steps with entity_name)  ← 손실 큰 단계
  │
  ├─ [Stage 3] execute_graph_search
  │     │
  │     └─ for step: get_neighbors(step.entity_name)
  │           ├─ exact lower-case 매칭
  │           ├─ scoped name 매칭 (FQN)
  │           ├─ short name 매칭
  │           └─ MISS — 빈 리스트 ← 가장 큰 손실 지점 (Critical)
  │
  └─ [Stage 4] retrieved_graph_entities=[] → graph_recall@k = 0
```

## 검색 측 핵심 발견

### F-SRCH-01 (CRITICAL): `get_neighbors`에 임베딩 fallback 부재 → LLM 추측 이름이 인덱스에 없으면 전량 손실

- **위치**: `src/context_loop/storage/graph_store.py:304-362` + `processor/graph_search_planner.py:216-249`
- **현재 동작**:
  ```python
  # graph_store.py get_neighbors
  query_lower = entity_name.lower()
  center_nodes = [n for n, d in self._graph.nodes(data=True)
                  if d.get("entity_name", "").lower() == query_lower]
  if not center_nodes:
      # scoped 매칭
      ...
  if not center_nodes:
      # short name 매칭
      ...
  if not center_nodes:
      return []
  ```
  → exact / scoped / short 모두 실패하면 빈 리스트
- **문제**:
  - LLM이 schema를 보고 entity_name을 답하지만, 작은 표기 차이(공백, 케이스, 하이픈/언더스코어)로 매칭 실패
  - 예: schema에 `"Auth Service"` 가 있지만 LLM이 `"Auth-Service"` 또는 `"AuthService"`로 답하면 매칭 0
  - 한국어 도메인에서 더 심함: `"인증 서비스"` ↔ `"인증서비스"`
- **이게 핵심 원인일 가능성**: execute_graph_search 코드(`if not all_nodes: return None`, line 248-249)와 결합되어 모든 search_step이 0개 노드 반환하면 `assembled.retrieved_graph_entities = []` → `run_entity_matching([], ...)` → 메트릭 0
- **개선 방향**:
  - `get_neighbors`에서 exact/scoped/short 모두 실패 시 임베딩 fallback:
    - entity_name 임베딩 → `search_entities_by_embedding(top_k=3, threshold=0.5)` → 시드 노드
  - 또는 별도 `get_neighbors_with_fallback` 분리
  - 추가로 정규화 강화: NFKC + 공백/하이픈/언더스코어 제거 후 비교 (graph_match._normalize와 같은 형태)
- **심각도**: Critical | **공수**: S (~M)

### F-SRCH-02 (HIGH): `search_entities_by_embedding` 기본 임계값 0.7이 매우 보수적

- **위치**: `src/context_loop/storage/graph_store.py:619`
- **현재 동작**: `threshold: float = 0.7` 기본값
- **문제**: cosine 0.7은 임베딩 공간에서 매우 가까운 의미. 실제 검색 시드 매칭에서 노드 표기와 쿼리(또는 LLM 추측 이름)는 약간만 다를 뿐인데 0.7을 넘기기 어려움. context_assembler.py:263에서 자동 호출 시 `get_query_relevant_schema(similarity_threshold=0.5)`를 거치므로 그 호출은 0.5로 override 됨 — 그러나 default가 보수적이라 다른 호출자(만약 있다면)나 직접 호출에서 문제. 또한 0.5 자체도 의미 매칭에 다소 보수적임.
- **개선 방향**:
  - default `threshold=0.5`로 낮춤
  - 또는 호출 측이 `top_k` 기준으로 fallback (threshold 이하라도 top-k는 반환)
- **심각도**: High | **공수**: S

### F-SRCH-03 (HIGH): `execute_graph_search`에서 시드 노드 0개면 즉시 None 반환

- **위치**: `src/context_loop/processor/graph_search_planner.py:236-249`
- **현재 동작**:
  ```python
  for step in plan.search_steps:
      neighbors = graph_store.get_neighbors(step.entity_name, depth=step.depth)
      if not neighbors:
          continue
      ...
  if not all_nodes:
      return None
  ```
- **문제**: LLM 추측 이름이 모두 인덱스에 없으면 retrieved=빈 리스트. fallback 없음. F-SRCH-01과 결합하여 메트릭 0의 직접 원인.
- **개선 방향**:
  - search_steps이 모두 실패하면 query embedding으로 `search_entities_by_embedding(top_k=5)` fallback → 가장 가까운 노드들로 시드 보강
  - 또는 query 전체를 임베딩으로 시드 노드를 항상 보충
- **심각도**: High | **공수**: S

### F-SRCH-04 (MEDIUM): `format_query_relevant_schema`의 LLM 가이드 텍스트가 표기 변형을 강제 안 함

- **위치**: `src/context_loop/processor/graph_search_planner.py:30-73` (system prompt)
- **현재 동작**: 시스템 프롬프트가 "실제 존재하는 엔티티 이름만 사용" 지시. 그러나 schema_text에 나열된 이름과 다른 이름을 LLM이 추측하면 실패.
- **문제**: LLM이 한글/영어 변형을 답할 위험
- **개선 방향**:
  - 프롬프트에 "schema에 나열된 이름과 **글자 단위로 정확히** 일치해야 합니다" 명시
  - schema_text를 코드 블록 안에 ` 이름 ` 형태로 감싸 LLM이 정확히 복사하도록 유도
- **심각도**: Medium | **공수**: S

### F-SRCH-05 (MEDIUM): schema_text의 entity_type별 max 10개가 작음

- **위치**: `src/context_loop/storage/graph_store.py:394, 414` (max_entities_per_type=10 기본)
- **현재 동작**: get_schema_summary가 type별 10개 노드 sample만 LLM에 전달
- **문제**: 인덱스의 핵심 노드가 type별로 100+개이면 LLM이 보는 schema는 무작위 10개 → 골드 정답 노드가 schema에 없을 가능성
- **개선 방향**:
  - query-relevant schema 경로(F-IDX-02 해결 후)에서는 seed entity 중심 → 노드가 좁혀짐 → 더 많이 보여줘도 됨
  - max를 20~30으로 늘림 (LLM 컨텍스트 여유 있음)
- **심각도**: Medium | **공수**: S

### F-SRCH-06 (MEDIUM): retrieved_graph_entities의 description이 검색 측에서 채워지지만 빈 경우 다수

- **위치**: `src/context_loop/processor/graph_search_planner.py:317-325`
- **현재 동작**:
  ```python
  node_props = node.get("properties") or {}
  description = ""
  if isinstance(node_props, dict):
      description = str(node_props.get("description") or "")
  entities.append(GraphEntityRef(name=name, type=etype, description=description))
  ```
- **문제**: 노드의 description은 추출 시점에만 채워짐 (body_extractor는 보통 비움, link_graph_builder는 일부만). 빈 description → T4 embedding 매칭에서 `r_text = name` fallback (graph_match.py:311) → 짧은 이름은 비특이적 임베딩
- **개선 방향**:
  - description 비어있을 때 entity_name + entity_type을 자연어 문장으로 묶기 (예: `"이 entity는 system 유형의 'Auth Service'입니다"`) — T4 매칭의 임베딩 신호 강화
  - 또는 retrieved에 properties도 포함시켜 골드와 추가 비교
- **심각도**: Medium | **공수**: S

## 매개변수 권고

| 매개변수 | 현재 기본 | 권고 |
|----------|----------|------|
| `search_entities_by_embedding.threshold` | 0.7 | **0.5** |
| `get_query_relevant_schema.top_k` | 10 | 20 |
| `format_query_relevant_schema.max_sample_relations` | 15 | 25 |
| `get_query_relevant_schema.neighbor_depth` | 1 | 2 (그러나 노이즈 증가) |
| `get_schema_summary.max_entities_per_type` | 10 | 20 |

## 검토하지 않은 영역

- 실제 운영 schema_text의 LLM 응답 — 정확히 어떤 entity_name을 답하는지
- entity_embeddings 모델의 의미 임베딩 품질 (한국어/영어 혼합)
- get_neighbors의 depth=2가 검색 정밀도에 어떤 영향
