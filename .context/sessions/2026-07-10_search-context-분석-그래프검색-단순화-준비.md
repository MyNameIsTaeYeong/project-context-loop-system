# 2026-07-10 search_context 검색 과정 분석 — 그래프 검색 단순화 준비

## 배경

사용자 요청 흐름:
1. "search_context 시 검색 과정을 상세히 알려주세요" — 파이프라인 전체 추적
2. HyDE 평균 임베딩이 왜 동의어·약어를 반영하는지 개념 설명
3. `plan_graph_search` 의 쿼리 임베딩 기반 서브그래프 추출 과정 설명
4. `execute_graph_search` 를 예시로 단계별 설명
5. "과정이 너무 복잡한데 각 층이 의미 있나? 단순화 가능한가?" — 비판적 검토
6. **사용자 결정: 그래프 검색 단순화를 다음 세션 메인 주제로 진행**

## 분석 결과 요약

### search_context 파이프라인 (src/context_loop/mcp/context_assembler.py)

쿼리 임베딩(HyDE 옵션) → 벡터 검색(6배 over-fetch + document_id dedup +
similarity threshold) → 리랭킹·그래프 탐색 병렬(asyncio.gather) →
parent-document 치환 → 그래프 연결 문서 첨부(_search_graph_sourced_chunks,
벡터가 못 찾은 순수 추가분만) → 원본 소스 코드 첨부 → 섹션 조립.

### execute_graph_search 복잡도 평가 (핵심 결론)

복잡도는 설계가 아니라 R1→R2→R3 패치의 지층:
- 시드 이름 임베딩 fallback / always-on 보강(0.6/top-3) / 최후 폴백(0.5/top-5)
  3층은 전부 "LLM 플래너가 틀리는 것"을 수습하는 층 — 임베딩 검색으로 수렴
- priority 정렬·_natural_description·GraphRelationRef 는 평가 메트릭
  (MRR/NDCG/T4) 지원용 — 검색 품질과 무관하게 프로덕션 코드에 침투
- search_steps/focus_relations 경로는 프로덕션 호출자 없는 죽은 코드
  (플래너 모듈 자신 + 테스트에서만 참조 — grep 으로 확인)
- 플래너의 고유 기여는 ① should_search 게이팅 ② 관계 방향성 인식뿐인데,
  ②의 결과물(target_relations)은 끝점 시딩에만 쓰이고 방향성 필터링은 안 함
- always-on 보강이 도입된 경위 자체가 "임베딩 검색이 LLM 계획보다 안정적"
  이라는 증거

## 다음 세션 메인 주제: 그래프 검색 단순화

### 권장 베이스라인 — 최소 버전

```
쿼리 임베딩 → search_entities_by_embedding(threshold, top_k=5)
→ 각 시드 1-hop 확장 → 노드 집합 내부 엣지 수집
→ 텍스트 포맷 + document_ids
```

- LLM 호출 0회, fallback 0층, 결정적 동작. 현재 코드의 "최후 폴백"
  (graph_search_planner.py 의 query_embedding 시드 폴백) 경로 하나가 전부.
- "그래프와 무관한 질문" 게이팅은 should_search LLM 판단 대신
  "threshold 를 못 넘는 시드 엔티티가 없으면 그래프 섹션 생략"이 대체.
- 구성 요소는 이미 전부 존재: `search_entities_by_embedding`
  (graph_store.py), `get_neighbors_from_node_id(depth=1)`,
  `get_edges_between`, 포맷팅 로직 (execute_graph_search 후반부).

### 단순화로 제거 대상

- `plan_graph_search` LLM 플래너 호출 + `_render_system_prompt` 프롬프트
- 서브그래프 스키마 생성 사용처 (`format_query_relevant_schema_for_llm` —
  LLM 에게 보여줄 일이 없어짐)
- `_build_seed_embeddings` 시드 배치 임베딩 + 시드별 임베딩 fallback
- always-on 보강 / 최후 폴백의 3층 구조 (임베딩 시딩 1층으로 통합)
- `search_steps` / `SearchStep` / `focus_relations` 죽은 코드
- `TargetEntity` / `TargetRelation` / R3 target_* 파싱 경로

### 유지해야 할 것 (호출부 계약)

- `GraphSearchResult(text, document_ids, entities, relations)` 반환 형태 —
  조립기(_search_graph_with_llm 호출부)와 평가(eval_search.py 의
  retrieved_graph_entities/relations 채점)가 의존
- `document_ids` → `_search_graph_sourced_chunks` 의 그래프 연결 문서 첨부
- entities 의 description 채움(_natural_description)은 평가 T4 매칭이
  사용 — 평가 유지 시 보존 또는 평가 측 이동 검토
- 엔티티 임베딩 자동 구축 (build_entity_embeddings lazy 보완) — 시딩의 전제

### 검토 포인트

- `assemble_context` / `assemble_context_with_sources` 양쪽의
  `llm_client` 의존 완화: 그래프 탐색이 LLM 없이 동작하게 되면
  `_maybe_graph` 의 `llm_client` 가드 제거 가능
- threshold/top_k 값: 현행 최후 폴백은 0.5/top-5, always-on 보강은
  0.6/top-3 — 최소 버전의 단일 값 선정 필요 (0.5/top-5 로 시작 권장)
- 짧은 엔티티 이름 vs 문장형 쿼리의 임베딩 비대칭으로 threshold 미달 →
  그래프 섹션 생략이 잦아질 수 있음 — 실측으로 확인
- 기존 테스트 대규모 수정 필요: test_graph_search_planner.py,
  test_context_assembler.py 의 플래너 의존 테스트

## 브랜치 상태

브랜치: `claude/search-context-process-w9pa4z` (main 1ea72db 에서 분기).
기능 코드 변경 없음 — 이번 세션은 분석과 단순화 방향 확정까지.
(중간에 실험용 스위치를 커밋했다가 사용자 결정으로 revert 하여 최종
소스는 main 과 동일.)
