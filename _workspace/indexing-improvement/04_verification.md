# Verification Report — Round 2

## 결과: **PASS**

## 실행한 테스트

| 영역 | 명령 | 결과 |
|------|------|------|
| llm_body_extractor 단위 테스트 (기존 + 신규) | `pytest tests/test_processor/test_llm_body_extractor.py` | **27 passed** |
| pipeline 통합 테스트 (기존 + 신규) | `pytest tests/test_processor/test_pipeline.py` | **22 passed** |
| processor/ingestion/storage 전체 | `pytest tests/test_processor/ tests/test_ingestion/ tests/test_storage/` | **662 passed** |
| 전체 테스트 스위트 | `pytest` | **1043 passed, 5 failed** |

## 5건 fail 분석

| 테스트 | 우리 변경과 관련 | 사전 존재 여부 |
|--------|------------------|----------------|
| `test_eval/test_synth.py::test_filter_question_passes_clean` | 무관 (eval 영역) | ✅ origin/main 에서도 fail (git stash 후 재실행 확인) |
| `test_eval/test_synth.py::test_filter_question_fails_generic` | 무관 | ✅ pre-existing fail |
| `test_eval/test_build_synthetic_gold_set.py::test_fetch_source_text_anchor_match` | 무관 | ✅ pre-existing fail |
| `test_eval/test_build_synthetic_gold_set.py::test_fetch_source_text_legacy_chunk_id_fallback` | 무관 | ✅ pre-existing fail |
| `test_eval/test_build_synthetic_gold_set.py::test_make_graph_gold_item_falls_back_to_node_description` | 무관 | ✅ pre-existing fail |

모두 `tests/test_eval/` 영역. `indexing-improvement` 하네스의 **범위 가드**(SKILL.md):
> 다음은 별도 하네스 영역 — 이 워크플로우에서 다루지 않는다:
> - `scripts/eval_search.py` / `scripts/build_synthetic_gold_set.py` 변경
> - `src/context_loop/eval/*` 변경

`grep` 으로 확인 — 실패 테스트들은 `llm_body_extractor` / `extract_llm_body_graph` / `_assemble_document_body` / `pipeline.py` 어디도 import 하지 않음.

결론: **신규 fail 0건, 기존 fail 5건 (범위 외)**.

## 코드 변경 영역 vs 통과한 테스트

| 변경 파일 | 직접 커버하는 테스트 파일 | 통과 |
|----------|------------------------|------|
| `llm_body_extractor.py` | `test_llm_body_extractor.py` (27건) | ✅ |
| `pipeline.py` | `test_pipeline.py` (22건) | ✅ |
| (간접) `chunker.count_tokens` 사용 | `test_chunker.py` | ✅ (변경 없음) |

## 검증 시나리오 매트릭스

| 시나리오 | 검증 테스트 | 결과 |
|----------|------------|------|
| 문서 단위 LLM 호출이 기본 경로 | `test_llm_body_extraction_runs_when_enabled` (mock 호출 횟수 1) | ✅ |
| 입력 한도 초과 시 unit 폴백 | `test_llm_body_extraction_falls_back_to_units_when_oversized` (`InputTooLargeError` → unit 호출) | ✅ |
| 비-Confluence 경로 영향 없음 | `test_llm_body_extraction_skipped_when_no_client`, `..._when_disabled` | ✅ |
| sections 우선 plain_text 폴백 | `test_assemble_document_body_*` 2건 | ✅ |
| 어휘 외 entity/relation drop | `test_for_document_drops_unknown_vocabulary_and_dangling_endpoints` | ✅ |
| 단일 호출로 N entity 추출 | `test_for_document_single_call_extracts_all_entities` | ✅ |
| LLM 응답 실패 처리 | `test_for_document_failed_llm_returns_empty_with_failed_stat` | ✅ |
| 빈 입력 가드 | `test_for_document_empty_inputs_skip_llm_call` | ✅ |
| reasoning_mode off 전달 | `test_for_document_call_disables_thinking_mode` | ✅ |

## 회귀 점검

- 기존 `extract_llm_body_graph(units, ...)` 시그니처 무변경 → 기존 22건 unit 기반 테스트 그대로 통과.
- 기존 pipeline 흐름 중 LLM 분기만 변경 → 결정론 그래프(`body_extractor`, `link_graph_builder`, `ast_code_extractor`) 경로 영향 없음.
- 기본값 `max_input_tokens=200_000` 은 256K 컨텍스트 모델 안전 마진 — 모든 신규 테스트가 이 한도 안에서 작동.

## 후속 권고 (R3 후보)

검증 범위 외 후속 작업으로 분리:

1. **chunker 임베딩 단위 재검토** — 작은 문서(< 8K 토큰) 1 벡터 vs N 청크의 검색 정밀도 정량 평가 후 결정 (분석가 1, 2 보고서의 옵션 B 권고)
2. **MCP `context_max_tokens=4096` 가드 검토** — 256K 모델 환경에서는 이 가드가 응답 컨텍스트를 과도하게 잘라낼 가능성 (분석가 1)
3. **거대 입력에서 LLM 출력 안정성** — `guided_json` 또는 2-call 점진 출력 (분석가 3 F-CG2-04)
4. **git_code 파일 단위 LLM 의미 분석** — 현재 LLM 호출 0건. 파일 전체 컨텍스트에서 call graph / 상속 보강 (분석가 4 F-GG-04)

이들은 모두 별도 라운드로 분리 — 이번 라운드 핵심(문서 단위 LLM 그래프 추출)과 결합 없이 독립 진행 가능.
