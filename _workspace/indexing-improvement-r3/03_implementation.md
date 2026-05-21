# R3 Implementation Report — 멀티 벡터/가상 질문 인덱싱

## 사용자 결정

| 항목 | 결정 |
|------|------|
| 변형 | D-2 (멀티 벡터 + 가상 질문 임베딩) |
| 질문 톤 | 자연 질의 (사용자 표현) |
| 임베딩 한도 | 8K |
| 우선순위 | 문서 레벨 "관련 문서 찾기" |

## 변경 파일

| 파일 | 변경 |
|------|------|
| `src/context_loop/processor/question_generator.py` | **신규** — `generate_questions_for_document(*, doc_title, extracted, llm_client, config)` 함수. 256K 컨텍스트 LLM 1회 호출로 모든 섹션의 가상 질문 JSON 추출. 어휘 검증·중복 제거·토큰 한도 가드 포함. `InputTooLargeError`, `QuestionGenConfig`, `QuestionGenStats` 자체 정의. |
| `src/context_loop/processor/chunker.py` | **신규 함수** `chunk_extracted_document_doclevel(extracted, max_tokens=8000)` — 작은 문서(<=8K) = 1청크 / 큰 문서 = 섹션 폴백 / 거대 단일 섹션 = 토큰 분할. 기존 `chunk_extracted_document` 는 호환을 위해 그대로 유지. |
| `src/context_loop/processor/pipeline.py` | (a) `PipelineConfig` 에 `max_embedding_tokens=8000`, `enable_question_indexing=True` 필드 추가. (b) Confluence/upload/editor 청킹을 `chunk_extracted_document_doclevel` 로 전환. (c) 가상 질문 생성 + 임베딩 + vector_store `view="question"` 엔트리 등록 단계 추가. (d) 한 LLM 호출로 모든 섹션의 가상 질문을 한 번에 JSON 출력 → 매핑 후 청크-섹션 인덱스 기준으로 분배. |
| `src/context_loop/mcp/context_assembler.py` | (a) `_search_chunks` dedup 키를 `logical_chunk_id` → `document_id` 로 전환. (b) over-fetch 배수 2 → 6 (멀티 벡터 고려). (c) 출처 라벨(`_format_chunk_results`, `assemble_context_with_sources`) 에 매칭 질문(`question_text`) 노출. |
| `config/default.yaml` | `mcp.context_max_tokens` 4096 → 32768 (256K LLM 가정, 문서 단위 결과 대응). |
| `tests/test_processor/test_question_generator.py` | **신규** — 12건 테스트 (가드, JSON 파싱, 중복 제거, max_questions_per_doc, sections-less, 프롬프트 검증 등). |
| `tests/test_processor/test_chunker.py` | **신규** 5건 — doclevel 단일 청크 / 섹션 폴백 / 거대 섹션 분할 / sections-less / 빈 문서. |
| `tests/test_processor/test_pipeline.py` | **신규** 3건 — question 인덱싱 enabled/disabled/per-section 매핑. 기존 12건 patch 사이트 일괄 변경. |
| `tests/test_mcp/test_context_assembler.py` | **신규** 1건 (matched question 출처 라벨), 기존 3건 의도 명확화 (logical_chunk_id → document_id dedup 정책 반영). |

## 핵심 동작

### 인덱싱 흐름 (Before/After)

```
BEFORE (origin/main):
  extract → chunker.chunk_extracted_document(chunk_size=512)
         → 멀티뷰 임베딩(body+meta)
         → vector_store.add_chunks
         → ExtractionUnit + LLM body graph

AFTER (R3):
  extract → chunker.chunk_extracted_document_doclevel(max_tokens=8000)
         → 가상 질문 생성 (LLM 1회 호출, enable_question_indexing=True)
         → 멀티뷰 임베딩(body + meta + question 3 view)
         → vector_store.add_chunks (view="question" 엔트리 포함)
         → ExtractionUnit + LLM body graph (기존 유지)
```

### vector_store 스키마 확장

기존 metadata: `{document_id, chunk_index, title, section_path, section_anchor, logical_chunk_id, view: "body"|"meta"}`

R3 추가: `view: "body"|"meta"|"question"`, `question_text: str` (view="question" 일 때만)

벡터 ID 규칙: `{chunk_id}#body` / `#meta` / `#q{i}` (가상 질문 인덱스).

### 검색 흐름

`_search_chunks` 가 over-fetch 후 **document_id 단위 dedup** — 같은 문서의 여러 view (body/meta/question) 가 매칭되면 가장 가까운 1건만 결과로 보존. 사용자 의도 "리턴은 문서 단위" 충족.

출처 라벨 예시:
```
[출처: 결제 시스템] (섹션: 인증 > 토큰 검증) (매칭 질문: AuthService 는 어떻게 토큰을 검증하나요?)
```

## 비용/효과 (예상)

| 항목 | Before | After |
|------|--------|-------|
| 인덱싱 LLM 호출/문서 | 1회 (R2 body graph) | **2회** (R2 + 가상 질문) |
| 임베딩 호출/문서 | body+meta = 2~6 | body+meta+question = 5~15 |
| 검색 결과 | 청크 단위 (1문서가 결과 점유 가능) | **문서 단위 dedup** |
| 검색 정밀도 (정의/방법 질의) | 보통 | **대폭 ↑** (query=질문 형태와 key=가상질문 형태 일치) |
| 검색 결과 다양성 | 의도치 않게 동일 문서 점유 | 문서 단위 강제 분포 |
| 출처 라벨 | section_path | section_path + 매칭 질문 |

## 테스트 검증

| 영역 | 결과 |
|------|------|
| `test_processor/test_question_generator.py` (신규) | **12/12 passed** |
| `test_processor/test_chunker.py` (기존+신규) | **32/32 passed** |
| `test_processor/test_pipeline.py` (기존 patch 변경 + 신규) | **22/22 passed** |
| `test_mcp/test_context_assembler.py` (dedup 정책 반영 + 신규) | **24/24 passed** |
| **전체** | **1055 passed, 5 failed** |

5건 fail 모두 `tests/test_eval/` 영역의 사전 존재 fail (R2 PR 검증에서도 동일 확인). 우리 변경과 무관 — `git stash -u` 비교로 검증 완료.

## 회귀 위험

| 위험 | 완화 |
|------|------|
| 큰 문서 LLM 응답 잘림 | `max_input_tokens=200_000` 가드 + `InputTooLargeError` raise (호출자가 가상 질문만 스킵하고 진행) |
| 임베딩 호출 증가로 인한 인덱싱 시간 | `enable_question_indexing` 플래그로 런타임 OFF 가능. 단일 문서 인덱싱은 동기적이고 영향 제한 |
| 같은 문서가 결과 점유 | document_id 단위 dedup으로 자동 방지 (사용자 의도) |
| vector_store metadata 추가 (view, question_text) | 백워드 호환 — 기존 entry 그대로 작동, 신규 키는 옵션 |
| 기존 테스트 patch 대상 변경 | 12개 site 일괄 sed 변경, 전체 통과 확인 |
| `mcp.context_max_tokens` 4096 → 32768 | 256K LLM 가정 — 너무 작으면 답변 컨텍스트가 잘렸음. 32K 도 보수적 |
