# 01. 기능/API 분석 — 자동화 building block 인벤토리

> 기준 리비전: `origin/main` (7e7bbe0). 모든 경로는 `src/context_loop/` 기준.
> 작성: 2026-06-01. 분석 전용(코드 수정 없음).

## 요약

이 시스템은 **사내 문서·코드를 인덱싱(텍스트 청크 + 지식 그래프)하여 저장하고, MCP 서버로 사내 LLM 앱이 질의하게 하는 RAG+그래프 지식 플랫폼**이다. 자동화 관점에서 가장 강력한 자산은 (1) 이미 동작하는 **MCP 서버**(stdio/SSE, 4개 tool), (2) **git_code AST 인덱싱**(LLM 없이 코드 심볼·import·contains 그래프를 100% 정확 추출), (3) **지식 그래프**(엔티티/관계 + 엔티티 임베딩 + LLM 플래너 탐색), (4) **정교한 검색 조립기**(멀티뷰 임베딩 + 그래프 탐색 + 리랭커 + HyDE + 원본 소스코드 첨부)이다. 반대로 **CLI는 선언만 있고 미구현(엔트리포인트 깨짐)** 이라 자동화 트리거(배치/CI 연동)의 공백이 있다.

---

## 1. MCP 서버 — 자동화의 1차 진입점

- 파일: `mcp/server.py`(FastMCP, `run_stdio()`, `run_sse(port)`), `mcp/tools.py`, `mcp/context_assembler.py`
- 전송: **stdio**(`run_stdio_async`) + **SSE**(`run_sse_async`, 기본 3001) 모두 지원
- 제공 tool 4종:

| tool | 시그니처 | 동작 | 자동화 활용 |
|------|----------|------|------------|
| `search_context` | `(query, max_chunks=10, include_graph=True, include_source_code=True) -> str` | 멀티뷰 벡터검색 → 리랭킹+그래프탐색(병렬) → 그래프 연결문서 본문 첨부 → code_doc의 원본 소스코드 첨부. 출처/섹션/매칭질문 라벨 포함 | **사내지식 RAG의 핵심.** Claude Code 등 LLM 앱이 사내 코딩 컨벤션·문서·유사 코드를 끌어오는 통로 |
| `list_documents` | `(source_type=None, status=None) -> list` | 문서 목록 메타 | 자동화 대상 코퍼스 범위 확인 |
| `get_document` | `(document_id, format="original"\|"chunks"\|"graph") -> dict` | 원본/청크/그래프 형태로 단일 문서 조회 | 특정 모듈/문서 정밀 인출 |
| `get_graph_context` | `(entity_name, depth=1) -> dict` | 엔티티 중심 이웃 노드/엣지 반환 | **영향도·의존성 탐색의 핵심 API** |

- `context_assembler.assemble_context()` 내부(중요 기능):
  - 멀티뷰 인덱싱(R3): 한 문서를 `body`/`meta`/`question`(가상질문) 3 view로 임베딩 → over-fetch 후 `document_id` 단위 dedup
  - LLM 그래프 플래너(`processor/graph_search_planner.py`): 그래프 스키마를 LLM에 보여주고 질의에 맞는 영역만 탐색. 엔티티 표면매칭 실패 시 **엔티티 임베딩 fallback**으로 시드 노드 보강
  - 리랭커(`processor/reranker.py` + `reranker_client.py`), HyDE(`query_expander.expand_query_embedding`)
  - 그래프로 도달했지만 벡터가 못 찾은 문서의 본문을 별도 섹션으로 첨부(개수/토큰 상한)
  - `include_source_code`: code_doc/code_summary → `document_sources` 테이블로 연결된 git_code 원본 코드를 "검증용 소스" 섹션으로 첨부
  - 또한 `assemble_context_with_sources()`가 출처 리스트 + 그래프 엔티티/관계 ref를 구조화 반환(평가/표시용)

> **성숙도: 완성.** MCP 서버는 자동화 시나리오 대부분의 데이터 인출 계층으로 즉시 재사용 가능.

## 2. 웹 대시보드 / REST API

- 프레임워크: FastAPI + Jinja2 + HTMX + Alpine.js + vis.js (`web/app.py` `create_app()` 팩토리, `--factory` 필수)
- 라우터(`web/api/*.py`):

| 영역 | 주요 엔드포인트 | 기능 |
|------|----------------|------|
| documents | `GET /`, `/documents/{id}`, `/editor`, `/partials/document/{id}/{original\|chunks\|graph\|sources\|metadata}` | 대시보드, 문서 상세 탭, 마크다운 에디터 |
| chat | `GET /chat`, `POST /api/chat` (NDJSON 스트리밍) | **대시보드 내 RAG 채팅** — assemble_context 활용 |
| graph | `GET /graph`, `GET /api/graph/full`, `/explore`, `/node/{id}`, `/merges` | 전역 그래프 시각화, 이웃 탐색, 노드 상세(병합 이력), 병합 그룹 |
| git_sync | `GET /git-sync`, `POST /api/git-sync/start`, `/status`, `DELETE /repositories[/products]` | git 레포 동기화 트리거/상태/정리 |
| confluence_mcp | `POST /api/confluence-mcp/connect`, `/search`, `GET /spaces`, `/pages/{id}/children`, `/user-pages`, `/health` | Confluence MCP 클라이언트 임포트 |
| upload | `POST /api/upload` | 파일 업로드 |
| stats | `GET /api/stats`, `/partials/stats` | 문서/청크/노드/엣지 통계 |

> **성숙도: 완성(운영 UI).** REST API는 자동화 결과를 사람이 검수·탐색하는 창구로 재사용 가능. 단, 자동화 산출물을 띄우는 전용 화면은 없음(신규 필요).

## 3. CLI — **공백(미구현)**

- `pyproject.toml [project.scripts]`: `context-loop = "context_loop.cli:main"`
- 그러나 `src/context_loop/cli.py` **부재** → `context-loop` 명령 실행 시 ImportError(엔트리포인트 깨짐)
- 현재 실행 가능한 진입점:
  - 웹: `uvicorn "context_loop.web.app:create_app" --factory ...`
  - MCP: `mcp/server.py` 의 `run_stdio()` / `run_sse()`
  - 배치 스크립트: `scripts/run_git_code_store.py`, `run_product_paths.py`, `run_category_agent.py`, `run_worker_agent.py`, `build_synthetic_gold_set.py`, `eval_search.py`, `compare_runs.py`
- **자동화 함의:** CI/cron/배치 연동의 표준 CLI가 없다. 자동화 시나리오를 "한 줄 명령/잡"으로 묶으려면 CLI 복원이 선행 enabler — 비용은 낮음(아래 04 참조).

> **성숙도: 미구현.** 자동화 트리거 표준화의 1순위 공백.

## 4. 인덱싱/처리 파이프라인 역량

- 오케스트레이터: `processor/pipeline.py` `process_document()` — 추출 → 청킹/임베딩 + 그래프추출 → `storage_method` 파생(chunk/graph/hybrid) → 처리이력 기록
- **git_code AST 추출**: `processor/ast_code_extractor.py`
  - `extract_code_symbols(content, file_path)` → Python은 `ast` 모듈로 함수/클래스/메서드/import 정밀 추출, Go/Java/TS/JS는 중괄호 매칭, 기타는 fallback(파일=단일심볼)
  - `to_chunks()` 메서드 단위 청킹(`section_path = file > class > method`)
  - `to_graph_data()` → 엔티티(module/함수/클래스/import모듈) + 관계(`imports`, `contains`). import 모듈은 단순이름으로 canonical 병합 의도
  - **LLM 호출 없음 → 100% 정확·ms 단위·비용 0** (D-036). 코드 기반 자동화의 신뢰 기반.
- **문서 그래프 추출**: `processor/graph_extractor.py`(LLM), `body_extractor.py`/`llm_body_extractor.py`(본문 엔티티), `link_graph_builder.py`(문서 간 링크 그래프), `graph_vocabulary.py`(용어 통제)
- **검색 보조**: `chunker.py`(doc-level + 멀티뷰), `question_generator.py`(가상질문 R3), `query_expander.py`(HyDE), `reranker.py`
- **재처리**: `processor/reprocessor.py` — content_hash 변경 감지 시 delete & recreate

> **성숙도: 코드(git_code) 그래프=완성/고신뢰, 문서 LLM 그래프=부분(품질 개선 이력 있음). 청킹·임베딩·검색=완성.**

## 5. 수집(ingestion) 역량

- `ingestion/git_repository.py`: clone/pull, `compute_content_hash`, 상품별 스코프(`parse_product_scopes`, `match_product`), `scope_analyzer.py`로 paths 자동 탐지
- `ingestion/confluence.py`(REST API), `mcp_confluence.py`(MCP 클라이언트), `confluence_extractor.py`, `html_converter.py`
- `uploader.py`, `editor.py`, `coordinator.py`(입력 경로 통합)

> **성숙도: 완성.** 코드·문서 코퍼스를 자동으로 동기화·갱신하는 기반.

## 6. 저장소

- `storage/vector_store.py`: ChromaDB(로컬 임베디드), 멀티뷰 검색, `where` 필터
- `storage/graph_store.py`: NetworkX+SQLite. 핵심 메서드 — `get_neighbors`, `get_connected_component`, `get_edges_between`, `get_schema_summary`/`format_schema_for_llm`/`get_query_relevant_schema`(LLM 플래너용), `build_entity_embeddings`/`search_entities_by_embedding`(임베딩 fallback), `delete_document_graph`
- `storage/metadata_store.py`(SQLite): `documents`, `chunks`, `graph_nodes`, `graph_edges`, `graph_node_documents`, `graph_merge_log`(병합 이력), `processing_history`, **`document_sources`**(code_doc↔git_code 연결), `confluence_sync_targets/membership`
- `storage/entity_normalizer.py`(엔티티 병합), `cascade.py`(연쇄 삭제)
- **source_type 확장**: `confluence`, `upload`, `manual`, **`git_code`**, **`code_doc`**, **`code_summary`** — 코드와 코드설명 문서가 분리·연결되어 있음

> **성숙도: 완성.** 그래프 + 출처추적(document_sources) + 병합이력(graph_merge_log)이 자동화 산출물의 추적성을 보장.

## 7. 평가/골드셋 하네스

- `scripts/build_synthetic_gold_set.py`: LLM Generator/Judge 분리로 검색 평가 골드셋 자동 생성(역방향 생성 + 4단계 품질 게이트)
- `scripts/eval_search.py` + `src/context_loop/eval/*`(`metrics.py`, `graph_match.py`, `synth.py`, `gold_set.py`): Recall/Precision/MRR/nDCG + 그래프 매칭
- `scripts/compare_runs.py`: 실행 간 비교

> **성숙도: 완성(전용 하네스 다수).** 자동화 산출물의 **품질 회귀를 정량 측정**하는 데 재사용 가능 — 자동화 신뢰성의 안전장치.

## 8. LLM/임베딩 추상화

- `processor/llm_client.py`, `reranker_client.py`, `embedder.py`: provider `openai`/`anthropic`/`endpoint`(자체 OpenAI-호환 엔드포인트 우선) 지원
- 함의: 자동화의 LLM 호출을 사내 모델 서버로 라우팅 가능 → 보안/비용 통제 용이

---

## 자동화 building block 요약 표

| building block | 위치 | 성숙도 | 자동화 활용 가능성 |
|---------------|------|--------|------------------|
| MCP search_context | `mcp/tools.py:20` | 완성 | ★★★ 사내지식 RAG 인출의 표준 통로 |
| MCP get_graph_context | `mcp/tools.py:159` | 완성 | ★★★ 영향도/의존성 탐색 |
| git_code AST 그래프 | `ast_code_extractor.py:210` | 완성·고신뢰 | ★★★ 코드 구조 자동분석 기반 |
| 지식 그래프 + 임베딩탐색 | `graph_store.py` | 완성 | ★★★ 의존·관계 질의 |
| 검색 조립기(멀티뷰/리랭크/HyDE) | `context_assembler.py:58` | 완성 | ★★ RAG 품질 |
| 웹 REST API + /graph 시각화 | `web/api/*` | 완성 | ★★ 검수·탐색 UI |
| document_sources(출처추적) | `metadata_store.py:103` | 완성 | ★★ 산출물 추적성 |
| graph_merge_log(병합이력) | `metadata_store.py:80` | 완성 | ★ 통합 품질 모니터 |
| 재처리(reprocessor) | `reprocessor.py` | 완성 | ★★ 변경기반 자동 갱신 |
| 평가 하네스 | `scripts/eval_search.py` | 완성 | ★★ 산출물 품질 회귀 측정 |
| 문서 LLM 그래프 | `graph_extractor.py` 등 | 부분 | ★ 품질 의존 시나리오 주의 |
| **CLI** | `cli.py` | **미구현** | ✗ 자동화 트리거 공백(선행과제) |
