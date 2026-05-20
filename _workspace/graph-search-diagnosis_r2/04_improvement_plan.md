# Graph Search Improvement Plan (R2)

## funnel 손실 재진단

| 단계 | R1 후 추정 손실률 | 주된 원인 |
|------|----------------|----------|
| Stage 1: 쿼리 임베딩 | 거의 0 | 안정 |
| Stage 2: plan_graph_search (LLM seed 선택) | 60~70% | LLM이 질의의 명시 keyword (sink 이웃)를 선택 — F-SRCH-R2-02 |
| **Stage 3: get_neighbors (directed-only)** | **88.9%** (sink 시드 시) | **F-SRCH-R2-01 (single_source_shortest_path_length=outgoing)** ← 메인 |
| Stage 4: T1 매칭 | retrieved 가 seed 누락 시 자동 0 | 위 결과 |
| Stage 5: T4 매칭 | 자연어 fallback의 비특이성 | F-METRIC-R2-01 (보조) |

핵심 가설: **directed traversal 이 sink 시드 케이스에서 88.9% 정보 손실** — 이게 < 10% 메트릭의 결정적 원인. R1 의 임베딩 fallback 은 surface 매칭 실패 시에만 발동되므로 sink 표면 매칭이 성공하면 skip 되어 누수.

## 우선순위 매트릭스

| ID | 출처 | 영역 | 영향 | 공수 | ROI | 라운드 |
|----|------|------|------|------|-----|--------|
| **F-SRCH-R2-01** | 02 | get_neighbors 양방향 | **Critical** | S | ★★★★★ | **R2** |
| **F-SRCH-R2-03** | 02 | execute_graph_search 시드 보강 always-on | High | S | ★★★★ | **R2** |
| **F-METRIC-R2-01** | 03 | retrieved description fallback 관계 요약 강화 | Medium | S | ★★★ | **R2** |
| F-SRCH-R2-02 | 02 | LLM 시드 선택 가이드 (질의 주체 선택) | Medium | S | ★★ | R3 |
| F-GOLD-R2-01 | 03 | load_candidate_subgraphs 양방향 | Medium | S | ★★ | R3 (gold 분포 영향) |
| F-IDX-R2-02 | 01 | entity_name strip 정제 | Low | S | ★ | indexing-improvement |
| F-IDX-R2-03 | 01 | entity_embeddings 영속화 | Medium | M | ★★ | R3 |

## 라운드 2: 양방향 traversal + always-on 시드 보강 + 관계 요약 description

### 포함 항목 (R2)

1. **F-SRCH-R2-01 (Critical)**: `get_neighbors` 가 양방향 1-hop 이웃을 따르도록 변경.
   - `single_source_shortest_path_length(G, source, cutoff=depth)` 대신 (직접) outgoing + incoming 합집합 BFS.
   - depth>1 이면 매 단계 양방향 — 폭증 위험은 depth ≤ 2 제한으로 통제.
   - `get_neighbors_from_node_id` 도 동일 변경.

2. **F-SRCH-R2-03 (High)**: `execute_graph_search` 가 search_steps 실행 후, 결과가 비지 않아도 `query_embedding` 기반 top-k 시드를 **추가** 합집합 (현재는 0개일 때만 fallback).
   - 단, 임계값 0.6 로 약간 보수 — noise 컨트롤.
   - 시드 노드 자체 + 1-hop 양방향 이웃 union.

3. **F-METRIC-R2-01 (Medium)**: retrieved 의 description fallback 이 빈 경우, **1-hop 관계 요약** 으로 채움.
   - 예: `"이 entity 는 X 에 depends_on, Y 를 uses 한다."` (실제 관계 기반)
   - T4 임베딩이 더 의미적 — 자연어 evidence (gold) 와 cosine 비교 시 더 분별력.

### 변경 파일

| 파일 | 변경 사유 |
|------|----------|
| `src/context_loop/storage/graph_store.py` | F-SRCH-R2-01 (get_neighbors 양방향), get_neighbors_from_node_id 양방향 |
| `src/context_loop/processor/graph_search_planner.py` | F-SRCH-R2-03 (always-on 시드 보강), F-METRIC-R2-01 (관계 요약 description) |

### 구현 순서

1. **F-SRCH-R2-01**: graph_store.py `get_neighbors` / `get_neighbors_from_node_id` — BFS 헬퍼 추가. 양방향 successors + predecessors 합집합 traversal.
2. **F-METRIC-R2-01**: graph_search_planner.py `execute_graph_search` 의 description fallback 분기 — `graph_store.get_edges_between` 호출하여 노드의 1-hop 관계를 자연어로 풀어쓰기. 빈 경우 R1 fallback 유지.
3. **F-SRCH-R2-03**: graph_search_planner.py `execute_graph_search` 끝부분 — search_steps 실행 후 query_embedding 있고 결과 ≥ 1 인 경우에도 top-k 보강. threshold=0.6, top_k=3.

### 회귀 위험

| 변경 | 잠재 회귀 |
|------|----------|
| 양방향 traversal | 노이즈 ↑ — retrieved 노드 수 ↑ → context_text 길이 증가 가능. depth=1 에서 평균 2~3 노드 → 양방향 시 3~5 노드 정도 (실측 17/21 노드). 운영 LLM context 한도(32k tokens) 대비 안전. |
| always-on 시드 보강 | LLM 의도 무관 시드가 retrieved 에 섞일 수 있음 — 임계값 0.6 + top_k 3 으로 제어. recall ↑, precision 약간 ↓ 트레이드오프. |
| 관계 요약 description | retrieved description 의 텍스트 길이 ↑ — T4 임베딩 비용 약간 ↑ (cache 흡수). 그러나 의미적 신호 더 강함. |

### 필요한 신규 테스트

- `tests/test_storage/test_graph_store.py::test_get_neighbors_follows_both_directions` — sink 노드에서 incoming 이웃도 반환되는지
- `tests/test_storage/test_graph_store.py::test_get_neighbors_from_node_id_bidirectional` — node_id seed 도 양방향
- `tests/test_processor/test_graph_search_planner.py::test_execute_seeds_augment_always_on` — search_steps 일부 성공해도 query_embedding 시드 추가
- `tests/test_processor/test_graph_search_planner.py::test_retrieved_description_uses_relation_summary` — 관계 요약 description fallback

### 기존 테스트 영향

- `test_get_neighbors_short_name_fallback` 등은 양방향이 super-set 이므로 표면 매칭 케이스는 변경 없음 (그대로 통과).
- `test_get_neighbors_falls_back_to_embedding_when_name_unknown` (R1 신규) — surface miss 시 임베딩 fallback 으로 center_nodes 찾고 그로부터 양방향 BFS. R1 의도 보존.
- `test_get_neighbors_exact_match_wins_over_short_name` — exact 매칭 우선, 변경 없음.

## 검증 체크리스트

R2 구현 후:
- [ ] `pytest tests/test_storage/test_graph_store.py -x -q`
- [ ] `pytest tests/test_processor/test_graph_search_planner.py -x -q`
- [ ] `pytest tests/test_mcp/test_context_assembler.py -x -q`
- [ ] `pytest tests/ --ignore=tests/test_eval` (전체 회귀)
- [ ] ruff check (touched files)

## 범위 외

- `eval_search.py` / `build_synthetic_gold_set.py` 변경 → rag-eval-audit 영역
- gold 후보 양방향 (F-GOLD-R2-01) → eval-gold-set-improvement 영역 (gold 분포 영향)
- 본 라운드는 **검색 funnel 의 directional 손실 회복**이 핵심

## 라운드 3 (다음 세션)

- F-SRCH-R2-02 LLM 시드 선택 프롬프트 강화
- F-GOLD-R2-01 gold 후보 양방향
- F-IDX-R2-03 entity_embeddings 영속화
