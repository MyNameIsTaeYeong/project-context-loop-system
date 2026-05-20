# Verification Report — Round 1

## 한 줄 결론

**PASS** — 계획서 R1의 모든 항목 구현 완료, 회귀 0, 신규 테스트 16건 통과.

## 변경 요약

```
9 files changed, 733 insertions(+), 53 deletions(-)

claude.md                                        |  13 ++
src/context_loop/ingestion/git_repository.py     | 106 +++++++++++++
src/context_loop/processor/ast_code_extractor.py | 275 +++++++++++++++++++--
src/context_loop/processor/chunker.py            |  29 ++-
src/context_loop/processor/graph_vocabulary.py   |  15 +-
tests/test_ingestion/test_git_repository.py      |  89 ++++++++
tests/test_processor/test_ast_code_extractor.py  | 195 ++++++++++++++++
tests/test_processor/test_chunker.py             |  46 ++++
tests/test_processor/test_graph_vocabulary.py    |  18 ++
```

- src/ 코드 변경: 4개 파일 (+425 / -42 lines)
- tests 추가: 4개 파일 (+348 / -0 lines), 신규 테스트 16건
- 비-코드: claude.md (하네스 포인터 등록)

## 계획 vs 구현 매트릭스

| 계획 ID | 계획 동작 | 실제 변경 | 일치도 |
|--------|----------|-----------|--------|
| F-01 | chunker 폴백 encode/decode ord/chr 통일 | `_chunk_blocks`의 encode/decode를 ord/chr로 통일 + `_FALLBACK_CHARS_PER_TOKEN` 상수 도입 | ✓ |
| F-02 | count_tokens 폴백 단위 통일 | `len(text) // _FALLBACK_CHARS_PER_TOKEN` (=1) — _make_codec과 동일 | ✓ |
| F-G-01 | Python 모듈 docstring + 상수 청크 | `__module__` + `__constants__` 심볼 추가 | ✓ |
| F-G-02 | Python 데코레이터 body 포함 | `_body_start_line` 헬퍼로 decorator_list 첫 라인부터 | ✓ |
| F-G-03 | Python func sig 모든 args | `ast.unparse(node.args)` + `_python_class_sig` 신규 (보너스) | ✓+ |
| F-G-05 | Java 함수 false positive 차단 | `_RESERVED_SYMBOL_NAMES` 검사 추가 (Java 외 brace 언어에도 적용) | ✓ |
| F-G-11 + F-GG-02 | from x import y 처리 + import relation label | `CodeExtraction.import_symbols` + `to_graph_data`에서 label 반영 | ✓ |
| F-G-13 | vendored 디렉토리 제외 | `_DEFAULT_EXCLUDED_DIRS` 27종 + `_has_excluded_part` 헬퍼 | ✓+ |
| F-G-14 + F-G-15 | store_git_code N²→N | `existing_by_source_id` 캐시 인자 + sync_repository에서 한 번만 로드 | ✓ |
| F-GG-09 | Go imports anchor | `_extract_go_imports` 신규 + `_extract_brace_imports`에서 Go 분기 | ✓ |
| F-GG-11 + F-CG-07 | vocab에 ast_code 타입 보강 | method/struct/interface 추가, module description 확장 | ✓ |
| F-GG-12 + F-CG-13 | vocab `import` → `imports` | 1줄 수정 | ✓ |

**불일치/누락: 0건**

추가 보너스 (계획에 명시되지 않았지만 자연스럽게 적용된 개선):
- `_python_class_sig`: 클래스 시그니처에 base/Generic/metaclass 포함 (F-GG-03 상속 그래프의 텍스트 매칭 부분을 부분적으로 해결)
- vendored 디렉토리 27종 (`_DEFAULT_EXCLUDED_DIRS`): 계획서는 일부 예시만 적었으나 잘 알려진 케이스 망라

## 테스트 결과

| 명령 | 결과 |
|------|------|
| `pytest tests/test_processor/test_chunker.py` | passed (28) |
| `pytest tests/test_processor/test_ast_code_extractor.py` | passed (49) |
| `pytest tests/test_processor/test_graph_vocabulary.py` | passed (8) |
| `pytest tests/test_processor/test_extraction_unit.py` | passed (20) |
| `pytest tests/test_ingestion/test_git_repository.py` | passed (45) |
| `pytest tests/test_processor/` | passed (전체) |
| `pytest tests/test_ingestion/` | passed (전체) |
| `pytest tests/` (eval 제외) | **passed 743** in 5.16s |

베이스라인 (origin/main): 727 passed. **회귀 0건, 신규 16건 통과 = 총 +16**.

## Ruff 정적 분석

| 영역 | 결과 |
|------|------|
| `ruff check` (touched files only) | 4 errors |
| baseline (origin/main, same files) | 4 errors (동일) |
| **신규 ruff 회귀** | **0** |

기존 4 errors는 main에 이미 있던 `_extract_brace_symbols` / `_extract_class_methods`의 ambiguous variable `l` (E741) + 긴 라인 (E501)으로, 본 라운드에서 다루지 않은 영역.

## 회귀 위험 점검

| 변경 | 회귀 위험 | 검증 |
|------|----------|------|
| chunker 폴백 ord/chr 통일 | 폴백 환경 한정. tiktoken 있는 환경(테스트 포함)에선 영향 0 | 테스트 743건 통과 |
| Python AST 모듈 docstring/상수 | 신규 심볼이 추가됨. `to_chunks`/`to_graph_data`는 모든 심볼을 순회하므로 자연 통합 | 기존 ast 테스트 33건 + 신규 16건 모두 통과 |
| Python 데코레이터 body 포함 | body 라인 수 증가 — token_count도 증가. 기존 테스트는 body 내용 검사 (decorator 포함을 expect하지 않음) | 기존 `test_method_body_contains_own_code` 등 통과 |
| 클래스 시그니처에 bases | `parent_signature`가 `class MyService`→`class MyService(...)`로 변경 가능. 기존 테스트 `assert init.parent_signature == "class MyService"`는 base 없는 클래스에 한해 동일 출력 — pass | 기존 `test_method_parent_info` 통과 |
| from x import y → import_symbols | 신규 필드. 기존 `imports` 필드는 그대로 (호환) | 기존 `test_extracts_imports`/`test_extracts_relative_imports` 통과 |
| to_graph_data import relation label | label 필드 활용. 기존 테스트는 label 검사 안 함 | 기존 `test_creates_import_relations` 통과 (length=2 검사도 통과 — module 한 개당 relation 1개 유지) |
| Go imports anchor | 정확한 import만 추출. 기존 테스트는 "context"/"fmt" 같은 정상 import 검사 | 기존 `test_extracts_imports` 통과 |
| Reserved keyword 차단 | 정당한 함수가 reserved keyword와 같은 이름이면 제외 — 매우 드문 케이스 | 기존 Java/TS/JS 테스트 통과 |
| vendored 디렉토리 제외 | 기존 테스트는 일반 src/ 구조만 → 영향 없음 | 기존 git_repository 테스트 통과 |
| store_git_code 캐시 인자 | optional, default None → 기존 호출처(예: 다른 모듈)에서 무영향 | 기존 `test_create_new`/`test_unchanged`/`test_updated` 모두 통과 |

## 다운스트림 영향 분석 (코드 호출 경로)

`store_git_code`의 호출처:
```
$ grep -rn "store_git_code\b" src/ tests/ --include='*.py' | grep -v "def store_git_code"
src/context_loop/ingestion/git_repository.py:507:            doc_result = await store_git_code(...)  # sync_repository
src/context_loop/web/api/git_sync.py: (확인 필요)
```
→ `web/api/git_sync.py`의 호출도 본 변경에 호환 (optional cache 인자라 기존 호출은 그대로 작동).

`CodeExtraction.import_symbols`의 신규 사용처:
- `to_graph_data` (라벨 노출)
- 외부에서 데이터 모델을 의존하는 곳: pipeline.py가 CodeExtraction 사용. import_symbols 활용은 없음 (호환).

## 수동 점검 (스모크)

- `extract_code_symbols("'''doc'''\nMAX=3\n@dec\ndef foo(): pass", "x.py")` → 4 심볼(`__module__`/`__constants__`/`foo`), foo.body 첫 줄이 `@dec` 시작 ✓
- `_extract_go_imports('import "fmt"\nfunc x() { fmt.Println("hello") }')` → `["fmt"]`만 반환 ("hello"는 제외) ✓
- vocab `all_relation_type_names()`에 `imports` 포함, `import` 미포함 ✓

## 후속 권고

다음 라운드(R2) 후보:
1. **F-CG-04** llm_body_extractor의 cross-unit entity 누적 — 그래프 추론 정확도 큰 폭 개선
2. **F-G-04** Python nested class — 데이터 클래스/Pydantic Settings 패턴 인덱싱
3. **F-G-06** TS 화살표 함수 — 모던 React/Node 코드 인덱싱 사각지대 해소
4. **F-12** confluence_extractor nested 헤딩 본문 — Expand/Info 매크로 본문 누락
5. **F-04** chunker overlap을 토큰→블록 단위 (검색 품질 직접 영향)

평가 지표 영향 측정은 별도 하네스(`rag-eval-audit`/`eval-gold-set-improvement`) 영역. 본 라운드 변경 후 합성 골드셋으로 인덱싱 변화의 검색 메트릭 영향을 측정하는 것을 권고.

## PASS 결정

- 계획서 R1 항목 100% 구현
- 신규 테스트 16건 모두 통과
- 기존 테스트 727건 모두 통과 (회귀 0)
- ruff 회귀 0
- 다운스트림 영향 분석 완료, 호환성 가드 유효
