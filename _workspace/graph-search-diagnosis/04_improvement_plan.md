# Graph Search Improvement Plan

## funnel 손실 진단 요약

| 단계 | 추정 손실률 | 주된 원인 |
|------|------------|----------|
| Stage 1: 쿼리 임베딩 | 거의 0 | 안정 |
| Stage 2: plan_graph_search (LLM → search_steps) | 30~50% | F-IDX-02 (임베딩 캐시 누락 시 query-relevant schema 없음) |
| **Stage 3: get_neighbors (entity_name → 시드 노드)** | **80%+** | **F-SRCH-01 (임베딩 fallback 없음)** ← 메인 손실 |
| Stage 4: 서브그래프 확장 | 거의 0 | depth=1, 시드 잡으면 정상 |
| Stage 5: 메트릭 매칭 | 30~50% | F-METRIC-01 (threshold 0.78 보수적), F-METRIC-03 (짧은 이름 비특이) |

**핵심 가설**: Stage 3에서 LLM 추측 entity_name이 인덱스에 표면 일치하지 않으면 즉시 빈 결과 → execute_graph_search None → retrieved=[] → 모든 메트릭 0. 이게 < 10% 메트릭의 주된 원인.

## 우선순위 매트릭스

| ID | 출처 | 영역 | 영향 | 공수 | ROI | 라운드 |
|----|------|------|------|------|-----|--------|
| **F-SRCH-01** | 02 | get_neighbors 임베딩 fallback | Critical | S | ★★★★★ | **R1** |
| **F-SRCH-03** | 02 | execute_graph_search 시드 0개 시 임베딩 fallback | High | S | ★★★★★ | **R1** |
| **F-SRCH-02** | 02 | search_entities_by_embedding default threshold 0.7 → 0.5 | High | S | ★★★★ | **R1** |
| **F-METRIC-01** | 03 | DEFAULT_GRAPH_MATCH_THRESHOLD 0.78 → 0.65 | High | S | ★★★★ | **R1** |
| **F-SRCH-06 + F-METRIC-03** | 02+03 | retrieved description fallback 보강 | High | S | ★★★★ | **R1** |
| **F-METRIC-02** | 03 | 골든 description 부재 시 name fallback | Medium | S | ★★★ | **R1** |
| **F-SRCH-04** | 02 | LLM 프롬프트에 entity_name 정확 복사 강제 | Medium | S | ★★★ | **R1** |
| F-IDX-02 | 01 | entity_embeddings 자동 build race-condition + lock | Critical | M | ★★★ | R2 |
| F-IDX-01 | 01 | DiGraph → MultiDiGraph (multi-edge) | High | M | ★★ | R2 |
| F-IDX-03 | 01 | save_graph_data multi-edge skip 해결 | High | S (F-IDX-01과 묶음) | ★★ | R2 |
| F-SRCH-05 | 02 | max_entities_per_type 10 → 20 | Medium | S | ★★ | R2 |
| F-IDX-02 (B) | 01 | entity_embeddings SQLite 영속화 | High | L | ★★ | R3 |

## 라운드 1: 검색 funnel 회복 + 메트릭 임계값 완화

### 포함 항목 (R1)

1. **F-SRCH-01**: `get_neighbors`에 임베딩 fallback (exact/scoped/short 실패 시)
2. **F-SRCH-03**: `execute_graph_search`에서 search_steps 모두 실패 시 query embedding 기반 시드 보강
3. **F-SRCH-02**: `search_entities_by_embedding` default `threshold=0.7 → 0.5`
4. **F-SRCH-04**: graph_search_planner system prompt에 "schema 이름을 글자 단위로 정확 복사" 강제 + 코드 블록 감싸기
5. **F-SRCH-06**: execute_graph_search가 retrieved GraphEntityRef의 description이 빈 경우 자연어 fallback (`"이 entity는 {type} 유형의 '{name}'입니다"`)
6. **F-METRIC-01**: `DEFAULT_GRAPH_MATCH_THRESHOLD = 0.78 → 0.65`
7. **F-METRIC-02**: golden description 부재 시 `golden.name` fallback

### 변경 파일

| 파일 | 변경 사유 |
|------|----------|
| `src/context_loop/storage/graph_store.py` | F-SRCH-01 (get_neighbors 임베딩 fallback), F-SRCH-02 (default threshold) |
| `src/context_loop/processor/graph_search_planner.py` | F-SRCH-03 (시드 fallback), F-SRCH-04 (prompt), F-SRCH-06 (description fallback) |
| `src/context_loop/eval/graph_match.py` | F-METRIC-01 (threshold), F-METRIC-02 (description fallback) |

### 구현 순서

1. **F-METRIC-01 + F-METRIC-02 + F-SRCH-02**: 단순 상수/기본값 조정 — 회귀 위험 가장 낮음
2. **F-SRCH-06**: GraphEntityRef description 폴백 (graph_search_planner의 execute_graph_search 끝)
3. **F-SRCH-01**: `get_neighbors`에 임베딩 fallback (별도 함수 또는 내부에서 호출)
4. **F-SRCH-03**: `execute_graph_search`에 query embedding 시드 보강 — signature에 query_embedding 추가 + 호출처도 함께 수정
5. **F-SRCH-04**: system prompt 수정 + schema_text 형식 조정

### 회귀 위험

| 변경 | 잠재 회귀 |
|------|----------|
| threshold 0.78 → 0.65 | 메트릭 절대값 변화. 기존 메트릭 비교 가능성 손실 — 그러나 새 baseline이 의도 |
| get_neighbors 임베딩 fallback | depth=2 검색이 더 큰 서브그래프 반환 — context_text 길이 증가, 노이즈 증가 가능 |
| execute_graph_search 시드 보강 | LLM 의도와 무관한 시드 들어옴 — 단, 의도 보존 위해 LLM 제안이 0개일 때만 fallback |
| description fallback 자연어 | 매칭 임베딩 시그널이 더 비특이적이 되는 역효과 가능 — 그러나 빈 description보다는 나음 |

### 필요한 신규 테스트

- `tests/test_storage/test_graph_store.py::test_get_neighbors_falls_back_to_embedding_when_name_unknown`
- `tests/test_storage/test_graph_store.py::test_search_entities_default_threshold_lowered`
- `tests/test_processor/test_graph_search_planner.py::test_execute_seeds_from_query_embedding_when_steps_miss`
- `tests/test_processor/test_graph_search_planner.py::test_execute_fills_description_fallback_for_retrieved`
- `tests/test_eval/test_graph_matching.py::test_threshold_default_065_or_attr` (기본값 가드)
- `tests/test_eval/test_graph_matching.py::test_golden_description_fallback_to_name`

### 기존 테스트 영향

- `tests/test_eval/test_graph_matching.py` — DEFAULT_GRAPH_MATCH_THRESHOLD 변경으로 일부 임계값 비교 테스트가 영향 받음. 검토 후 0.78 명시적 사용 케이스만 유지
- `tests/test_storage/test_graph_store.py` — `search_entities_by_embedding` default 0.7 가정 테스트가 있으면 조정
- `tests/test_processor/test_graph_search_planner.py` — system prompt 텍스트 검사 테스트가 있으면 조정

## 라운드 2 (다음 세션)

- F-IDX-02 entity_embeddings race lock + 명시적 build 실패 보고
- F-IDX-01/03 MultiDiGraph 전환
- F-SRCH-05 max_entities_per_type 확장
- entity_embeddings SQLite 영속화

## 검증 체크리스트

R1 구현 후:
- [ ] `pytest tests/test_storage/test_graph_store.py -x -q`
- [ ] `pytest tests/test_processor/test_graph_search_planner.py -x -q`
- [ ] `pytest tests/test_eval/test_graph_matching.py -x -q`
- [ ] `pytest tests/test_mcp/test_context_assembler.py -x -q`
- [ ] `pytest tests/ --ignore=tests/test_eval` (전체 회귀)
- [ ] ruff check (touched files)
- [ ] 평가 메트릭 변화 측정은 별도 (사용자가 직접 eval_search.py 재실행)

## 비범위

- `eval_search.py`/`build_synthetic_gold_set.py`의 정합성 — 별도 하네스
- 그래프 추출 알고리즘 변경 (extractor/link_graph_builder 등) — `indexing-improvement`
- 본 라운드는 **검색 funnel의 회복 + 메트릭 임계값 완화**가 핵심
