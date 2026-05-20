# Graph Search Pipeline Diagnosis (R2)

> R1은 정적 분석 — funnel 손실의 가설을 7개 항목으로 머지함. R2는 **실제 그래프 데이터 + 시뮬레이션**으로 실제 손실 지점을 정량 입증.

## R1 fix 적용 확인 — 코드에 실제 들어갔는가

| R1 항목 | 위치 | 확인 |
|---------|------|------|
| F-SRCH-01 get_neighbors 임베딩 fallback | `graph_store.py:361-371` | ✓ 적용 (parameter `embedding_fallback`) |
| F-SRCH-02 threshold 0.7→0.5 | `graph_store.py:667` (`def search_entities_by_embedding(threshold: float = 0.5)`) | ✓ |
| F-SRCH-03 execute_graph_search query_embedding 시드 | `graph_search_planner.py:286-309` | ✓ |
| F-SRCH-04 system prompt 강화 | `graph_search_planner.py:46-52` | ✓ |
| F-SRCH-06 retrieved description fallback | `graph_search_planner.py:386-393` | ✓ |
| F-METRIC-01 threshold 0.65 | `graph_match.py:33` | ✓ |
| F-METRIC-02 golden name fallback | `graph_match.py:310` (`g_text = golden.description or golden.name or ""`) | ✓ |

→ **R1 코드는 정확히 적용됨**. 그러나 메트릭이 여전히 < 10% → R1으로 잡히지 않는 **다른 funnel 손실 지점**이 있다.

## F-SRCH-R2-01 (CRITICAL): get_neighbors 방향성 — `single_source_shortest_path_length(DiGraph)` 가 outgoing만 따름

### 코드 위치

`src/context_loop/storage/graph_store.py:377-380`:
```python
for center in center_nodes:
    reachable = nx.single_source_shortest_path_length(
        self._graph, center, cutoff=depth
    )
```

- `self._graph` 는 `nx.DiGraph`
- `single_source_shortest_path_length(DiGraph, source)` 는 **successors만 (outgoing edges)** 따라감
- depth=1 이면 source 자신 + outgoing 1-hop 이웃만 반환

### 실측 영향 (스크립트 시뮬레이션)

| 시나리오 | retrieved 에 gold-seed 포함? |
|----------|---------------------------|
| LLM이 gold-seed 정확히 선택 (예: "Payment Service" → "Payment Service") | OK (당연) |
| LLM이 gold-seed의 이웃 entity를 선택 (질의에 그 이웃 이름이 명시되어 그것에 끌릴 때) | **directed 88.9% miss / undirected 0% miss** |

```
시드 후보(gold-eligible) 6개 × 평균 3개 이웃 = 18개 (gold-seed, neighbor) 페어
  - directed (현재):    2 hit / 16 miss (88.9% miss)
  - undirected (제안):  18 hit / 0 miss
```

### 왜 R1 임베딩 fallback 이 못 잡나

- R1 fallback은 `get_neighbors` 에서 **표면(exact/scoped/short) 매칭 모두 실패** 시에만 발동 (`graph_store.py:365`).
- LLM이 sink 이웃 (예: "KakaoPay", "Elasticsearch") 을 search_step.entity_name 으로 답하면 **표면 매칭 성공** → 임베딩 fallback skip.
- center_nodes = [sink_id], outgoing 0 → 결과 = {sink_id 자신}만 반환.
- retrieved = [sink], gold = seed (다른 노드) → T1/T2/T3 모두 miss. T4 임베딩도 description 비특이라 cosine ≥ 0.65 미달.

### 시나리오 예시

- gold subgraph: **Search Service** (seed) → uses → Elasticsearch
- gold question (LLM이 생성): "Elasticsearch를 사용하는 서비스는?"
- gold relevant_graph_entities = [{name: "Search Service", type: "service"}]
- eval LLM (plan_graph_search) 가 schema 에서 "Elasticsearch" 를 보고 search_step.entity_name="Elasticsearch" 답함 (자연스러움 — 질의에 명시)
- execute_graph_search → get_neighbors("Elasticsearch", depth=1) → DiGraph 에서 Elasticsearch의 outgoing 0 → **retrieved=[Elasticsearch]**
- run_entity_matching: T1 "Search Service" vs ["Elasticsearch"] → miss
- 결과: graph_hit = 0

### MRR/NDCG = 0.065 의 해석

- 0.065 ≈ 1/15. 즉 평균 매칭 순위가 ~15위 (top_k 안에 거의 안 들어옴).
- "전혀 매칭 안 됨"이 아니라 가끔 매칭됨 — LLM이 운 좋게 seed를 직접 답한 ~6.5% 케이스만 hit.
- 정확히 본 시뮬레이션의 directed 88.9% miss 비율과 정합.

## F-SRCH-R2-02 (HIGH): plan_graph_search의 LLM 시드 선택이 sink 이웃에 끌림

### 증거

- 시스템 프롬프트 (graph_search_planner.py:30-78) 는 "schema 에 있는 entity 이름만 쓰라" 고 강제하지만, **어느 entity 를 선택할지의 판단**은 LLM 에게 일임.
- 골드 질의는 자연 한국어로 풀어쓴 표현 — sink 이웃이 명시된 keyword 로 더 두드러질 수 있음 (예: "KakaoPay 와 협력하는 결제 서비스는?" → LLM이 "KakaoPay" 를 더 두드러진 키워드로 인식).
- search_steps 가 최대 3개 (graph_search_planner.py:198) — 보통 1~2개만 활용.

### 부분 완화

- **focus_relations**: 시스템 프롬프트에 intent → relation 매핑 가이드 (intent_mapping). 이 가이드가 풍부하면 LLM이 의도를 잘 파악하지만, 시드 entity 선택 자체에는 직접 영향 약함.

## F-SRCH-R2-03 (MEDIUM): execute_graph_search 의 retrieved 가 seed 누락 가능

execute_graph_search 가 search_steps 의 entity_name 만 사용 — gold-seed 가 들어 있다고 보장 없음. R1 의 query_embedding fallback 은 **all_nodes 가 0개일 때** 만 발동 (graph_search_planner.py:289):

```python
if not all_nodes and query_embedding is not None:
    similar = graph_store.search_entities_by_embedding(...)
```

→ search_step 이 일부 성공해도 (sink 만 잡혀도) 0이 아니므로 fallback skip → gold-seed 누락 그대로.

## F-SRCH-R2-04 (LOW): retrieved 자연어 description fallback 의 비특이성

- R1에서 description 빈 경우 `"이 entity 는 {type} 유형의 '{name}' 이며 그래프 노드로 등록되어 있다."` 로 채움 (graph_search_planner.py:388-393).
- gold 측은 LLM 합성 description (`evidence_description`). 두 description 은 의미 임베딩 cosine 이 비특이 (둘 다 metadata-스러운 문장).
- T4 매칭이 0.65 임계값을 못 넘김 → T4 미발동.
- 영향은 F-SRCH-R2-01 대비 작지만, **R1 fix 가 T4 회복이라기보다 noise** 의 가능성 — 운영 데이터 검증 필요.

## 검색 funnel 재추정 (R1 이후 — 실제 손실 분포)

```
쿼리 (graph 모드 gold)
  │
  ├─ Stage 1: query embedding (작동 정상)
  │
  ├─ Stage 2: plan_graph_search
  │     ├─ schema_text ✓ (21 entity 모두 LLM 에 노출)
  │     └─ LLM이 search_step.entity_name 답함
  │           ├─ gold-seed 정확 (안: 6 service 중 하나) ~ 30%
  │           └─ gold-seed의 이웃 (sink) ~ 70%  ← LLM이 질의의 명시 keyword 에 끌림
  │
  ├─ Stage 3: execute_graph_search → get_neighbors
  │     ├─ gold-seed 선택 시: retrieved={seed + outgoing neighbors} ✓
  │     └─ sink 선택 시: retrieved={sink만} ✗ (88.9% miss)  ← Critical funnel 손실
  │
  └─ Stage 4: T1~T4 매칭
        - T1 (exact): retrieved 가 seed 누락이면 miss
        - T4 (embedding): description 자연어 fallback 의 cosine 임계값 미달
```

**예상 손실 분포 R1 이후**:
- LLM 시드 선택 오차로 인한 직접 miss: ~70% × 88.9% ≈ 62%
- gold-seed 정확 선택 후의 retrieve 정상: ~30%
- T4 recovery: ~5% (description 임베딩 비특이로 약함)
- → graph_recall 약 35% 예상이지만 실측 < 10% → 추가 손실 (LLM의 retrieved 매칭 실패 + 짧은 description fallback) ~ 25% 추가 손실

## entity_embeddings 캐시 흐름 — 평가 컨텍스트 영향 적음

- eval_search.py:880-887 에서 동시 평가 시작 **전에** `build_entity_embeddings` 1회 호출 → race 회피.
- 평가 컨텍스트에서 캐시 휘발성은 funnel 손실의 원인 아님.
- 운영 MCP 컨텍스트에서는 R3 후보로 보존.

## 권고 (검색측)

| ID | 권고 | 효과 추정 | 우선 |
|----|------|----------|------|
| **F-SRCH-R2-01** | get_neighbors 양방향 traversal | recall ~ +50%p | **Critical** |
| F-SRCH-R2-03 | execute_graph_search 시드 보강 always-on (top_k similar 항상 union) | recall ~ +10%p (보완) | High |
| F-SRCH-R2-02 | LLM 프롬프트에 "질의 주체 entity 선택" 가이드 강화 | recall ~ +5%p (보완) | Medium |
| F-SRCH-R2-04 | description 자연어 fallback 의 더 의미적인 문구 | T4 score 약간 ↑ | Low |
