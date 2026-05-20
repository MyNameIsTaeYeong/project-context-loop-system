# Indexing-Side LLM Prompt 분석 (R3 Phase A)

## 인덱싱 LLM의 출력 형태 (`llm_body_extractor.py`)

```json
{
  "entities": [
    {"name": "Auth Service", "type": "system", "description": "사용자 인증 담당"}
  ],
  "relations": [
    {"source": "Auth Service", "target": "Token Validator", "type": "depends_on"}
  ]
}
```

**핵심 특징**:
1. **`graph_vocabulary.py` 의 ENTITY_TYPES / RELATION_TYPES 어휘만 사용** (entity_types 15개 + relation_types 17개)
2. **관계가 방향성을 가짐**: `source → target` 명시. 인덱싱 시점에서 `A depends_on B` 와 `B depends_on A` 가 구분됨.
3. **본문에 명시된 관계만 추출** (추론 금지)
4. **이름 정규화 정책**: 본문 표기 그대로, 약어보다 풀네임 우선

## 인덱싱 → 그래프 저장 변환

`save_graph_data` (graph_store.py):
- 정규 엔티티 병합: `(entity_name 대소문자 무시 + entity_type)` 기준 → 같은 노드로 수렴
- 관계: `(src_id, tgt_id, relation_type, document_id)` 키로 중복 방지
- DiGraph 에 `add_edge(src, tgt, relation_type=...)` — **방향성 유지**

## 검색 측과의 불일치 (R3 이전)

- 인덱싱: `{entities: [...], relations: [{source, target, type}]}`
- 검색: `{search_steps: [{entity_name, depth, focus_relations}]}` — 단일 시드 + 깊이 + 관계 필터

**관계의 방향성을 검색 측에서 표현 못함**:
- "Order Service 가 KakaoPay 에 depends_on" 을 검색하려면 search_step.entity_name = "Order Service", focus_relations = ["depends_on"]
- 그러나 LLM 이 "KakaoPay" 를 시드로 답하면 (R2 의 양방향 traversal 로 회복은 되지만) **방향성 정보**가 사라진 채 traversal
- gold 가 관계 채점 (`relevant_graph_relations`) 을 켜면, 검색 측이 관계 자체를 식별 못함 → relation_recall 가 graph_recall 보다 더 낮음

## 결론

검색 LLM 의 출력 schema 를 인덱싱과 정렬해야 함:
- `target_entities: [{name, type}]` — 인덱싱의 `entities` 와 동일 shape
- `target_relations: [{source, target, relation_type}]` — 인덱싱의 `relations` 와 동일 shape
- 같은 vocabulary (`graph_vocabulary.py`)
- 같은 방향성 규약

이렇게 정렬되면 LLM 의 mental model 이 "인덱싱 시점에서 어떻게 추출됐는지" → "그래서 검색 시 어떤 (entity, relation) 을 찾아야 하는지" 로 일관됨.
