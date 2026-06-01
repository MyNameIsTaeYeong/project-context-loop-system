# A. 역량 인벤토리 — project-context-loop-system

> **분석 기준 리비전:** `origin/main` 워크트리 (`/private/tmp/clp-origin-main`)
> **분석 범위:** 현재 코드베이스가 *실제로 제공하는* 기능/API. 구현 제안 없음.
> **상위 목표 관점:** 각 역량을 "개발/설계 업무 자동화 플랫폼"의 building block 으로 재해석.

---

## ① 요약 (한 문단)

이 시스템은 **사내 지식(Confluence·업로드·직접작성·Git 코드)을 인덱싱하여 벡터 검색 + 지식 그래프로 질의할 수 있게 만드는 로컬-퍼스트 RAG 플랫폼**이며, 동일한 검색·조립 엔진을 (a) **MCP 서버**(stdio/SSE)로 사내 LLM 앱에 노출하고 (b) **FastAPI 웹 대시보드**(문서 CRUD·RAG 채팅 스트리밍·전역 그래프 가시화·Git/Confluence 싱크)로 운영자에게 노출한다. 인덱싱 파이프라인은 **LLM 호출 없는 결정론 경로(AST 코드 심볼 추출, Confluence 링크/본문 그래프, 멀티뷰 임베딩)**를 기본으로 하되, 옵션으로 LLM 기반 의미 그래프 추출·가상 질문 인덱싱·HyDE·리랭킹을 켤 수 있다. 검색 조립기는 벡터 유사도 검색과 **LLM 플래너 기반 그래프 탐색을 병렬 실행**하여 청크 + 그래프 연결 문서 + 원본 소스코드를 한 컨텍스트로 합친다. 핵심 자동화 자산은 **(a) MCP `search_context` 한 번으로 사내지식+코드+그래프를 동시 질의, (b) git_code AST 인덱싱으로 코드베이스를 심볼/import 단위로 구조적 질의, (c) NetworkX 그래프로 엔티티/의존 관계 탐색**이다. 단, **`pyproject.toml`의 CLI 엔트리포인트(`context-loop = context_loop.cli:main`)는 `cli.py`가 존재하지 않아 깨져 있다** — 실행은 uvicorn(웹)·`mcp.server.run_stdio/run_sse`·`scripts/*.py`로만 가능하다.

---

## ② 영역별 상세

### 1. MCP 서버 (`src/context_loop/mcp/`)

**전송 방식:** stdio + SSE 모두 구현. `mcp/server.py:98 run_stdio()` (FastMCP `run_stdio_async`), `server.py:108 run_sse(port=3001)` (`run_sse_async`). 서버 인스턴스는 `server.py:22` `FastMCP("context-loop", ...)`. 단 이 함수들을 호출하는 **CLI/엔트리포인트가 없으므로**(아래 3 참조) 현재는 코드 호출이나 별도 러너 스크립트로만 기동 가능 — *추정상* 운영 진입점 미완.

**초기화:** `server.py:34 _initialize()` — Config 로드, MetadataStore/VectorStore/GraphStore 초기화, 임베딩 클라이언트(endpoint|local), LLM 클라이언트·리랭커 클라이언트(웹 `_build_llm_client`/`_build_reranker_client` 재사용). LLM/리랭커 초기화 실패 시 graceful degrade(그래프 탐색·리랭킹 비활성화).

**제공 Tools** (`mcp/tools.py:16 register_tools`):

| Tool | 파라미터 | 반환 | 내부 동작 |
|------|----------|------|-----------|
| `search_context` (`tools.py:20`) | `query:str`, `max_chunks:int=10`, `include_graph:bool=True`, `include_source_code:bool=True` | `str` (조립된 컨텍스트 텍스트) | `context_assembler.assemble_context` 위임. config에서 similarity_threshold/reranker/HyDE/max_graph_context_* 주입 |
| `list_documents` (`tools.py:68`) | `source_type:str?`, `status:str?` | `list[dict]` (id/title/source_type/status/storage_method/updated_at) | `meta_store.list_documents` |
| `get_document` (`tools.py:98`) | `document_id:int`, `format:"original"\|"chunks"\|"graph"` | `dict` | 원본/청크/그래프 노드·엣지 반환 |
| `get_graph_context` (`tools.py:158`) | `entity_name:str`, `depth:int=1` | `dict` (entity/nodes/edges) | `graph_store.get_neighbors`(양방향 BFS) + `get_edges_between` |

**컨텍스트 조립 흐름** (`mcp/context_assembler.py:58 assemble_context`, `:469 assemble_context_with_sources`):
1. **쿼리 임베딩** — HyDE 활성 시 `query_expander.expand_query_embedding`(가상 문서 임베딩 평균), 아니면 `_embed_query`.
2. **벡터 검색** (`_search_chunks:183`) — 멀티뷰(body/meta/question) over-fetch ×6 후 **document_id 단위 dedup**, similarity_threshold 필터.
3. **리랭킹 + 그래프 탐색 병렬** (`_rerank_and_search_graph:425`, `asyncio.gather`) — 리랭킹은 `reranker.rerank`, 그래프는 `_search_graph_with_llm`(LLM 플래너 `graph_search_planner.plan_graph_search` → `execute_graph_search`, 표면 매칭 실패 시 임베딩 fallback 시드).
4. **그래프 연결 문서 본문 첨부** (`_search_graph_sourced_chunks:289`) — 그래프가 도달했지만 벡터가 못 찾은 문서의 청크를 개수(`max_graph_docs`)·토큰(`max_graph_tokens`) 상한 내에서 추가.
5. **원본 소스코드 첨부** (`_fetch_and_format_source_code:649`) — `code_doc`/`code_summary` 문서의 `document_sources` 연결 git_code 원본 코드를 검증용 섹션으로 조립.
- `assemble_context_with_sources`는 추가로 `Source` 출처 리스트 + `retrieved_graph_entities/relations`(평가용 `GraphEntityRef`/`GraphRelationRef`)를 반환.

> **자동화 building block:** 사내 LLM 앱(Claude Code·커스텀 에이전트)이 **단일 MCP tool 호출**로 사내 문서 + 코드 원본 + 그래프 관계를 한 번에 받아 컨텍스트 그라운딩 — 개발 자동화 에이전트의 "사내지식 검색" 표준 인터페이스로 직결.

---

### 2. 웹 대시보드 / REST API (`src/context_loop/web/`)

**앱 팩토리:** `web/app.py:169 create_app()` (반드시 `--factory`로 실행). lifespan에서 스토어·LLM·임베딩·리랭커 클라이언트를 `app.state`에 적재. 정적 자산 캐시버스팅(`_compute_asset_version`).

**엔드포인트 전체** (라우터별):

| Method | Path | 기능 | 파일:라인 |
|--------|------|------|-----------|
| GET | `/` | 메인 대시보드 페이지 | documents.py:38 |
| GET | `/documents/{id}` | 문서 상세 페이지 | documents.py:48 |
| GET | `/editor`, `/editor/{id}` | 마크다운 에디터(신규/수정) | documents.py:65,75 |
| GET | `/partials/document-list` | 문서목록 HTMX 파셜(필터) | documents.py:95 |
| GET | `/partials/document/{id}/original` | 원본 탭(언어 힌트·HTML 폴백) | documents.py:114 |
| GET | `/partials/document/{id}/chunks` | 청크 탭(멀티뷰 meta·가상질문 표시) | documents.py:144 |
| GET | `/partials/document/{id}/graph` | 문서별 그래프 탭(vis-network) | documents.py:217 |
| GET | `/partials/document/{id}/sources` | 소스 연결 탭(코드↔문서) | documents.py:248 |
| GET | `/partials/document/{id}/metadata` | 메타/처리이력 탭 | documents.py:269 |
| GET | `/api/documents/{id}/status` | 처리 상태 JSON(폴링) | documents.py:291 |
| POST | `/api/documents` | 에디터 신규 문서 생성 | documents.py:303 |
| PUT | `/api/documents/{id}` | 문서 수정 | documents.py:316 |
| DELETE | `/api/documents/{id}` | cascade 삭제(벡터/그래프/메타) | documents.py:338 |
| POST | `/api/documents/{id}/process` | 백그라운드 파이프라인 재처리 | documents.py:360 |
| GET | `/chat` | 채팅 페이지 | chat.py:57 |
| POST | `/api/chat` | **RAG NDJSON 스트리밍**(reasoning/delta/sources/done/error) | chat.py:64 |
| GET | `/api/stats`, `/partials/stats` | 통계 JSON/카드 파셜 | stats.py:13,21 |
| POST | `/api/upload` | 파일 업로드(.md/.txt/.html) | upload.py:22 |
| GET | `/graph` | **전역 그래프 페이지** | graph.py:36 |
| GET | `/api/graph/full` | 전체 그래프(노드 상한 300, 차수 우선) | graph.py:74 |
| GET | `/api/graph/explore` | 키워드→연결 컴포넌트(임베딩 fallback, hop 거리) | graph.py:115 |
| GET | `/api/graph/node/{id}` | 노드 상세(출처 문서 + 병합 내역) | graph.py:187 |
| GET | `/api/graph/merges` | 크로스-문서 병합 그룹 목록 | graph.py:247 |
| GET | `/confluence` | Confluence API 임포트 페이지 | confluence.py:35 |
| POST | `/api/confluence/connect` | 연결 설정·테스트(토큰 keyring) | confluence.py:54 |
| GET | `/api/confluence/spaces`, `/spaces/{id}/pages` | 스페이스/페이지 목록 | confluence.py:78,89 |
| POST | `/api/confluence/import` | 선택 페이지 임포트 | confluence.py:100 |
| GET | `/confluence-mcp` | Confluence MCP Client 페이지 | confluence_mcp.py:92 |
| POST | `/api/confluence-mcp/connect` | MCP 서버 연결·tool 목록 | confluence_mcp.py:107 |
| GET | `/api/confluence-mcp/health`, `/tools` | 진단·도구 조회 | confluence_mcp.py:135,173 |
| POST | `/api/confluence-mcp/search` | 콘텐츠 검색 | confluence_mcp.py:185 |
| GET | `/api/confluence-mcp/spaces`, `/pages/{id}/children`, `/user-pages` | 트리 탐색·기여 페이지 | confluence_mcp.py:205,217,232 |
| POST | `/api/confluence-mcp/import` | 페이지 임포트 | confluence_mcp.py:247 |
| GET | `/api/confluence-mcp/search`(merged), `/spaces/{key}/estimate` | 공간+페이지 통합검색·예상치 | confluence_mcp.py:287,336 |
| POST/GET/DELETE | `/api/confluence-mcp/sync-targets[...]` | **3-scope(page/subtree/space) 싱크 대상 CRUD + 재싱크**(target별 락·진행상태) | confluence_mcp.py:353~505 |
| GET | `/git-sync` | Git 동기화 페이지 | git_sync.py:78 |
| GET | `/partials/git-sync/status`, `/documents` | 상태·문서 파셜(폴링) | git_sync.py:108,120 |
| GET | `/api/git-sync/status` | 동기화 상태 JSON | git_sync.py:146 |
| POST | `/api/git-sync/start` | **백그라운드 Git 싱크 시작**(CoordinatorAgent) | git_sync.py:152 |
| DELETE | `/api/git-sync/repositories`, `/repositories/products` | 레포/상품 단위 싱크 결과 purge | git_sync.py:219,247 |

**프론트엔드:** Jinja2 템플릿(`web/templates/`: dashboard/document_detail/editor/chat/graph/confluence/confluence_mcp/git_sync.html + `partials/*`) + `web/static/js/`(`graph.js` = vis.js 그래프 렌더 `initGraph`). HTMX + Alpine.js 경량 UI(추정 — 파셜 라우트 패턴으로 확인).

> **자동화 building block:** `/api/chat`의 **NDJSON 스트리밍 RAG**는 reasoning/answer/sources를 분리 전달하므로 설계 리뷰·문서 Q&A 자동화 UI에 바로 임베드 가능. `/api/graph/*`는 의존성·엔티티 맵을 외부 도구가 JSON으로 소비. git-sync/confluence-mcp 싱크 API는 **무인 인덱싱 파이프라인**의 트리거로 재사용.

---

### 3. CLI — ⚠️ 미구현 (엔트리포인트 깨짐)

- `pyproject.toml:40` 은 `context-loop = "context_loop.cli:main"` 선언.
- **`src/context_loop/cli.py` 가 존재하지 않음** (Read 결과 `File does not exist`). 따라서 `pip install -e .` 후 `context-loop ...` 명령은 ImportError로 실패한다. CLAUDE.md의 `context-loop mcp serve` 예시는 **현재 동작하지 않는다.**
- **실제 동작하는 진입점:**
  - 웹: `python3 -m uvicorn "context_loop.web.app:create_app" --factory ...` (CLAUDE.md 빠른시작과 일치).
  - MCP: `context_loop.mcp.server.run_stdio()` / `run_sse(port)` 함수 직접 호출 (러너 스크립트 필요 — 현재 번들된 러너 없음, *추정* 미완).
  - 평가/유틸: `scripts/eval_search.py`, `scripts/build_synthetic_gold_set.py` (각자 `sys.path`에 `src` 추가하여 단독 실행).

> **자동화 영향:** MCP 서버를 Claude Code mcpServers에 `command: context-loop` 로 등록하는 표준 경로가 막혀 있음 — 자동화 통합 전 **cli.py 또는 console_scripts 보강 필요**(building block 으로는 "거의 완성, 진입점만 결손").

---

### 4. 인덱싱/처리 파이프라인 역량 (`src/context_loop/processor/`)

**오케스트레이터:** `pipeline.py:93 process_document` — 재처리 시작(`reprocessor.start_reprocessing`) → 소스별 분기 → 청킹/임베딩/그래프 저장 → `storage_method` 파생(`_derive_storage_method`) → 완료 기록. **classifier(LLM 저장방식 판단)는 더 이상 사용 안 함** — `storage_method`는 산출물에서 파생(chunks+graph=hybrid). `classifier.py`는 존재하나 파이프라인 미참조(*추정* 레거시).

**소스별 처리:**
- **git_code:** `ast_code_extractor.extract_code_symbols`(Python=`ast`, Go/Java/TS/JS=중괄호 매칭, 기타=단일 심볼) → `to_chunks`(메서드 단위, `file>class>method` section_path) + 멀티뷰 임베딩(body=코드, meta=식별자 요약 `embed_text`) → `to_graph_data`(import + contains 관계).
- **confluence/confluence_mcp:** `confluence_extractor.extract`(HTML→sections/links/code/tables/mentions) → `chunk_extracted_document_doclevel`(문서 단위, 큰 문서는 섹션 폴백) → 멀티뷰(body+meta+**가상질문** `question_generator.generate_questions_for_document`) → **3종 그래프**: `link_graph_builder.build_link_graph`(outbound 링크) + `body_extractor.extract_body_graph`(굵게/API/표헤더/Jira키 결정론) + `llm_body_extractor`(옵션 LLM 의미관계 depends_on/implements/owned_by, 문서단위 1콜, 한도초과 시 unit 폴백).
- **그 외(upload/manual):** `chunk_text` 멀티뷰(body+meta)만.

**검색측 처리 모듈:** `chunker.py`(`count_tokens`, doclevel 청킹), `embedder.py`(멀티뷰), `reranker.py`(`rerank`), `query_expander.py`(HyDE), `graph_search_planner.py`(LLM 플래너 `plan_graph_search`/`execute_graph_search`), `question_generator.py`(가상질문), `reprocessor.py`(delete&recreate 이력).

**엔티티 정규화/머지:** `storage/entity_normalizer.normalize_entity_name` → `graph_store.save_graph_data`가 정규화 키로 기존 노드 탐색·병합(`exact`/`normalized`/`new` `graph_merge_log` 기록), `graph_node_documents`로 노드↔문서 다대다, description 보강, 엔티티 임베딩 fallback(`build_entity_embeddings`/`search_entities_by_embedding`).

> **자동화 building block:** **git_code AST 인덱싱**이 핵심 — LLM 없이 100% 정확·ms 단위로 코드베이스를 심볼/시그니처/import 그래프로 구조화 → "이 함수 누가 호출?"·"이 모듈 의존성?"류 질의가 그래프 탐색으로 해소. 가상질문·HyDE·리랭킹은 검색 정밀도 튜닝 노브.

---

### 5. 수집(ingestion) 역량 (`src/context_loop/ingestion/`)

- **confluence.py:** `ConfluenceClient`(Cloud Basic / DC Bearer), `import_page`/`import_space`, HTML→MD 변환.
- **mcp_confluence.py:** Confluence MCP Client — `connect_mcp`(http/sse/stdio), `search_content`/`search_content_envelope`, `get_all_spaces`/`get_child_pages`/`get_space_info`, `get_user_contributed_pages`, `get_page_with_ancestors`/`format_breadcrumb`, `estimate_space_page_count`, `import_page_via_mcp`, `list_available_tools`.
- **git_repository.py:** `clone_or_pull`, `collect_files`(vendored/빌드 디렉토리 제외 frozenset, 확장자 필터), `parse_product_scopes`, `store_git_code`(원본 코드 DB 저장), `content_hash`(hashlib) 기반 변경감지, `purge_synced_results`.
- **scope_analyzer.py:** config 상품명 → 파일경로 자동탐지(복수형 변형·토큰 경계 매칭).
- **coordinator.py:** `CoordinatorAgent.run_and_store` — 레포 clone/pull → 상품별 수집 → git_code 저장 → 신규/변경분 AST 처리. `ProductResult`/`PipelineResult` 집계.
- **uploader.py:** `upload_file`(.md/.txt/.html, `UnsupportedFileTypeError`). **editor.py:** `save_document`(직접작성/수정).
- **sync/mcp_sync.py:** `execute_sync_target`(Phase1 임포트 + Phase2 인덱싱 동시, 동시성 제어).

> **자동화 building block:** **content_hash 증분 감지 + delete&recreate**로 무인 재인덱싱이 안전. Git 코드 자동 수집 → 코드베이스 변경이 그래프/검색에 자동 반영되는 "살아있는 코드 컨텍스트" 파이프라인.

---

### 6. 저장소 (`src/context_loop/storage/`)

- **vector_store.py:** ChromaDB 래퍼 — `search`(distance, `where` 필터 `$in`), `add_chunks`, `delete_by_document`, `list_by_document(view=)`, `count`. 멀티뷰 엔트리(`{chunk_id}#body|#meta|#q{n}`).
- **graph_store.py:** NetworkX `DiGraph` + SQLite. `save_graph_data`(정규 노드 병합), `get_neighbors`/`get_connected_component`/`get_neighbors_from_node_id`(**양방향 BFS** `_bidirectional_bfs`, 4단계 시드 해석 `_resolve_seed_nodes`: 완전→스코프제거→짧은이름→임베딩 fallback), `get_schema_summary`/`format_schema_for_llm`/`get_query_relevant_schema`(LLM 플래너용 스키마), 엔티티 임베딩 캐시.
- **metadata_store.py:** SQLite 스키마 — `documents`(+`raw_content` 컬럼), `chunks`(+`section_path`/`embed_text`/`section_index`), `graph_nodes`(+`normalized_name`), `graph_edges`, `graph_node_documents`(다대다), `graph_merge_log`(머지 관측성), `processing_history`, `document_sources`(코드↔문서), `confluence_sync_targets`/`confluence_sync_membership`(3-scope 싱크 소유권/참조카운트). WAL + FK ON.
- **cascade.py:** `delete_document_cascade`(벡터+그래프+메타 동시 삭제, 고아 노드/엣지 정리).

> **자동화 building block:** **graph_merge_log + graph_node_documents**가 "엔티티가 어느 문서에서 왔는가 / 어떻게 병합됐는가"의 추적성을 제공 → 통합 품질 진단·신뢰 가능한 자동 답변의 근거 데이터.

---

### 7. 평가/골드셋 하네스 (`scripts/`, `src/context_loop/eval/`)

- **build_synthetic_gold_set.py:** LLM으로 검색 평가 골드셋 자동 생성 — 문서 통째/그래프 서브그래프 계층 샘플링(source_type 균등) → Generator LLM 역방향 질문 생성 → Judge LLM 4단계 품질 게이트 → YAML 저장. Generator/Judge 모델 분리(자기평가 편향 회피), `--include-graph-questions`(그래프 노드 기반), 시드 고정.
- **eval_search.py:** 골드셋으로 `assemble_context_with_sources` 실행 → **Recall@k/Precision@k/MRR/nDCG@k** 계산, `--judge`(LLM 0~5점 응답 품질), `--gold-set-glob`(다중 골드셋 mean/std/min/max 변동성), `--score-relations`(그래프 관계 채점). per-question CSV + summary JSON.
- **eval/:** `gold_set.py`(`GoldItem`/`GraphEntityRef`/`GraphRelationRef`/`load_gold_set`), `graph_match.py`(엔티티 매칭 tier, 임베딩 기반 `build_embed_fn`/`run_entity_matching`).

> **자동화 building block:** 검색 품질을 **정량 회귀 측정**할 수 있어, 인덱싱/프롬프트 변경의 효과를 자동화 파이프라인에서 게이팅(개선 ≥ std 시만 머지)하는 CI 신뢰성 장치로 재사용.

---

### 8. LLM/임베딩 추상화 (`src/context_loop/processor/`)

- **llm_client.py:** `LLMClient`(ABC) → `OpenAIClient`(api_key), `AnthropicClient`(api_key, keyring), `EndpointLLMClient`(OpenAI 호환 자체 엔드포인트 URL, `headers`/`reasoning_profiles`). 메서드: `complete`/`stream`/`stream_events`(reasoning+delta), `reasoning_mode`/`purpose` 타이밍 로그.
- **embedder.py:** `EndpointEmbeddingClient`(OpenAI 호환 REST, 배치 100, sync+async), `LocalEmbeddingClient`(sentence-transformers 추정). langchain `Embeddings` 상속.
- **reranker_client.py:** `RerankerClient`(ABC) → `EndpointRerankerClient`(cross-encoder, Cohere/Jina/TEI 응답 포맷 다중 지원).
- **빌더:** `web/app.py:66 _build_llm_client`, `:88 _build_reranker_client`(endpoint/model 비면 None=스킵), `:107 _build_embedding_client`(endpoint/local/openible).

> **자동화 building block:** **provider 무관 추상화 + 자체 엔드포인트 우선** → 사내 모델 서버(OpenAI 호환)로 비용/보안 통제하며 LLM·임베딩·리랭커를 독립 교체. reasoning_mode/purpose는 멀티스텝 자동화의 단계별 모델 라우팅 기반.

---

## ③ 자동화 building block 요약 표

| 역량 | 성숙도 | 자동화 활용 가능성 |
|------|--------|-------------------|
| MCP `search_context` (벡터+그래프+소스코드 통합 조립) | 완성 | ★★★ 사내 LLM 앱이 단일 호출로 사내지식+코드+관계 그라운딩 — 개발 자동화 에이전트의 표준 검색 인터페이스 |
| MCP stdio/SSE 전송 (`run_stdio`/`run_sse`) | 부분(코드 완성, **기동 진입점 결손**) | ★★ 함수는 동작하나 CLI 부재로 표준 등록 불가 |
| MCP `list/get_document`, `get_graph_context` | 완성 | ★★ 문서·엔티티 인벤토리 프로그래밍적 조회 |
| 웹 `/api/chat` NDJSON RAG 스트리밍 | 완성 | ★★★ 설계 리뷰·문서 Q&A 자동화 UI에 즉시 임베드(reasoning/sources 분리) |
| 웹 `/api/graph/*` (full/explore/node/merges) | 완성 | ★★★ 의존성·엔티티 맵을 외부 도구가 JSON 소비 |
| 문서 CRUD + 백그라운드 재처리 API | 완성 | ★★ 무인 문서 등록·재인덱싱 트리거 |
| git_code AST 인덱싱 (심볼 청크 + import/contains 그래프) | 완성 | ★★★ 코드베이스 구조적 질의(호출관계·의존성) LLM 없이 정확 |
| Confluence 링크/본문 그래프 (결정론) | 완성 | ★★ 문서 간 참조·도메인 엔티티 자동 그래프화 |
| LLM 의미 그래프 추출 (depends_on/implements 등) | 완성(opt-in) | ★★ 의미 관계 보강으로 추론형 질의 강화 |
| 가상질문 인덱싱 / HyDE / 리랭킹 | 완성(opt-in) | ★★ 검색 정밀도 튜닝 노브 |
| 엔티티 정규화·크로스문서 병합 + merge_log | 완성 | ★★★ "엔티티 출처/병합 추적" — 신뢰 가능한 자동 답변 근거 |
| Git 수집 (clone/pull, content_hash 증분, scope_analyzer) | 완성 | ★★★ 살아있는 코드 컨텍스트(변경 자동 반영) |
| Confluence API + MCP Client 수집 (3-scope 싱크) | 완성 | ★★ 사내 위키 무인 동기화 |
| 업로드/직접작성 수집 | 완성 | ★ 보조 입력 경로 |
| 저장소 (Chroma/NetworkX+SQLite/cascade) | 완성 | ★★ 로컬-퍼스트 영속·삭제 정합성 |
| 평가 하네스 (골드셋 생성 + Recall/MRR/nDCG + judge) | 완성 | ★★★ 인덱싱/검색 변경의 정량 회귀 게이팅 |
| LLM/임베딩/리랭커 추상화 (openai/anthropic/endpoint) | 완성 | ★★★ 사내 모델 서버 우선·provider 무관 교체 |
| **CLI (`context_loop.cli:main`)** | **미구현(엔트리포인트 깨짐)** | ✗ MCP 표준 등록·CLI 자동화 전 보강 필요 |
| LLM Classifier (저장방식 판단) | 부분(존재하나 파이프라인 미사용, *추정* 레거시) | — storage_method는 산출물에서 파생으로 대체됨 |

---

### 주요 발견 (요약)

1. **CLI 미구현 — `pyproject.toml`의 `context-loop=context_loop.cli:main`은 `cli.py` 부재로 깨짐.** 실행은 uvicorn(웹)·`mcp.server.run_stdio/run_sse` 함수·`scripts/*.py`로만 가능하며, CLAUDE.md의 `context-loop mcp serve`는 현재 동작 안 함.
2. **MCP `search_context`가 자동화의 핵심 허브** — 벡터검색 + LLM플래너 그래프탐색(병렬) + 그래프 연결문서 + 원본 소스코드를 한 컨텍스트로 조립.
3. **LLM Classifier는 사실상 폐기** — `storage_method`는 처리 산출물(chunks+graph)에서 파생; 파이프라인은 결정론(AST/링크/본문 그래프) 기본 + LLM 그래프/가상질문/HyDE/리랭킹은 opt-in.
4. **git_code AST 인덱싱 + import/contains 그래프**로 코드베이스를 심볼·의존 단위로 구조적 질의 가능 — 개발 자동화의 1급 자산.
5. **엔티티 정규화·크로스문서 병합 + graph_merge_log + graph_node_documents**가 "엔티티 출처/병합" 추적성을 제공해 `/api/graph/node/{id}`·전역 그래프 페이지가 운영자에게 통합 품질을 노출.
