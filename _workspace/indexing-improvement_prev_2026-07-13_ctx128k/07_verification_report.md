# Verification Report — 128K 컨텍스트 모델 대응 (R-1/R-2/R-3)

> 검증자: indexing-change-verifier · 날짜: 2026-07-13
> 입력: `00_round_scope.md`, `05_improvement_plan.md`, `06_implementation_report.md`, `git diff HEAD`(미커밋 작업트리)
> 원칙: 코드 미수정(판정·보고만), stash 검증 후 작업트리 원상복구 완료, git commit/push 없음.

## 한 줄 결론

**PASS-WITH-NOTES** — R-1/R-2/R-3 세 건 모두 계획대로 구현·배선되었고, 변경 영역 테스트 105건 전부 통과하며 신규 회귀·신규 린트 위반이 없다. 노트는 전부 pre-existing 이슈(테스트 실패 22건, 잔존 린트)와 의도적 dead-path 존치 1건으로, 이번 구현의 결함이 아니다.

## 변경 요약

- 변경 파일: 13개 (소스 8 + 테스트 5) + `config/default.yaml`
- 라인: **+792 / -29**
- 범위 밖(`src/context_loop/eval/*`, `scripts/eval_*`) diff **0** — 불가침 준수.

## 계획 vs 구현 매트릭스

| 계획 ID | 계획 동작 | 실제 변경 (검증) | 일치도 |
|--------|----------|-----------------|--------|
| R-1 설정키 | `config/default.yaml llm.max_input_tokens: 200000` + 주석 | L146~ 추가, 주석 포함, 기본 200000 | ✓ |
| R-1 필드 | `PipelineConfig.llm_max_input_tokens=200_000` | pipeline.py L92 추가 | ✓ |
| R-1 주입(질문) | 호출부에 `config=QuestionGenConfig(max_input_tokens=cfg.llm_max_input_tokens)` | pipeline.py L291~ | ✓ |
| R-1 주입(본문+unit폴백) | `body_cfg` 동일 인스턴스 문서·unit 폴백 모두 주입 | pipeline.py L469, L497(unit 폴백 `config=body_cfg`) | ✓ |
| R-1 배선 3곳 | coordinator/confluence_mcp/documents 에 `config.get("llm.max_input_tokens", 200_000)` | 3파일 모두 추가 | ✓ |
| R-1 mcp_sync 무변경 | 전달만 | 변경 없음 확인 | ✓ |
| R-2 판별 헬퍼 | `is_context_length_error` + 마커 상수 (llm_client.py 단일 출처) | L456~ 신설, 구조화 code + body(nested) + 메시지 마커 폴백 | ✓ |
| R-2 호출/파싱 분리 | 컨텍스트 초과만 `InputTooLargeError` 승격, 파싱실패는 degraded | llm_body_extractor.py 두 try 물리 분리(L336 호출 / L359 파싱) | ✓ |
| R-2 pipeline 폴백 | 기존 `except InputTooLargeError` 재사용, unit 폴백 라우팅 | 구조 변경 없이 `config=body_cfg`만 추가 | ✓ |
| R-3 배치 분할 | 결정론 탐욕 패킹, 단독 초과 절단, gather 순서 병합 | `_plan_section_batches`/`_truncate_to_tokens`/`_run_batched` 신설 | ✓ |
| R-3 dedup/총량상한 배치 공유 | `seen_global`/`total_emitted` 배치 간 공유 | `_merge_sections_into` 공용 헬퍼, `_run_batched`가 상태를 이어 전달 | ✓ |
| R-3 stats | `fallback_used/batch_count/sections_truncated` + pipeline 로그 노출 | QuestionGenStats L92~ + pipeline 로그 확장 | ✓ |
| R-3 트리거 2경로 | 사전 가드 + API 컨텍스트 에러 모두 배치로 self-heal | L200(사전) / L228(API except) 분기 | ✓ |

계획에 있으나 미구현된 항목: **없음.** 계획에 없는 추가 변경: `_plan_section_batches`에 `stats` 인자 추가 등 6건의 세부 조정이 있으나 모두 06 보고서에 명시되었고 계획 의도 내 개선(파싱 분리 강화, 부분 배치 실패 격리 등)으로 합당.

## 상세 검증 (요청 항목별)

### (a) R-1 미설정 시 기본값 200000 → 기존 동작 동일 — 확인
config 기본 200000 = `PipelineConfig.llm_max_input_tokens` 기본 200_000 = `QuestionGenConfig`/`LLMBodyExtractionConfig` dataclass 기본 200_000. 세 계층 모두 동일 상수. 5개 `PipelineConfig(` 생성부 중 배선 3곳은 `config.get(..., 200_000)` 폴백, 나머지 2곳(`pipeline.py:138`, `mcp_sync.py:336`)은 `PipelineConfig()` 기본 사용 — 전 경로가 200000 로 수렴. `test_default_llm_max_input_tokens` 통과로 고정.

### (b) R-2 일반 400/파싱 실패가 컨텍스트 초과로 오분류되지 않음 — 확인
`is_context_length_error`는 컨텍스트 특정 마커/구조화 code 에만 True. `extract_llm_body_graph_for_document`가 LLM 호출 try(L336)와 JSON 파싱 try(L359)를 **물리적으로 분리** → 파싱 `ValueError`는 승격 경로에 도달 불가, 무조건 degraded(`units_failed=1`). 테스트 `test_doc_call_generic_error_degraded_not_raised`, `test_doc_call_json_parse_failure_still_degraded`, `test_generic_400_false` 통과.

### (c) R-3 결정론·총량상한·배치 간 dedup — 확인
- **결정론**: `_plan_section_batches`가 문서 순서 유지 탐욕 패킹, `asyncio.gather`가 입력 순서로 결과 보존, 모든 호출 `temperature=cfg.temperature`(0.0). `test_plan_section_batches_deterministic`가 반복 동일성 검증.
- **총량상한**: `total_emitted`를 배치 간 이어 전달, `_merge_sections_into` 내부·`_run_batched` 배치 루프 양쪽에서 `max_questions_per_doc` break. `test_batched_respects_max_questions_per_doc`(5×3 반환 → 상한 2 컷) 통과.
- **배치 간 dedup**: `seen_global`을 `_run_batched`에서 1회 생성해 모든 배치 병합에 공유. `test_batched_dedup_across_batches`(3배치 동일 질문 → 1개) 통과.

## 배선 완결성 — 확인

`grep 'PipelineConfig('` 전수 5곳:
| 위치 | 처리 | 판정 |
|------|------|------|
| coordinator.py:303 | 필드 배선 추가 | ✓ |
| confluence_mcp.py:519 (`_build_pipeline_config`, 주경로) | 필드 배선 추가 | ✓ |
| documents.py:488 (단건 재처리) | 필드 배선 추가 | ✓ |
| pipeline.py:138 (`config or PipelineConfig()`) | 기본값 폴백(의도적) | ✓ |
| mcp_sync.py:336 (`pipeline_config or PipelineConfig()`) | 전달/기본(계획대로 무변경) | ✓ |

데이터 흐름 `config.yaml → Config.get → PipelineConfig.llm_max_input_tokens → process_document → QuestionGenConfig/LLMBodyExtractionConfig(문서+unit 폴백 동일 인스턴스)` 전 구간 연결 확인. `test_pipeline_injects_llm_max_input_tokens_to_question_cfg`/`..._to_body_cfg`가 값 전달(123)을 캡처 검증.

## 테스트 결과 (직접 실행 · `.venv`, pytest 9.1.1, openai 2.45.0)

| 명령 | 결과 |
|------|------|
| `pytest test_question_generator + test_llm_body_extractor + test_llm_client + test_pipeline + test_config` | **105 passed, 0 failed** |
| `pytest tests/test_processor` | 300 passed, **2 failed** (test_extraction_unit) |
| `pytest tests/test_ingestion tests/test_sync tests/test_web` | 391 passed, **20 failed** (test_web) |

### 신규/기존 실패 책임 구분 (git stash → HEAD 재현)
- 작업트리 실패 = **22건** (test_web 20 + test_extraction_unit 2).
- `git stash push`로 HEAD 상태 재현 후 동일 대상 실행 → **22 failed** 동일 (test_web 20 `TypeError: unhashable type: 'dict'` jinja2 LRUCache 이슈 + test_extraction_unit 2).
- **신규 실패 = 0. 기존(pre-existing) 실패 = 22.** 06 보고서 주장(test_web 20 + test_extraction_unit 2)과 정확히 일치.
- stash pop 으로 작업트리 완전 복원 확인(diff `+792/-29` 재확인).

## 엣지 케이스 검토

| 케이스 | 코드 동작 | 판정 |
|--------|-----------|------|
| 빈 섹션 문서 | `_assemble_sections_payload` 빈 → L188 조기 `return {}` (배치·토큰검사 이전) | ✓ 안전 |
| 단일 섹션 한도 초과 | `_plan_section_batches`가 단독 배치 + head-절단, `sections_truncated += 1`. `test_single_section_over_budget_truncated` 통과 | ✓ |
| InputTooLargeError 경로 stats 일관성 | 승격 시 예외 전파 → pipeline `except`가 `extract_llm_body_graph(units)` 로 `llm_stats` 재산출(문서 단위 stats 폐기) → 이중 계산 없음 | ✓ |
| unit 폴백 재실패 → degraded | `extract_llm_body_graph` 내부 per-unit `except`가 `units_failed` 증가 → pipeline `llm_degraded` 노출. 폴백의 폴백 없음(계획대로) | ✓ |
| 배치 전부 실패 | `gather(return_exceptions=True)` 격리, `batches_failed == len(batches)` 시에만 `llm_failed=True`(부분 실패는 성공분 유지). `test_generic_api_error_still_returns_empty`로 단일 호출 일반 예외 계약 검증 | ✓ |

배치 파티션은 섹션을 disjoint 하게 나누므로 `result[section_index]` 덮어쓰기 위험 없음(각 섹션 정확히 1배치). 유일한 이론적 취약점: 특정 배치의 LLM 응답이 다른 배치 소속 `section_index`를 환각하면 덮어쓰기 가능 — 그러나 프롬프트에 해당 배치 섹션만 제공되므로 발생 가능성 낮고, 발생해도 데이터 손상·크래시 없음(질문 카운트 경미 오차뿐). 회귀 아님, 후속 관찰 권고 수준.

## 린트 (ruff) — 신규 위반 0

변경 12개 소스/테스트 파일 대상 working-tree vs HEAD 위반 카운트 비교:
- **HEAD**: 6 E501, 2 F401, 1 F841, 2 I001, 1 N806
- **작업트리**: 6 E501, 1 F841, 1 I001, 1 N806

작업트리 위반은 HEAD 위반의 **진부분집합** — 신규 위반 0건, 오히려 pre-existing **F401 2건 + I001 1건 해소**(R-1이 `QuestionGenConfig`를 실사용하며 자연 해소). 잔존 위반(E501 6, F841 1, I001 1, N806 1)은 전부 HEAD 에도 존재하며 편집 라인과 무관한 스코프 밖 이슈. 06 보고서 린트 주장과 일치.

## 회귀 위험

- 정상 경로(한도 이내·API 성공)는 R-2/R-3 폴백 미발동 — 관측 가능한 출력 차이 없음. 기존 정상-경로 테스트 전부 통과로 확인.
- `frozen` dataclass override는 새 인스턴스 생성으로 처리(수정 아님).
- pipeline `except QuestionInputTooLargeError`는 R-3 self-heal 이후 dead-path 이나 방어적 존치(06 조정사항 5) — 무해, 계획 승인 범위.

## 후속 권고 (비차단)

1. 배치 응답이 타 배치 소속 `section_index`를 반환하는 환각 케이스에 대한 방어(배치별 valid 인덱스로 제한)를 다음 라운드에서 고려 — 현재 위험 낮음.
2. pre-existing test_web 20건(jinja2/starlette 버전 조합 `unhashable dict`)·test_extraction_unit 2건은 본 라운드 밖이나 리포 위생 차원에서 별도 처리 권고.
3. 메트릭 영향(eval_search) 평가는 별도 하네스 영역 — 본 검증 범위 밖, 권고만.

## 검증 무결성

- 코드 직접 수정 없음(판정·보고만).
- `git stash push/pop`으로 HEAD 비교 후 작업트리 `+792/-29` 완전 복원 확인.
- git commit/push 없음.
