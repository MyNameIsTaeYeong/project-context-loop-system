# 2026-04-01 세션: LLM JSON 추출 실패 수정 (Qwen3 추론 모델 대응)

## 배경

- `graph_search_planner.py`의 `plan_graph_search()` 메서드(121번 라인)에서 LLM 응답의 JSON 추출(`extract_json()`) 실패가 빈번하게 발생
- 원인: Qwen3-235B 모델이 응답에 `<think>...</think>` 태그로 추론 과정을 포함하여 반환하기 때문
- `extract_json()`의 정규식이 `<think>` 블록 내부의 텍스트를 JSON 후보로 잘못 매칭하거나, 전체 파싱 실패

## 검토한 해결 방법

| 방법 | 설명 | 장점 | 단점 |
|:---:|---|---|---|
| A | `LLMClient.complete()`에 `**kwargs` 추가, `extra_body`로 thinking 비활성화 전달 | 호출부에서 1회성 제어 가능, 불필요한 토큰 소모 방지 | ABC 인터페이스 변경 필요 |
| B | `complete()`에 `extra_body` 명시적 파라미터 추가 | 타입 안전 | A와 동일하게 인터페이스 변경 필요 |
| C | `extract_json()`에서 `<think>` 태그 제거 | 클라이언트 코드 변경 불필요, 방어적 | 불필요한 추론 토큰 소모 (근본 해결 아님) |
| D | 시스템 프롬프트에 `/nothink` 지시 | 코드 변경 최소화 | 모델 의존적, 보장 안됨 |

**채택: 방법 A** — API 레벨에서 근본적으로 thinking을 비활성화

## 작업 내역

### LLMClient.complete()에 `**kwargs` 지원 추가

**변경 파일**: `src/context_loop/processor/llm_client.py`

- **LLMClient ABC**: `complete()` 시그니처에 `**kwargs: Any` 추가
- **AnthropicClient, OpenAIClient**: `**kwargs` 수신하되 사용하지 않음 (호환성 유지)
- **EndpointLLMClient**: `**kwargs`에서 `extra_body` 키가 있으면 OpenAI SDK의 `chat.completions.create()`에 `extra_body`로 전달
- 기존 6개 호출부(`chat.py`, `classifier.py`, `reranker.py`, `query_expander.py`, `graph_extractor.py` x2)는 `**kwargs`를 넘기지 않으므로 영향 없음

### graph_search_planner.py에서 thinking 비활성화

**변경 파일**: `src/context_loop/processor/graph_search_planner.py` (121번 라인)

- `llm_client.complete()` 호출 시 `extra_body={"chat_template_kwargs": {"enable_thinking": False}}` 전달
- Qwen3 모델이 `<think>` 블록 없이 JSON만 응답하도록 처리
- vLLM 등 OpenAI 호환 서버에서 `extra_body`를 요청 body에 포함하여 전달

## 알려진 제한사항

- `chat_template_kwargs.enable_thinking`은 Qwen3 전용 파라미터. 다른 모델로 교체 시 해당 파라미터가 무시될 수 있음 (에러는 발생하지 않음)
- 모델 교체 시 추론 블록이 다시 포함될 가능성이 있으므로, `extract_json()`에 `<think>` 태그 제거 방어 로직 추가를 권장

## 테스트 결과

- `test_graph_search_planner.py`: 14개 전체 통과
- 전체 테스트: processor/storage/ingestion 등 267개 통과 (web 14개 실패는 기존 Jinja2 이슈로 본 변경과 무관)
