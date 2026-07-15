# Git Code 인덱싱 파이프라인 분석

> 기준: 현재 HEAD `cdf291e`. 분석 전용 — 코드 동작 서술이며 개선점 제안이 아니다.
> 갱신: 2026-07-13 산출물을 커밋 `79cdcde`(그래프 어휘 alias 정규화) 이후 코드와 대조하여 보완. 변경 요약은 문서 맨 끝 "2026-07-13 대비 달라진 부분" 참조.

> **문서-코드 대조 결과 (핵심):** 과제 지시문은 `79cdcde`가 "processor/ingestion 코드를 변경"했다고 했으나, 실제 그 커밋이 건드린 소스는 `processor/graph_vocabulary.py`, `storage/graph_store.py`, `processor/llm_body_extractor.py`(confluence 전용) 3개뿐이다 (`git show 79cdcde --stat` 확인). **ingestion(1단계)과 `ast_code_extractor`(2·3·5단계 추출부)는 이 커밋으로 바뀌지 않았다.** git_code에 실제로 영향을 준 지점은 **6단계 저장 시점의 어휘 정규화**이며, 5단계 서술의 entity_type도 "저장 시점 정규화"를 반영해 보정한다. (07-13 이후 src를 건드린 커밋은 `79cdcde`와 `389ed59`(confluence 워터마크 수정, git_code 무관) 둘뿐 — `git log --since=2026-07-13 -- src/` 확인.)

## 0. 진입점 & 전체 흐름

git_code 문서의 인덱싱은 두 개의 별개 흐름으로 나뉜다.

1. **수집 흐름** — `ingestion/git_repository.py::sync_repository()` (라인 502). clone/pull → 변경 감지 → 파일 필터 → `documents` 테이블에 `source_type='git_code'` 레코드 저장. **이 단계는 파생 데이터(청크/그래프)를 만들지 않는다.** 원본 코드만 SQLite `documents.original_content`에 저장한다.
2. **처리 흐름** — `processor/pipeline.py::process_document()` (라인 93). 저장된 문서 ID를 받아 AST 추출 → 청킹 → 임베딩 → 그래프 추출 → 저장.

`process_document`의 분기 (pipeline.py:153):

```python
if source_type == "git_code":
    extraction = extract_code_symbols(content, title)       # 2. AST 전처리
    chunks, meta_texts = to_chunks(extraction, title)       # 3. 청킹
    ... embedding_client.aembed_documents(to_embed) ...      # 4. 임베딩 (body+meta 2뷰)
    vector_store.add_chunks(...)                             # 6. 벡터 저장
    ... meta_store.create_chunk(...) ...                     # 6. SQLite 청크 저장
    graph_data = to_graph_data(extraction, title)           # 5. 그래프 추출
    graph_store.save_graph_data(document_id, graph_data)     # 6. 그래프 저장
```

**중요 사실:**
- git_code 분기는 **LLM을 전혀 호출하지 않는다.** 순수 정적 분석(AST)으로만 동작 (ast_code_extractor.py:4-5). confluence 분기의 LLM 본문 그래프/가상 질문 생성과 대비된다.
- `storage_method`는 처리 결과에서 **파생**된다 (`_derive_storage_method`, pipeline.py:542). 청크와 그래프가 모두 생기면 `hybrid`, 그래프만 있으면 `graph`, 기본은 `chunk`. git_code는 심볼이 있으면 보통 `hybrid`가 된다. **classifier(LLM 판정)는 현재 코드 경로에 없다** — CLAUDE.md의 "LLM Classifier"는 과거 설계이며 현재는 결과 파생 방식이다 (pipeline.py:11-16).

```
sync_repository (git clone/pull)
   └─> documents 테이블 (source_type=git_code, original_content=코드)
          │ (별도 트리거)
          ▼
process_document(document_id)
   ├─ 2. extract_code_symbols → CodeExtraction(symbols[], imports[], import_symbols[])
   ├─ 3. to_chunks → (Chunk[], meta_texts[])   ※ 심볼 1개 = 청크 1개
   ├─ 4. aembed_documents(body + meta)         ※ 청크당 2개 임베딩
   ├─ 6. vector_store.add_chunks + meta_store.create_chunk
   ├─ 5. to_graph_data → GraphData(entities[], relations[])
   └─ 6. graph_store.save_graph_data
```

---

## 1. 데이터 수집

**진입 함수:** `ingestion/git_repository.py::sync_all_repositories()` (라인 604) → `sync_repository()` (라인 502)

**데이터 출처:** Git 레포지토리 (설정 `sources.git.repositories`의 URL). 로컬에 clone한 뒤 파일시스템에서 읽는다.

**처리 순서 (sync_repository, 라인 509-601):**
1. **clone 또는 pull** — `clone_or_pull(repo_url, clone_dir, branch)` (라인 224). `.git`이 있으면 `git pull`, 없으면 `git clone --branch <branch> --single-branch`. clone 위치는 `_repo_clone_dir()` (라인 493): `<data_dir>/git_repos/<repo_name>`. pull이면 pull 직전 커밋 해시를 반환한다.
2. **변경 감지** — `get_changed_files(clone_dir, prev_commit)` (라인 267). `prev_commit`이 None(최초 clone)이면 None 반환→전체 처리. 아니면 `git diff --name-only <prev> <current>`로 변경 파일만. 변경 없으면 `[]` 반환→조기 종료 (라인 546).
3. **상품 스코프 파싱** — `parse_product_scopes(repo_config)` (라인 117). 레포 설정의 `products`에서 `ProductScope(name, paths, exclude)` 생성. `paths`가 비면 `scope_analyzer.resolve_product_paths()`로 자동 탐지.
4. **파일 수집** — `collect_files()` (라인 316). 전체 모드는 `clone_dir.rglob("*")`로 순회. 필터 단계:
   - `_has_excluded_part()` (라인 61): 경로에 `_DEFAULT_EXCLUDED_DIRS`(`.git`, `node_modules`, `.venv`, `dist`, `build`, `vendor`, `__pycache__` 등 27개, 라인 33) segment가 있으면 제외.
   - `filter_file()` (라인 185): 확장자가 `supported_extensions`에 있어야 하고, 크기 ≤ `file_size_limit_kb * 1024`.
   - `match_product()` (라인 171): exclude glob에 안 걸리고 paths glob에 매칭되는 첫 상품에 배정. 어떤 상품에도 안 맞으면 제외.
   - UTF-8로 못 읽으면 건너뜀 (라인 369-373, 바이너리 자연 제외).
5. **문서 저장** — 각 `FileInfo`마다 `store_git_code()` (라인 389):
   - `source_id = relative_path` (라인 410).
   - 신규: `meta_store.create_document(source_type="git_code", source_id=상대경로, title=파일명, original_content=내용, content_hash=SHA-256, url=repo_url, author=product)` (라인 423). `add_processing_history(action="created")`.
   - 기존 + hash 동일: 변경 없음 (라인 441).
   - 기존 + hash 다름: `update_document_content` + `update_document_status(status="changed")` + `add_processing_history(action="updated")` (라인 444-456).
6. **삭제 처리** — `get_deleted_files()` (`git diff --diff-filter=D`) → `delete_removed_files()` (라인 462). 삭제된 source_id의 문서 제거.

**산출 데이터 형태:** `documents` 테이블의 git_code 레코드. `FileInfo`(라인 76): `relative_path, absolute_path, product, content, content_hash, size_bytes`. 코드는 `original_content`에 평문 그대로 저장 (HTML 변환 없음 — confluence와의 핵심 차이).

**주요 파라미터:**
| 이름 | 기본값 | 정의 위치 | 영향 |
|------|--------|-----------|------|
| `branch` | `"main"` | `repo_config.get("branch")` git_repository.py:525 | clone/pull 대상 브랜치 |
| `supported_extensions` | (설정값) | `git_config.get(...)` :530 | 수집 대상 확장자 |
| `file_size_limit_kb` | `500` | `git_config.get(...)` :531 | 파일 크기 상한 |
| `_DEFAULT_EXCLUDED_DIRS` | 27개 디렉토리 | git_repository.py:33 | vendored/빌드 산출물 제외 |

---

## 2. 전처리/변환 (AST 추출)

**진입 함수:** `processor/ast_code_extractor.py::extract_code_symbols(content, file_path)` (라인 103)

**입력:** 코드 문자열 + 파일 경로(언어 감지용).

**처리 로직:**
1. 확장자 → 언어 매핑 `_LANG_MAP` (라인 86): `.py→python`, `.go→go`, `.java→java`, `.ts/.tsx→typescript`, `.js/.jsx→javascript`, 그 외 `unknown`.
2. 언어별 분기 (라인 121-126):
   - **Python**: `_extract_python()` (라인 322) — Python `ast` 모듈로 **정확한** 파싱. 추출 대상:
     - 모듈 docstring → `__module__` 심볼 (symbol_type="module", 라인 350-364)
     - 모듈 레벨 상수/타입 alias → `__constants__` 단일 심볼로 묶음 (라인 366-400)
     - top-level 함수 → `function` 심볼 (라인 403). 데코레이터 포함 (`_body_start_line`, 라인 479).
     - 클래스: 메서드가 있으면 **메서드별 개별 심볼**(`method`, `parent_name=클래스명`), 없으면 클래스 전체 1심볼(`class`) (라인 416-456).
     - import: `ast.Import`/`ast.ImportFrom` → `imports[]` + `import_symbols[]`(`(module, symbol)` 튜플, 상대 import는 `.` prefix) (라인 457-474).
   - **Brace 언어 (go/java/ts/js)**: `_extract_brace_language()` (라인 622) — **정규식 기반 휴리스틱**. 정확도 Python보다 낮음:
     - `_DEFINITION_PATTERNS` (라인 561)로 함수/클래스/struct/interface 감지 → `_find_matching_brace()`(문자열 인식 중괄호 매칭, 라인 886)로 본문 범위 결정.
     - Go 리시버 메서드 `func (s *Type) M(`는 `parent_name=Type` 기록 (라인 647).
     - Java/TS/JS 클래스는 `_extract_class_methods()`(라인 766)로 내부 메서드를 개별 심볼화.
     - `_RESERVED_SYMBOL_NAMES`(라인 614)로 `if`/`for` 등 false positive 차단.
     - import: `_extract_brace_imports()` (라인 846). Go는 `import` 키워드 anchor 별도 처리(`_extract_go_imports`, 라인 870)로 본문 문자열 리터럴 오인 방지.
   - **그 외(unknown)**: `_extract_fallback()` (라인 940) — 파일 전체를 단일 `module` 심볼로.
3. 파싱 중 예외 발생 시 `_extract_fallback`으로 폴백 (라인 127-132).

**산출 데이터 형태:** `CodeExtraction`(라인 58): `file_path, language, symbols: list[CodeSymbol], imports: list[str], import_symbols: list[(module, symbol|None)]`. `CodeSymbol`(라인 31): `name, symbol_type, signature, body, line_start, line_end, docstring, parent_name, parent_signature`.

**Python vs Brace 핵심 차이:**
| 항목 | Python (ast) | Brace (정규식) |
|------|-------------|---------------|
| 정확도 | AST 기반 정확 | 휴리스틱, 멀티라인 시그니처/제네릭 누락 가능 |
| 모듈 docstring/상수 | 별도 심볼 추출 | 없음 |
| import_symbols | `(module, symbol)` 정밀 | `imports[]`만 (import_symbols 비어있음) |
| 데코레이터/어노테이션 | body에 포함 | 미보장 |

---

## 3. 청킹

**진입 함수:** `processor/ast_code_extractor.py::to_chunks(extraction, file_title)` (라인 143)

**입력:** `CodeExtraction`.

**처리 로직:** **심볼 1개 = 청크 1개** (토큰 기반 분할 없음, 라인 160).
- 청크 `content` = 헤더 + 심볼 body. 헤더는 `# File: <title>` + (메서드면) `# <parent_signature>` + `# <symbol_type>: <signature>` (라인 162-173).
- `section_path` = `"<file> > <parent> > <name>"` (메서드) 또는 `"<file> > <name>"` (라인 168-171).
- `Chunk`(chunker.py:43): `id=uuid4, index, content, token_count=count_tokens(content), section_path`. `section_anchor`/`section_index`는 git_code에서 비어있음/None.
- **meta_texts** (별도 반환): `file_title + parent + name + signature + docstring` 줄바꿈 결합 (라인 184-190). 이것이 meta 뷰 임베딩 입력이 된다.

**산출 데이터 형태:** `(chunks: list[Chunk], embed_texts: list[str])` 튜플.

**핵심 사실:** git_code는 토큰 한도(512/8000)로 자르지 않는다. 거대 함수도 통째로 1청크. `count_tokens`(chunker.py:94)는 tiktoken `cl100k_base`, 없으면 1 char=1 token 폴백.

---

## 4. 임베딩

(임베딩 클라이언트 자체는 **confluence와 공유** — `processor/embedder.py`)

**진입:** `pipeline.py:174-178` — git_code 분기 내부.

**처리 로직 (멀티뷰, D-042/I-046, 라인 165-202):**
- `body_texts = [c.content for c in chunks]` (코드 본문, 자연어 도메인 용어/주석 친화)
- `to_embed = body_texts + meta_texts` (두 리스트 연결, 한 번에 임베딩)
- `embeddings = await embedding_client.aembed_documents(to_embed)` → 앞쪽 N개=body, 뒤쪽 N개=meta로 분할 (라인 177-178).
- 청크당 벡터 엔트리 **2개** 생성: `<chunk_id>#body`(view="body"), `<chunk_id>#meta`(view="meta") (라인 194-202). 둘 다 `documents` 컬럼에는 동일 `chunk.content` 저장. `logical_chunk_id`(=chunk.id) 공유 → 검색 dedup에서 흡수.

**임베딩 클라이언트 (embedder.py):**
- `EndpointEmbeddingClient`(라인 25): OpenAI 호환 REST. `aembed_documents`(라인 81)가 `_BATCH_SIZE=100`(라인 22) 단위로 `POST {url} {"input": batch, "model": model}` 호출. 응답 `data[].embedding`을 index 순 정렬(라인 56).
- `LocalEmbeddingClient`(라인 122): sentence-transformers (기본 `all-MiniLM-L6-v2`), executor에서 실행.
- 모델은 `PipelineConfig.embedding_model`(기본 `"text-embedding-3-small"`, pipeline.py:76)로 토큰 카운팅에 쓰이지만, 실제 임베딩 모델 ID는 클라이언트 생성 시 주입.

**주요 파라미터:**
| 이름 | 기본값 | 정의 위치 | 영향 |
|------|--------|-----------|------|
| `_BATCH_SIZE` | `100` | embedder.py:22 | 임베딩 배치 크기 |
| `embedding_model` | `"text-embedding-3-small"` | pipeline.py:76 | 토큰화 모델 |
| timeout | `60.0` | embedder.py:35 | HTTP 타임아웃 |

**git_code는 가상 질문(question) 뷰가 없다** — confluence 분기에만 존재 (pipeline.py:267-).

---

## 5. 그래프 추출

**진입 함수:** `processor/ast_code_extractor.py::to_graph_data(extraction, file_title)` (라인 210)

**입력:** `CodeExtraction`.

**처리 로직 (LLM 없음, 순수 AST):**
- **엔티티** (라인 224-273):
  - 파일 엔티티 1개: `Entity(name=file_title, entity_type="module")` (단순 이름, 전역 고유).
  - import 모듈: 단순 이름 `module` 엔티티 (파일 간 canonical 병합 의도 — 여러 파일이 `logging` import 시 1노드로 수렴).
  - 코드 심볼: **FQN 이름** `<file>::<parent>.<name>` 또는 `<file>::<name>` (`_symbol_fqn`). entity_type = **추출기가 emit하는 실제 symbol_type** `sym.symbol_type` (ast_code_extractor.py:257) — function/method/class/**struct**/interface. Go `type X struct`는 `struct`로, `type X interface`는 `interface`로 감지된다 (라인 564-565).
  - 부모 클래스: `<file>::<name>` (`_class_fqn`), entity_type="class" (라인 271).
  - **[79cdcde 반영] 저장 시점 entity_type 정규화:** 추출기는 여전히 `struct`를 그대로 emit하지만, 6단계 `save_graph_data`가 `canonical_entity_type()`로 **`struct→class`**로 수렴시킨다 (graph_vocabulary.py `ENTITY_TYPE_ALIASES`, graph_store.py:199). `interface`는 alias가 아니라 그대로 유지된다. 즉 그래프 노드에 최종 저장되는 git_code entity_type은 function/method/class/interface/module 5종이며, Go struct는 class 노드로 병합된다.
- **관계** (라인 275-312):
  - **imports**: `Relation(source=file_title, target=module, relation_type="imports", label=import된 심볼 이름들)` (라인 298-303). `from x import a,b`의 심볼 이름을 label에 join(최대 20, 라인 293). Python 외 언어처럼 `import_symbols`가 비면 `imports`로 폴백(라인 287-288).
  - **contains**: 메서드→클래스 `Relation(source=<file>::<class>, target=<file>::<class>.<method>, relation_type="contains")` (라인 306-312).
  - **[79cdcde 대조] git_code 관계는 저장 정규화의 영향을 받지 않는다:** `RELATION_TYPE_ALIASES`의 키는 `has_part/has_attribute/uses/mentions_user/mentions_ticket/documented_in` 6종인데, git_code가 emit하는 `imports`·`contains`는 이미 canonical relation_type이라 alias에 없다 (graph_vocabulary.py:180-186). 따라서 이름 변경·방향 교환 없이 그대로 저장된다. (참고: `contains`는 이 커밋에서 confluence body/llm_body의 `has_part`/`has_attribute`까지 흡수하는 공용 canonical이 되었다 — git_code가 원래 쓰던 `contains`가 canonical로 채택된 형태.)
  - **호출 그래프(call graph)는 추출하지 않음** — 함수 호출 `foo()`는 엣지가 되지 않는다. 상속/구현(extends/implements)도 관계로 추출되지 않는다 (graph_extractor의 vocab에는 있으나 AST 추출기가 emit하지 않음).

**산출 데이터 형태:** `GraphData(entities: list[Entity], relations: list[Relation])` (graph_extractor.py:48). `Entity(name, entity_type)`, `Relation(source, target, relation_type, label)`.

**FQN 설계 이유 (라인 216-222):** 심볼은 파일 범위 FQN을 써서 서로 다른 파일/클래스의 동명 심볼이 graph_store의 `(name, type)` canonical 병합으로 잘못 합쳐지는 것을 방지. 반면 파일/import 모듈은 단순 이름으로 의도적 병합.

---

## 6. 저장

(저장소 3종 모두 **confluence와 공유**)

**벡터 저장 (`storage/vector_store.py`, ChromaDB):**
- `vector_store.delete_by_document(document_id)` (라인 71): 재처리 시 기존 청크 삭제 (`where={"document_id": ...}`).
- `vector_store.add_chunks(ids, embeddings, documents, metadatas)` (라인 47): ChromaDB `collection.add`. 컬렉션명 `context_loop_chunks`, 코사인 거리 (`hnsw:space: cosine`, 라인 37).
- git_code 메타데이터: `document_id, chunk_index, title, section_path, section_anchor, logical_chunk_id, view`(body/meta) (pipeline.py:186-202).

**SQLite 청크 (`storage/metadata_store.py`):**
- `meta_store.delete_chunks_by_document` → `meta_store.create_chunk(...)` 루프 (pipeline.py:209-221).
- git_code는 `embed_text=meta_text`를 채운다 (라인 219) — 임베딩 입력이 본문과 다르므로 감사용 영속화 (create_chunk docstring, metadata_store.py:327). `section_index`는 None.

**그래프 저장 (`storage/graph_store.py`):**
- `graph_store.save_graph_data(document_id, graph_data)` (라인 171). SQLite `graph_nodes`/`graph_edges` + NetworkX 인메모리 그래프 동시 갱신.
- **[79cdcde 신규] 어휘 alias 정규화 — 저장 시점 1회 적용:** 결정론 추출기(ast_code_extractor)는 자체 어휘를 그대로 emit하고, 저장 직전 여기서 canonical로 수렴시킨다.
  - **entity_type**: `entity_type = canonical_entity_type(entity.entity_type)` (라인 199). 이 정규화된 타입으로 병합 키 검색·신규 생성이 이뤄진다. git_code에서는 `struct→class` 수렴이 발생 → 같은 파일의 Go struct와 여타 class-타입 노드가 동일 병합 네임스페이스를 공유.
  - **relation**: `rel_type, rel_source, rel_target = canonical_relation(relation.relation_type, source, target)` (라인 287-289). 이름 정규화 + 역방향 alias 시 source/target 교환. **git_code의 `imports`/`contains`는 alias가 아니므로 무변화** (위 5단계 참조).
  - **주의(감사 로그):** 병합 로그 `_record_merge_safely`에는 정규화 **이전** 원본 타입 `raw_entity_type=entity.entity_type`가 기록된다 (라인 247·276). 실제 노드에 저장되는 타입(canonical)과 감사 로그의 raw 타입이 다를 수 있다.
- **정규 병합** (라인 194-281): `find_graph_node_by_entity(entity.name, entity_type=canonical, normalized_name=...)` (metadata_store.py:638). 현재 병합 키는 SQL `WHERE normalized_name = ? AND entity_type = ?` (metadata_store.py:663) — 대소문자/공백/하이픈/언더스코어 표기 변형을 흡수하는 `normalize_entity_name` 정규화 이름 + canonical entity_type. 기존 노드 있으면 재사용+document_id 링크 추가(병합), 없으면 `create_graph_node_with_link()`로 신규(노드+문서링크 단일 트랜잭션, race 방지).
- 엣지: `name_to_node_id`는 **원본 `entity.name`** 으로 키잉되고(라인 281), 관계는 정규화된 `rel_source`/`rel_target`로 조회(라인 290-291). `(src, tgt, relation_type, document_id)` 중복 방지(라인 300-307) 후 `create_graph_edge`.
- 반환 `{"nodes": 신규수, "edges": 엣지수, "merged": 병합수}`.

**문서 상태 마무리:** `_derive_storage_method(has_chunks, has_graph)` (pipeline.py:542) → `complete_reprocessing(meta_store, document_id, history_id, storage_method)` (라인 503). 예외 시 `storage_method="chunk"` + error_message 기록 (라인 531-538).

---

## 부록 A: 주요 파라미터 표 (git_code 전 단계)

| 이름 | 기본값 | 위치 | 단계 |
|------|--------|------|------|
| branch | "main" | git_repository.py:525 | 1 수집 |
| supported_extensions | 설정값 | git_config | 1 수집 |
| file_size_limit_kb | 500 | git_repository.py:531 | 1 수집 |
| _DEFAULT_EXCLUDED_DIRS | 27개 | git_repository.py:33 | 1 수집 |
| _LANG_MAP | 7확장자 | ast_code_extractor.py:86 | 2 전처리 |
| (심볼=청크, 토큰분할 없음) | — | ast_code_extractor.py:160 | 3 청킹 |
| embedding_model | text-embedding-3-small | pipeline.py:76 | 4 임베딩 |
| _BATCH_SIZE | 100 | embedder.py:22 | 4 임베딩 |
| FQN 규칙 | `<file>::<parent>.<name>` | ast_code_extractor.py:195 | 5 그래프 |
| import label 상한 | 20 | ast_code_extractor.py:293 | 5 그래프 |
| ChromaDB 거리 | cosine | vector_store.py:37 | 6 저장 |
| 노드 병합 키 | LOWER(name)+type | metadata_store.py:455 | 6 저장 |

## 부록 B: 데이터 모델

- `FileInfo` (git_repository.py:76): relative_path, absolute_path, product, content, content_hash, size_bytes
- `CodeSymbol` (ast_code_extractor.py:31): name, symbol_type, signature, body, line_start, line_end, docstring, parent_name, parent_signature
- `CodeExtraction` (ast_code_extractor.py:58): file_path, language, symbols, imports, import_symbols
- `Chunk` (chunker.py:43): id, index, content, token_count, section_path, section_anchor, section_index
- `Entity`/`Relation`/`GraphData` (graph_extractor.py:15/30/48)

## 검토하지 못한 영역

- `scope_analyzer.py::resolve_product_paths` 자동 탐지 알고리즘 상세
- `coordinator.py`가 sync→process_document를 어떻게 트리거/스케줄하는지 (sync는 documents만 저장, process_document는 별도 호출)
- `reprocessor.py::start_reprocessing/complete_reprocessing`의 파생 데이터 삭제 범위 상세
- `storage/cascade.py::delete_document_cascade` (purge 경로)
- NetworkX 그래프의 고아 노드/엣지 정리 SQL의 실제 트리거 시점
