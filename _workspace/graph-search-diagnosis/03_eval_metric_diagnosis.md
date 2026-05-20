# Eval Metric 영향 분석 (R3 Phase A)

## R2 메트릭이 안 오른 원인 가설 (사용자 보고 기반)

> "성능 지표가 크게 변한 게 없습니다."

R2 의 양방향 traversal 은 retrieved 의 **포함성** (gold seed 가 retrieved 안에 들어가는가) 을 회복했지만, **순위** (rank) 는 다루지 않았다.

```python
# graph_match.py:380
hits.sort(key=lambda t: t[0])  # t[0] = retrieved_index (rank)
```

→ MRR / NDCG 가 retrieved 의 rank 에 민감.

## retrieved 순서가 어떻게 결정되었나 (R2 까지)

`execute_graph_search`:
1. search_steps 순회 → 각 step 의 get_neighbors 결과를 dict 발견 순서로 all_nodes 에 push
2. R2 always-on 보강 → 임베딩 top-k 의 1-hop 이웃을 뒤에 append
3. 마지막 fallback → 같음

→ retrieved 순서 = BFS 발견 순서 + boost 순서. **LLM 이 식별한 정답 후보가 어디에 있는지** 가 보장되지 않음.

## 시나리오

- 그래프: Order Service → KakaoPay, Toss PG사, Payment Service, MySQL DB, Kafka (5 outgoing)
- 질의: "Order Service 의 결제 의존성?"
- gold seed: Order Service
- R2 search_steps: `[{entity_name: "Order Service", depth: 1}]`
- get_neighbors("Order Service") → BFS 결과의 dict.values() 순서:
  - DiGraph 내부 dict 순서는 add_edge 순서 / node insertion 순서에 의존
  - Order Service 가 첫 번째일 보장 없음 (양방향이면 incoming 노드가 먼저 올 수도)

→ Order Service 가 rank-3 이면 MRR = 1/3 ≈ 0.33. rank-5 면 0.2. 골드셋 평균이 0.065 → 평균 rank ≈ 15 (top_k=10 이면 매번 거의 outside).

## R3 으로 어떻게 회복되는가

`execute_graph_search` 에 priority_node_ids 도입:
- LLM 의 target_entities / target_relations 끝점 노드 → priority 등록
- 결과 빌드 시 priority 노드를 retrieved 의 앞순위에 배치
- gold = LLM 의 target_entity 중 하나 → rank-1 hit → MRR = 1.0

**예상 효과**:
- graph_hit@10: R2 와 동일 또는 약간 ↑ (포함성은 R2 가 이미 잡음)
- graph_recall@10: 약간 ↑
- **MRR/NDCG: 큰 폭 ↑** (R2 대비 priority ordering 의 직접 효과)

## 메트릭 매칭 흐름 (참고)

`graph_match.run_entity_matching`:
1. T1 exact: `(name.lower, type)` 양쪽 strip+lower 비교
2. T2 alias: golden.aliases vs retrieved.name
3. T3 normalize: NFKC + 구두점 제거
4. T4 embedding: cosine ≥ 0.65 (R2 에서 완화됨)

→ retrieved 가 gold 와 표면 키 동일하면 T1 즉시 hit. R3 의 priority ordering 은 이 hit 의 **rank** 를 앞당김.

## 관계 채점 (relation_recall)

`run_relation_matching`:
- 검색 측의 retrieved_graph_relations 는 `execute_graph_search` 가 `edges = graph_store.get_edges_between(node_ids)` 로 추출한 실제 edge 들
- gold 의 relevant_graph_relations = `{source, target, relation_type}` 형태
- 매칭: `(source.lower, target.lower, type)` 키

R3 의 **target_relations 가 retrieved 의 priority 끝점 노드를 만들어** → edges 가 양 끝점을 모두 포함 → retrieved_graph_relations 에 해당 edge 가 노출 → relation_recall ↑.

## 결론

R3 의 핵심 효과는 두 가지:
1. retrieved 의 **포함성** 은 R2 가 잡음
2. R3 는 retrieved 의 **순위** 를 잡음 — MRR/NDCG 회복의 직접 동인
3. + 관계 채점에도 양 끝점 priority 가 효과
