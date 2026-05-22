# Confluence MCP 인덱싱 파이프라인 분석

> 기준: 현재 HEAD (origin/main 머지 완료). 분석 전용 — 코드 동작 서술이며 개선점 제안이 아니다.

## 0. 진입점 & 전체 흐름

confluence_mcp 문서도 git_code처럼 **수집 흐름**과 **처리 흐름**이 분리된다.

1. **수집 흐름** — `ingestion/mcp_confluence.py::import_page_via_mcp()` (라인 959). MCP 서버에서 페이지 가져옴 → HTML→MD 변환 → `documents` 테이블에 저장. **원본 HTML(`raw_content`)과 마크다운(`original_content`)을 모두** 저장하는 것이 핵심 (라인 999). git_code는 `raw_content`가 없다.
2. **처리 흐름** — `processor/pipeline.py::process_document()` (라인 93). `else` 분기(git_code가 아닌 모든 소스)로 들어가며, 그 안에서 `source_type in ("confluence", "confluence_mcp")` 이고 `raw_content`가 있을 때 구조화 추출 (라인 238).

`process_document`의 confluence 분기 (pipeline.py:231-496):

```python
else:  # git_code가 아닌 경로
    raw_html = doc.get("raw_content")
    if source_type in ("confluence","confluence_mcp") and raw_html:
        extracted = extract_confluence(raw_html)               # 2. 전처리(HTML→구조화)
    chunks = chunk_extracted_document_doclevel(extracted, ...)  # 3. 청킹(문서단위)
    # 4. 임베딩: body + meta + 가상질문 멀티뷰
    ... generate_questions_for_document(...) ...               # (LLM) 가상질문
    embeddings = await embedding_client.aembed_documents(to_embed)
    vector_store.add_chunks(...); meta_store.create_chunk(...)  # 6. 저장
    # 5. 그래프: 링크 + 본문(휴리스틱) + LLM 의미관계
    build_link_graph(...)            → save_graph_data           # 5a 링크 그래프
    extract_body_graph(units, ...)   → save_graph_data           # 5b 본문 그래프(결정론)
    extract_llm_body_graph_for_document(...) → save_graph_data    # 5c LLM 의미 그래프
```

**중요 사실:**
- confluence는 **3중 그래프 추출**을 한다: 링크(결정론) + 본문 휴리스틱(결정론) + LLM 의미관계(opt-in). git_code는 AST 1종뿐.
- LLM 호출이 **2곳**: 가상 질문 생성(`enable_question_indexing`, 기본 ON) + LLM 본문 그래프(`enable_llm_body_extraction`, 기본 ON). 둘 다 `llm_client`가 주입돼야 동작.
- `storage_method`는 결과 파생(`_derive_storage_method`, pipeline.py:542). 청크+그래프 모두 있으면 hybrid.

```
import_page_via_mcp (MCP get_page → HTML→MD)
   └─> documents (source_type=confluence_mcp, original_content=MD, raw_content=HTML)
          │ (별도 트리거)
          ▼
process_document(document_id)
   ├─ 2. extract_confluence(raw_html) → ExtractedDocument(sections, outbound_links, code_blocks, tables, mentions, plain_text)
   ├─ 3. chunk_extracted_document_doclevel → Chunk[]  ※ 작은 문서=1청크, 큰 문서=섹션 폴백
   ├─ (LLM) generate_questions_for_document → {section_index: [questions]}
   ├─ 4. aembed_documents(body + meta + questions)  ※ 청크당 최대 3종 뷰
   ├─ 6. vector_store.add_chunks + meta_store.create_chunk
   ├─ 5a build_link_graph → save_graph_data
   ├─ 5b build_extraction_units → extract_body_graph → save_graph_data
   └─ 5c extract_llm_body_graph_for_document → save_graph_data
```

---

## 1. 데이터 수집

**진입 함수:** `ingestion/mcp_confluence.py::import_page_via_mcp(session, store, page_id)` (라인 959)

**데이터 출처:** Confluence — **MCP(Model Context Protocol) 서버**를 통해 접근. `connect_mcp()`(라인 98)로 MCP 세션을 열고, `get_page(session, page_id)`(라인 368)가 JSON-RPC tool call로 페이지를 가져온다. (REST 직접 호출이 아니라 MCP 경유가 confluence_mcp의 정체성.) 공간/하위트리 열거는 `enumerate_space_pages`(라인 738)/`walk_subtree`(라인 551), CQL 검색은 `search_content`(라인 247).

**처리 순서 (import_page_via_mcp, 라인 979-1029):**
1. `get_page(session, page_id)` → page_data (라인 979).
2. `_extract_page_content(page_data)` (라인 915): `markdown/content/body/value/text` 순으로 본문 추출. body가 dict면 `body.storage.value`/`view`/`export_view`에서 HTML 추출 → `html_body`.
3. `_extract_page_title(page_data, page_id)` (라인 937): `title`/`name`/폴백.
4. **HTML→MD 변환**: `convert_html_to_markdown(html_body)` (라인 942) → `html_converter.html_to_markdown`에 위임. 결과가 `content`(마크다운).
5. `content_hash = compute_content_hash(content)` (SHA-256).
6. 기존 문서 조회(source_id=page_id):
   - 신규: `create_document(source_type="confluence_mcp", source_id=page_id, title, original_content=content(MD), content_hash, raw_content=html_body)` (라인 993). **`raw_content`에 원본 HTML 저장** — 처리 단계의 구조화 추출 입력. `add_processing_history(action="created", status="started")`.
   - hash 동일: 변경 없음 (라인 1010).
   - hash 다름: `update_document_content(... raw_content=html_body)` + `status="changed"` + history(action="updated") (라인 1013-1026).

**산출 데이터 형태:** `documents` 레코드. `original_content`=마크다운(검색 표시/폴백용), `raw_content`=원본 HTML(처리 단계 구조화 추출의 진짜 입력).

**주요 파라미터:**
| 이름 | 값/출처 | 위치 | 영향 |
|------|---------|------|------|
| SOURCE_TYPE | "confluence_mcp" | mcp_confluence.py(상수) | 문서 분류 |
| body 추출 우선순위 | markdown>content>body>... | mcp_confluence.py:921 | 본문 키 선택 |
| body.dict 키 | storage>view>export_view | :928 | HTML 소스 |

---

## 2. 전처리/변환 (HTML → 구조화)

**진입 함수:** `ingestion/confluence_extractor.py::extract(html)` (라인 132). pipeline.py:239에서 `raw_content`(원본 HTML)로 호출.

**입력:** Confluence Storage Format HTML (`raw_content`). **마크다운(`original_content`)이 아니라 원본 HTML을 쓴다** — 매크로/링크/표 구조를 보존하기 위해.

**처리 로직:** BeautifulSoup 1회 파싱(`html.parser`, 라인 144)으로 5종 정보 추출:
1. **sections** — `_extract_sections()` (라인 167): h1~h6 스캔. 각 헤딩의 `level, title, anchor(_slugify), path(헤딩 계층 경로), md_content`. `md_content`는 헤딩 다음~동일/상위 레벨 헤딩 직전 형제 요소를 `_section_body_markdown()`(라인 206)으로 MD 변환.
2. **outbound_links + mentions** — `_extract_links_and_mentions()` (라인 235): `ac:link`에서 `ri:page`(kind=page, content-id/title/space), `ri:user`(kind=user + Mention), `ri:attachment`(kind=attachment) 추출. 일반 `<a href>`는 kind=url. `ac:structured-macro[name=jira]`는 kind=jira + Mention. 각 링크에 `in_section`(등장 섹션 경로) 부착(`_locate_section`, 라인 428).
3. **code_blocks** — `_extract_code_blocks()` (라인 332): `ac:structured-macro[name=code/noformat]` + `<pre><code>`. language 태그 보존.
4. **tables** — `_extract_tables()` (라인 391): `<table>`의 헤더(전부 th인 행)/행 구조화.
5. **plain_text** — `html_to_markdown(html)` (라인 150): 전체 HTML을 마크다운으로 평탄화.

**산출 데이터 형태:** `ExtractedDocument`(라인 116): `plain_text, sections: list[Section], outbound_links: list[OutLink], code_blocks: list[CodeBlock], tables: list[Table], mentions: list[Mention]`. `Section`(라인 32): level, title, anchor, path, md_content. `OutLink`(라인 51): kind, target_id, target_title, target_space, anchor_text, in_section.

**핵심 사실:** `extracted`는 청킹/링크그래프/본문그래프/반환 메트릭에 모두 재사용된다. `raw_content`가 없으면(예: upload 소스) 이 단계를 건너뛰고 `chunk_text(original_content)`로 폴백 (pipeline.py:259-265).

**주요 파라미터:**
| 이름 | 값 | 위치 |
|------|-----|------|
| _HEADING_TAGS | h1~h6 | confluence_extractor.py:27 |
| _CODE_MACRO_NAMES | code, noformat | :28 |
| 파서 | html.parser | :144 |

---

## 3. 청킹

**진입 함수:** `processor/chunker.py::chunk_extracted_document_doclevel(extracted, max_tokens, model)` (라인 387). pipeline.py:254에서 호출.

**입력:** `ExtractedDocument` + `max_tokens=cfg.max_embedding_tokens(8000)`.

**처리 로직 (R3 문서 단위 청킹):**
1. sections 없으면 → `_chunk_plain_with_fallback`(plain_text를 1청크 또는 토큰분할) (라인 424).
2. **문서 전체 합본**(헤딩+md_content 이어붙임) 토큰 ≤ max_tokens → **문서 전체를 1청크** (`section_path=""`, `section_index=None`) (라인 440-448). ← 작은 문서의 기본 경로.
3. 한도 초과 → `_chunk_by_section()`(라인 481): 섹션별 1청크. 단일 섹션이 또 초과면 `_chunk_blocks`로 토큰 분할(코드/표는 atomic 보호).

**산출 데이터 형태:** `list[Chunk]`. 1청크 문서는 `section_index=None`, 다청크는 `section_index=원본 섹션 인덱스`(가상질문/ExtractionUnit 조인 키).

**핵심 사실:** git_code(심볼=청크)와 달리 confluence는 **문서 단위**가 기본. 이는 가상 질문 인덱싱의 source와 정렬하기 위함 (chunker.py:405-409). `chunk_extracted_document`(512토큰 분할, 라인 329)는 현재 파이프라인에서 쓰이지 않고 doclevel 버전이 쓰인다.

**주요 파라미터:**
| 이름 | 기본값 | 위치 | 영향 |
|------|--------|------|------|
| max_embedding_tokens | 8000 | pipeline.py:89 | 1청크 vs 섹션폴백 분기점 |
| chunk_size/overlap | 512/50 | pipeline.py:74-75 | doclevel에선 미사용(섹션 분할 시 max_tokens 사용) |
| 토큰 모델 | cl100k_base 폴백 1char=1tok | chunker.py:94 | 토큰 카운팅 |

---

## 4. 임베딩

(임베딩 클라이언트는 **git_code와 공유** — `embedder.py`. §git_code 4단계 참조)

**진입:** pipeline.py:266-396 (confluence 분기).

**처리 로직 (멀티뷰 + 가상 질문):**
1. **가상 질문 생성 (LLM, opt-in)** — `extracted`가 있고 `enable_question_indexing`(기본 True)이고 `llm_client`가 있으면 `generate_questions_for_document(doc_title, extracted, llm_client)` (라인 277, question_generator.py:139). LLM 1회 호출로 섹션별 자연 질의 추출 → `{section_index: [questions]}`. 입력 토큰 초과 시 `InputTooLargeError`로 스킵 (라인 293).
2. **3종 뷰 텍스트 구성** (라인 303-330):
   - `body_texts` = 청크 본문
   - `meta_texts` = `build_meta_view_text(title, section_path)` (pipeline.py:575) — title+section_path. 빈 값이면 meta 뷰 스킵(`meta_mask`).
   - `question_lists` = 청크별 가상 질문. 1청크 문서(section_index=None)는 문서의 모든 질문을 그 청크에 연결, 다청크는 section_index 매칭 (라인 313-324).
   - `to_embed = body_texts + (meta only kept) + (all questions)` 한 번에 임베딩.
3. **벡터 엔트리 생성** (라인 346-377): 청크당 `#body`(view=body), 조건부 `#meta`(view=meta), 질문마다 `#q{idx}`(view=question, `question_text` 메타 포함). 질문 뷰의 `documents` 컬럼에도 source 청크 본문을 저장(답변 컨텍스트 조립용, 라인 366-377).

**산출 데이터 형태:** 청크당 최대 3종(body/meta/question N개) 벡터. 모두 `logical_chunk_id` 공유.

**질문 생성 파라미터 (question_generator.py):**
| 이름 | 기본값 | 위치 |
|------|--------|------|
| questions_per_section | 5 | :58 |
| max_input_tokens | 200,000 | :60 |
| max_output_tokens | 32,768 | :61 |
| max_questions_per_doc | 50 | :63 |
| temperature | 0.0 | :62 |

**git_code와 차이:** git_code는 body+meta 2뷰만, 가상 질문 뷰 없음.

---

## 5. 그래프 추출 (3중)

confluence는 세 추출 경로가 같은 `document` 노드로 GraphStore에서 수렴한다.

### 5a. 링크 그래프 (결정론, LLM 없음)
**진입:** `processor/link_graph_builder.py::build_link_graph(extracted, doc_title)` (라인 52). `extracted.outbound_links`가 있을 때 (pipeline.py:399).
- self-entity: `Entity(doc_title, "document")`.
- OutLink→매핑 (라인 37-49): page→`references`(document), user→`mentions_user`(person), jira→`mentions_ticket`(ticket), attachment→`has_attachment`(attachment). **url kind는 SKIP** (`_should_include`, 라인 118).
- 중복 제거: `(name.lower(), type)` 엔티티 키, `(source, target, relation_type)` 관계 키 (라인 79-110).

### 5b. 본문 그래프 (휴리스틱, 결정론, LLM 없음)
**진입:** `processor/extraction_unit.py::build_extraction_units(extracted, ...)` (라인 147) → `processor/body_extractor.py::extract_body_graph(units, doc_title)` (라인 100). (pipeline.py:421-432, `extracted.sections`가 있을 때)
- `build_extraction_units`: 섹션 트리를 토큰 균형 단위로 묶음(`target_tokens=1500, max_tokens=2400`, extraction_unit.py:56-57). 각 unit에 `section_ids, section_path, body, breadcrumb`.
- `extract_body_graph`: 각 unit body에서 추출 (BodyExtractionConfig 기본값, 라인 85-92):
  - **API 엔드포인트** (ON): `GET /path` → `api` 엔티티, `documents` 관계.
  - **Jira 키** (ON): `PROJ-123` → `ticket`, `mentions_ticket`.
  - **굵게 용어** (OFF 기본), **표 헤더** (OFF 기본) — 노이즈 많아 비활성.
  - 추출 관계 없으면 빈 그래프 (self-entity도 미방출, 라인 170).

### 5c. LLM 의미 관계 그래프 (opt-in, 비용 발생)
**진입:** `processor/llm_body_extractor.py::extract_llm_body_graph_for_document(doc_title, body, llm_client)` (라인 277). `enable_llm_body_extraction`(기본 True) + `llm_client`가 있을 때 (pipeline.py:453).
- 입력 body = `_assemble_document_body(extracted)` (pipeline.py:551) — 섹션 헤딩+md_content 트리 순서 합본. **문서 단위 1회 호출**이 기본(256K 컨텍스트 가정).
- `max_input_tokens=200,000`(라인 88) 초과 시 `InputTooLargeError` → unit 단위 `extract_llm_body_graph`로 폴백 (pipeline.py:471-480). unit 폴백은 `_gate_units`(라인 429)로 short unit(`min_unit_tokens=200`)/overlap part 스킵.
- 시스템 프롬프트(라인 108)가 허용 entity_type/relation_type 어휘를 강제. 출력은 JSON `{entities, relations}`. 어휘 외 타입은 드롭(stats.dropped).
- 어휘는 `graph_vocabulary.py`에서 단일 관리: entity_type 15종(document/person/ticket/api/concept/system/module/policy/team/function/class/method/struct/interface), relation_type 19종(references/depends_on/implements/calls/owned_by/uses 등).
- 설정: `temperature=0.0`, `max_tokens=32768`, `max_concurrency=3` (라인 76-83).

**산출 데이터 형태:** 세 경로 모두 `GraphData(entities, relations)`. node_count/edge_count는 셋의 합 (pipeline.py:407-486).

---

## 6. 저장

(저장소 3종 모두 **git_code와 공유** — §git_code 6단계 참조. 동일 함수 사용)

- **벡터**: `vector_store.delete_by_document` → `add_chunks` (ChromaDB, cosine). confluence 메타데이터에 `view`가 body/meta/question 3종 + question 뷰에 `question_text`.
- **SQLite 청크**: `meta_store.create_chunk`. confluence는 `embed_text`를 채우지 않음(본문 자체가 임베딩 입력, metadata_store.py:327). `section_index`는 다청크일 때 채워짐.
- **그래프**: 세 번의 `graph_store.save_graph_data(document_id, graph_data)` 호출. `(LOWER(name), type)` 정규 병합으로 링크/본문/LLM 그래프가 같은 document 노드로 수렴(graph_store.py:160-214). 각 호출이 `{nodes, edges, merged}` 반환, pipeline이 누적.
- **마무리**: `_derive_storage_method` → `complete_reprocessing`. 보통 hybrid (청크+그래프).

---

## 부록 A: 주요 파라미터 표 (confluence 전 단계)

| 이름 | 기본값 | 위치 | 단계 |
|------|--------|------|------|
| SOURCE_TYPE | confluence_mcp | mcp_confluence.py | 1 수집 |
| 파서 | BeautifulSoup html.parser | confluence_extractor.py:144 | 2 전처리 |
| max_embedding_tokens | 8000 | pipeline.py:89 | 3 청킹 |
| enable_question_indexing | True | pipeline.py:90 | 4 임베딩(LLM) |
| questions_per_section | 5 | question_generator.py:58 | 4 임베딩 |
| max_questions_per_doc | 50 | question_generator.py:63 | 4 임베딩 |
| BodyExtractionConfig.api/jira | ON | body_extractor.py:85-86 | 5b 그래프 |
| BodyExtractionConfig.bold/table | OFF | body_extractor.py:87-88 | 5b 그래프 |
| extraction unit target/max tokens | 1500/2400 | extraction_unit.py:56-57 | 5b 그래프 |
| enable_llm_body_extraction | True | pipeline.py:80 | 5c 그래프(LLM) |
| llm max_input_tokens | 200,000 | llm_body_extractor.py:88 | 5c 그래프 |
| llm temperature/max_tokens | 0.0/32768 | llm_body_extractor.py:82-83 | 5c 그래프 |
| 노드 병합 키 | LOWER(name)+type | metadata_store.py:455 | 6 저장 |

## 부록 B: 데이터 모델

- `ExtractedDocument` (confluence_extractor.py:116): plain_text, sections, outbound_links, code_blocks, tables, mentions
- `Section` (:32) / `OutLink` (:51) / `CodeBlock` (:73) / `Table` (:88) / `Mention` (:103)
- `Chunk` (chunker.py:43)
- `ExtractionUnit` (extraction_unit.py:68): section_ids, section_path, body, breadcrumb
- `Entity`/`Relation`/`GraphData` (graph_extractor.py:15/30/48)
- `LLMBodyExtractionConfig`/`Stats` (llm_body_extractor.py:54/92)

## 검토하지 못한 영역

- `html_converter.py::html_to_markdown`의 매크로 전처리 상세 (panel/info/warning/expand 변환 규칙)
- `enumerate_space_pages`/`walk_subtree`의 페이지네이션·동기화 스케줄 (sync engine)
- `extraction_unit._build_tree`의 노드 응축/분할 알고리즘 상세
- `llm_body_extractor._call_llm`의 JSON 파싱 실패 폴백·`_canonical_name` 정규화 상세
- `question_generator`의 LLM 응답 파싱 (`_assemble_sections_payload` 이후)
- `sync` 디렉토리(`sync/engine.py`)의 confluence 증분 동기화 트리거
