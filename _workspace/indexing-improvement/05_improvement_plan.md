# Indexing Improvement Plan

## 입력 보고서 요약

| 보고서 | 발견 건수 | 주요 영역 |
|--------|----------|-----------|
| 01 confluence_chunking | 12 (C:2, H:7, M:3) | chunker 폴백 결함, overlap, extraction_unit 분할, confluence_extractor 본문 누락 |
| 02 git_code_chunking | 16 (C:2, H:11, M:2, L:1) | Python 모듈 레벨 누락/데코레이터, Java false positive, vendored 디렉토리, O(N²) 성능 |
| 03 confluence_graph | 13 (C:1, H:7, M:4, L:1) | LLM cross-unit 단절, 외부 URL 누락, vocab drift |
| 04 git_code_graph | 11 (C:1, H:7, M:2, L:1) | 상속/구현 누락, Go imports false positive, vocab 누락, FQN 일관성 |

**중복/충돌 발견**:
- F-CG-13 ↔ F-GG-12: vocab `import` vs 실제 `imports` 불일치 — **동일 이슈, 한 번에 처리**
- F-CG-07 ↔ F-GG-11: vocab 단일 출처 문제 — F-GG-11(method/struct/interface 추가)과 함께 처리
- F-G-11 ↔ F-GG-02: `from x import y`의 y 누락 — chunking 보고서가 imports 단순 보존, graph 보고서가 그래프 추가 — **묶어서 처리**

## 우선순위 매트릭스

심각도/공수 ROI 기반 R1/R2/R3 분류. R1은 즉시 처리(이번 라운드), R2는 다음 세션, R3는 보류/별도 작업.

| ID | 출처 | 영역 | 영향 | 공수 | ROI | 라운드 |
|----|------|------|------|------|-----|--------|
| **F-01** | 01 | chunker.py 폴백 | Critical | S | ★★★★★ | **R1** |
| **F-02** | 01 | chunker.count_tokens 폴백 | Critical | S | ★★★★★ | **R1** |
| **F-G-01** | 02 | ast_code_extractor Python 모듈 | Critical | M | ★★★★★ | **R1** |
| **F-G-02** | 02 | Python 데코레이터 body 누락 | High | S | ★★★★★ | **R1** |
| **F-G-03** | 02 | Python func sig (varargs/kwonly) | High | S | ★★★★ | **R1** |
| **F-G-05** | 02 | Java 함수 false positive | Critical | S | ★★★★★ | **R1** |
| **F-G-11** + **F-GG-02** | 02+04 | `from x import y` 처리 | High | S | ★★★★ | **R1** |
| **F-G-13** | 02 | vendored 디렉토리 제외 | High | S | ★★★★★ | **R1** |
| **F-G-14** + **F-G-15** | 02 | store_git_code O(N²) → O(N) | High | M | ★★★★ | **R1** |
| **F-GG-09** | 04 | Go imports false positive | Critical | S | ★★★★★ | **R1** |
| **F-GG-11** + **F-CG-07** + **F-GG-12** + **F-CG-13** | 03+04 | graph_vocabulary 단일 출처 정합성 | Critical | S | ★★★★★ | **R1** |
| F-03 | 01 | CJK 폴백 추정 | High | S | ★★★ | R2 |
| F-04 | 01 | overlap 토큰→블록 단위 | High | M | ★★★ | R2 |
| F-06 | 01 | chunk_text intro section_path | High | S | ★★★ | R2 |
| F-09 | 01 | 부모 흡수 split part 컨텍스트 | High | M | ★★★ | R2 |
| F-12 | 01 | nested 헤딩 본문 누락 | High | M | ★★★ | R2 |
| F-G-04 | 02 | nested class | High | M | ★★ | R2 |
| F-G-06 | 02 | TS/JS 화살표 함수 | High | M | ★★★ | R2 |
| F-G-07 | 02 | TS type/enum | High | S | ★★ | R2 |
| F-G-09 | 02 | _extract_class_methods line 계산 | High | M | ★★ | R3 |
| F-G-10 | 02 | brace symbols used_ranges 1-off | High | S | ★★ | R3 |
| F-CG-03 | 03 | split part LLM 호출 정책 | High | S | ★★★ | R2 |
| F-CG-04 | 03 | LLM cross-unit entity 누적 | Critical | M | ★★★★ | R2 |
| F-CG-05 | 03 | 외부 URL 그래프 추가 | High | M | ★★★ | R2 |
| F-CG-10 | 03 | 동명 doc 충돌 | High | M | ★★ | R3 |
| F-GG-01 | 04 | stdlib/third/internal 구분 | High | M | ★★ | R3 |
| F-GG-03 | 04 | 상속/구현 관계 | Critical | M | ★★★ | R2 |
| F-GG-04 | 04 | call graph | High | L | ★★ | R3 |
| F-GG-05 | 04 | FQN ↔ section_path join 키 | High | M | ★★★ | R2 |
| F-GG-06 | 04 | file_title path 충돌 | Medium→High | M | ★★ | R3 |
| F-GG-10 | 04 | TS alias 추출 | High | M | ★★ | R3 |
| F-07/F-08/F-10/F-11 | 01 | 일관성/정밀도 미세개선 | Medium | S | ★★ | R3 |
| F-CG-01/02/06/08/09/11/12 | 03 | 정밀도/디버깅 미세개선 | Medium | S/M | ★★ | R3 |
| F-G-12/F-G-16/F-G-17 | 02 | fallback/통계/URL 정규화 | Medium/Low | S | ★ | R3 |
| F-GG-07/F-GG-08 | 04 | nested FQN/module description | Medium | S | ★ | R3 (F-G-01 후속) |

## 충돌/중복 항목

| 발견 IDs | 처리 |
|---------|------|
| F-CG-13 + F-GG-12 | vocab `import` → `imports` 한 줄 수정 |
| F-CG-07 + F-GG-11 | vocab에 method/struct/interface/module/external_module 추가 (단일 작업) |
| F-G-11 + F-GG-02 | `from x import y` 처리 — imports를 (module, symbol) 튜플로 변환하면 청크 임베딩과 그래프 모두 개선 |

## 라운드 1: 인덱싱 핵심 결함 수정 (Critical + High 즉시 효과)

### 포함 항목

11개 발견 묶음 (중복 통합 후 8개 작업):

1. **F-01 + F-02**: chunker 폴백 정합 (encode/decode/count_tokens 단위 통일)
2. **F-G-01**: Python 모듈 docstring/상수 청크화
3. **F-G-02**: Python 데코레이터 body 포함
4. **F-G-03**: Python func sig 모든 args 처리
5. **F-G-05**: Java 함수 정규식 false positive 차단
6. **F-G-11 + F-GG-02**: `from x import y` 처리 + import relation에 symbol 정보
7. **F-G-13**: vendored 디렉토리 제외
8. **F-G-14 + F-G-15**: store_git_code/delete_removed_files O(N²) → O(N)
9. **F-GG-09**: Go imports 정규식 anchor 강화
10. **F-GG-11 + F-CG-07 + F-GG-12 + F-CG-13**: graph_vocabulary 보강 (`imports` 수정 + method/struct/interface/module 추가)

### 변경 파일

| 파일 | 변경 사유 |
|------|----------|
| `src/context_loop/processor/chunker.py` | F-01, F-02 (폴백 codec) |
| `src/context_loop/processor/ast_code_extractor.py` | F-G-01/02/03/05/11, F-GG-02/09 |
| `src/context_loop/ingestion/git_repository.py` | F-G-13/14/15 |
| `src/context_loop/processor/graph_vocabulary.py` | F-CG-07/13, F-GG-11/12 |
| `src/context_loop/storage/metadata_store.py` | F-G-14 (get_document_by_source 추가) — 단순 lookup 메서드 |

### 구현 순서 (서로 독립한 묶음부터)

1. **vocab 수정** (F-GG-11/12 + F-CG-07/13) — 가장 단순, 회귀 0
2. **chunker 폴백 통일** (F-01, F-02) — 폴백 환경 한정, tiktoken 있는 환경(테스트 일반)에서는 영향 없음
3. **vendored 디렉토리 제외** (F-G-13) — collect_files 한 함수
4. **Java/Go regex 강화** (F-G-05, F-GG-09) — 패턴 두 곳
5. **Python AST 보강** (F-G-01, F-G-02, F-G-03) — `_extract_python` + `_python_func_sig`
6. **`from x import y` 처리** (F-G-11, F-GG-02) — CodeExtraction 데이터 모델 + to_graph_data
7. **N² 제거** (F-G-14, F-G-15) — store/sync_repository 시그니처 변경

### 회귀 위험

| 변경 | 잠재 회귀 |
|------|----------|
| chunker 폴백 통일 | 폴백 환경에서 chunk_size 의미 변경 (1 char = 1 token). 기존 테스트가 fallback에 의존하면 깨질 수 있음 (단, 폴백은 운영 환경에서 거의 없음) |
| F-G-01 (모듈 docstring) | Python 파일당 청크 수 1~2 증가. 벡터 저장 용량 미증가 |
| F-G-02 (데코레이터 body) | 청크 토큰 수 약간 증가 (decorator 라인 수). 청크 ID는 새 uuid로 발급되어 기존 검색 캐시 무효화 — 그러나 이는 코드 재처리 시 항상 발생 |
| F-G-05 (Java 패턴) | 일부 정당한 함수가 누락될 수 있음 — modifier 1회 이상 강제 시 modifier 없는 package-private 메서드 (예: `void foo() {}`) 누락. 테스트로 보호 |
| F-G-11 (imports 데이터 모델) | CodeExtraction.imports 타입이 `list[str]`에서 `list[tuple|str]`로 변경 시 호환성 깨짐 — **별도 필드 `import_symbols`로 추가**하여 호환성 유지 |
| F-G-14 (cache 도입) | store_git_code 시그니처 변경 — 호출처 (sync_repository, ?) 모두 업데이트 필요 |

### 필요한 신규 테스트

- `tests/test_processor/test_chunker.py::test_fallback_overlap_consistent` — tiktoken 없는 환경에서 overlap이 실제로 적용되는지 (mock으로 _get_tokenizer가 None 반환)
- `tests/test_processor/test_chunker.py::test_count_tokens_matches_codec` — count_tokens와 _make_codec 토큰 단위 일관성
- `tests/test_processor/test_ast_code_extractor.py::test_python_module_docstring_extracted` — `"""모듈 설명"""\ndef foo(): pass` → module symbol 존재
- `tests/test_processor/test_ast_code_extractor.py::test_python_module_constants_extracted` — 모듈 레벨 상수가 별도 심볼 또는 묶음으로 들어감
- `tests/test_processor/test_ast_code_extractor.py::test_python_decorator_included_in_body` — `@app.route("/x")\ndef foo(): pass` → body에 `@app.route("/x")` 포함
- `tests/test_processor/test_ast_code_extractor.py::test_python_func_sig_varargs_kwonly` — `def foo(a, *args, key=1, **kw)` 시그니처 정확
- `tests/test_processor/test_ast_code_extractor.py::test_java_function_pattern_no_false_positive` — `int x = bar(a);` 같은 호출은 함수로 인식 안 됨
- `tests/test_processor/test_ast_code_extractor.py::test_go_imports_no_string_false_positive` — Go 파일의 `"hello"` 같은 string literal은 imports에 들어가지 않음
- `tests/test_processor/test_ast_code_extractor.py::test_python_from_import_symbols_extracted` — `from os.path import join` → import_symbols에 `("os.path", "join")` 보존
- `tests/test_processor/test_ast_code_extractor.py::test_to_graph_data_includes_imported_symbols` — `from x import y` → relation `file uses y`가 그래프에 들어감
- `tests/test_ingestion/test_git_repository.py::test_collect_files_excludes_vendored_dirs` — `.venv/`, `node_modules/`, `__pycache__/` 안의 파일 제외
- `tests/test_processor/test_graph_vocabulary.py::test_vocab_includes_ast_code_types` — method/struct/interface/module 어휘 포함
- `tests/test_processor/test_graph_vocabulary.py::test_vocab_imports_relation_name` — `imports` (실제 데이터와 일치)

### 기존 테스트 영향

- `tests/test_processor/test_ast_code_extractor.py::test_python_function_extraction` — body에 decorator 포함되도록 기대값 조정 (해당 테스트가 있으면)
- `tests/test_processor/test_ast_code_extractor.py::test_to_graph_data_*` — `from x import y` 처리 후 relation 수 증가 (기대값 조정)
- `tests/test_processor/test_graph_vocabulary.py` (기존) — vocab 항목 수 증가 (4개 추가)
- `tests/test_ingestion/test_git_repository.py::test_store_git_code_*` — 시그니처에 cache 인자 추가되었으면 호환성 영향. 본 라운드는 **store_git_code의 기존 시그니처를 유지하고, sync_repository에서만 cache 사용** — store_git_code은 cache가 None이면 기존 동작 (호환성 보장)

## 라운드 2: 정밀도/완전성 보강

### 포함 항목

청킹: F-03 (CJK 폴백 추정), F-04 (overlap 블록 단위), F-06 (intro section_path), F-09 (split 부모 흡수), F-12 (nested 헤딩 본문)
git_code 청킹: F-G-04 (nested class), F-G-06 (TS 화살표), F-G-07 (TS type/enum)
그래프: F-CG-03 (split LLM 정책), F-CG-04 (cross-unit entity), F-CG-05 (외부 URL), F-GG-03 (상속/구현), F-GG-05 (FQN join 키)

### 회귀 위험

- F-04 overlap 변경 → 모든 confluence_mcp 청크 텍스트 변경 (대규모 인덱싱 재처리 필요)
- F-CG-04 cross-unit → LLM 호출 비용 가능, 그래프 노드/엣지 수 증가
- F-GG-03 상속/구현 → 그래프 엣지 수 증가

## 라운드 3 / 보류

- F-GG-04 call graph (공수 L, 신중한 휴리스틱 필요)
- F-GG-01 stdlib/third/internal 구분 (heuristic 검증 필요)
- F-CG-10 동명 doc 충돌 (graph_store 영향 검토 필요)
- F-G-09, F-G-10, F-GG-06 정밀도 개선

## 검증 체크리스트

R1 구현 후:
- [ ] `pytest tests/test_processor/test_chunker.py -x -q`
- [ ] `pytest tests/test_processor/test_ast_code_extractor.py -x -q`
- [ ] `pytest tests/test_processor/test_graph_vocabulary.py -x -q`
- [ ] `pytest tests/test_processor/test_extraction_unit.py -x -q` (간접 영향 — chunker 폴백 통일)
- [ ] `pytest tests/test_ingestion/test_git_repository.py -x -q`
- [ ] `pytest tests/test_processor/test_pipeline.py -x -q` (전체 파이프라인 통합 회귀)
- [ ] `ruff check src/context_loop/processor src/context_loop/ingestion`
- [ ] 수동 점검: 작은 Python 파일에 대해 `extract_code_symbols` 호출하여 module symbol, 데코레이터 포함 확인
- [ ] 수동 점검: 작은 Go 파일에 대해 `extract_code_symbols` → imports에 정상 import만 들어감 확인

## 비범위 (이 라운드에서 명시적으로 다루지 않는 영역)

- `scripts/eval_search.py`, `scripts/build_synthetic_gold_set.py`, `src/context_loop/eval/*` — 별도 하네스(`eval-gold-set-improvement`/`rag-eval-audit`) 영역
- `graph_search_planner.py`, `query_expander.py`, `reranker.py` — 검색 단계(인덱싱 아님)
- `confluence_mcp.py`의 페이지 수집 정확성 — 수집 단계
- 사용자 UI/API 변경 — 본 라운드는 backend 정합성만
