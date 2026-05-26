# R4 — Confluence 추출 4-패치 독립 검증 보고서

> implementer 보고서(`03_implementation.md`) 와 designer 설계(`02_design.md`)
> 를 실제 `git diff origin/main` 및 직접 실행한 테스트·정적 분석 결과로 대조
> 한다. 모든 검증은 verifier 가 직접 실행했다.

---

## 1. 검증 결과 종합 — **PASS**

4개 패치(F-CG-04 / F-CG2-08 / F-CG2-02·04 / F-CG2-06) 모두 설계서와 정합되게
구현됐다. 핵심 회귀 가드(47-test 묶음) 가 통과하고, 전체 `tests/test_processor/`
회귀에서도 본 라운드와 무관한 2건만 실패한다 — 이 2건은 verifier 가 *origin/main
pristine 상태* 에서도 동일하게 실패함을 직접 확인했다. ruff·스코프 가드도 모두
청결. 별도 fix-up 없이 곧바로 커밋 가능한 상태.

---

## 2. 설계-구현 정합성 점검

### 2.1 F-CG-04 — `document_valid_entity_names` 스코프 끌어올림 — **✓**

| 검증 항목 | 결과 |
|---|---|
| `unit_valid_entity_names` 완전히 *제거* | ✓ (`grep` 결과 `llm_body_extractor.py` 에 없음) |
| `document_valid_entity_names: set[str] = set()` 가 *루프 밖* 에 위치 | ✓ (라인 229, `for unit, payload in results:` 직전) |
| 엔티티 등록 시 `document_valid_entity_names.add(...)` | ✓ (라인 261) |
| 관계 끝점 검증이 `document_valid_entity_names` 참조 | ✓ (라인 278-279) |
| 함수 진입 시 매번 새로 초기화 — 문서 간 누출 차단 | ✓ (함수 지역 변수, `extract_llm_body_graph` 본문 내부에서만 선언) |

### 2.2 F-CG2-06 — stem 기반 dedup/rel_key/canonical 일관성 — **✓**

설계서 §2.4 의 "한쪽만 바꾸면 mismatch" 경고에 따라 3 지점이 모두 stem 으로
전환됐는지 *unit 경로 + 문서 경로 양쪽에서* 확인.

| 변경 지점 | unit 경로 | 문서 경로 |
|---|---|---|
| entities dedup key | ✓ 라인 255-256 (`normalize_name_stem(name)` → `key`) | ✓ 라인 415-416 |
| rel_key | ✓ 라인 285 (`(src_stem, tgt_stem, rtype)`) | ✓ 라인 442 |
| `_canonical_name(..., raw_stem)` 호출 | ✓ 라인 288-289 | ✓ 라인 444-445 |
| `_canonical_name` 본체가 stem 매칭 | ✓ 라인 568-580 (`name_stem == raw_stem`) | — (공유) |
| endpoint set 키도 stem | ✓ 라인 261 / 278-279 | ✓ 라인 421 / 436-437 |

### 2.3 F-CG2-08 — alias 정규화 4 적용 지점 — **✓**

설계서 §2.2 의 "4 적용 지점 모두" 요구사항.

| 적용 지점 | 위치 | 결과 |
|---|---|---|
| unit 경로 entity_type 검증 직전 | 라인 250 (`etype = normalize_entity_type(etype_raw)`) | ✓ |
| unit 경로 relation_type 검증 직전 | 라인 273 (`rtype = normalize_relation_type(rtype_raw)`) | ✓ |
| 문서 경로 entity_type 검증 직전 | 라인 410 | ✓ |
| 문서 경로 relation_type 검증 직전 | 라인 431 | ✓ |

**alias 표 내용**:
- `RELATION_TYPE_ALIASES`: **9건** (depends_on 계열 3 + owned_by 2 + implements 2 + documented_in 2)
- `ENTITY_TYPE_ALIASES`: **4건** (policy 2 + module 2)
- 설계서 §4.2 표와 1:1 일치. 설계서 §2.2 표의 "8건" 은 canonical 그룹 6개를
  세는 informal 카운트 — alias 개수는 9.
- 자기 참조 (`alias == canonical`) 없음: verifier 가 `assert a != c` 로 직접
  확인 → 모두 통과.
- 모든 canonical 이 실제 vocab(`all_relation_type_names()` /
  `all_entity_type_names()`) 에 존재: verifier 가 set difference 계산 → 빈
  집합. (단위 테스트 `test_*_alias_table_is_unique_mapping` 가 회귀 가드.)
- 방향 반전 / 의미 충돌 항목 (`has_part↔part_of`, `service↔system`,
  `user↔person` 등) 은 의도적으로 *제외* — 설계서 권고 그대로.

### 2.4 F-CG2-02/04 — `OutputTruncatedError` + cfg + pipeline catch — **✓**

| 검증 항목 | 결과 |
|---|---|
| `OutputTruncatedError` 신규 정의 (Exception 서브클래스) | ✓ `llm_body_extractor.py:44-52` |
| `LLMBodyExtractionConfig.max_input_tokens` 디폴트 16_000 | ✓ `llm_body_extractor.py:108`. 단위 테스트 `test_max_input_tokens_default_is_16k` 가드 |
| `LLMBodyExtractionConfig.fallback_on_output_truncation: bool = True` 신규 필드 | ✓ `llm_body_extractor.py:113` |
| 문서 경로 try/except 가 cfg 에 따라 raise vs 빈 그래프 | ✓ `llm_body_extractor.py:377-389` |
| `pipeline.py` 의 except 분기를 `(InputTooLargeError, OutputTruncatedError)` 로 확장 | ✓ `pipeline.py:472` |
| 로그 메시지에 `reason=<예외 클래스명>` 추가 | ✓ `pipeline.py:478-482` |

### 2.5 implementer 가 추가 신고한 deviation 의 검증 — **✓**

- `test_entity_type_alias_normalized_via_relation` 한 건이 설계서에 *명시적으로
  열거되지 않은* 신규 테스트. alias 표 범위 안에서만 동작(`components→module`),
  vocab 또는 코드를 *추가로* 건드리지 않음. **scope-positive 보강** 으로 허용.
- `test_for_document_failed_llm_returns_empty_with_failed_stat` 갱신은 설계서
  §3.2 의 option (a) — `cfg=LLMBodyExtractionConfig(fallback_on_output_truncation=False)`
  명시 — 그대로. ✓

→ 의미적 deviation 없음.

---

## 3. 테스트 결과

### 3.1 핵심 회귀 가드

```bash
.venv/bin/python -m pytest tests/test_processor/test_llm_body_extractor.py \
  tests/test_processor/test_graph_vocabulary.py -v
```

→ **47 passed in 0.35s.** implementer 보고서 §4.1 의 기대치(47 passed) 와 동일.

신규 테스트 8건 모두 통과:
- `test_relation_type_alias_normalized` (F-CG2-08)
- `test_entity_type_alias_normalized_via_relation` (F-CG2-08 보강)
- `test_cross_unit_relation_endpoint_preserved` (F-CG-04 핵심)
- `test_name_stem_dedup_across_casings_and_punctuation` (F-CG2-06)
- `test_for_document_output_truncation_raises_for_fallback` (F-CG2-04)
- `test_for_document_output_truncation_opt_out_returns_empty` (F-CG2-04 옵트아웃)
- `test_max_input_tokens_default_is_16k` (F-CG2-02 디폴트 가드)
- `test_*_alias_table_is_unique_mapping`, `test_normalize_name_stem_does_not_collapse_plural_forms` (graph_vocabulary)

### 3.2 전체 `tests/test_processor/` 회귀

```bash
.venv/bin/python -m pytest tests/test_processor/ -v
```

→ **297 passed, 2 failed in 4.33s.**

| 실패 테스트 | 위치 | 본 라운드 무관 여부 |
|---|---|---|
| `test_short_parent_body_absorbed_into_first_child` | `test_extraction_unit.py:198-218` | **무관 — pristine origin/main 에서도 동일하게 실패** |
| `test_long_parent_body_emitted_as_standalone_unit` | `test_extraction_unit.py:225-244` | **무관 — 동일** |

**무관성 직접 검증** (verifier 가 git stash 후 origin/main pristine 체크아웃):
```bash
git stash
git checkout origin/main -- src/context_loop/processor/ tests/test_processor/
.venv/bin/python -m pytest tests/test_processor/test_extraction_unit.py::<두 케이스>
# → 2 failed (동일 assert 실패)
```
→ implementer 의 "본 라운드 변경과 무관, 단계 1 이전부터 존재" 주장은 사실.
영역도 `extraction_unit` 의 parent body 흡수/분할 동작 — 본 라운드 스코프(LLM
본문 그래프 추출) 와 분리된 모듈.

### 3.3 delta 분석

| 메트릭 | origin/main | 본 라운드 | delta |
|---|---|---|---|
| 본 라운드 핵심 묶음 통과 수 | (해당 신규 8건 없음) | 47 | +8 신규 + 39 기존 모두 통과 |
| 전체 processor 통과 수 | (R4 전 추정) ~289 | 297 | +8 (신규) |
| 전체 processor 실패 수 | 2 (pre-existing) | 2 (동일) | 0 |

→ 새 회귀 없음.

---

## 4. ruff / 스코프 가드

### 4.1 ruff — 본 라운드 변경 파일

```bash
.venv/bin/python -m ruff check src/context_loop/processor/graph_vocabulary.py \
  src/context_loop/processor/llm_body_extractor.py \
  tests/test_processor/test_llm_body_extractor.py \
  tests/test_processor/test_graph_vocabulary.py
```

→ **All checks passed!** 본 라운드가 신규로 만든 파일·코드는 ruff 경고 0건.

### 4.2 ruff — `pipeline.py` 잔존 경고

`pipeline.py` 에는 `I001` (import 정렬) / `F401` (`QuestionGenConfig` 미사용)
경고 2건이 존재한다. verifier 가 `git stash` 후 pristine 상태에서 직접 확인:
**두 경고 모두 origin/main 에서 이미 존재** — 본 라운드와 무관.

설계서 §5 / implementer 보고서 §3 의 "스코프 가드 — 본 라운드 외 리팩토링
금지" 정책 그대로. *수정 권고* 하지 않음 (별도 정리 PR 권장).

### 4.3 ruff — 그 외 영역 잔존 경고

`ast_code_extractor.py` (E741, E501), `question_generator.py` (F401),
`test_ast_code_extractor.py`, `test_chunker.py`, `test_graph_search_planner.py`,
`test_llm_client.py` 등 R4 스코프 *밖* 파일들에 잔존 경고 16건이 있으나 모두
origin/main 에서 이미 존재. **본 라운드 무관 — 별도 트랙에서 다뤄야 함.**

### 4.4 스코프 가드 검증

```bash
git diff origin/main --name-only
```

변경된 코드 파일 (워크스페이스 문서 제외):
- `src/context_loop/processor/graph_vocabulary.py`
- `src/context_loop/processor/llm_body_extractor.py`
- `src/context_loop/processor/pipeline.py`
- `tests/test_processor/test_graph_vocabulary.py`
- `tests/test_processor/test_llm_body_extractor.py`

→ **모두 `processor/` 모듈 + 그 테스트.** 스코프 *밖* (`eval/`, `scripts/`,
`ast_code_extractor.py`, `body_extractor.py`, `link_graph_builder.py`,
`graph_search_planner.py`, `context_assembler.py`) 변경 0건. ✓

---

## 5. 회귀 위험 spot check

### 5.1 F-CG-04 — 문서 간 누출 가능성 — **✓ 안전**

- `document_valid_entity_names` 는 `extract_llm_body_graph` 함수 *지역 변수*
  (라인 229). 함수 호출이 1 문서당 1회 (`pipeline.py:483` 폴백 호출) 이므로
  문서 간 누출 불가능. ✓
- `extract_llm_body_graph_for_document` 도 단일 문서 한정 — `valid_names` (라인
  402) 가 함수 지역.

### 5.2 F-CG2-06 — 의도 외 노드 통합 — **✓ 가드 작동**

- `User Service` vs `Users` 분리 테스트(`test_normalize_name_stem_does_not_collapse_plural_forms`)
  통과 확인. stem 정규식 `r"[\s\-_]+"` 가 형태론적 변형은 건드리지 않음 — *공백/
  하이픈/언더스코어/대소문자만* 통합.
- `test_name_stem_dedup_across_casings_and_punctuation` 가 `AuthService` /
  `Auth Service` / `auth-service` 통합 + 첫 등장 표기 보존 보장.

### 5.3 F-CG2-08 — alias 표 1:1 매핑 / canonical 자기참조 부재 — **✓**

- verifier 가 직접 `RELATION_TYPE_ALIASES` / `ENTITY_TYPE_ALIASES` 를 import
  하여 `alias != canonical` 검증 → 모든 13 entry 통과.
- 모든 canonical 이 실제 vocab 에 존재 (set difference = ∅).
- 단위 테스트 `test_*_alias_table_is_unique_mapping` 가 회귀 가드.
- 방향 반전 (`has_part↔part_of` 등) / 의미 모호 (`service↔system`) 등 위험
  매핑은 의도적으로 제외 — 설계서 권고 그대로.

### 5.4 F-CG2-02/04 — 기존 테스트의 의미 변경 명시 — **✓**

- `test_for_document_failed_llm_returns_empty_with_failed_stat` 갱신: cfg
  `fallback_on_output_truncation=False` 명시 → 기존 assert (빈 그래프 +
  `units_failed=1`) 보존. ✓ 설계서 §3.2 option (a) 그대로.
- 디폴트 동작은 신규 테스트 `test_for_document_output_truncation_raises_for_fallback`
  가 커버.
- pipeline 측 통합 테스트는 본 라운드에서 추가하지 않음 — implementer 보고서
  §4.2 가 "사내 fixture 측정" 으로 후보 권고. 단위 테스트 수준의 회귀 가드는
  충분.

### 5.5 다운스트림 영향 spot check

- `pipeline.py:472` 의 except 분기 확장은 *기존 `InputTooLargeError` 동작을
  보존* — 폴백 함수 (`extract_llm_body_graph(units, ...)`) 시그니처 무변.
- 기존 `llm_graph`/`llm_stats` 반환 형식 동일 (`GraphData`, `LLMBodyStats`).
- 호출자(`process_document`) 의 후속 처리(`stats.raw_entities`,
  `stats.raw_relations`) 도 동일하게 동작.
- `graph_store` / `vector_store` 측 저장 경로 변경 없음 — `Entity.name` 표기
  형식만 stem 기반 dedup 으로 *통합 후 첫 등장 표기 보존* 으로 바뀜. 기존
  데이터 호환성: **새로 재인덱싱 시점부터 적용**, 기존 인덱싱된 노드는 그대로
  유지 (rebuild 또는 reindex 권장 — 운영 트랙).

---

## 6. 결론과 권고

### 6.1 사용자에게 보고할 핵심

- R4 4-패치 모두 설계대로 적용됨. 47-test 핵심 묶음 통과, 전체 processor 테스트
  297 통과 / 2 실패 (실패 2건은 pre-existing, 본 라운드 무관 — verifier 가
  pristine origin/main 에서 동일 실패 직접 확인).
- ruff: R4 변경 파일 0 경고. `pipeline.py` 의 잔존 2 경고는 origin/main 부터
  존재 — 본 라운드 스코프 밖.
- 스코프 가드 위반 없음 — `processor/` + 그 테스트 + 워크스페이스 문서만 변경.

### 6.2 사용자 승인 후 권장 커밋 분할

본 라운드는 *단계별로* 깔끔하게 분리 가능한 변경이지만, **단일 feature commit**
이 합리적이다 — 4 패치가 서로 보강 관계 (F-CG-04 가 F-CG2-02/04 의 unit 폴백
경로를 보호하고, F-CG2-06 dedup 키와 F-CG-04 endpoint set 키가 같은 stem 을
공유) 이고, 부분 revert 가 의미를 갖기 어려움.

**권고: 1 PR / 2 커밋**:

1. *(이미 적용됨)* `a192176` — vocab alias + stem 헬퍼 (graph_vocabulary 단독).
2. *(신규)* implementer 보고서 §4.5 의 커밋 메시지 그대로 — llm_body_extractor
   + pipeline + 테스트 추가.

implementer 가 제안한 단일 커밋 메시지는 변경 의도를 정확히 반영. 적용 절차:

```bash
git add src/context_loop/processor/llm_body_extractor.py \
        src/context_loop/processor/pipeline.py \
        tests/test_processor/test_llm_body_extractor.py
git commit -m "$(cat <<'EOF'
feat(graph): F-CG-04 cross-unit endpoint 보존 + F-CG2-06/08 stem·alias 정규화 + F-CG2-02/04 출력 잘림 폴백
...
EOF
)"
```

워크스페이스 문서 4건 (`_workspace/indexing-improvement-r4/0[1-4]_*.md`) 은
`docs(indexing-improvement-r4): ...` 형태로 *별도 docs 커밋* 분리 권장 — 이미
03 까지는 단계별로 커밋돼 있고, 본 04 보고서가 verifier 측 산출물로 추가.

푸시는 사용자 명시 승인 후. `claude/indexing-improvement-r4` 브랜치는 origin
에 아직 푸시되지 않음 (`Your branch is ahead of 'origin/main' by 5 commits`).

### 6.3 다음 단계 권고 (스코프 밖)

- 사내 fixture 로 그래프 통계 diff 측정 — F-CG-04 회복 효과
  (`dropped_relations` 비율 감소) 와 F-CG2-06 dedup 효과 (final_entities 감소)
  를 실측. verifier 는 평가 스크립트 직접 실행 금지 정책 — 사용자/오케스트레
  이터가 별도 트랙에서.
- `pipeline.py` ruff 경고 2건 정리 — *별도 chore PR*.
- `test_extraction_unit.py::test_*_parent_body_*` 2건 — *별도 트랙* (이번 R4
  스코프 외 모듈).
- `max_input_tokens=16_000` 디폴트 변경의 운영 영향 모니터링 — 256K 모델 환경
  사용자가 있다면 cfg 명시 주입 경로 (R5 또는 후속 라운드) 추가 검토.

### 6.4 최종 등급

**PASS** — 사용자 승인 후 그대로 커밋·푸시 가능.
