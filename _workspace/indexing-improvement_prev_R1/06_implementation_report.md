# Implementation Report — Round 1

## 적용 항목

| ID | 파일 | 변경 요약 |
|----|------|----------|
| F-CG-07 + F-CG-13 + F-GG-11 + F-GG-12 | `processor/graph_vocabulary.py` | (a) `import` → `imports` (실제 데이터와 일치), (b) `method`/`struct`/`interface` 항목 추가, (c) `module` description 확장 (LLM 컴포넌트 + 코드 파일 둘 다 커버) |
| F-01 + F-02 | `processor/chunker.py` | 폴백 codec을 ord/chr round-trip으로 통일. `_CHARS_PER_TOKEN` 4 → `_FALLBACK_CHARS_PER_TOKEN` 1. `count_tokens` 폴백도 동일 단위로 통일하여 `_make_codec`과 일관. overlap이 폴백 환경에서도 실제 텍스트에 반영됨 |
| F-G-13 | `ingestion/git_repository.py` | `_DEFAULT_EXCLUDED_DIRS` frozenset 정의 (.venv/node_modules/__pycache__/dist/build/target 등 27종). `collect_files`가 전체 순회/증분 두 경로 모두에서 제외 |
| F-G-05 | `processor/ast_code_extractor.py` | `_RESERVED_SYMBOL_NAMES` frozenset 정의. `_extract_brace_symbols`이 매치된 name이 reserved 키워드(`if`/`for`/`return`/`throw`/`new` 등)이면 심볼 등록 안 함 |
| F-GG-09 | `processor/ast_code_extractor.py` | Go imports를 `import` 키워드 anchor 기반 `_extract_go_imports`로 분리. 단일 import + alias + 블록 import 모두 처리. 본문 string literal false positive 제거 |
| F-G-01 | `processor/ast_code_extractor.py` | `_extract_python`이 모듈 docstring을 `__module__` 심볼로 emit. 모듈 레벨 Assign/AnnAssign을 한 `__constants__` 심볼로 묶음 (개별 상수 청크 폭증 방지) |
| F-G-02 | `processor/ast_code_extractor.py` | `_body_start_line` 헬퍼 추가. 함수/클래스에 decorator_list가 있으면 body 시작이 첫 데코레이터 라인부터 (`@app.route("/x")` 포함) |
| F-G-03 | `processor/ast_code_extractor.py` | `_python_func_sig`이 `ast.unparse(node.args)` 사용 — `*args`, `**kwargs`, keyword-only, positional-only, defaults 모두 보존 |
| F-G-03 (보너스) | `processor/ast_code_extractor.py` | `_python_class_sig` 신규 — 클래스 시그니처에 bases/Generic/metaclass 포함 (`class Repository(Generic[T], BaseRepo, metaclass=ABCMeta)`) |
| F-G-11 + F-GG-02 | `processor/ast_code_extractor.py` | `CodeExtraction.import_symbols: list[tuple[str, str|None]]` 신규. `_extract_python`이 `from x import a, b` → `(x, a), (x, b)` 보존. `to_graph_data`가 import된 심볼명을 `imports` relation의 `label`로 노출 |
| F-G-14 + F-G-15 | `ingestion/git_repository.py` | `store_git_code` / `delete_removed_files`에 optional `existing_by_source_id` 캐시 인자 추가. `sync_repository`가 한 번만 `list_documents` 호출 후 캐시를 전달 — O(N²) → O(N) |

## 신규 테스트

| 파일 | 테스트 | 검증 대상 |
|------|--------|----------|
| `test_processor/test_graph_vocabulary.py` | `test_ast_code_vocab_subset_of_vocabulary` | ast_code의 entity/relation 타입이 vocab에 모두 정의됨 |
| `test_processor/test_chunker.py` | `test_fallback_count_tokens_one_char_one_token` | 폴백 1 char = 1 token |
| `test_processor/test_chunker.py` | `test_fallback_chunk_text_overlap_actually_applied` | 폴백 환경에서 overlap이 실제로 동작 |
| `test_processor/test_ast_code_extractor.py` | `test_module_docstring_emitted_as_symbol` | 모듈 docstring을 `__module__` 심볼로 추출 |
| `test_processor/test_ast_code_extractor.py` | `test_module_constants_grouped_into_single_symbol` | 모듈 상수를 `__constants__` 단일 심볼로 묶음 |
| `test_processor/test_ast_code_extractor.py` | `test_decorator_included_in_function_body` | 데코레이터가 함수 body의 prefix로 포함 |
| `test_processor/test_ast_code_extractor.py` | `test_class_decorator_and_bases_in_signature` | 클래스 시그니처에 bases/Generic/metaclass 포함 |
| `test_processor/test_ast_code_extractor.py` | `test_function_signature_handles_varargs_and_kwonly` | varargs/kwonly/defaults 시그니처 보존 |
| `test_processor/test_ast_code_extractor.py` | `test_import_symbols_preserved_for_from_import` | `from x import y`의 y 보존 |
| `test_processor/test_ast_code_extractor.py` | `test_import_relations_include_symbol_label_from_python` | import relation label에 import된 symbol 포함 |
| `test_processor/test_ast_code_extractor.py` | `test_reserved_keywords_not_extracted_as_function` | Java: `if`/`for`/`return` 등이 함수로 잘못 등록 안 됨 |
| `test_processor/test_ast_code_extractor.py` | `test_imports_no_string_literal_false_positive` | Go: 본문 string literal이 import로 잘못 잡히지 않음 |
| `test_processor/test_ast_code_extractor.py` | `test_imports_single_line_and_aliased` | Go: 단일 import + alias 처리 |
| `test_ingestion/test_git_repository.py` | `test_excludes_vendored_directories` | node_modules/.venv/__pycache__/dist/build 제외 |
| `test_ingestion/test_git_repository.py` | `test_excludes_vendored_in_incremental` | 증분 경로에서도 vendored 제외 |
| `test_ingestion/test_git_repository.py` | `test_cache_avoids_full_list_call` | 캐시 제공 시 list_documents 호출 안 함 |

신규 테스트: 16건. 기존 테스트 수정: 0건 (호환성 보장 설계).

## 보류 항목 (R2/R3로 이관)

R1에서 다루지 않은 발견은 `05_improvement_plan.md` 참조:
- 청킹: F-03 (CJK 폴백), F-04 (overlap 블록 단위), F-06 (intro section_path), F-09 (split 부모 흡수), F-12 (nested 헤딩 본문) — R2
- git_code 청킹: F-G-04 (nested class), F-G-06 (TS 화살표), F-G-07 (TS type/enum), F-G-09/10 (line 계산), F-G-12 (fallback 청킹) — R2/R3
- confluence 그래프: F-CG-03 (split LLM 정책), F-CG-04 (cross-unit entity), F-CG-05 (외부 URL), F-CG-10 (동명 doc), F-CG-01/02/06/08/09/11/12 — R2/R3
- git_code 그래프: F-GG-01 (stdlib/3rd/internal), F-GG-03 (상속/구현), F-GG-04 (call graph), F-GG-05 (FQN join), F-GG-06 (file_title path), F-GG-10 (TS alias) — R2/R3

## 신규 발견 (구현 중)

- **F-G-08 무효화**: brace 멀티라인 시그니처 후속 처리 발견은 실제 코드 동작이 이미 OK였음 (`body[:body.find("{")]`이 멀티라인 시그니처도 처리). 보고서에서 제외 처리됨.
- F-G-05 재평가: Java 함수 패턴이 실제 false positive를 만들기는 어렵다 (정규식이 의외로 잘 작동) — 그러나 `if`/`for`/`return` 등 reserved keyword 차단은 여전히 안전망으로 유의미. 구현 유지.
- F-G-13 보너스: vendored 디렉토리 frozenset에 27종 포함 — 잘 알려진 케이스 망라.
- F-G-03 보너스: `_python_class_sig` 추가로 클래스 시그니처에 base 정보 포함 → F-GG-03(상속 관계 그래프)의 50%를 사실상 해결 (signature 텍스트 임베딩에 base가 들어가서 "Foo가 어떤 base를 상속하나" 질의가 텍스트 매칭으로도 hit 가능)

## 추천 커밋 메시지

```
feat(indexing): R1 — chunker fallback, AST 보강, Go/Java false positive 차단, vendored 제외

Critical/High 결함 11건 묶음 수정 (분석 보고서 _workspace/indexing-improvement/ 참조):

* chunker 폴백 codec을 ord/chr round-trip으로 통일 (F-01/02)
  - tiktoken 없는 환경에서 overlap이 실제로 텍스트에 반영되도록 수정
  - count_tokens / _chunk_blocks 토큰 단위 일관성 확보
* Python AST 보강 (F-G-01/02/03)
  - 모듈 docstring → __module__ 심볼
  - 모듈 레벨 상수 → __constants__ 심볼 (개별 청크 폭증 방지)
  - 함수/클래스 body에 데코레이터 포함 (@app.route 등)
  - 시그니처에 *args/**kwargs/keyword-only/defaults 보존
  - 클래스 시그니처에 bases/Generic/metaclass 포함
* from x import y 처리 (F-G-11/F-GG-02)
  - CodeExtraction.import_symbols 추가, 그래프 relation label에 노출
* Go imports false positive 차단 (F-GG-09)
  - import 키워드 anchor 기반 추출. 본문 string literal 무시
* Java/brace 예약어 false positive 차단 (F-G-05)
  - if/for/return/throw/new 등이 함수로 잘못 등록 안 됨
* vendored 디렉토리 제외 (F-G-13)
  - .venv/node_modules/__pycache__/dist/build 등 27종
* graph_vocabulary 정합화 (F-CG-07/13, F-GG-11/12)
  - imports/method/struct/interface 항목 보강 (실제 데이터와 일치)
* store_git_code O(N²) → O(N) (F-G-14/15)
  - sync_repository에서 list_documents 캐시 1회로 모든 lookup 처리
```

## 즉시 실행한 테스트 결과

```
$ pytest tests/test_processor/ tests/test_ingestion/ -q
538 passed in 2.83s

$ pytest tests/ --ignore=tests/test_eval -q
743 passed, 11 warnings in 5.16s

$ ruff check (touched files only): 4 errors — baseline에 동일 4건 존재 (regression 0)
```

- 신규 테스트 16건 모두 통과
- 기존 테스트 727건 모두 통과 — 호환성 가드 유효
- 폴백 환경 한정 변경 (F-01/02)은 tiktoken 있는 운영 환경(테스트 포함)에서 영향 0
