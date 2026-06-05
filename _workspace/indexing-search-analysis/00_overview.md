# 인덱싱 & 검색 end-to-end 개요 — Context Loop System

> 목적: 이 프로젝트가 **(1) 문서를 어떻게 인덱싱**하고 **(2) 질의에 대해 어떻게 검색·
> 컨텍스트를 조립**하는지를 한 문서로 잇는다. 각 파이프라인의 단계별 상세는 아래
> 하위 보고서를 참조한다.
>
> 기준 브랜치: `claude/indexing-search-analysis-tq5Is` · 작성일 2026-06-02.

## 참조 문서 맵

| 영역 | 문서 |
|------|------|
| 인덱싱 통합 비교 | `_workspace/indexing-analysis/00_overview.md` |
| 인덱싱 — confluence_mcp 6단계 | `_workspace/indexing-analysis/01_confluence_mcp_indexing.md` |
| 인덱싱 — confluence_mcp 그래프 추출 심층 | `_workspace/indexing-analysis/01b_confluence_mcp_graph_extraction.md` |
| 인덱싱 — git_code 6단계 | `_workspace/indexing-analysis/02_git_code_indexing.md` |
| **검색/질의 파이프라인** | `_workspace/indexing-search-analysis/02_search_pipeline.md` |

---

## 1. 시스템 한 장 요약

Context Loop System은 사내 분산 지식(Confluence 문서, Git 코드, 업로드/직접 작성)을
수집·처리해 **하이브리드 저장소(벡터 + 그래프 + 메타데이터)** 로 인덱싱하고, MCP
서버/웹 API를 통해 LLM 애플리케이션에 컨텍스트를 제공하는 RAG 플랫폼이다.

- 벡터: **ChromaDB** (컬렉션 `context_loop_chunks`, cosine)
- 그래프: **NetworkX(DiGraph) + SQLite** (`graph_nodes`/`graph_edges`/`graph_merge_log`)
- 메타데이터: **SQLite/aiosqlite** (`documents`/`chunks`/...)
- 임베딩: endpoint(OpenAI 호환, 기본 `text-embedding-3-small`) 또는 local(sentence-transformers)
- LLM: endpoint 기반 (그래프 추출·HyDE·그래프 플래너용)
- 인터페이스: MCP 서버(stdio/SSE), 웹 대시보드(FastAPI)
- 설정 단일 출처: `config/default.yaml`

---

## 2. 인덱싱 파이프라인 (6단계) — 요약

단일 진입점 `processor/pipeline.py::process_document()`. 핵심 분기는
`source_type == "git_code"` 하나이며, 그 외(confluence/confluence_mcp/upload)는
`else` 로 묶인다. **`storage_method`(chunk/graph/hybrid)는 LLM classifier가 아니라
처리 결과에서 파생**(`_derive_storage_method`)된다 — CLAUDE.md 설계의 "LLM
Classifier"는 현재 코드에 없다.

| 단계 | confluence_mcp | git_code |
|------|----------------|----------|
| 1 수집 | MCP `get_page` → HTML→MD, `original_content`(MD)+`raw_content`(HTML) 저장 (`mcp_confluence.py`) | `git clone/pull` → 파일순회·필터 → `original_content`(코드 평문) (`git_repository.py`) |
| 2 전처리 | `confluence_extractor.extract()` (BeautifulSoup: sections/links/code/tables/mentions) | `ast_code_extractor.extract_code_symbols()` (Python=ast, brace=정규식, 기타=fallback) |
| 3 청킹 | `chunk_extracted_document_doclevel()` — **문서 단위**(작으면 1청크, 크면 섹션 폴백, max 8000 tok) | `to_chunks()` — **심볼 1개=청크 1개** |
| 4 임베딩 | body + meta + **가상질문(LLM)** = 청크당 최대 3 view | body + meta = 2 view (질문 없음) |
| 5 그래프 | **3중**: 링크(결정론) + 본문 휴리스틱(결정론) + LLM 의미관계(기본 ON) | **1종**: AST import/contains (call·상속 미추출) |
| 6 저장 | (공유) ChromaDB + SQLite chunks + graph_nodes/edges, **정규화 키 병합** | (공유) 동일 |

**공유 모듈**: 임베딩(`embedder.py`, batch 100, body/meta는 `logical_chunk_id`
공유), 저장소 3종, 그래프 데이터 모델(`graph_extractor.py`)·어휘(`graph_vocabulary.py`),
토큰 카운팅(`chunker.count_tokens`, tiktoken cl100k_base).

**6단계 저장의 엔티티 정규 병합 (2026-05-28 갱신)**: 동명/표기변형 엔티티를 하나의
그래프 노드로 합치는 키가 `(LOWER(name), entity_type)` → `normalize_entity_name`
(NFKC→strip→공백/`-`/`_` 제거→lower)으로 만든 `normalized_name + entity_type` 으로
바뀌었다. 병합 결과는 `graph_merge_log`(exact/normalized/new)에 관측 기록되며
(`_record_merge_safely`), 실패해도 저장을 중단하지 않는다.

---

## 3. 검색/질의 파이프라인 — 요약

세 진입점(MCP `search_context` / 웹 `/api/chat` / CLI)이 모두
`mcp/context_assembler.py` 의 `assemble_context()`(→ str) 또는
`assemble_context_with_sources()`(→ `AssembledContext`)로 수렴한다.

1. **쿼리 임베딩**: 기본 `_embed_query()`, 또는 HyDE(`expand_query_embedding()` — 가상 답변 문서 임베딩과 평균).
2. **벡터 검색** `_search_chunks()`: `vector_store.search(n_results=max_chunks*6)` over-fetch → `document_id` dedup → `similarity_threshold` 필터.
3. **리랭킹 + 그래프 탐색 병렬** `_rerank_and_search_graph()` (`asyncio.gather`):
   - 리랭킹: cross-encoder `reranker.rerank()` (옵션).
   - 그래프: `plan_graph_search()`(LLM 계획 JSON: target_entities/target_relations) → `execute_graph_search()`(엔티티 매칭 4단계 폴백 → 양방향 BFS → query-embedding 시드 보강 → 포맷).
4. **그래프 도달 문서 보강** `_search_graph_sourced_chunks()`: 그래프가 닿았지만 벡터가 못 찾은 문서 본문을 `max_graph_docs`/`max_graph_tokens` 예산 안에서 첨부.
5. **원본 소스 코드 첨부**(옵션): code_doc/code_summary ↔ git_code 원본.
6. **섹션 조립**: `## 관련 문서` / `## 관련 그래프 컨텍스트` / `## 그래프 연결 문서` / `## 원본 소스 코드`.

상세는 `02_search_pipeline.md`.

---

## 4. ★ 인덱싱 ↔ 검색 정렬 지점 (end-to-end 연결)

이 시스템의 설계 핵심은 **인덱싱에서 만든 산출물이 검색에서 그대로 소비되도록
어휘·방향성·입자도를 맞추는 것**이다.

| 인덱싱 산출물 | 검색에서의 소비 | 정렬 메커니즘 |
|---------------|------------------|----------------|
| **멀티뷰 임베딩** (body/meta/question, 한 청크가 여러 view로 임베딩) | 벡터 검색 `n_results = max_chunks * 6` over-fetch 후 `document_id` 단위 dedup | 한 문서의 여러 view가 중복 점유하지 않도록 dedup. 살아남은 결과의 `view`/`section_path`/`question_text` 가 출처 라벨로 보존 |
| **가상 질문 인덱싱** (`question_generator`, 섹션당 ~5개, 문서당 ≤50) | HyDE 가상 문서(`expand_query_embedding`) ↔ question view 매칭. `view=="question"` 히트는 "매칭 질문"으로 표기 | 질의↔질문 임베딩 정렬로 recall 보강 |
| **링크/본문/LLM 그래프** (정규화 노드로 수렴) | 그래프 플래너가 **동일 어휘**(`graph_vocabulary`)·**동일 방향성**으로 target_entities/target_relations 생성 | 프롬프트가 인덱싱 어휘(entity/relation types)만 쓰도록 강제, 스키마 표기 "글자 단위 복사" 지시 |
| **엔티티 정규화 병합** (`normalized_name`+type, 동명 단일 노드) | `get_neighbors()` 가 크로스-문서 관계를 자연스럽게 탐색 | 동명 엔티티가 한 노드 → 문서 경계 넘는 1-hop 탐색 가능 |
| **엔티티 이름/FQN** (git_code: `<file>::<parent>.<name>`) | 그래프 시드 해석 4단계 폴백(완전→scoped→short→임베딩) | LLM이 짧은 이름/부분경로로 물어도 매칭 |
| **노드 description / 1-hop 관계** | 평가용 `GraphEntityRef.description`(없으면 `_natural_description`으로 1-hop 관계를 자연어화) | tiered matching T4 임베딩의 비특이성 완화 |
| **code_doc/code_summary ↔ git_code 원본** (`document_sources`) | `include_source_code` 첨부 (`_fetch_and_format_source_code`) | 요약/문서 히트 시 원본 코드 검증용 첨부 |

### 정렬이 깨지면 생기는 funnel 손실(코드 주석이 명시)
- LLM 추측 엔티티명 ≠ 인덱스 표기 → 표면 매칭 0개 → **임베딩 fallback**(4단계)으로 회복.
- sink 노드(DB/외부시스템)가 시드로 선택 → successor-only면 자기 자신만 반환 → **양방향 BFS**(`_bidirectional_bfs`)로 회복.
- LLM이 sink 이웃만 선택 → **always-on query-embedding 시드 보강**(threshold 0.6, top_k 3) + 전체 실패 시 최종 폴백(threshold 0.5, top_k 5).

---

## 5. 비대칭 관찰 (사실 기록)

1. **LLM 의존도**: confluence 인덱싱은 문서당 LLM 최대 2회(질문 생성 + 의미 그래프), git_code는 0회(순수 정적). 검색 측은 양쪽 공통으로 HyDE(옵션)+그래프 플래너 LLM 사용.
2. **청킹 입자도 철학**: confluence는 문서 단위(검색 dedup이 입자도 담당), git_code는 심볼 단위(코드 검색의 자연 단위).
3. **그래프 풍부도**: confluence는 3경로 수렴(의미 관계까지), git_code는 구조 관계(import/contains)만 — **호출 그래프·상속 미추출**.
4. **수집 ≠ 처리**: `sync_repository`/`import_page_via_mcp`는 `documents`만 적재. 파생 데이터 생성(`process_document`)은 별도 오케스트레이션(`coordinator.py`/`sync/engine.py`).
5. **설계 vs 코드 차이**: CLAUDE.md의 "LLM Classifier"는 미구현 — `storage_method`는 결과에서 파생.

---

## 6. 핵심 파일 인덱스

**인덱싱**: `processor/pipeline.py`(오케스트레이션), `ingestion/{mcp_confluence,git_repository}.py`, `ingestion/confluence_extractor.py`, `processor/{ast_code_extractor,chunker,extraction_unit,question_generator,embedder}.py`, `processor/{link_graph_builder,body_extractor,llm_body_extractor,graph_extractor,graph_vocabulary}.py`, `storage/{vector_store,metadata_store,graph_store,entity_normalizer}.py`.

**검색**: `mcp/context_assembler.py`(조립), `processor/{query_expander,reranker,graph_search_planner}.py`, `storage/{graph_store,vector_store}.py`, `mcp/tools.py`, `web/api/chat.py`.
