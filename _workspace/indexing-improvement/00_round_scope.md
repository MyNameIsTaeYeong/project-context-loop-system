# 라운드 범위 — 128K 컨텍스트 모델 대응 (2026-07-13)

## 배경

현재 인덱싱 LLM 호출 2곳(가상 질문 생성, LLM 본문 그래프)은 256K 컨텍스트 모델을
가정하고 `max_input_tokens=200_000` 하드코딩 가드를 사용한다. 128K 모델 사용 시
다음 문제가 발생한다 (상세: `_workspace/indexing-analysis/03_llm_usage_in_indexing.md`).

### 확인된 실패 모드 (코드 라인 기준)

1. **가드 사각지대**: 출력 예산 `max_tokens=32768` 을 빼면 128K 모델의 실질 입력
   한도는 ~95K 토큰. 그러나 가드는 200K(tiktoken 추정)에서만 발동
   (`question_generator.py:61` `QuestionGenConfig.max_input_tokens`,
   `llm_body_extractor.py:87` `LLMBodyExtractionConfig.max_input_tokens`).
   → **~95K~200K 구간 문서는 가드 통과 후 API 컨텍스트 초과 에러**.

2. **본문 그래프 폴백 미발동 (역전 현상)**: `pipeline.py:475` 의 unit 폴백은
   `InputTooLargeError`(사전 토큰 추정)에만 반응. 95K~200K 문서의 API 에러는
   `extract_llm_body_graph_for_document` 내부 `except Exception`
   (llm_body_extractor.py:344)이 삼켜 빈 GraphData 반환 → 폴백 없이 그래프 0건.
   200K 초과 문서는 오히려 unit 폴백으로 그래프가 생성되는 역전.

3. **가상 질문 폴백 부재**: `generate_questions_for_document` 는 입력 초과 시
   (사전 가드든 API 에러든) 질문 생성이 통째로 스킵됨. 섹션 단위 분할 호출 같은
   폴백이 없다.

4. **설정 불가**: 두 `max_input_tokens` 모두 dataclass 기본값이며 pipeline.py 는
   config 인자 없이 호출 (pipeline.py:281, 460). `config/default.yaml` `llm:`
   블록에 컨텍스트 한도 항목 없음 → 모델 교체 시 코드 수정 필요.

## 이번 라운드 요구사항 (사용자 승인 완료 — 3건)

### R-1. `max_input_tokens` 를 config 로 노출
- `config/default.yaml` `llm:` 블록에 컨텍스트 한도(예: `max_input_tokens` 또는
  `context_window` + 파생) 추가.
- 설정값이 `QuestionGenConfig` / `LLMBodyExtractionConfig` 까지 주입되는 배선:
  app.py(설정 로드) → coordinator / mcp_sync → `process_document` →
  `generate_questions_for_document(config=...)` /
  `extract_llm_body_graph_for_document(config=...)`.
- 기본값은 기존 동작 보존 (미설정 시 200K).

### R-2. 본문 그래프: API 컨텍스트 초과 에러 → unit 폴백 연결
- `extract_llm_body_graph_for_document` 가 API 레벨 컨텍스트 초과를 삼키지 않고
  호출자(pipeline)가 unit 폴백으로 전환할 수 있게 한다.
- 컨텍스트 초과 판별: openai SDK BadRequestError(400) 중 context/token 관련
  메시지 식별 (vLLM/OpenAI 호환 서버의 에러 메시지 패턴 고려). 판별 불가한
  일반 실패는 기존대로 degraded 처리 (폴백 남용 금지).
- unit 폴백은 기존 `extract_llm_body_graph(units, ...)` 재사용.

### R-3. 가상 질문: 입력 초과 시 섹션 분할 호출 폴백
- 문서 전체가 한도 초과일 때 (사전 가드 or API 에러), 섹션들을 한도 이하
  배치(batch)로 묶어 여러 번 호출하고 결과 매핑을 병합.
- 배치 간 중복 질문 제거는 기존 `seen_global` 방식과 일관되게.
- `max_questions_per_doc` 총량 상한은 문서 전체 기준으로 유지.
- stats 에 폴백 발생 여부/배치 수 기록.

## 제약

- 기존 테스트 회귀 금지. 신규 동작은 테스트 추가.
- `scripts/eval_*`, `src/context_loop/eval/*` 는 범위 밖 (별도 하네스 영역).
- 미설정 시(256K 가정 유지) 기존 동작과 완전히 동일해야 함.
- 결정성 유지: temperature=0.0, 배치 분할은 결정론적 (섹션 순서 기반).

## 진행 상태

- [x] Phase 0: 범위 확정 (본 문서)
- [x] Phase A: 분석 — 기존 `_workspace/indexing-analysis/03_llm_usage_in_indexing.md` 로 대체 (스킵)
- [ ] Phase B: `05_improvement_plan.md`
- [ ] Phase C: 구현 + `06_implementation_report.md`
- [ ] Phase D: 검증 + `07_verification_report.md`
