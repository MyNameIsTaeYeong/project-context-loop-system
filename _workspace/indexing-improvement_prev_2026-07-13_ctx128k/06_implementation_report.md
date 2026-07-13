# Implementation Report — R-1 / R-2 / R-3 (128K 컨텍스트 모델 대응)

> 입력: `05_improvement_plan.md`, `00_round_scope.md`.
> 범위: R-1(설정화)·R-2(본문그래프 폴백)·R-3(가상질문 배치 폴백) 3건 전부 구현.
> 제약 준수: `src/context_loop/eval/*`, `scripts/eval_*` **무변경**(git status 확인 완료).

## 적용 항목

| ID | 파일 | 변경 요약 |
|----|------|----------|
| R-2 | `src/context_loop/processor/llm_client.py` | `_CONTEXT_OVERFLOW_MARKERS`(L458) + `is_context_length_error()`(L470) 신설 — 컨텍스트 초과 판별 단일 출처. |
| R-1 | `config/default.yaml` | `llm:` 블록에 `max_input_tokens: 200000`(L154) 추가 + 안내 주석. |
| R-1 | `src/context_loop/processor/pipeline.py` | `PipelineConfig.llm_max_input_tokens=200_000`(L95) 필드. `LLMBodyExtractionConfig` import 추가(L45). 질문 호출부에 `config=QuestionGenConfig(max_input_tokens=cfg.llm_max_input_tokens)`(L291), 본문그래프 문서·unit 폴백 호출부에 `body_cfg`(L469, L478, L500) 주입. 질문 stats 로그에 `fallback/batches` 노출(L288~). |
| R-2 | `src/context_loop/processor/llm_body_extractor.py` | `is_context_length_error` import(L38). `extract_llm_body_graph_for_document`에서 LLM 호출/JSON 파싱을 **분리**(L334~) — 호출 예외가 컨텍스트 초과면 `InputTooLargeError`로 승격(L346~351), 그 외 일반 실패·파싱 실패는 기존대로 degraded(L365~371). |
| R-3 | `src/context_loop/processor/question_generator.py` | `asyncio`·`_get_tokenizer`·`is_context_length_error` import. 예산 상수 `_TEMPLATE_SLACK/_SAFETY_MARGIN/_MIN_BUDGET`. `QuestionGenStats`에 `fallback_used/batch_count/sections_truncated`(L92~94). 진입점 self-heal: 사전 가드 초과(L200) 또는 API 컨텍스트 에러(L228) → `_run_batched`. 신규 헬퍼 `_merge_sections_into`(L283, 단일/배치 공용), `_truncate_to_tokens`(L362), `_plan_section_batches`(L379, 결정론 탐욕 패킹+단독 절단), `_run_batched`(L441, gather 병렬·순서 병합). |
| R-1 | `src/context_loop/web/api/confluence_mcp.py` | `_build_pipeline_config`에 `llm_max_input_tokens=config.get("llm.max_input_tokens", 200_000)`(L525). |
| R-1 | `src/context_loop/web/api/documents.py` | `_run_pipeline`의 `PipelineConfig`에 동일 배선(L492). |
| R-1 | `src/context_loop/ingestion/coordinator.py` | git_code 경로 `PipelineConfig`에 동일 배선(L309~311, 균일성 — LLM 미사용이라 실효 없음, 무해). |

`sync/mcp_sync.py`는 상위에서 만든 `pipeline_config`를 전달만 하므로 **무변경**(계획대로).

## 계획 대비 조정 사항

계획서의 가정은 실제 코드와 일치했으며 설계를 무효화하는 오류는 없었다. 아래는 세부 조정.

1. **`extract_json` 예외 처리 정합 (R-2)** — 계획서 R-2 §2 의사코드는 `is_context_length_error(exc)`를 `complete()` except에서만 검사한다. 실제 코드에서 `extract_json`도 `complete()`와 같은 try 안에 있었으므로, 계획대로 **호출 try와 파싱 try를 물리적으로 분리**했다. 이로써 파싱 실패(`ValueError`)가 컨텍스트 초과로 오분류되지 않음이 구조적으로 보장된다(신규 테스트 `test_doc_call_json_parse_failure_still_degraded`로 검증).

2. **`_plan_section_batches`에 `stats` 인자 추가** — 계획서 시그니처는 `_plan_section_batches(sections_payload, *, doc_title, cfg)`였으나, 단독 섹션 절단 시 `stats.sections_truncated`를 증가시켜야 하므로 `stats: QuestionGenStats` 인자를 추가했다(순수성은 유지 — stats만 부수효과). 테스트는 fresh `QuestionGenStats()`를 넘겨 결정성을 검증한다.

3. **토큰 절단 헬퍼는 `chunker._get_tokenizer` 재사용** — 계획서는 "tiktoken 인코딩 후 head 유지, 디코드"만 명시. `count_tokens`와 round-trip 일관성을 위해 chunker의 (모듈-프라이빗) `_get_tokenizer`를 import해 `_truncate_to_tokens`를 구현했다(재구현 대신 단일 인코더 재사용). 동일 패키지 내 참조이며 ruff 통과.

4. **`_run_batched` 배치 실패 처리 구체화** — 계획서는 "배치조차 전부 실패하는 극단 상황"만 언급. `asyncio.gather(..., return_exceptions=True)`로 배치별 실패를 격리하고, **모든 배치가 실패한 경우에만** `stats.llm_failed=True`로 표시하도록 구현(부분 실패는 성공 배치 결과 유지). 신규 테스트 `test_generic_api_error_still_returns_empty`는 폴백 이전 단일 호출 일반 예외 경로(기존 계약)를 검증.

5. **`InputTooLargeError`(question_generator)는 존치** — R-3로 함수가 더 이상 이 예외를 raise하지 않지만, 클래스와 pipeline의 `except QuestionInputTooLargeError`(방어적 안전망)는 계획대로 유지. pipeline은 배치가 self-heal하므로 실질 도달하지 않는 dead-path이나 제거하지 않았다.

6. **pipeline F401 자동 해소** — 기존 pipeline.py는 `QuestionGenConfig`를 import만 하고 사용하지 않아 latent F401(HEAD 기준)이 있었다. R-1에서 실제 사용하게 되어 이 pre-existing 린트가 자연 해소됐다.

## 신규/조정 테스트

**R-2 판별 헬퍼 — `tests/test_processor/test_llm_client.py`** (신규, `TestIsContextLengthError`)
- `test_openai_code` — `BadRequestError(body={"code": "context_length_exceeded"})` → True
- `test_openai_nested_error_code` — `body={"error":{"code":...}}` 중첩 code → True
- `test_vllm_message` — vLLM "maximum context length / longer than the maximum model length" 메시지 → True
- `test_generic_400_false` — 컨텍스트 무관 400(temperature) → False
- `test_non_openai_exceptions_false` — `TimeoutError`/`ValueError("boom")` → False
- `test_non_openai_but_context_message_true` — 비-openai 예외라도 마커 명확 시 True

**R-2 승격 — `tests/test_processor/test_llm_body_extractor.py`** (신규)
- `test_doc_call_context_error_raises_input_too_large` — 컨텍스트 초과 예외 → `InputTooLargeError` 승격
- `test_doc_call_generic_error_degraded_not_raised` — 일반 예외 → 승격 없이 `(빈 GraphData, units_failed=1)`
- `test_doc_call_json_parse_failure_still_degraded` — 비-JSON 응답 → 승격 없이 degraded

**R-3 배치 폴백 — `tests/test_processor/test_question_generator.py`**
- `test_oversized_input_triggers_batched_fallback` — **기존 `test_oversized_input_raises_input_too_large` 대체**(의도된 동작 변경): 사전 가드 초과 → raise 대신 배치, `fallback_used=True`, `batch_count>=2`, 병합 결과
- `test_batched_dedup_across_batches` (신규) — 배치 간 동일 질문 → `seen_global`로 1개 유지
- `test_batched_respects_max_questions_per_doc` (신규) — 문서 전체 상한에서 컷
- `test_single_section_over_budget_truncated` (신규) — 단독 거대 섹션 절단, `sections_truncated=1`, 1배치
- `test_api_context_error_triggers_batched_fallback` (신규) — 첫 전체 호출 컨텍스트 초과 → 배치 self-heal
- `test_generic_api_error_still_returns_empty` (신규) — 첫 호출 일반 예외 → 폴백 아님, `llm_failed=True`
- `test_plan_section_batches_deterministic` (신규, 순수 함수) — 문서 순서 유지·배치 예산 이하·반복 시 동일

**R-1 배선 — `tests/test_processor/test_pipeline.py`** (신규)
- `test_pipeline_injects_llm_max_input_tokens_to_question_cfg` — `QuestionGenConfig.max_input_tokens==123` 전달 검증
- `test_pipeline_injects_llm_max_input_tokens_to_body_cfg` — 문서 단위 + unit 폴백 모두 `LLMBodyExtractionConfig.max_input_tokens==123`
- 기존 `test_llm_body_extraction_falls_back_to_units_when_oversized` — R-2 경로 회귀 커버(변경 없이 통과)

**R-1 설정 — `tests/test_config.py`** (신규)
- `test_default_llm_max_input_tokens` — `Config().get("llm.max_input_tokens")==200000`

## 즉시 실행한 테스트 결과

실행 환경: 프로젝트 의존성 미설치 상태였으므로 `uv venv` + `uv pip install -e ".[dev]"`로 dev 의존성 설치 후 실행.

- 변경 영역(집중): `pytest test_question_generator + test_llm_body_extractor + test_llm_client + test_pipeline + test_config` → **105 passed, 0 failed**
- 배선 회귀: `pytest tests/test_ingestion tests/test_sync tests/test_web` → 391 passed, **20 failed**
- 전체 processor: `pytest tests/test_processor` → 300 passed, **2 failed**

### 실패 분석 — 전부 pre-existing, 본 변경과 무관 (git stash로 HEAD 재현 확인)
- **test_web 20건**: `TypeError: unhashable type: 'dict'` — jinja2 LRUCache 템플릿 렌더 이슈(설치된 jinja2/starlette 버전 조합). HEAD에서도 동일 실패. 본 변경은 템플릿을 건드리지 않음.
- **test_extraction_unit 2건** (`test_short_parent_body_absorbed_into_first_child`, `test_long_parent_body_emitted_as_standalone_unit`): HEAD에서도 실패. `extraction_unit.py`는 본 변경 대상 아님.

## 린트 (ruff) 결과

- 변경한 4개 processor 파일 중 `llm_client.py` / `llm_body_extractor.py` / `question_generator.py` + 신규/수정 테스트 5개 파일: **신규 위반 0건**.
- `pipeline.py`: 잔존 `I001`(import 블록 정렬)은 **pre-existing** — `confluence_extractor`의 `extract as ...` 별칭 결합 import 스타일에 대한 ruff 기본 isort 지적으로, 리포 전반이 쓰는 관례이며 HEAD에서도 동일 발생. 리포 컨벤션 유지를 위해 자동수정을 적용하지 않음(적용 시 별칭 import가 별도 블록으로 분리되어 리포 스타일과 불일치). 본 변경은 오히려 pre-existing `F401`(미사용 `QuestionGenConfig`)을 해소.
- 배선 3파일(`coordinator.py` N806, `confluence_mcp.py` 6×E501, `documents.py` F841)의 잔존 위반: 모두 **pre-existing**이며 내가 편집한 라인과 무관(추가한 라인은 각각 L309~311 / L525 / L492). 스코프 밖 리팩터를 섞지 않기 위해 미수정.

## 하위 호환

- **R-1**: `llm.max_input_tokens` 기본 200000 → 두 dataclass 기본값과 동일 → 가드 발동 지점 불변(기존과 완전 동일). `test_default_llm_max_input_tokens`로 고정.
- **R-2/R-3**: 정상 경로(한도 이내·API 성공)에서 관측 가능한 차이 없음 — 오직 에러 경로만 개선. 기존 정상-경로 테스트 전부 통과로 회귀 없음 확인.

## 신규 발견

- (해당 없음 — 계획 범위 내에서 완결. 단, 위 "실패 분석"의 pre-existing 테스트 실패 2종은 본 라운드와 무관하나 리포 상태로 기록해 둔다.)

## 추천 커밋 메시지

```
feat(indexing): make LLM input limit configurable + add context-overflow fallbacks

- R-1: expose llm.max_input_tokens (default 200000, backward-compatible) and
  wire it through PipelineConfig into QuestionGenConfig / LLMBodyExtractionConfig
- R-2: promote API-level context-overflow errors in the document-level body
  graph extractor to InputTooLargeError so the existing unit fallback triggers
  (split LLM call from JSON parsing to avoid misclassifying parse failures)
- R-3: self-heal oversized question generation via deterministic section batch
  splitting (greedy packing, single-section truncation) with fallback stats
- add is_context_length_error() as the single source for overflow detection
```
