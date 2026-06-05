# Git Code 인덱싱 파이프라인 분석

> 기준: 현재 HEAD (origin/main 머지 완료). 분석 전용 — 코드 동작 서술이며 개선점 제안이 아니다.
> 최종 검증: 2026-06-02. 1~5단계(수집~그래프추출)는 `processor/`·`ingestion/` 미변경으로 기존 서술 유효. **6단계 저장의 그래프 병합 로직은 R3(63e1fd3 / 51fc495, 2026-05-28)로 변경되어 현재 코드 기준 갱신함** — `entity_normalizer.normalize_entity_name` 정규화 키 병합 + `graph_merge_log` 관측 로그 도입.

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
  - 코드 심볼: **FQN 이름** `<file>::<parent>.<name>` 또는 `<file>::<name>` (`_symbol_fqn`, 라인 195). entity_type = 실제 symbol_type(function/method/class/struct/interface).
  - 부모 클래스: `<file>::<name>` (`_class_fqn`, 라인 205), entity_type="class".
- **관계** (라인 275-312):
  - **imports**: `Relation(source=file_title, target=module, relation_type="imports", label=import된 심볼 이름들)` (라인 298). `from x import a,b`의 심볼 이름을 label에 join(최대 20, 라인 293).
  - **contains**: 메서드→클래스 `Relation(source=<file>::<class>, target=<file>::<class>.<method>, relation_type="contains")` (라인 306-312).
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
- git_code는 `embed_text=meta_text`를 채운다 (라인 219) — 임베딩 입력이 본문과 다르므로 감사용 영속화 (create_chunk docstring, metadata_store.py:396-407). `section_index`는 None(AST 코드 경로에선 항상 None, metadata_store.py:406).

**그래프 저장 (`storage/graph_store.py`):** *(R3 변경 반영 — 2026-05-28 63e1fd3/51fc495)*
- `await graph_store.save_graph_data(document_id, graph_data)` (라인 138, async). SQLite `graph_nodes`/`graph_edges` + NetworkX 인메모리 그래프 동시 갱신. pipeline 호출부: pipeline.py:225-227.
- **정규화 키 병합** (라인 161-244): 엔티티마다 다음 순서로 처리.
  1. `normalized = normalize_entity_name(entity.name)` (graph_store.py:168). 정규화는 **graph_store 책임** — storage(metadata_store)는 받은 키를 그대로 신뢰(책임 분리).
  2. `await find_graph_node_by_entity(entity.name, entity.entity_type, normalized_name=normalized)` (라인 171-175 → metadata_store.py:540). 매칭 SQL이 R3에서 변경됨: 과거 `WHERE LOWER(entity_name)=LOWER(?)` → 현재 **`WHERE normalized_name = ? AND entity_type = ?`** (metadata_store.py:564-566). `idx_graph_nodes_normalized(normalized_name, entity_type)` 인덱스 활용(LOWER() 함수 제거로 인덱스 적용 가능).
  3. **기존 노드 존재(병합)**: `node_id` 재사용 + `add_node_document_link(node_id, document_id)`. description이 비어 있으면 `update_graph_node_properties`로 보강. NetworkX `document_ids`에 document_id 추가 (라인 177-201).
  4. **신규**: `create_graph_node_with_link(document_id, entity_name, entity_type, properties, normalized_name=normalized)` (라인 220-226 → metadata_store.py:472). `graph_nodes` INSERT(normalized_name 포함) + `graph_node_documents` link INSERT를 **단일 commit**으로 묶어 고아노드 정리 SQL과의 FK-violation race 제거.
- **`normalize_entity_name` 규칙** (entity_normalizer.py:39): ① NFKC → ② strip → ③ 연속공백→단일공백 → ④ `[\s\-_]+` 전부 제거(빈문자 join) → ⑤ `lower()`. 결정적·idempotent·None/빈문자 안전(`""` 반환). 괄호/버전 표기(`(v2)`)는 **보존**(false-merge 회피). source-agnostic(git_code·confluence 공유).
  - git_code 영향: 코드 심볼은 FQN(`<file>::<parent>.<name>`)을 entity_name으로 쓰므로 정규화 후에도 파일/클래스 범위가 키에 남아 동명 심볼 오병합을 막는다. 단 정규화가 구분자(`.`/`::`는 미제거, `-`/`_`/공백만 제거)·대소문자를 흡수하므로, 예컨대 `auth_service`·`AuthService`는 같은 키(`authservice`)로 수렴 → 동일 FQN을 다른 표기로 쓴 노드가 병합될 수 있음(현재 동작 그대로 기술).
- **`graph_merge_log` 관측 로그** (R3 신규): 엔티티 처리마다 `_record_merge_safely()` (graph_store.py:298)가 `record_graph_merge()` (metadata_store.py:651) 호출로 1행 INSERT. `merge_method` 분류 — **`'exact'`**(canonical 원본 `entity_name`이 입력 `entity.name`과 정확 동일, 라인 206), **`'normalized'`**(정규화 키로만 일치=표기변형 흡수), **`'new'`**(신규 생성, 라인 236-242). `similarity_score`는 binary 매칭이라 항상 `None`. **로그 INSERT 실패는 swallow**(graph_store.py:323-329) — 그래프 저장 critical path 보호.
- 엣지: `(src, tgt, relation_type, document_id)` 중복 방지(라인 257-265) 후 `create_graph_edge`(metadata.py). label은 `properties.label`로 직렬화(라인 267).
- 반환 `{"nodes": 신규수, "edges": 엣지수, "merged": 병합수}` (라인 296).

**스키마 / 마이그레이션 (R3):**
- `graph_nodes.normalized_name TEXT NOT NULL DEFAULT ''` 컬럼(metadata_store.py:55). `graph_merge_log` 테이블(라인 80-89): `canonical_node_id, raw_entity_name, raw_entity_type, source_document_id, merge_method, similarity_score, created_at`.
- 인덱스 생성 순서 수정(51fc495): `idx_graph_nodes_normalized`는 `_SCHEMA_SQL`의 executescript에서 제외하고 **`_migrate_schema`가 ALTER TABLE 이후 항상 생성**(metadata_store.py:222-227, if 블록 밖). legacy DB(R3 이전)에서 컬럼 없이 CREATE INDEX가 "no such column" 으로 실패하던 회귀 해소.
- 백필: `_backfill_normalized_names()` (라인 233)가 `normalized_name=''` 행만 idempotent하게 채움(executemany). 신규 DB는 no-op.

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
| 노드 병합 키 (R3) | `normalized_name`+`entity_type` | metadata_store.py:564 / entity_normalizer.py:39 | 6 저장 |
| 정규화 규칙 (R3) | NFKC→strip→공백압축→`[\s\-_]` 제거→lower | entity_normalizer.py:39 | 6 저장 |
| merge_method | exact / normalized / new | graph_store.py:206,236 | 6 저장 |

## 부록 B: 데이터 모델

- `FileInfo` (git_repository.py:76): relative_path, absolute_path, product, content, content_hash, size_bytes
- `CodeSymbol` (ast_code_extractor.py:31): name, symbol_type, signature, body, line_start, line_end, docstring, parent_name, parent_signature
- `CodeExtraction` (ast_code_extractor.py:58): file_path, language, symbols, imports, import_symbols
- `Chunk` (chunker.py:43): id, index, content, token_count, section_path, section_anchor, section_index
- `Entity`/`Relation`/`GraphData` (graph_extractor.py:15/30/48)
- `graph_nodes` (metadata_store.py:49): id, document_id, entity_name, entity_type, properties, **normalized_name**(R3)
- `graph_merge_log` (metadata_store.py:80, R3): id, canonical_node_id, raw_entity_name, raw_entity_type, source_document_id, merge_method, similarity_score, created_at

## 검토하지 못한 영역

- `scope_analyzer.py::resolve_product_paths` 자동 탐지 알고리즘 상세
- `coordinator.py`가 sync→process_document를 어떻게 트리거/스케줄하는지 (sync는 documents만 저장, process_document는 별도 호출)
- `reprocessor.py::start_reprocessing/complete_reprocessing`의 파생 데이터 삭제 범위 상세
- `storage/cascade.py::delete_document_cascade` (purge 경로)
- 고아 노드/엣지 정리는 `graph_store.delete_document_graph` (graph_store.py:331) → `delete_graph_data_by_document`(SQLite)에서 트리거되며, 신규 노드 생성 시 `create_graph_node_with_link`가 노드+링크를 단일 commit으로 묶어 정리 SQL과의 FK race를 차단(metadata_store.py:481-517). 정리 SQL 자체의 다른 호출 경로(coordinator/reprocessor 동시성)는 미추적.
- `graph_merge_log`를 소비하는 분석/대시보드 경로(get_graph_merge_log/그래프 탐색 탭, 594d27a·996cee7)는 인덱싱 범위 밖이라 미검토.
