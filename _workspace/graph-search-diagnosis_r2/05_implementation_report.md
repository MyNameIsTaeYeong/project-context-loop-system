# Implementation Report — Round 2

## 한 줄 결론

R1 의 fallback 들이 발동하지 못하던 **directed traversal (sink 노드에서 outgoing 0)** funnel 손실을 제거. 양방향 BFS + always-on 시드 보강 + 관계 요약 description 으로 검색 측 funnel 가장 큰 손실 지점 회복.

## 핵심 변경

### F-SRCH-R2-01 (Critical) — `get_neighbors` 양방향 traversal

`src/context_loop/storage/graph_store.py`

- 신규 헬퍼 `_bidirectional_bfs(sources, depth)`: successor + predecessor 합집합 BFS. depth 단계마다 양방향 확장.
- `get_neighbors`: 기존 `nx.single_source_shortest_path_length` (outgoing only) → `_bidirectional_bfs` 사용.
- `get_neighbors_from_node_id`: 동일 변경 (R1 도입 헬퍼도 양방향).

근거: 시뮬레이션 결과 18개 (gold-seed, neighbor) 페어 중 directed 88.9% miss vs undirected 0% miss — 실제 인덱스 데이터 기반 검증.

### F-SRCH-R2-03 (High) — `execute_graph_search` always-on 시드 보강

`src/context_loop/processor/graph_search_planner.py`

- R1: `if not all_nodes:` 조건에서만 query_embedding fallback 발동 → 시드 일부 성공 시 (LLM 이 sink 답함) 보강 skip.
- R2: `query_embedding` 이 있으면 항상 top-3 (threshold 0.6) 시드 union 보강. all_nodes 가 일부 차있어도 의미적으로 더 가까운 seed 가 누락되지 않도록.
- 전체 step 실패 시의 폴백(threshold 0.5, top-5)은 보조 폴백으로 유지.

### F-METRIC-R2-01 (Medium) — retrieved description 관계 요약 fallback

`src/context_loop/processor/graph_search_planner.py`

- R1: description 빈 경우 `"이 entity 는 {type} 유형의 '{name}' 이며 그래프 노드로 등록되어 있다."` (metadata 보일러플레이트, T4 임베딩이 비특이).
- R2: 노드의 1-hop 관계를 자연어 문장으로 풀어쓰기 → `"{etype} 유형의 '{name}' 은(는) {N} 에 대해 {rel}, {M} 가(이) {rel2} 관계를 가진다."`. 관계 없으면 R1 fallback 유지.
- T4 임베딩이 gold 의 evidence_description 과 더 의미적으로 비교 가능.

## 변경 파일 통계

| 파일 | 변경 라인 |
|------|----------|
| `src/context_loop/storage/graph_store.py` | +35 / -10 |
| `src/context_loop/processor/graph_search_planner.py` | +75 / -15 |
| `tests/test_storage/test_graph_store.py` | +52 / 0 |
| `tests/test_processor/test_graph_search_planner.py` | +72 / 0 |

## 계획-구현 매트릭스

| ID | 계획 | 실제 | 일치 |
|----|------|------|------|
| F-SRCH-R2-01 | get_neighbors 양방향 + get_neighbors_from_node_id 양방향 | ✓ + 공통 헬퍼 `_bidirectional_bfs` (보너스) | ✓+ |
| F-SRCH-R2-03 | always-on 시드 보강 (threshold 0.6, top_k 3) | ✓ | ✓ |
| F-METRIC-R2-01 | retrieved description 관계 요약 fallback | ✓ + 양방향(out+in) 요약 | ✓+ |

불일치/누락: 0건.

## 신규 테스트

| 테스트 | 검증 의도 |
|--------|----------|
| `test_get_neighbors_follows_both_directions` | sink 노드 시드에서 incoming neighbor 도 반환 (방향성 회복 직접 검증) |
| `test_get_neighbors_from_node_id_bidirectional` | node_id 직접 시드 경로도 양방향 |
| `test_execute_search_seeds_augment_always_on` | search_step 일부 성공해도 query_embedding 으로 추가 시드 union |
| `test_execute_search_description_uses_relation_summary` | description 빈 노드에 대해 관계 요약 fallback 적용 |

## 회귀 위험 점검

| 변경 | 회귀 위험 | 검증 |
|------|----------|------|
| 양방향 traversal | retrieved 노드 수 ↑ — context_text 길이 ↑, precision 약간 ↓ 가능 | 의미적으로 검색은 양방향이 자연스러움. 실측 21노드 그래프 기준 평균 2~3 → 3~5 노드 (만큼 늘어남) |
| always-on 시드 보강 | LLM 의도 무관 노드 유입 가능 | threshold 0.6 + top_k 3 으로 보수 통제. all_nodes 가 비어있을 때만 noise 우려 큰데 그 경우는 fallback 의도 |
| 관계 요약 description | 보일러플레이트 → 관계 텍스트 → 임베딩 신호 더 강함 | T4 매칭에서 false-positive 가능성은 score threshold (0.65) 가 흡수 |

## 다운스트림 영향

- `get_neighbors` 시그니처 동일 (kwargs 변동 없음) → 호출자 영향 없음
- `get_neighbors_from_node_id` 시그니처 동일
- `execute_graph_search` 시그니처 동일 (R1 의 query_embedding/embedding_client 그대로)
- 관계 요약은 description 빈 경우만 발동 — 정상 description 보유 노드는 R1과 동일 동작

## 운영 권고

R2 적용 후 평가 메트릭 재측정:
```bash
python -m scripts.eval_search --gold-set <path> --label r2-baseline
# 비교: r1 vs r2 graph_recall@k, MRR, NDCG
```

예상 효과:
- graph_recall@10: < 10% → 30~50% (양방향 traversal 의 88.9% miss → ~0% 회복)
- MRR/NDCG: 0.065 → 0.3~0.5 (retrieved 가 seed 포함 시 T1 rank-1 hit)
- precision: 약간 감소 가능 (양방향 + always-on 보강의 retrieved 확장 효과)

검증은 별도 평가 파이프라인 (rag-eval-audit 영역) — 본 라운드는 검색 funnel 의 회복 자체에 한정.

## R3 후보 (다음 세션)

- F-SRCH-R2-02 LLM 시드 선택 가이드 강화 (system prompt)
- F-GOLD-R2-01 gold 후보 양방향 (gold 분포 영향, eval-gold-set-improvement 영역)
- F-IDX-R2-03 entity_embeddings 영속화 + lock
- F-IDX-R2-02 entity_name strip 정제 (indexing-improvement 영역)
