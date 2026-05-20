# Graph Index Diagnosis

> 정적 코드 분석 기준 진단 (사용자 평가 결과 row-level 데이터 없음).

## 인덱스 측 핵심 발견

### F-IDX-01 (HIGH): `load_from_db`의 NetworkX `DiGraph` 가 cross-document multi-edge를 손실

- **위치**: `src/context_loop/storage/graph_store.py:115-128`
- **현재 동작**:
  ```python
  for doc in docs:
      edges = await self._store.get_graph_edges_by_document(doc["id"])
      for edge in edges:
          if self._graph.has_node(edge["source_node_id"]) and ...:
              self._graph.add_edge(
                  edge["source_node_id"],
                  edge["target_node_id"],
                  id=edge["id"],
                  relation_type=edge.get("relation_type", "related_to"),
                  document_id=edge["document_id"],
                  ...
              )
  ```
- **문제**: `nx.DiGraph`는 (src, tgt) 한 쌍에 **단일** 엣지 데이터만 보관. 같은 (src, tgt)에 다른 `relation_type`(예: `depends_on` + `mentions`)이 SQLite에 있으면, 후속 `add_edge` 호출이 이전 데이터를 **덮어쓴다**. 운영 영향:
  - 그래프 노드 페어 사이의 multi-relation 정보 손실 → `execute_graph_search`의 `get_edges_between`이 한 종류만 반환 → 메트릭의 `graph_rel_recall` 도 낮아짐
  - `save_graph_data` 시에는 같은 (src, tgt, type, doc_id)면 skip하지만, type만 다른 case는 막지 않음 → 재로드 시 덮어씀
- **개선 방향**:
  - (A) `nx.MultiDiGraph`로 교체 — 표준 해법이지만 API 호환 영향
  - (B) DiGraph 유지하되 같은 (src, tgt) edges를 list로 보관: `data["relations"] = [{...}, {...}]`
  - 권장: (A). NetworkX의 multi 메서드(`has_edge` 등)가 같이 동작.
- **심각도**: High | **공수**: M

### F-IDX-02 (CRITICAL): `_entity_embeddings`가 휘발성 캐시 + 자동 구축의 race-condition 위험

- **위치**: `src/context_loop/storage/graph_store.py:86, 130, 591-614` + `context_assembler.py:263-264`
- **현재 동작**:
  - `_entity_embeddings`는 인메모리 dict — 프로세스 재시작 시 사라짐
  - `load_from_db`도 `_entity_embeddings.clear()` 호출 (line 130) → 매 로드 후 빈 상태
  - context_assembler가 `if entity_embedding_count == 0: await build_entity_embeddings(...)` 한 번만 호출 (line 263-264)
- **문제**:
  - 평가 스크립트는 보통 GraphStore를 새로 인스턴스화 → 매번 임베딩 재구축 비용
  - 동시 요청에서 race: 두 요청이 동시에 `count == 0` 체크 후 둘 다 build 시도
  - build가 실패하면 catch만 하고 다음 요청에도 빈 캐시 → `get_query_relevant_schema`가 `get_schema_summary` fallback (line 482-483) → LLM이 받는 schema에 query-relevant entity 정보 없음
- **이 진단의 평가 메트릭 < 10% 가설 연결도**: 매우 높음 — 임베딩 캐시가 비어있으면 schema_text가 일반 dump → LLM이 정확한 entity_name 추출 못함 → execute_graph_search 빈 결과 → graph 메트릭 0
- **개선 방향**:
  - (A) `build_entity_embeddings`를 비동기 lock으로 보호 (한 번만 실행)
  - (B) SQLite에 임베딩 영속화 (재시작 비용 제거)
  - (C) `count == 0` 체크 시 build 실패면 retry 또는 명시적 에러 보고
  - 권장: (A) + (C) 우선, (B)는 R2
- **심각도**: Critical | **공수**: S~M

### F-IDX-03 (HIGH): `save_graph_data`가 같은 (src, tgt) 다른 relation_type의 NetworkX edge 등록 skip

- **위치**: `src/context_loop/storage/graph_store.py:225-232`
- **현재 동작**:
  ```python
  existing_edge = False
  if self._graph.has_edge(src_id, tgt_id):
      edge_data = self._graph.edges[src_id, tgt_id]
      if (edge_data.get("relation_type") == relation.relation_type
              and edge_data.get("document_id") == document_id):
          existing_edge = True
  if existing_edge:
      continue
  ```
- **문제**: SQLite에는 새 엣지가 추가되지만 NetworkX `add_edge`는 같은 (src, tgt)를 덮어씀 → 다른 relation_type을 SQLite에는 둘 다, NetworkX에는 마지막 것만. 검색은 NetworkX를 사용하므로 SQLite ↔ NetworkX 불일치
- **개선 방향**: F-IDX-01 해결과 묶음 (MultiDiGraph)

### F-IDX-04 (MEDIUM): entity_name의 정규화·중복 진단 부재

- **현재 동작**: `find_graph_node_by_entity(name, type)`이 lowercase 매칭으로 병합. 그러나 "Auth Service"와 "AuthService"는 별개 노드
- **문제**: 골드셋과 인덱스의 표면 표기 차이가 매칭 실패의 원인 가능 — 별도로 인덱스 자체에 어떤 표기 변형이 존재하는지 진단 도구가 없음
- **개선 방향**: 인덱스 진단 스크립트 추가 (별도)
- **심각도**: Medium | **공수**: M (스크립트 분리)

## 검색 매칭 측면 인덱스 품질 진단

골드셋 entity_name이 인덱스 노드명과 매칭되려면:
1. **표면 정확 일치** — find_graph_node_by_entity의 lowercase 매칭. 정규화 함수의 깊이가 약함 (공백 차이 무시 안 함).
2. **임베딩 유사도** — `_entity_embeddings`가 채워져 있어야 함. F-IDX-02 위험.

## 검토하지 않은 영역

- 실제 SQLite DB의 노드/엣지 수 통계 (사용자 환경 접근 불가)
- 골드셋 entity 표기와 실제 인덱스 표기 차이 sampling
- entity_embedding 모델의 한국어 적합성
