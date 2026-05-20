# R3 Implementation Report — 인덱싱-검색 Schema 정렬

## 한 줄 결론

검색 LLM 의 출력 schema 를 인덱싱과 정렬 (`{target_entities, target_relations}`) + retrieved 의 rank-1 priority ordering 으로, R2 까지의 fallback 누적 후에도 변화 없던 MRR/NDCG 회복을 노린다.

## 핵심 변경

### F-LLM-R3-01: GraphSearchPlan schema 정렬

`src/context_loop/processor/graph_search_planner.py`

- 신규 `TargetEntity {name, entity_type}` — 인덱싱 `Entity {name, entity_type, description}` 와 같은 shape
- 신규 `TargetRelation {source, target, relation_type}` — 인덱싱 `Relation {source, target, relation_type, label}` 와 같은 shape
- `GraphSearchPlan` 에 `target_entities` + `target_relations` 추가
- `search_steps` 는 후방 호환을 위해 유지 (legacy LLM 응답 / 직접 호출자)
- `has_targets` property 로 R3 신호 감지

### F-LLM-R3-02: retrieved priority ordering

`execute_graph_search`:
- `priority_node_ids: list[int]` + `priority_set: set[int]` 도입
- `_add_node_to_result(nid, node, priority)`: priority=True 이면 priority_node_ids 에 등록 (idempotent 승격)
- target_entities 의 시드 노드 자기 자신 → priority
- target_relations 의 source / target 끝점 노드 자기 자신 → priority
- 결과 빌드 시 priority 노드를 retrieved 의 앞순위 (rank-1, 2, ...) 에 배치
- 나머지 (BFS 확장, 임베딩 보강) 는 그 뒤로

### F-LLM-R3-03: system prompt 정렬

- 인덱싱 LLM 의 entity_types / relation_types / 의도 매핑을 같은 형태로 노출
- 방향성 규약 명시: `source --[type]--> target` (인덱싱과 일치)
- target_relations 의 끝점 미상 시 빈 문자열 허용 — 시스템이 fuzzy 매칭으로 회복

### 후방 호환 동작

- LLM 이 구식 응답 (`search_steps`) 을 돌려줘도 _parse_plan 이 둘 다 파싱
- execute_graph_search 는 R3 신호 우선, 없으면 R2 경로 (search_steps + 양방향 traversal)
- 기존 호출자 (외부 코드 / 테스트) 가 `GraphSearchPlan(search_steps=[...])` 로 직접 호출하는 코드도 영향 없음

## 변경 파일 통계

| 파일 | 변경 라인 |
|------|----------|
| `src/context_loop/processor/graph_search_planner.py` | +140 / -50 |
| `tests/test_processor/test_graph_search_planner.py` | +135 / -1 |

## 신규 테스트 (5건)

| 테스트 | 검증 의도 |
|--------|----------|
| `test_parse_plan_r3_target_entities_and_relations` | R3 schema 파싱 |
| `test_parse_plan_r3_relation_with_empty_endpoint` | 끝점 미상 허용 |
| `test_parse_plan_r3_falls_back_to_search_steps` | 후방 호환 — 구식 응답도 파싱 |
| `test_execute_search_with_target_entities_prioritizes_seed` | target_entities 의 시드가 rank-1 (priority ordering 의 직접 검증) |
| `test_execute_search_with_target_relations_includes_both_endpoints` | target_relations 의 source/target 양쪽이 모두 priority |
| `test_execute_search_target_entity_uses_embedding_fallback` | R3 + R1 임베딩 fallback 연동 |

## 계획-구현 매트릭스

| ID | 계획 | 실제 | 일치 |
|----|------|------|------|
| F-LLM-R3-01 | TargetEntity/Relation 추가 + 파싱 | ✓ + has_targets property (보너스) | ✓+ |
| F-LLM-R3-02 | priority ordering | ✓ + idempotent 승격 (보너스) | ✓+ |
| F-LLM-R3-03 | system prompt 정렬 | ✓ + 방향성 규약 명시 (보너스) | ✓+ |
| 후방 호환 | search_steps fallback | ✓ | ✓ |

## 회귀 위험 점검

| 변경 | 회귀 위험 | 검증 |
|------|----------|------|
| schema 변경 | LLM 이 새 형태로 답해야 하나 — 못 답하면 0 응답 | 후방 호환 search_steps 경로 유지 |
| priority ordering | retrieved 의 rank-1 가 LLM 정답 후보로 강제 | LLM 의도가 정확하면 정합; 부정확하면 R2 와 동일한 rank 손실 |
| system prompt 1.5x 증가 | max_tokens 32768 한도 안전 | 응답 토큰 한도 영향 없음 |

## 다운스트림 영향

- `GraphSearchPlan` 시그니처 확장 (target_entities, target_relations 추가) — 기존 호출자는 영향 없음 (기본값 빈 리스트)
- `execute_graph_search` 시그니처 동일
- context_assembler 변경 불필요

## R4 후보 (다음 세션)

- LLM 의 target_relations 자체를 retrieved_graph_relations 에 직접 포함 (현재는 실제 edge 가 있어야만 노출)
- LLM 의 target_relations 의 fuzzy 매칭 (source 미상 시 target 만으로 incoming 추적)
- 인덱싱 LLM 의 결정성 강화 (재인덱싱 시 같은 입력 → 같은 출력)
- 인덱싱-검색 LLM 의 공유 vocabulary 자동 동기화 (graph_vocabulary.py 갱신 시 양쪽 프롬프트 자동 반영 — 이미 일부 적용됨)
