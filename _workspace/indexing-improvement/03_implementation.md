# Implementation Report — Round 2 (문서 단위 LLM 그래프 추출)

## 적용 항목

| 파일 | 변경 |
|------|------|
| `src/context_loop/processor/llm_body_extractor.py` | (a) `InputTooLargeError` 신규 예외 추가. (b) `LLMBodyExtractionConfig.max_input_tokens: int = 200_000` 필드 추가. (c) `extract_llm_body_graph_for_document(*, doc_title, body, llm_client, config) -> tuple[GraphData, LLMBodyExtractionStats]` 신규 함수 — 문서 전체 본문을 1회 LLM 호출로 처리. 입력 토큰 가드, 빈 입력 가드, JSON 파싱 실패 시 빈 결과 + stats 보고. 어휘/끝점 검증은 기존 unit 기반 함수와 동일. |
| `src/context_loop/processor/pipeline.py` | (a) `extract_llm_body_graph_for_document` + `InputTooLargeError` import. (b) LLM 본문 그래프 호출 분기를 문서 단위 호출 우선 + `InputTooLargeError` 발생 시 unit 기반 폴백으로 전환. (c) `_assemble_document_body(extracted)` 헬퍼 신규 — sections 가 있으면 헤딩+본문 트리 순서 합본, 없으면 plain_text. |

## 변경 핵심 동작 (Before/After)

**Before** (`pipeline.py:352-371`):
- 문서당 `build_extraction_units` 로 생성된 N개 unit 마다 LLM 호출 (병렬, sem=3).
- cross-unit 엔티티가 unit 별로 격리되어 같은 entity 가 description 부분적으로 중복 등장.

**After** (`pipeline.py:352-389`):
- 문서당 **1회** LLM 호출 (`extract_llm_body_graph_for_document(body=문서 전체)`).
- 본문 토큰이 `max_input_tokens=200_000` 초과면 `InputTooLargeError` → 기존 unit 기반 폴백 자동 동작.
- 결정론 `body_extractor` 의 unit 입력 경로는 그대로 유지 — `section_label` 시그널 보존.

## 변경하지 않은 것 (Out of Scope, 별도 라운드)

- `chunker` 의 임베딩 청크 단위 (검색 정밀도/임베딩 한도 결합 — 별도 평가 필요)
- `ast_code_extractor.to_chunks` (의미적 분할, 토큰 분할 아님)
- git_code 경로 (현재 LLM 호출 없음, 변경 불필요)
- `body_extractor` 결정론 추출의 unit 입력 (section_label 시그널 손실 위험)
- MCP `context_max_tokens` 가드 (검색 응답 단위 문제)

## 신규 테스트

| 파일 | 테스트 | 검증 대상 |
|------|--------|----------|
| `test_processor/test_llm_body_extractor.py` | `test_for_document_single_call_extracts_all_entities` | 문서 1회 호출로 N entity + M relation 모두 추출, `units_total==1, units_called==1` |
| 〃 | `test_for_document_oversized_body_raises_input_too_large` | `max_input_tokens` 초과 시 `InputTooLargeError` raise, LLM 미호출 |
| 〃 | `test_for_document_empty_inputs_skip_llm_call` | doc_title 또는 body 가 비면 LLM 미호출, 빈 GraphData |
| 〃 | `test_for_document_drops_unknown_vocabulary_and_dangling_endpoints` | 어휘 외 entity, 양끝점 누락/자기루프 relation 모두 drop, label="" |
| 〃 | `test_for_document_failed_llm_returns_empty_with_failed_stat` | LLM JSON 파싱 실패 시 빈 GraphData + `units_failed=1` |
| 〃 | `test_for_document_call_disables_thinking_mode` | `reasoning_mode="off"`, `purpose="body_extraction_doc"` |
| `test_processor/test_pipeline.py` | `test_llm_body_extraction_runs_when_enabled` (수정) | 문서 단위 호출이 1회, unit 기반 호출은 0회 |
| 〃 | `test_llm_body_extraction_falls_back_to_units_when_oversized` (신규) | 문서 단위 호출 `InputTooLargeError` 시 unit 기반 폴백 |
| 〃 | `test_assemble_document_body_uses_sections_when_present` | sections 가 있으면 헤딩+본문 합본, plain_text 무시 |
| 〃 | `test_assemble_document_body_falls_back_to_plain_text` | sections 가 없으면 plain_text 그대로 |

수정 1건 (`test_llm_body_extraction_skipped_when_disabled`): 의도를 명시화 — `PipelineConfig(enable_llm_body_extraction=False)` 로 변경. 기존엔 기본값이 True 인데도 부수효과(unit 게이트)로 통과해온 케이스. 신규 함수는 문서 단위 1회 호출이라 게이트 우회 → 명시적 disable 만이 진짜 의도를 표현.

## 회귀 위험 평가

| 위험 | 완화 결과 |
|------|----------|
| 큰 문서에서 LLM 응답 잘림 | `max_input_tokens=200_000` 가드 + `InputTooLargeError` 시 unit 폴백 |
| 기존 `extract_llm_body_graph(units)` 시그니처 변경 | 보존 — 28개 기존 테스트 + pipeline 폴백 경로에서 그대로 사용 |
| section_label 손실 | 결정론 `body_extractor` 경로 유지 → `Relation.label = unit.section_path` 시그널 보존 |
| stats 호환성 | `LLMBodyExtractionStats` 스키마 그대로 — 문서 단위 호출에서 `units_total=1, units_called=1` 로 매핑 |
| 빈 응답·실패 처리 | 빈 GraphData 반환, stats 에 `units_failed=1` 기록 — 호출자(pipeline)는 `llm_graph.entities` 빈 체크로 안전 분기 |

## 비용/효과 (예상)

- 문서당 LLM 호출 횟수: 평균 3~5회 → **1회** (-70~80%)
- Cross-section 엔티티 통합: GraphStore 사후 병합에만 의존 → **LLM 자체에서 1회 통합** (description 풍부도 +)
- 인덱싱 wall time: unit 직렬+동시성(sem=3) → 단일 호출 (-30~50%)
- 검색 정밀도 (벡터): 변경 없음 (=)
- 검색 정밀도 (그래프): 통합된 description + 누락 관계 감소 → **+**

## 의존성 추가

- `llm_body_extractor.py` 가 `chunker.count_tokens` 를 import. 기존에는 의존하지 않았음 — 토큰 카운트가 필요해진 것은 입력 가드 때문. 의존 그래프상 `chunker` 는 `llm_body_extractor` 의 하류이므로 순환 위험 없음.

## 코드 변경 라인 수 (실제 추정)

| 파일 | LOC ± |
|------|------|
| `llm_body_extractor.py` | +148 |
| `pipeline.py` | +43 (호출 분기 변경 + helper) |
| `test_llm_body_extractor.py` | +120 |
| `test_pipeline.py` | +90 |
| **합계** | **+401** |

