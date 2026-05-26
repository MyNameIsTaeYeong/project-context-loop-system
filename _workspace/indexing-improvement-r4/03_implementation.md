# R4 — Confluence 추출 4-패치 구현 보고서

> designer 의 5단계 적용 순서(`02_design.md` §5) 를 그대로 따라 4개 패치
> (F-CG-04 / F-CG2-08 / F-CG2-02·04 / F-CG2-06) 를 적용. 각 단계 후 회귀
> 테스트로 깨진 게 *예상 한 건* 인지 확인하며 진행.

---

## 1. 변경 파일 목록 + 변경 요약

| 파일 | 변경 종류 | 요약 |
|---|---|---|
| `src/context_loop/processor/graph_vocabulary.py` | (이전 단계 1에서 적용 완료) | `ENTITY_TYPE_ALIASES`(4건) + `RELATION_TYPE_ALIASES`(9건) 상수, `normalize_entity_type`/`normalize_relation_type`/`normalize_name_stem` 헬퍼 3개. 본 라운드 구현 단계에서는 *변경 없음* — 단계 1은 directly 직전 커밋 `a192176` 으로 이미 적용됐다. |
| `src/context_loop/processor/llm_body_extractor.py` | 변수 스코프 / stem & alias / 신규 예외 / cfg 디폴트 | (a) `unit_valid_entity_names` → `document_valid_entity_names` 로 스코프 끌어올림 (F-CG-04). (b) entity/relation 등록·검증의 dedup·끝점 키를 `normalize_name_stem` 으로 일괄 전환 + `_canonical_name` 이 stem 매칭하도록 시그니처 의미 변경 (F-CG2-06). (c) entity_type / relation_type 검증 직전에 `normalize_entity_type` / `normalize_relation_type` 호출을 unit 경로 2지점 + 문서 경로 2지점 = 4 지점에 적용 (F-CG2-08). (d) `OutputTruncatedError` 신규 정의. `LLMBodyExtractionConfig.max_input_tokens` 디폴트 `200_000` → `16_000`, `fallback_on_output_truncation: bool = True` 필드 추가. `extract_llm_body_graph_for_document` 의 try/except 가 cfg 에 따라 `OutputTruncatedError` raise 또는 기존 빈 그래프 반환 (F-CG2-02/04). |
| `src/context_loop/processor/pipeline.py` | catch 분기 확장 | `OutputTruncatedError` import 추가. 문서 단위 LLM 호출의 `except InputTooLargeError` 를 `except (InputTooLargeError, OutputTruncatedError)` 로 확장, 로그 메시지에 `reason=<예외 클래스명>` 추가. |
| `tests/test_processor/test_llm_body_extractor.py` | 신규 5건 + 기존 1건 갱신 | 신규: `test_cross_unit_relation_endpoint_preserved` (F-CG-04), `test_name_stem_dedup_across_casings_and_punctuation` (F-CG2-06), `test_relation_type_alias_normalized` + `test_entity_type_alias_normalized_via_relation` (F-CG2-08), `test_for_document_output_truncation_raises_for_fallback` + `test_for_document_output_truncation_opt_out_returns_empty` + `test_max_input_tokens_default_is_16k` (F-CG2-02/04). 갱신: `test_for_document_failed_llm_returns_empty_with_failed_stat` 에 `cfg=LLMBodyExtractionConfig(fallback_on_output_truncation=False)` 명시 — 기존 동작이 *옵트인으로 보존됨* 을 표현. |
| `tests/test_processor/test_graph_vocabulary.py` | (이전 단계 1에서 적용 완료) | alias 표 1:1 unique 매핑 검증 2건, stem 비-collapse 1건. 본 라운드 구현 단계에서는 변경 없음. |

**비변경 (스코프 가드)**: `eval/`, `scripts/eval_search.py`, `scripts/build_synthetic_gold_set.py`, `ast_code_extractor.py`, `link_graph_builder.py`, `body_extractor.py`, `graph_search_planner.py`, `context_assembler.py`, 청크 사이즈 정책.

---

## 2. 단계별 회귀 결과

각 단계 후 `pytest tests/test_processor/test_llm_body_extractor.py tests/test_processor/test_graph_vocabulary.py` 를 실행하여 예상한 신규/갱신만 통과·실패하는지 확인.

| 단계 | 패치 | 누적 PASS | 신규 테스트 |
|---|---|---|---|
| 1 (사전) | graph_vocabulary alias + stem 헬퍼 | 40 | 3건 (커밋 `a192176`) |
| 2 | F-CG-04 (`document_valid_entity_names`) | 41 | +1 (`test_cross_unit_relation_endpoint_preserved`) |
| 3 | F-CG2-06 (stem 정규화 + `_canonical_name` 시그니처) | 42 | +1 (`test_name_stem_dedup_across_casings_and_punctuation`) |
| 4 | F-CG2-08 (alias 4 적용 지점) | 44 | +2 (`test_relation_type_alias_normalized`, `test_entity_type_alias_normalized_via_relation`) |
| 5 | F-CG2-02/04 (`OutputTruncatedError` + cfg) | 47 | +3 (truncation raise/opt-out, default 16K) |

**최종**: `pytest tests/test_processor/test_llm_body_extractor.py tests/test_processor/test_graph_vocabulary.py -v` → **47 passed in 0.36s**.

전체 `tests/test_processor/` 회귀 (`297 passed, 2 failed`):
- 2 failed = `test_extraction_unit.py::test_short_parent_body_absorbed_into_first_child`,
  `test_long_parent_body_emitted_as_standalone_unit`.
- **본 라운드 변경과 무관**. `git stash` 로 본 라운드 변경 전부를 stash 한 상태에서도 같은 두 케이스가 실패 — 단계 1 (`a192176`) 이전부터 존재한 회귀이며, 영역 (extraction_unit 의 parent body 처리) 도 본 라운드 스코프 (LLM 본문 그래프 추출) 와 무관.

---

## 3. 설계 deviation

**없음.** designer 의 5단계 적용 순서·의사 코드·alias 표(8 건)·테스트 명세를 그대로 따랐다.

세부 메모:
- 설계서 §3.1 T2 가 `test_relation_type_alias_normalized` 1건만 신규 권고했으나, alias 가 entity_type 양쪽에 적용되는지를 명시적으로 보이기 위해 `test_entity_type_alias_normalized_via_relation` 한 건을 추가했다. 설계서가 *명시적 제한* 을 두지 않았고 (vocab 검증 정황상 자연스러운 보강), alias 표 8건 범위 안에서만 동작 — 새 alias 추가는 없다.
- `test_for_document_failed_llm_returns_empty_with_failed_stat` 갱신은 설계서 §3.2 권고대로 cfg 옵트아웃 명시 방식(option a) 채택. 기존 assert (빈 그래프 + `units_failed=1`) 그대로 보존.
- ruff `pipeline.py` 의 I001 / F401 두 경고는 본 라운드 패치 *이전부터* 존재 (`git stash` 후 확인). 설계서에 없는 import 리팩토링은 *하지 않음* — 스코프 가드 준수.

---

## 4. 다음 단계 (verifier) 에 전달할 사항

### 4.1 핵심 회귀 가드 (반드시 통과)

```bash
.venv/bin/python -m pytest \
  tests/test_processor/test_llm_body_extractor.py \
  tests/test_processor/test_graph_vocabulary.py -v
```

→ **47 passed** 이어야 함.

### 4.2 영향 확인 포인트

verifier 가 *집중해* 확인할 영역:

1. **`pipeline.py` 통합 동작** — 문서 단위 LLM 호출이 `OutputTruncatedError` 를
   raise 했을 때 unit 폴백이 정상적으로 동작하는지. 현재 본 라운드는 단위
   테스트만 추가했고 pipeline 측 통합 테스트는 신규로 만들지 않았다 (설계서
   §3.3 의 권고). 사내 fixture 로 그래프 통계 diff 측정 가능하면 폴백 라우팅
   동작도 함께 검증해주기 바람.
2. **F-CG-04 회복 효과** — 폴백 경로의 unit 기반 함수 (`extract_llm_body_graph`)
   가 cross-unit 관계를 보존하므로, R4 환경(qwen2.5:7b @ 32K) 에서 거대 문서가
   unit 폴백으로 떨어지더라도 cross-unit 관계가 살아남는다. 사내 측정에서
   `final_relations` / `dropped_relations` 비율 개선 확인 권장.
3. **`max_input_tokens` 16K 디폴트 변경의 부작용** — 16K~200K 토큰 범위 문서
   가 *전부* unit 폴백 경로로 떨어진다. 256K 컨텍스트 모델을 쓰는 환경이
   있다면 호출자가 cfg 를 명시 주입해야 한다 (`pipeline.py:457` 는 현재 cfg
   미주입). 후속 라운드에서 `LLMSettings` 와 묶을 가능성.
4. **stem 정규화 부작용** — `Auth Service` vs `AUTH SERVICE` 가 같은 stem 으로
   통합되는 기존 케이스(`test_cross_unit_entity_dedup_keeps_first_casing`) 는
   그대로 통과. 단·복수형 분리 유지(`User Service` vs `Users`) 도 단위 테스트로
   가드. 운영 데이터에 *의도 외 노드 통합* 이 생기는지 사내 fixture 그래프
   통계로 확인 권장.

### 4.3 기존 무관 회귀 (verifier 가 무시 가능)

- `tests/test_processor/test_extraction_unit.py::test_short_parent_body_absorbed_into_first_child`
- `tests/test_processor/test_extraction_unit.py::test_long_parent_body_emitted_as_standalone_unit`

본 라운드 변경 이전부터 실패 상태였고 영역 (extraction_unit parent body 처리)
도 본 라운드 스코프(LLM 본문 그래프 추출) 와 무관. 별도 트랙에서 다뤄질 것.

### 4.4 ruff 잔존 경고 (verifier 가 무시 가능)

`pipeline.py` 의 I001 (import 정렬) / F401 (`QuestionGenConfig` 미사용) — 둘
다 본 라운드 *이전부터* 존재. 설계서에 없는 리팩토링은 본 라운드 스코프 가드
상 적용 금지.

### 4.5 추천 커밋 메시지 (verifier PASS 후 사용자 승인 받고 commit)

```
feat(graph): F-CG-04 cross-unit endpoint 보존 + F-CG2-06/08 stem·alias 정규화 + F-CG2-02/04 출력 잘림 폴백

- llm_body_extractor: 끝점 검증 set 을 unit 스코프 → 문서 누적 스코프로 끌어올림 (F-CG-04).
  cross-unit 관계가 통째로 드롭되던 동작 회복.
- llm_body_extractor: entities dedup 키·rel_key·_canonical_name 매칭을 normalize_name_stem
  기반 stem 키로 일괄 전환 (F-CG2-06). AuthService/Auth Service/auth-service 가 단일
  노드로 수렴. 단·복수형은 분리 유지.
- llm_body_extractor: entity_type / relation_type 검증 직전에 normalize_entity_type /
  normalize_relation_type 호출을 unit 경로·문서 경로 각각 2지점씩 = 4지점 적용
  (F-CG2-08). depending_on / components / policies 등 LLM 형태론적 변형이 canonical
  어휘로 정규화되어 vocab strict 검증을 통과한다.
- llm_body_extractor: OutputTruncatedError 신규. LLMBodyExtractionConfig.max_input_tokens
  디폴트 200_000 → 16_000 (32K 컨텍스트 모델 환경 기준). fallback_on_output_truncation:
  bool = True 신규 필드. extract_llm_body_graph_for_document 의 JSON 파싱 실패가
  디폴트에서 OutputTruncatedError raise 로 승격 (F-CG2-02/04).
- pipeline: LLM 본문 그래프 호출의 except 분기에 OutputTruncatedError 추가. 입력
  한도 초과뿐 아니라 출력 잘림도 unit 기반 폴백으로 라우팅.
- tests/test_llm_body_extractor: 신규 6건 + 기존 1건 cfg 옵트아웃 명시.

회귀: pytest tests/test_processor/test_llm_body_extractor.py
tests/test_processor/test_graph_vocabulary.py → 47 passed.
```
