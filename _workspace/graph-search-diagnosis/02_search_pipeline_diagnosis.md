# Search Pipeline 분석 (R3 Phase A)

## R2 이전 `graph_search_planner.py` 의 LLM 활용

```
사용자 질의
   │
   ▼
plan_graph_search (LLM 호출)
   │ 입력: query + 그래프 스키마 요약 (entity 이름 목록 + relation 통계)
   │ 출력: GraphSearchPlan {should_search, reasoning, search_steps: [{entity_name, depth, focus_relations}]}
   ▼
execute_graph_search (코드 실행)
   │ for each step: get_neighbors(entity_name, depth) — depth 1~2, focus_relations 로 필터
   ▼
retrieved_graph_entities = [GraphEntityRef(name, type, description)]
```

## R2 까지의 fix 누적

1. **R1 임베딩 fallback** (F-SRCH-01~06): get_neighbors 가 surface 매칭 실패 시 임베딩 cosine
2. **R2 양방향 traversal** (F-SRCH-R2-01): get_neighbors 가 successor + predecessor
3. **R2 always-on 시드 보강** (F-SRCH-R2-03): query_embedding 으로 top-3 union
4. **R2 관계 요약 description** (F-METRIC-R2-01): retrieved description fallback 강화

## R2 까지 여전한 한계 — 사용자 보고 "메트릭 크게 변화 없음"

핵심 funnel 손실은 **LLM 의 mental model 이 검색-시점에 인덱싱-시점과 다름**:

| 측면 | 인덱싱 LLM | 검색 LLM (R2 까지) |
|------|----------|------------------|
| 출력 schema | `{entities, relations:[{source, target, type}]}` | `{search_steps:[{entity_name, depth, focus_relations}]}` |
| 방향성 표현 | 자연스럽게 `source → target` | 시드 + 깊이 + 관계 필터 (방향 직접 표현 불가) |
| 관계 추출 | 직접 식별 | 시드의 이웃 traversal 로 간접 발견 |
| 어휘 | graph_vocabulary 어휘 | graph_vocabulary 어휘 (이건 같음) |

→ 검색 LLM 은 "어디서 시작할까" 만 답하지, "정답이 될 entity/relation 은 무엇인가" 를 직접 답하지 못함.

## 구체적 funnel 손실 패턴

질문: "Order Service 가 의존하는 결제 게이트웨이는?"

**R2 흐름**:
- LLM: `search_steps=[{entity_name: "Order Service", depth: 1, focus_relations: ["depends_on"]}]`
- execute: get_neighbors("Order Service") → Order Service + KakaoPay + Toss PG사 + Payment Service + MySQL DB + Kafka (양방향)
- retrieved = [Order Service, KakaoPay, ...]
- gold = relevant_graph_entities=[Order Service]  ← rank 어딘가에 있긴 있음

**문제**:
- retrieved 순서가 BFS 발견 순서 — Order Service 가 rank-N 일 수 있음
- gold 가 관계 채점 (`relevant_graph_relations=[{source: "Order Service", target: "KakaoPay", type: "depends_on"}]`) 을 켜면 검색 측이 이걸 직접 식별 못함

**R3 정렬된 흐름**:
- LLM: `target_entities=[{name: "Order Service", type: "service"}, {name: "KakaoPay", type: "team"}]`,
       `target_relations=[{source: "Order Service", target: "KakaoPay", relation_type: "depends_on"}]`
- execute: target_entities + target_relations 끝점을 **priority** 시드로 처리 → retrieved 앞순위 보장
- retrieved = [Order Service (rank-1), KakaoPay (rank-2), ...]
- gold = [Order Service] → **rank-1 hit** → MRR=1.0

## 결론

R3 은 검색 LLM 의 schema 를 인덱싱과 정렬:
- 출력 shape 통일 (target_entities + target_relations)
- 방향성 직접 표현
- 같은 vocabulary
- + retrieved priority ordering (target_* 가 rank 앞순위) → MRR/NDCG 회복
