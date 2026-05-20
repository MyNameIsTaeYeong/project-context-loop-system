# R3 Improvement Plan — 검색-인덱싱 LLM Schema 정렬

## 핵심 가설

R2 까지의 fix (양방향 traversal + always-on 보강 + 관계 요약 description) 가
적용됐는데도 메트릭 변화 미미 → funnel 손실의 가장 큰 잔여 지점은 **검색
LLM 의 mental model 이 인덱싱과 다른 schema 로 동작** 한다는 것.

LLM 은 인덱싱 시점에 `{entities, relations:[{source, target, type}]}` 로
훈련됐는데, 검색 시점에는 `{search_steps:[{entity_name, depth,
focus_relations}]}` 라는 다른 schema 로 답을 요구받음 → mental model
간섭, 정답이 될 entity/relation 을 직접 표현하지 못함, retrieved 의
priority 도 LLM 의도가 반영되지 못함.

## 라운드 3 변경

### 변경 1 (Critical): 검색 LLM 출력 schema 를 인덱싱과 정렬

**전**:
```json
{
  "search_steps": [
    {"entity_name": "Order Service", "depth": 1, "focus_relations": ["depends_on"]}
  ]
}
```

**후**:
```json
{
  "target_entities": [
    {"name": "Order Service", "type": "service"},
    {"name": "KakaoPay", "type": "team"}
  ],
  "target_relations": [
    {"source": "Order Service", "target": "KakaoPay", "relation_type": "depends_on"}
  ]
}
```

### 변경 2 (Critical): retrieved 의 rank-1 priority 보장

`execute_graph_search` 가 LLM 의 `target_entities` / `target_relations` 끝점
노드를 **priority 시드** 로 표시 → 결과 빌드 시 priority 노드가 retrieved 의
앞순위에 배치 → gold 의 relevant_graph_entities 가 rank-1 hit → **MRR/NDCG 회복**.

### 변경 3 (High): 후방 호환

- `GraphSearchPlan.search_steps` 유지 — 기존 호출자가 직접 SearchStep 을 만들거나, LLM 이 구식 응답을 돌려주는 경우 graceful fallback
- `_parse_plan` 이 target_* 와 search_steps 둘 다 파싱
- `execute_graph_search` 가 R3 신호 우선, 없으면 R2 경로

### 변경 4 (Medium): system prompt 가 인덱싱과 같은 어휘/규약 명시

- 인덱싱 LLM 의 entity_types / relation_types / 의도 매핑 가이드를 **동일하게** 노출
- 방향성 규약 (`source --[type]--> target`) 을 명시 — 인덱싱과 일치

## 우선순위 매트릭스

| ID | 영역 | 영향 | 공수 | 라운드 |
|----|------|------|------|--------|
| **F-LLM-R3-01** | schema 정렬 (target_entities + target_relations) | **Critical** | M | **R3** |
| **F-LLM-R3-02** | retrieved priority ordering | **Critical** | S | **R3** |
| **F-LLM-R3-03** | system prompt 정렬 (어휘 + 방향성 규약) | High | S | **R3** |
| F-LLM-R3-04 | LLM 의 target_relations 자체를 retrieved_graph_relations 에 포함 | Medium | S | R4 후보 |

## 변경 파일

| 파일 | 변경 |
|------|------|
| `src/context_loop/processor/graph_search_planner.py` | TargetEntity/TargetRelation dataclass, GraphSearchPlan 확장, system prompt 재작성, _parse_plan 확장, execute_graph_search R3 경로 + priority ordering |
| `tests/test_processor/test_graph_search_planner.py` | R3 신규 테스트 5건, 기존 test_system_prompt_includes_intent_mapping 갱신 |

## 회귀 위험

| 변경 | 잠재 위험 | 검증 |
|------|----------|------|
| 새 schema 출력 요구 | LLM 이 학습 분포에 더 가까운 형태 | 후방 호환 search_steps 경로 유지 |
| priority ordering | retrieved 순서 변화 → precision 분포 변화 가능 | priority 는 LLM 식별 정답 후보만이라 의도와 정합 |
| system prompt 길이 | 1.5x 증가 | max_tokens 32768 한도 안전 |

## 검증 체크리스트

- [x] `pytest tests/test_processor/test_graph_search_planner.py` (27 passed = 21 기존 + 5 신규 + 1 갱신)
- [x] `pytest tests/test_storage/test_graph_store.py` (41 passed)
- [x] `pytest tests/test_mcp/test_context_assembler.py` (23 passed)
- [x] `pytest tests/ --ignore=tests/test_eval` (**762 passed**, 전체 회귀 0)
- [x] ruff: 3 errors == baseline 3 errors (regression 0)

## R4 후보 (다음 세션)

- LLM 의 target_relations 를 retrieved_graph_relations 에 직접 포함 (실제 edge 가 없어도)
- gold 의 evidence_description ↔ LLM target_relation 매칭 (intent 매칭)
- 인덱싱 LLM 의 결정성 강화 (재인덱싱 시 같은 입력 → 같은 출력 보장)
