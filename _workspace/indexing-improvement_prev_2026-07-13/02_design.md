# 설계 결정 — Round 2

## 사용자 의도 vs 코드 현실

사용자: "현재 문서가 큰 경우 청크사이즈로 나눠서 인덱싱을 진행하고 있습니다. 이를 없애고 문서단위로 인덱싱"

코드 현실: 분할은 **3 곳**에서 일어나며 각각 다른 다운스트림에 결합되어 있다:

| 분할 | 입력→출력 | 결합된 다운스트림 | 256K 컨텍스트로 사라지는가 |
|------|---------|------------------|----------------------------|
| `chunker` (512 토큰) | 임베딩 입력 | ChromaDB 벡터, `section_path` 출처 라벨, `logical_chunk_id` dedup, 대시보드 청크 탭 | ❌ (임베딩 한도/검색 정밀도 결합) |
| `extraction_unit` (1500~2400 토큰) | LLM/결정론 그래프 추출 입력 | `body_extractor` (`section_label`), `llm_body_extractor` (LLM 호출 단위) | ✅ LLM만 (결정론은 unit 유지 필요) |
| `ast_code_extractor.to_chunks` (심볼 단위) | 코드 임베딩 입력 | 코드 검색 정밀도 | ❌ (의미적 분할, 토큰 분할 아님) |

## 핵심 결정

> **이번 라운드는 ExtractionUnit 분할이 LLM 호출용으로 강제되던 부분만 문서 단위로 전환한다.**
>
> - chunker (임베딩): **그대로 유지** — 사용자가 명시한 "청크사이즈" 가 이쪽이지만, 임베딩 한도와 검색 정밀도의 진짜 제약은 LLM 컨텍스트와 무관. 이를 변경하면 다음 4개 다운스트림 모두 깨진다:
>   1. `assemble_context_with_sources` 의 "섹션: …" 출처 라벨
>   2. body+meta 멀티뷰의 `logical_chunk_id` dedup
>   3. 대시보드 청크 탭
>   4. MCP 응답 `context_max_tokens=4096` 가드
> - `extraction_unit` (결정론 body_extractor 입력): **유지** — `body_extractor` 의 `Relation.label = unit.section_path` 가 그래프 카드의 "어디서 나온 관계인가" 시그널. unit 1개로 합치면 모든 label이 동일해져 정보 손실.
> - `llm_body_extractor` (LLM 그래프 추출): **문서 단위 1회 호출로 전환** — 256K 컨텍스트로 거의 모든 문서를 안전하게 처리. cross-unit entity 누적 문제(R1 F-CG-04) 자동 해결. LLM 호출 수 N → 1로 감소.

## 변경 범위 (정확한 파일/함수)

### 1. `src/context_loop/processor/llm_body_extractor.py`

**신규**: `extract_llm_body_graph_for_document` 함수

```python
async def extract_llm_body_graph_for_document(
    *,
    document_id: int,
    doc_title: str,
    body: str,                      # 문서 전체 본문 (plain_text 또는 sections 합본)
    section_paths: list[str] | None = None,  # 선택: entity 매핑용 (지금은 미사용)
    llm_client: LLMClient,
    config: LLMBodyExtractionConfig | None = None,
) -> tuple[GraphData, LLMBodyExtractionStats]:
    """문서 전체 본문을 1회 LLM 호출로 처리하여 GraphData 를 반환한다.

    기존 ``extract_llm_body_graph(units, ...)`` 는 unit 단위 호출이라 cross-unit
    entity 통합이 안 되고 호출 비용이 N배였다. 256K 컨텍스트 모델에서는
    이 문서 단위 호출이 더 정확하고 저렴하다.

    토큰 한도 가드: ``body`` 가 ``config.max_input_tokens`` 를 초과하면
    ``ValueError`` 를 raise → 호출자가 기존 unit 기반 경로로 폴백.
    """
```

**기존 보존**: `extract_llm_body_graph(units, ...)` 는 fallback / 테스트 호환용으로 그대로 유지.

**Config 확장**: `LLMBodyExtractionConfig` 에 `max_input_tokens: int = 200_000` 추가 (256K 모델 안전 마진).

### 2. `src/context_loop/processor/pipeline.py`

`process_document` 의 LLM 본문 그래프 호출 분기 변경:

**Before** (`pipeline.py:352-371`):
```python
if cfg.enable_llm_body_extraction and llm_client is not None:
    llm_graph, llm_stats = await extract_llm_body_graph(
        units, doc_title=title, llm_client=llm_client,
    )
    ...
```

**After**:
```python
if cfg.enable_llm_body_extraction and llm_client is not None:
    # 문서 단위 1회 호출 우선 (256K 컨텍스트). 토큰 한도 초과 시 자동 폴백.
    try:
        llm_graph, llm_stats = await extract_llm_body_graph_for_document(
            document_id=document_id,
            doc_title=title,
            body=_assemble_document_body(extracted),
            llm_client=llm_client,
        )
    except _InputTooLargeError:
        logger.info(
            "문서 본문이 LLM 한도 초과 — unit 기반 호출로 폴백 (doc_id=%d)",
            document_id,
        )
        llm_graph, llm_stats = await extract_llm_body_graph(
            units, doc_title=title, llm_client=llm_client,
        )
    ...
```

신규 헬퍼 `_assemble_document_body(extracted: ExtractedDocument) -> str`: sections 가 있으면 헤딩+본문 합본, 없으면 plain_text. lead_paragraph 별도 prepend 불필요(전체가 다 들어감).

### 3. 테스트

신규:
- `tests/test_processor/test_llm_body_extractor.py::test_extract_for_document_single_call` — 한 번의 LLM 호출로 모든 entity/relation 추출 확인.
- `tests/test_processor/test_llm_body_extractor.py::test_extract_for_document_oversized_raises` — body 가 max_input_tokens 초과면 `_InputTooLargeError`.
- `tests/test_processor/test_pipeline.py::test_llm_body_extraction_uses_document_path` — pipeline 이 document-level 호출 우선 사용. mock LLM 으로 호출 횟수 1 검증.
- `tests/test_processor/test_pipeline.py::test_llm_body_extraction_falls_back_to_units_when_too_large` — 한도 초과 시 unit 폴백.

기존:
- `extract_llm_body_graph(units, ...)` 의 모든 테스트 그대로 유지 (시그니처 무변경).

## 비용/효과 추정

| 항목 | Before | After | Δ |
|------|--------|-------|---|
| 평균 LLM 호출/문서 | ~3 (unit 수) | **1** | -67% |
| Cross-unit entity 통합 | LLM 자체 불가, GraphStore 후처리 의존 | LLM 단일 호출에서 자연 통합 | 품질 + |
| 응답 잘림 위험 | unit당 1500토큰 입력, 32K 출력 충분 | 문서당 평균 20K 입력, 32K 출력 — 안전 마진 압박 시 가드 | 가드 필요 |
| 인덱싱 wall time | unit별 직렬+동시성 N/3 | 단일 호출 | -30~50% |
| 검색 정밀도 (그래프) | description 부분적 | 통합 description | + |
| 검색 정밀도 (벡터) | 변경 없음 | 변경 없음 | = |

## 비-목표 (Out of Scope)

이번 라운드에서 다루지 않는 것:
- chunker의 임베딩 청크 단위 변경 (별도 R3, 검색 정밀도 평가 필요)
- AST `to_chunks` 변경 (의미적 분할, 무관)
- git_code 경로 LLM 호출 신설 (현재 LLM 호출 없음)
- 결정론 `body_extractor` 의 unit 입력 변경 (section_label 손실 위험)
- MCP `context_max_tokens` 가드 변경 (검색 응답 단위 문제, 인덱싱과 분리)

## 회귀 위험 평가

| 위험 | 완화책 |
|------|--------|
| LLM 응답 잘림 (큰 문서) | max_input_tokens 가드 + JSON 파싱 실패 시 unit 기반 폴백 |
| 시그니처 변경으로 기존 테스트 깨짐 | 기존 함수 시그니처 보존, 새 함수만 추가 |
| section_label 손실 | body_extractor 경로 유지 (결정론 그래프는 unit 단위) → 그래프 카드 label 시그널 유지 |
| stats 호환성 | `LLMBodyExtractionStats` 그대로 사용. document-level 호출은 `units_total=1, units_called=1` 로 보고 |
