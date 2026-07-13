# Indexing Improvement Plan — 128K 컨텍스트 모델 대응 (R-1/R-2/R-3)

> 입력: `00_round_scope.md`(범위·요구사항 3건), `../indexing-analysis/03_llm_usage_in_indexing.md`(근거).
> 본 라운드는 통상의 4개 분석 보고서 대신 위 2개 문서를 분석 입력으로 삼는다.
> 이 문서는 **설계만** 담는다. 코드 수정은 implementer 담당.
> 범위 밖(절대 미변경): `src/context_loop/eval/*`, `scripts/eval_*`.

## 입력 요약

| 입력 | 핵심 |
|------|------|
| 00_round_scope | 128K 모델에서 `max_input_tokens=200_000` 하드코딩 가드가 사각지대(≈95K~200K)를 만듦. 승인된 요구사항 3건(R-1 설정화, R-2 본문그래프 API 초과→unit 폴백, R-3 가상질문 섹션분할 폴백). |
| 03_llm_usage_in_indexing | 인덱싱 생성형 LLM 호출은 confluence 경로 문서당 최대 2회: ① 가상 질문 생성(`question_generator.py`), ② 본문 의미 그래프(`llm_body_extractor.py`). git_code 는 LLM 미사용. |

## 요구사항 → 변경 매트릭스

| ID | 영역 | 핵심 변경 파일 | 영향 | 공수 | 설정 게이팅 | 라운드 |
|----|------|----------------|------|------|-------------|--------|
| R-1 | 설정화 | `config/default.yaml`, `pipeline.py`(PipelineConfig+주입), 4개 PipelineConfig 생성부 | High | S | 예(값) | R1 |
| R-2 | 본문그래프 폴백 | `llm_client.py`(판별 헬퍼), `llm_body_extractor.py`, `pipeline.py`(config 주입) | High | M | 아니오(항상 활성) | R1 |
| R-3 | 가상질문 폴백 | `question_generator.py`(배치 분할), `pipeline.py`(config 주입) | High | M | 아니오(항상 활성) | R1 |

이번 라운드 = R1 에 3건 전부. R2(후속) 없음.

## 변경 파일 수 점검 (≤5 권고 준수)

핵심 소스 5개: `config/default.yaml`, `processor/pipeline.py`, `processor/llm_client.py`, `processor/llm_body_extractor.py`, `processor/question_generator.py`.
추가로 **동일 1줄 주입**만 하는 배선부 3곳(리스크 낮음, 한 묶음): `ingestion/coordinator.py`, `web/api/confluence_mcp.py`, `web/api/documents.py`. `sync/mcp_sync.py` 는 `pipeline_config` 를 그대로 전달만 하므로 **무변경**.

---

## 현행 구조 확인 (근거 라인)

### 설정 로드·전달 경로 (실측)
- `Config` (`config.py`): `config/default.yaml` + `~/.context-loop/config.yaml` deep-merge, `config.get("dotted.key", default)` 접근 (config.py:42~60).
- `_build_llm_client` (`web/app.py:66`): `llm.provider/endpoint/model/api_key/headers/reasoning_profiles` 만 읽어 클라이언트 생성. **컨텍스트 한도 항목 없음** — R-1 이 추가.
- LLM 클라이언트 주입: `app.state.llm_client` → `coordinator`(생성자, coordinator.py:93,104) / `mcp_sync`(execute_sync_target, mcp_sync.py:262,343) → `process_document(llm_client=...)`.
- `PipelineConfig` 생성부는 **4곳**이며 현재 모두 `chunk_size/chunk_overlap/embedding_model` 3개만 채운다:
  1. `ingestion/coordinator.py:303` (git_code 경로 — LLM 미사용이나 config 는 생성)
  2. `web/api/confluence_mcp.py:517` `_build_pipeline_config` (confluence_mcp 백그라운드 싱크 주경로)
  3. `web/api/documents.py:488` (단건 재처리)
  4. (`mcp_sync.py:336` 은 전달받은 `pipeline_config or PipelineConfig()` — 자체 생성 아님)

### R-1 이 메꿔야 할 실제 gap (중요)
`pipeline.py` 는 두 LLM 진입점을 **config 인자 없이** 호출한다:
- `generate_questions_for_document(doc_title=title, extracted=extracted, llm_client=llm_client)` — pipeline.py:281~287 (config 미전달 → `QuestionGenConfig()` 기본값 200K 사용)
- `extract_llm_body_graph_for_document(doc_title=title, body=doc_body, llm_client=llm_client)` — pipeline.py:460~466 (config 미전달 → `LLMBodyExtractionConfig()` 기본값 200K 사용)
- unit 폴백 `extract_llm_body_graph(units, doc_title=title, llm_client=llm_client)` — pipeline.py:482~484 (동일)

즉 지금은 `PipelineConfig` 에 한도를 넣어도 두 dataclass 로 흘러가지 못한다. R-1 은 (a) 한도를 설정에서 읽어 `PipelineConfig` 에 싣고 (b) pipeline 호출부에서 `QuestionGenConfig`/`LLMBodyExtractionConfig` 로 **주입**하는 배선을 완성해야 한다.

---

## R-1. `max_input_tokens` 를 config 로 노출

### 설계 결정: 직접 스칼라 `llm.max_input_tokens`
`context_window` + 파생(`max_input = context_window - max_output - margin`) 방식도 검토했으나 기각한다. 이유: 기존 두 dataclass 필드가 이미 `max_input_tokens` 스칼라이므로 1:1 매핑이 가장 단순하고, 파생식은 `max_output_tokens`(각 dataclass 별로 다름)와의 결합을 새로 만든다. 운영자는 "모델 컨텍스트 − 출력예산 − 마진"을 스스로 계산해 한 숫자로 넣는다(주석으로 안내). 128K 모델 → `90000`, 256K 모델 → `200000`.

### 1) `config/default.yaml` — `llm:` 블록에 키 추가
`classifier_prompt: "default"` 다음(라인 148 부근)에 삽입:
```yaml
  # 인덱싱 LLM 호출(가상 질문 생성·LLM 본문 그래프)의 문서 입력 토큰 상한.
  # 모델 컨텍스트 윈도우에서 출력 예산(약 32768)과 안전 마진을 뺀 값으로 설정한다.
  #   예) 128K 모델 → 90000,  256K 모델 → 200000.
  # 이 한도를 넘는 문서는 R-3(가상질문=섹션 배치 분할)·R-2(본문그래프=unit 폴백)로 전환된다.
  # 미설정 시 200000 (기존 256K 가정 동작과 완전히 동일).
  max_input_tokens: 200000
```
기본값을 **200000** 으로 두는 것이 하위 호환의 핵심(§하위호환 참조).

### 2) `PipelineConfig` 에 필드 추가 (`processor/pipeline.py:64~90`)
```python
    # 인덱싱 LLM 입력 토큰 상한. QuestionGenConfig / LLMBodyExtractionConfig 로
    # 주입되어 문서 단위 1회 호출의 가드 및 R-2/R-3 폴백 전환 기준이 된다.
    # 기본 200000 = 기존 하드코딩 가드값과 동일(하위 호환).
    llm_max_input_tokens: int = 200_000
```

### 3) pipeline 호출부에서 두 dataclass 로 주입 (핵심 배선)
- 가상 질문 (pipeline.py:281):
  ```python
  from context_loop.processor.question_generator import QuestionGenConfig  # 이미 import 됨
  question_map, q_stats = await generate_questions_for_document(
      doc_title=title,
      extracted=extracted,
      llm_client=llm_client,
      config=QuestionGenConfig(max_input_tokens=cfg.llm_max_input_tokens),
  )
  ```
- 본문 그래프 문서 단위 (pipeline.py:460):
  ```python
  from context_loop.processor.llm_body_extractor import LLMBodyExtractionConfig
  body_cfg = LLMBodyExtractionConfig(max_input_tokens=cfg.llm_max_input_tokens)
  llm_graph, llm_stats = await extract_llm_body_graph_for_document(
      doc_title=title, body=doc_body, llm_client=llm_client, config=body_cfg,
  )
  ```
  unit 폴백(pipeline.py:482)에도 `config=body_cfg` 를 전달(동일 인스턴스 재사용).
- `QuestionGenConfig`/`LLMBodyExtractionConfig` 의 다른 필드는 기본값 유지 → 한도만 override. dataclass 가 `frozen=True` 이므로 새 인스턴스 생성 방식이 맞다.

### 4) 4개 PipelineConfig 생성부에 설정값 로드 배선
각 생성부에 아래 한 줄 추가(값·기본 동일):
```python
llm_max_input_tokens=config.get("llm.max_input_tokens", 200_000),
```
- `web/api/confluence_mcp.py:519` `_build_pipeline_config` — **주경로, 필수**.
- `web/api/documents.py:488` — 단건 재처리 경로, 필수.
- `ingestion/coordinator.py:303` — git_code 경로. LLM 미사용이라 실효 없음이나 균일성 위해 추가(무해).
- `mcp_sync.py` — 무변경(상위에서 만든 `pipeline_config` 전달만).

### 데이터 흐름 요약
`config/default.yaml(llm.max_input_tokens)` → `Config.get` → `_build_pipeline_config`/생성부 → `PipelineConfig.llm_max_input_tokens` → `process_document(cfg)` → `QuestionGenConfig(max_input_tokens=...)` / `LLMBodyExtractionConfig(max_input_tokens=...)` → 각 모듈의 사전 토큰 가드.

---

## R-2. 본문 그래프: API 컨텍스트 초과 → unit 폴백 연결

### 문제 재확인 (근거)
`extract_llm_body_graph_for_document` 의 `try … except Exception`(llm_body_extractor.py:330~349)이 **모든** 예외를 삼켜 빈 `GraphData` + `units_failed=1` 로 반환한다. 사전 토큰 가드(라인 314~318)는 tiktoken 추정 200K 초과만 잡으므로, 95K~200K 문서는 가드를 통과한 뒤 서버가 실제 컨텍스트 초과 400 을 던지고 → except 가 삼켜 → pipeline.py:475 의 `except InputTooLargeError` 폴백이 **발동하지 못한다**(역전 현상).

### 설계: 컨텍스트 초과만 `InputTooLargeError` 로 승격
API 컨텍스트 초과를 감지하면 기존 폴백 계약(`InputTooLargeError`)에 **합류**시킨다. 그러면 pipeline.py:475 의 `except InputTooLargeError:` 가 unit 폴백(`extract_llm_body_graph`)으로 이미 라우팅하므로 **pipeline 분기 로직 변경이 최소**다. (참고: 기존 테스트 `test_pipeline.py::test_llm_body_extraction_falls_back_to_units_when_oversized` 가 정확히 이 경로를 검증 중 — 그대로 재사용된다.)

### 1) 판별 헬퍼 — `processor/llm_client.py` 에 신설 (단일 출처)
openai SDK 를 실제로 쓰는 모듈이 `llm_client.py` 이므로 예외 taxonomy 의 단일 출처로 둔다. 다른 모듈(향후 R-3 API 트리거 포함)이 import 해 재사용.

```python
# 컨텍스트 초과로 판별되는 에러 메시지 조각 (소문자 부분일치).
# OpenAI 정식 code + vLLM/OpenAI 호환 서버(TGI 등)의 400 메시지 패턴을 함께 커버.
_CONTEXT_OVERFLOW_MARKERS: tuple[str, ...] = (
    "context_length_exceeded",          # OpenAI 정식 error code
    "maximum context length",           # OpenAI/vLLM 공통 문구
    "context length",                   # 일반
    "context window",                   # 일부 호환 서버
    "reduce the length",                # OpenAI: "Please reduce the length of the messages"
    "longer than the maximum",          # vLLM: "... is longer than the maximum model length"
    "maximum model length",             # vLLM 변형
    "decrease the input",               # 일부 서버
)

def is_context_length_error(exc: BaseException) -> bool:
    """예외가 LLM 컨텍스트(입력 토큰) 초과인지 판별한다.

    True 조건 (둘 중 하나):
      1) openai.BadRequestError(status 400) 이고 구조화 code == "context_length_exceeded"
      2) 예외 메시지(및 body message)에 _CONTEXT_OVERFLOW_MARKERS 중 하나가 포함
    판별 불가한 일반 실패(다른 400, 타임아웃, 5xx, 파싱 오류 등)는 False.
    """
    try:
        import openai  # 지연 import (llm_client 관례와 동일)
    except Exception:
        openai = None  # type: ignore

    # 1) 구조화 코드 (OpenAI 정식)
    if openai is not None and isinstance(exc, openai.BadRequestError):
        code = getattr(exc, "code", None)
        if code == "context_length_exceeded":
            return True
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            b_code = (body.get("error") or {}).get("code") if isinstance(body.get("error"), dict) else body.get("code")
            if b_code == "context_length_exceeded":
                return True
        # 2) 400 이면서 메시지 패턴 매칭
        msg = (getattr(exc, "message", "") or str(exc)).lower()
        return any(m in msg for m in _CONTEXT_OVERFLOW_MARKERS)

    # openai 미설치/비-openai 예외라도 메시지 패턴이 명확하면 인정 (호환 래퍼 대비).
    # 단, BadRequest 컨텍스트가 없으므로 마커 매칭만으로 판단(보수적).
    msg = str(exc).lower()
    return any(m in msg for m in _CONTEXT_OVERFLOW_MARKERS)
```
근거: openai>=1.50 (`pyproject.toml`) 의 `BadRequestError` 는 `APIStatusError → APIError` 상속, `status_code == 400`, `.code`/`.message`/`.body` 노출. OpenAI 는 컨텍스트 초과 시 `code="context_length_exceeded"` 를 body 에 담는다. vLLM/OpenAI 호환 서버는 400 + 메시지 `"This model's maximum context length is N tokens. However, you requested M tokens..."` 또는 `"... is longer than the maximum model length ..."` 형태 → 메시지 부분일치로 커버.

### 2) `extract_llm_body_graph_for_document` 개편 (llm_body_extractor.py:330~349)
LLM 호출부와 JSON 파싱을 **분리**한다(핵심: 파싱 실패는 컨텍스트 초과가 아님 — 오분류 방지).
```python
from context_loop.processor.llm_client import LLMClient, extract_json, is_context_length_error

try:
    response = await llm_client.complete(
        user, system=system, max_tokens=cfg.max_tokens,
        temperature=cfg.temperature, reasoning_mode="off",
        purpose="body_extraction_doc",
    )
except Exception as exc:
    if is_context_length_error(exc):
        # 사전 가드 통과했지만 API 레벨 컨텍스트 초과 → 폴백 계약에 합류
        raise InputTooLargeError(
            f"API 컨텍스트 초과 (doc_title={doc_title}): {exc}",
        ) from exc
    logger.warning("문서 단위 LLM 본문 추출 실패 — doc_title=%s", doc_title, exc_info=True)
    stats.units_failed = 1
    return GraphData(), stats

try:
    payload = extract_json(response)
    if not isinstance(payload, dict):
        raise ValueError(...)
except Exception:
    logger.warning("문서 단위 LLM 본문 추출 응답 파싱 실패 — doc_title=%s", doc_title, exc_info=True)
    stats.units_failed = 1
    return GraphData(), stats
# 이하 엔티티/관계 처리 기존과 동일
```
- **오탐(일반 400 을 컨텍스트 초과로 오판) 시 동작**: pipeline 이 `InputTooLargeError` 를 받아 unit 폴백(`extract_llm_body_graph`)으로 전환한다. 폴백은 문서를 ~1500 토큰 unit 으로 쪼개 호출하므로, 원인이 진짜 컨텍스트 초과였다면 해소되고, 원인이 malformed-request 류였다면 각 unit 호출이 다시 실패 → `extract_llm_body_graph` 내부 `except Exception`(라인 187) 이 unit 단위로 격리 → `units_failed` 증가 → `llm_degraded` 로 노출된다(pipeline.py:513). 즉 오탐의 최악 비용은 **추가 호출 낭비뿐이며 크래시·데이터 손상 없음**. 폴백 남용 방지를 위해 마커는 컨텍스트 특정 문구로 한정한다.
- **오탐 반대(미탐: 진짜 초과를 놓침)** 시: 기존 동작(degraded, 그래프 0, 폴백 없음)으로 남는다 — 회귀 없음. 마커를 다소 포괄적으로 둬 미탐을 줄인다.

### 3) unit 폴백 자체의 컨텍스트 초과
unit 폴백(`extract_llm_body_graph`)의 `run()` 내부 `except Exception`(라인 187~191)은 유지. unit 은 ~1500~2400 토큰이라 컨텍스트 초과 가능성이 낮고, 발생해도 unit 격리로 `units_failed` 처리. 여기서는 추가 승격 불필요(폴백의 폴백은 만들지 않음).

### pipeline 측 변경
분기 로직(pipeline.py:459~484)은 **구조 변경 불필요**. R-1 의 `config=body_cfg` 주입만 추가하면, 승격된 `InputTooLargeError` 가 기존 `except InputTooLargeError:` 로 흡수된다.

---

## R-3. 가상 질문: 입력 초과 시 섹션 배치 분할 폴백

### 설계: `generate_questions_for_document` 내부 self-heal
현재는 초과 시 `InputTooLargeError` 를 raise → pipeline.py:297 `except QuestionInputTooLargeError:` 가 질문을 **통째로 스킵**한다. R-3 은 함수 내부에서 섹션을 한도 이하 배치로 나눠 여러 번 호출하고 병합한다. 트리거 두 경우 모두 처리:
1. **사전 가드 초과**: `input_tokens > cfg.max_input_tokens` (기존 라인 178) 에서 raise 대신 배치 경로로.
2. **API 컨텍스트 에러**: 단일 전체 문서 호출의 `complete()` 예외를 `is_context_length_error` 로 판별 → 배치 경로로.

### 배치 분할 알고리즘 (결정론적)
입력: `sections_payload = _assemble_sections_payload(extracted, cfg)` → `[(section_index, rendered_text), ...]` (기존 헬퍼 재사용, 문서 순서 보존).

1. **오버헤드/예산 산정**:
   - `overhead = count_tokens(_SYSTEM_PROMPT) + count_tokens(doc_title) + TEMPLATE_SLACK`(≈ 64: 유저 템플릿 스캐폴딩·마커 여유).
   - `budget = max(cfg.max_input_tokens - overhead - SAFETY_MARGIN, MIN_BUDGET)` (`SAFETY_MARGIN≈1000`, `MIN_BUDGET` 예: 512 — 지나친 0/음수 방지).
   - 각 섹션 비용 `cost_i = count_tokens("--- section_index={idx} ---\n" + text)` (프롬프트 렌더와 동일 형식 — `_render_sections_for_prompt` 규칙 일치).
2. **탐욕적 패킹(문서 순서 유지 → 결정론)**:
   - 누적합이 `budget` 이하가 되도록 앞에서부터 섹션을 현재 배치에 담고, 다음 섹션을 더하면 초과할 때 배치를 flush 하고 새 배치 시작.
3. **단일 섹션이 한도 초과(`cost_i > budget`)**:
   - 그 섹션을 **단독 배치**로 만들되 rendered_text 를 예산 이하로 **토큰 단위 head-절단**(결정론: tiktoken 인코딩 후 앞에서 `budget` 토큰 유지, 디코드). 마커/헤딩은 보존. `stats.sections_truncated += 1` 기록. (스킵 대신 절단 채택: 최소한의 질문이라도 확보 — recall 우선. 스킵은 정보 손실.)
4. **배치별 호출**: 각 배치를 `_USER_PROMPT_TEMPLATE.format(...)` 로 렌더 → `llm_client.complete(..., temperature=0.0, reasoning_mode="off", purpose="question_generation")`. 배치는 `asyncio.gather` 로 병렬 호출하되 **결과를 배치 순서대로** 병합(gather 는 입력 순서로 결과 보존 → 결정론 유지). 결정성 요건(temperature=0, 섹션 순서 기반 분할) 충족.
5. **병합·중복 제거·총량 상한 (순서 고정)**:
   - 공유 상태: `result: dict[int,list[str]]`, `seen_global: set[str]`, `total_emitted: int` 를 **배치 간 공유**(기존 단일 호출의 `seen_global` 의미와 일관 — 서로 다른 배치의 동일 질문도 제거).
   - 배치를 순서대로 순회 → 각 배치 응답의 `sections` 를 기존 검증 로직으로 처리:
     a. `section_index` 정수화 실패/해당 배치 valid 인덱스 아님 → drop(dropped_questions).
     b. 질문 문자열: strip, 4자 미만/비문자열 drop, `key=lower` 가 `seen_local`/`seen_global` 에 있으면 drop.
     c. 통과분 append, `seen_global.add`, `total_emitted += 1`.
     d. `max_questions_per_doc` 도달 시 즉시 중단(해당 섹션 break → 배치 break → 전체 batch 순회 break). **총량 상한은 문서 전체 기준**(요구사항 준수).
   - 이 로직은 현재 단일 호출 병합 루프(question_generator.py:214~271)와 **동일** → `_merge_sections_into(...)` 헬퍼로 추출해 단일 경로와 배치 경로가 공유(중복 구현 방지).

### 리팩터링 구조 (question_generator.py)
- `_merge_sections_into(raw_sections, valid_indices, *, result, seen_global, stats, cfg, total_emitted) -> int`: 검증·dedup·상한 루프를 담고 갱신된 `total_emitted` 반환. 단일/배치 공용.
- `_plan_section_batches(sections_payload, *, doc_title, cfg) -> list[list[tuple[int,str]]]`: 예산 계산 + 탐욕 패킹 + 단일 초과 절단. 순수 함수(테스트 용이).
- `generate_questions_for_document`: 사전 가드 초과 또는 API 컨텍스트 에러 시 `_run_batched(...)` 로 분기. 정상 경로는 기존 단일 호출 유지.

### `QuestionGenStats` 확장 (question_generator.py:66~78)
```python
    fallback_used: bool = False   # 섹션 배치 분할 폴백이 사용됐는지
    batch_count: int = 0          # 폴백 시 배치(LLM 호출) 수. 비폴백=0
    sections_truncated: int = 0   # 단독 한도 초과로 토큰 절단된 섹션 수
```
pipeline.py 의 질문 로그(라인 288~296)에 `fallback_used/batch_count` 를 덧붙여 stats 노출(요구사항 "stats 에 폴백 발생 여부/배치 수 기록").

### pipeline 측 변경
- R-1 의 `config=QuestionGenConfig(max_input_tokens=cfg.llm_max_input_tokens)` 주입만 추가.
- 기존 `try/except QuestionInputTooLargeError`(pipeline.py:297~301)는 **방어적으로 유지**(배치 경로가 self-heal 하므로 실질적으로 도달하지 않으나, 배치조차 전부 실패하는 극단 상황의 안전망). 다만 함수가 더 이상 초과 시 raise 하지 않으므로 이 except 는 dead-path 가 된다 — 주석으로 명시(제거해도 무방하나 안전 위해 존치 권장).

---

## 설정 스키마 요약

| 키 | 위치 | 기본값 | 의미 |
|----|------|--------|------|
| `llm.max_input_tokens` | `config/default.yaml` `llm:` | `200000` | 인덱싱 LLM 문서 입력 토큰 상한. R-2/R-3 폴백 전환 기준. |
| `PipelineConfig.llm_max_input_tokens` | `pipeline.py` | `200_000` | 위 값의 런타임 캐리어. |
| `QuestionGenConfig.max_input_tokens` | 기존 | `200_000` | pipeline 에서 override 주입. |
| `LLMBodyExtractionConfig.max_input_tokens` | 기존 | `200_000` | pipeline 에서 override 주입. |

신규 코드 상수(모듈 내부, 설정 아님): `_CONTEXT_OVERFLOW_MARKERS`(llm_client.py), `TEMPLATE_SLACK`/`SAFETY_MARGIN`/`MIN_BUDGET`(question_generator.py).

---

## 구현 순서 (한 라운드, 회귀 통제)

1. `llm_client.py`: `is_context_length_error` + 마커 상수 추가 (독립, 부작용 없음).
2. `config/default.yaml`: `llm.max_input_tokens: 200000` 추가.
3. `pipeline.py`: `PipelineConfig.llm_max_input_tokens` 필드 + 두 LLM 호출부에 `config=` 주입(R-1 배선). 이 시점에 R-1 완성.
4. `llm_body_extractor.py`: `extract_llm_body_graph_for_document` 호출/파싱 분리 + 컨텍스트 초과 승격(R-2).
5. `question_generator.py`: `_merge_sections_into`/`_plan_section_batches`/`_run_batched` + stats 확장 + 진입점 self-heal(R-3).
6. 3개 배선부(`confluence_mcp.py`, `documents.py`, `coordinator.py`)에 `llm_max_input_tokens=config.get("llm.max_input_tokens", 200_000)` 추가.
7. 테스트 추가/갱신.

---

## 테스트 계획

### 기존 테스트 위치 (확인됨)
- `tests/test_processor/test_question_generator.py`
- `tests/test_processor/test_llm_body_extractor.py`
- `tests/test_processor/test_llm_client.py`
- `tests/test_processor/test_pipeline.py`
- `tests/test_config.py`
- 배선: `tests/test_ingestion/test_coordinator.py`, `tests/test_sync/test_sync/*`, `tests/test_web/*`

### 신규 테스트

**R-2 판별 헬퍼 — `test_llm_client.py`**
- `test_is_context_length_error_openai_code`: `openai.BadRequestError`(mock, `code="context_length_exceeded"`) → True. (openai 미설치 환경 대비 mock 객체로 body/message 세팅.)
- `test_is_context_length_error_vllm_message`: 메시지 `"... maximum context length is 4096 tokens ... longer than the maximum model length"` → True.
- `test_is_context_length_error_generic_400_false`: 400 이지만 `"invalid value for parameter 'temperature'"` → False.
- `test_is_context_length_error_non_openai_false`: `TimeoutError()`/`ValueError("boom")` → False.

**R-2 승격 — `test_llm_body_extractor.py`**
- `test_doc_call_context_error_raises_input_too_large`: `llm_client.complete` 가 컨텍스트 초과 예외 raise → `extract_llm_body_graph_for_document` 가 `InputTooLargeError` raise.
- `test_doc_call_generic_error_degraded_not_raised`: 일반 예외 raise → `InputTooLargeError` 아님, `(빈 GraphData, units_failed=1)` 반환.
- `test_doc_call_json_parse_failure_still_degraded`: 정상 200 응답이나 비-JSON → 승격 없이 degraded(파싱 실패가 컨텍스트로 오분류되지 않음 확인).

**R-3 배치 폴백 — `test_question_generator.py`**
- `test_plan_section_batches_deterministic`: 순수 함수. 여러 섹션 + 작은 한도 → 문서 순서 유지, 각 배치 예산 이하, 같은 입력 반복 시 동일 결과.
- `test_oversized_input_triggers_batched_fallback`: (기존 `test_oversized_input_raises_input_too_large` **갱신**) 작은 `max_input_tokens` + 다섹션 → raise 대신 배치 호출, `stats.fallback_used=True`, `stats.batch_count>=2`, 병합 결과 존재.
- `test_batched_dedup_across_batches`: 서로 다른 배치가 동일 질문 반환 → `seen_global` 로 1개만 유지.
- `test_batched_respects_max_questions_per_doc`: 배치 합산이 상한 초과해도 문서 전체 상한에서 컷.
- `test_single_section_over_budget_truncated`: 단일 거대 섹션 → 절단 후 1배치 호출, `stats.sections_truncated=1`, 질문 반환.
- `test_api_context_error_triggers_batched_fallback`: 첫 전체 호출이 컨텍스트 초과 예외 → 배치 경로로 self-heal(`fallback_used=True`). (mock: 첫 호출 예외, 이후 정상 JSON.)
- `test_generic_api_error_still_returns_empty`: 첫 호출이 일반 예외 → 폴백 아님, 빈 dict + `llm_failed=True`(기존 계약 유지).

**R-1 배선 — `test_pipeline.py`**
- `test_pipeline_injects_llm_max_input_tokens_to_question_cfg`: `PipelineConfig(llm_max_input_tokens=123)` → `generate_questions_for_document` 에 전달된 `config.max_input_tokens==123` (patch 로 캡처).
- `test_pipeline_injects_llm_max_input_tokens_to_body_cfg`: 동일 검증(문서 단위 + unit 폴백 모두 동일 인스턴스 한도).
- 기존 `test_llm_body_extraction_falls_back_to_units_when_oversized` 는 R-2 경로를 그대로 커버 — 회귀 확인용으로 유지.

**R-1 설정 — `test_config.py`**
- `test_default_llm_max_input_tokens`: `Config().get("llm.max_input_tokens") == 200000`.

**배선부(경량) — `test_web`/`test_ingestion`**
- `_build_pipeline_config`(confluence_mcp), documents 재처리, coordinator 가 `llm_max_input_tokens` 를 설정에서 싣는지 1건씩(있으면 기존 헬퍼 테스트에 assert 추가, 없으면 생략 가능 — 핵심은 pipeline/모듈 단위 테스트).

---

## 하위 호환 정의 (요구사항의 R-1 vs R-2/R-3 구분)

| 항목 | 미설정(200K 유지) 시 동작 | 설정 게이팅 |
|------|---------------------------|-------------|
| **R-1 (한도값)** | **기존과 완전 동일.** `llm.max_input_tokens` 미설정 → 200000 → 두 dataclass 기본값과 동일 → 가드 발동 지점 불변. | 예(값으로 제어) |
| **R-2 (본문그래프 폴백)** | **새 동작이 기본 활성.** 한도와 무관하게, 문서 단위 호출이 API 컨텍스트 초과를 내면 이제 unit 폴백으로 전환(기존: 삼켜 그래프 0). 정상 성공 경로는 불변 — 오직 에러 경로만 개선. | 없음(항상) |
| **R-3 (가상질문 폴백)** | **새 동작이 기본 활성.** 한도와 무관하게, 초과(사전/ API) 문서는 이제 섹션 배치로 질문 생성(기존: 통째 스킵). 한도 이하 문서는 단일 호출 그대로 — 불변. | 없음(항상) |

정리: **"미설정 시 완전 동일"은 R-1(한도값)에만 해당**한다. R-2/R-3 은 한도 설정 여부와 무관하게 항상 활성인 새 폴백이며, 이는 03 분석이 지적한 실패 모드(2·3번, 역전 현상·질문 스킵)를 직접 해소한다. 단, 두 폴백 모두 **정상 경로(한도 이내·API 성공)에서는 관측 가능한 차이가 없어** 기존 데이터/그래프 회귀는 없다.

결정성: 배치 분할은 섹션 순서 기반(결정론), 모든 인덱싱 호출 `temperature=0.0` 유지. 판별 헬퍼는 순수·부작용 없음.

---

## 회귀 위험 및 완화

| 위험 | 완화 |
|------|------|
| `test_oversized_input_raises_input_too_large` 가 R-3 로 의미 변경 → 갱신 필요(신규 폴백 반영). | 명시적 테스트 갱신(회귀 아님, 의도된 동작 변경). 아래 "기존 테스트 영향" 참조. |
| R-2 오탐(일반 400→폴백) | 마커를 컨텍스트 특정 문구로 한정. 오탐 시 최악=추가 호출 후 degraded, 크래시 없음(§R-2). |
| `frozen=True` dataclass override | 새 인스턴스 생성으로 처리(수정 아님). |
| 4개 PipelineConfig 생성부 누락 | 3곳 필수 배선(coordinator 는 무해). mcp_sync 는 전달만 → 무변경 확인. |
| openai 미설치 환경(테스트) | `is_context_length_error` 가 지연 import + 실패 시 메시지 마커 폴백 → import 없이도 동작·테스트 가능. |

## 기존 테스트 영향
- `tests/test_processor/test_question_generator.py::test_oversized_input_raises_input_too_large` — **기대값 변경 필요.** raise/`assert_not_called` → 배치 폴백 호출·`fallback_used=True` 로 재작성(위 신규 `test_oversized_input_triggers_batched_fallback` 로 대체). 이는 R-3 의 의도된 동작 변경.
- 그 외 question_generator/llm_body_extractor/pipeline 기존 테스트는 **정상 경로 불변**이라 회귀 없음(한도 이내·성공 응답 케이스). 특히 `test_pipeline.py::test_llm_body_extraction_runs_when_enabled`/`..._falls_back_to_units_when_oversized` 는 그대로 통과(config 주입은 기본값 동등).

---

## 검증 체크리스트
- [ ] `pytest tests/test_processor/test_llm_client.py`
- [ ] `pytest tests/test_processor/test_question_generator.py`
- [ ] `pytest tests/test_processor/test_llm_body_extractor.py`
- [ ] `pytest tests/test_processor/test_pipeline.py`
- [ ] `pytest tests/test_config.py`
- [ ] `pytest tests/test_ingestion tests/test_sync tests/test_web` (배선 회귀)
- [ ] `ruff check` / 타입 체크(mypy 사용 시)
- [ ] 수동: `llm.max_input_tokens: 90000` 설정 + ≈100K 문서 → 질문 배치·본문그래프 unit 폴백 로그 확인
- [ ] 수동: `src/context_loop/eval/*`, `scripts/eval_*` diff 0 확인(범위 밖 불가침)

## 보류/제외
- `context_window + 파생` 설정 방식: 단순성 위해 직접 스칼라 채택(§R-1). 후속 필요 시 재검토.
- unit 폴백의 컨텍스트 초과 재승격("폴백의 폴백"): 불필요(unit 크기상 발생 희박) — 제외.
- 검색 시점 LLM(query_expander/HyDE/answer) 및 git_code 경로: 인덱싱·범위 밖 — 미변경.
