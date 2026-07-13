# 인덱싱 과정의 LLM 사용 지점 분석

> 기준: 현재 HEAD. `00_overview.md`의 6단계 분석을 "LLM이 어디서 어떻게 쓰이는가" 관점으로 보완한 문서.
> 범위: 인덱싱(`process_document`) 경로만. 검색 시점의 LLM 사용(query_expander, HyDE, 답변 생성)은 범위 밖.

## 요약

인덱싱 중 **생성형 LLM 호출은 confluence/confluence_mcp 경로에만 존재하며, 문서당 최대 2회**다.
git_code 경로는 생성형 LLM을 전혀 사용하지 않는다 (순수 정적 분석: ast/정규식).

| # | 사용 지점 | 모듈 | 호출 시점 | 기본값 | purpose 태그 |
|---|-----------|------|-----------|--------|--------------|
| 1 | 가상 질문 생성 (question indexing) | `processor/question_generator.py` | 청킹 직후, 임베딩 직전 (pipeline.py:271~) | **ON** (`enable_question_indexing=True`) | `question_generation` |
| 2 | 본문 의미 그래프 추출 | `processor/llm_body_extractor.py` | 그래프 추출 단계 (pipeline.py:447~) | **ON** (`enable_llm_body_extraction=True`) | `body_extraction_doc` / `body_extraction` |
| — | 임베딩 (참고: 생성형 아님) | `processor/embedder.py` | 임베딩 단계 | 항상 | — |

주의: CLAUDE.md 설계 문서의 "LLM Classifier"(storage_method 분류)는 **현재 코드에 없다**.
`storage_method`는 처리 결과에서 결정론적으로 파생된다 (`_derive_storage_method`, pipeline.py:542).

---

## 사용 지점 1 — 가상 질문 생성 (Question-based Indexing, R3)

**무엇을**: 문서 전체를 LLM에 1회 입력하여, 각 섹션이 "답할 수 있는" 자연 질의 형태의
한국어 질문을 섹션당 최대 5개 생성한다 (`{section_index: [질문...]}` 매핑 반환).

**왜**: 사용자 쿼리(자연 질문)와 임베딩 키(가상 질문)를 같은 의미 공간에 두어 검색 정밀도 향상
(proposition / multi-vector retrieval 패턴). 생성된 질문은 각각 임베딩되어 `view="question"`
메타데이터로 vector_store에 추가 등록된다 — 즉 청크당 임베딩 뷰가 body + meta + question×N이 된다.

**어떻게**:
- 진입: `generate_questions_for_document()` (question_generator.py:139) ← pipeline.py:281
- 프롬프트: 시스템 프롬프트(question_generator.py:81)가 "검색 엔지니어" 역할 + 7개 작성 규칙
  (자연 질의 톤, 본문에 답 있는 것만, 중복 금지, 섹션 격리 등) + JSON 출력 형식 강제
- 호출 파라미터: `temperature=0.0`(재현성 우선), `max_tokens=32768`, `reasoning_mode="off"`
- 입력 가드: 문서 본문 200K 토큰 초과 시 `InputTooLargeError` → 호출 스킵 (질문 없이 진행)
- 후처리(question_generator.py:214~271): JSON 파싱(`extract_json`) → 유효 section_index 검증 →
  4자 미만/비문자열 드롭 → 섹션 내·문서 전체 중복 제거 → 문서당 총 50개 상한(`max_questions_per_doc`)

**실패 처리**: LLM 예외/JSON 파싱 실패 시 빈 dict 반환하고 파이프라인 계속.
`q_stats.llm_failed=True`가 기록되어 `llm_degraded` 플래그로 처리 결과에 노출된다 (pipeline.py:510~).

## 사용 지점 2 — 본문 의미 그래프 추출 (LLM Body Extractor, R2)

**무엇을**: 결정론 추출기(`body_extractor`, 휴리스틱)가 못 잡는 도메인 의미 관계
(`depends_on`/`implements`/`calls` 등)를 LLM으로 추출하여 `GraphData`(entities+relations)로 반환.
graph_store의 `(LOWER(name), type)` 정규 병합으로 링크 그래프·휴리스틱 그래프와 같은 노드에 수렴한다.

**어떻게** — 2단 전략:
1. **문서 단위 1회 호출**(우선): `extract_llm_body_graph_for_document()` ← pipeline.py:460.
   섹션 헤딩 포함 전체 본문을 한 번에 입력 (256K 컨텍스트 모델 가정, 입력 한도 200K 토큰).
   cross-section 엔티티 통합·중복 제거를 LLM 자체에 맡긴다.
2. **unit 단위 폴백**: 문서가 200K 초과로 `InputTooLargeError`면
   `extract_llm_body_graph(units, ...)` ← pipeline.py:482. `ExtractionUnit` 단위로 호출하되:
   - `min_unit_tokens=200` 미만 unit 스킵 (짧은 섹션은 의미 관계 거의 없음)
   - 거대 섹션 분할의 overlap part(`split_part>0`) 스킵 (중복 추출 방지)
   - 문서 내 동시 호출 `max_concurrency=3`

**어휘 통제**: 엔티티/관계 타입을 `graph_vocabulary.py`의 llm_body subset으로 프롬프트에서 강제하고,
출력에서 어휘 외 타입·끝점 누락 관계는 드롭한다 (`dropped_entities`/`dropped_relations` 통계).
그래프 스키마 무한 확장 방지가 목적.

**호출 파라미터**: `temperature=0.0`, `max_tokens=32768`, `reasoning_mode="off"`.

**실패 처리**: unit 단위 격리 — 한 unit 실패해도 나머지는 계속. `units_failed>0`이면
`llm_degraded` 플래그에 반영 (pipeline.py:513).

## LLM 클라이언트 인프라 (`processor/llm_client.py`)

- 추상화: `LLMClient.complete()/stream()/stream_events()` — 3개 구현체를 config로 선택:
  - `EndpointLLMClient` (기본, `provider: "endpoint"`): vLLM/Ollama 등 OpenAI 호환 자체 서버.
    `reasoning_profiles` 설정으로 `reasoning_mode="off"` 의도를 모델별 `extra_body`
    (예: Qwen3 `enable_thinking: false`)로 매핑 — 모델 교체 시 호출부 수정 불필요.
  - `AnthropicClient` (기본 모델 `claude-haiku-4-5-20251001`)
  - `OpenAIClient` (기본 모델 `gpt-4o-mini`)
- 생성 위치: `web/app.py:_build_llm_client()` (라인 66) → `app.state.llm_client`.
  config는 `config/default.yaml`의 `llm:` 블록 (provider/model/endpoint/api_key/reasoning_profiles).
- 주입 경로: `app.state.llm_client` → `ingestion/coordinator.py`(생성자 주입, :104) 및
  `sync/mcp_sync.py`(:188) → `process_document(llm_client=...)`. **`llm_client=None`이면
  두 LLM 단계 모두 조용히 스킵**되고 나머지 파이프라인은 정상 진행 (opt-in 안전 설계).
- 응답 파싱 보조: `extract_json()` — `<think>` 태그 제거, 마크다운 코드블록/직접 JSON 추출,
  max_tokens로 잘린 JSON의 괄호 복구(`_repair_truncated_json`)까지 시도.
- 관측성: 모든 호출에 `purpose` 태그 + elapsed/ttft/토큰 로그. purpose가
  `answer_generation`이 아니면 응답 본문도 INFO 로그에 남긴다.

## LLM이 아닌 것 (혼동 주의)

- **임베딩**: `processor/embedder.py`의 `EndpointEmbeddingClient`(OpenAI 호환 embeddings API,
  기본 모델 `text-embedding-3-small`, batch 100, 동시 4, 429 백오프) 또는
  `LocalEmbeddingClient`(sentence-transformers). 신경망 모델 호출이지만 생성형 LLM 채팅이 아니며,
  두 소스타입 공통으로 항상 실행된다.
- **git_code 전 단계**: AST/정규식 기반 — LLM 0회.
- **링크 그래프(`link_graph_builder`)·휴리스틱 본문 그래프(`body_extractor`)**: 결정론, LLM 0회.
- **storage_method 결정**: LLM classifier 아님 — `_derive_storage_method`로 파생.

## 문서당 비용 프로파일 (confluence_mcp 기준)

| 항목 | 호출 수 |
|------|---------|
| 가상 질문 생성 | 1회 (200K 초과 시 0회) |
| LLM 본문 그래프 | 1회 (200K 초과 시 unit 수만큼, 동시 3) |
| 임베딩 | (청크 수 × 2 + 질문 수) ÷ batch 100 |

두 LLM 단계 모두 기본 ON이지만 `ProcessingConfig`의
`enable_question_indexing=False` / `enable_llm_body_extraction=False`로 개별 차단 가능 (pipeline.py:80,90).
