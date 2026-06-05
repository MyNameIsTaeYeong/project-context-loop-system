# 인덱싱 파이프라인 통합 개요 — confluence_mcp vs git_code

> 기준: 현재 HEAD (origin/main 머지 완료). 두 소스타입의 6단계 인덱싱 동작을 비교한다.
> 상세는 `01_confluence_mcp_indexing.md`, `02_git_code_indexing.md` 참조.

## 공통 진입점과 분기

두 소스 모두 **수집 흐름**(documents 테이블 적재)과 **처리 흐름**(`process_document`로 파생 데이터 생성)이 분리된다.

처리 흐름의 단일 진입점은 `processor/pipeline.py::process_document()` (라인 93). 핵심 분기는 `source_type == "git_code"` (pipeline.py:153) 하나뿐이며, 그 외 모든 소스(confluence/confluence_mcp/upload)는 `else`로 묶인 뒤 내부에서 `source_type in ("confluence","confluence_mcp") and raw_content` 조건으로 구조화 추출 여부를 다시 가른다 (pipeline.py:238).

**`storage_method`(chunk/graph/hybrid)는 LLM classifier가 아니라 처리 결과에서 파생된다** (`_derive_storage_method`, pipeline.py:542). CLAUDE.md 설계 문서의 "LLM Classifier" 단계는 현재 코드에 없다 — 이것이 설계와 실제 코드의 가장 큰 차이.

## 6단계 나란히 비교

| 단계 | confluence_mcp | git_code |
|------|----------------|----------|
| **1. 수집** | MCP 서버 경유 `get_page` → HTML→MD 변환 → `documents`에 **MD(original_content) + HTML(raw_content) 둘 다** 저장. `mcp_confluence.py:import_page_via_mcp` | `git clone/pull` → 파일시스템 순회 → 필터 → `original_content`에 **코드 평문**(raw_content 없음). `git_repository.py:sync_repository` |
| **2. 전처리** | `confluence_extractor.extract(raw_html)` — BeautifulSoup로 sections/links/code/tables/mentions 구조화 | `ast_code_extractor.extract_code_symbols` — Python=ast모듈(정확), brace언어=정규식(휴리스틱), 기타=fallback |
| **3. 청킹** | `chunk_extracted_document_doclevel` — **문서 단위**(작으면 1청크, 크면 섹션 폴백), max 8000토큰 | `to_chunks` — **심볼 1개=청크 1개**, 토큰 분할 없음 |
| **4. 임베딩** | body + meta + **가상질문(LLM 생성)** = 청크당 최대 3종 뷰 | body + meta = 청크당 2종 뷰 (가상질문 없음) |
| **5. 그래프** | **3중**: 링크(결정론) + 본문 휴리스틱(결정론) + LLM 의미관계(opt-in) | **1종**: AST import/contains (call graph·상속 없음) |
| **6. 저장** | (공유) ChromaDB + SQLite chunks + graph_nodes/edges | (공유) 동일 |

## 공유하는 단계/모듈 (소스 무관 동일 코드)

- **4. 임베딩 클라이언트** — `processor/embedder.py`. `EndpointEmbeddingClient`(OpenAI 호환 REST, batch 100) 또는 `LocalEmbeddingClient`(sentence-transformers). 멀티뷰 패턴(body/meta + `logical_chunk_id` 공유)도 양쪽 공통.
- **6. 저장소 3종** — `storage/vector_store.py`(ChromaDB, 컬렉션 `context_loop_chunks`, cosine), `storage/metadata_store.py`(SQLite `create_chunk`), `storage/graph_store.py`(`save_graph_data`, **정규화 키(`normalized_name` + `entity_type`) 기반 정규 병합** + `graph_merge_log` 관측 로그 + NetworkX). 2026-05-28(63e1fd3/51fc495)부터 병합 키가 `(LOWER(name), type)` → `normalize_entity_name`(NFKC→strip→공백/`-`/`_` 제거→lower) 기반으로 변경됨.
- **그래프 데이터 모델** — `Entity`/`Relation`/`GraphData` (graph_extractor.py), 어휘 `graph_vocabulary.py`.
- **재처리 래퍼** — `reprocessor.start_reprocessing`/`complete_reprocessing`, `_derive_storage_method`.
- **토큰 카운팅** — `chunker.count_tokens` (tiktoken cl100k_base, 폴백 1char=1tok).

## 갈라지는 단계 (소스별 전용)

| 측면 | confluence_mcp | git_code |
|------|----------------|----------|
| 1 수집 모듈 | `mcp_confluence.py` (MCP/JSON-RPC) | `git_repository.py` (git CLI subprocess) |
| 2 전처리 모듈 | `confluence_extractor.py` (BeautifulSoup) | `ast_code_extractor.py` (ast/정규식) |
| 3 청킹 함수 | `chunk_extracted_document_doclevel` | `to_chunks` (심볼 단위) |
| LLM 사용 | 가상질문 + LLM 본문그래프 (2곳, 기본 ON) | **없음** (순수 정적) |
| 그래프 추출 모듈 | `link_graph_builder` + `body_extractor`(+`extraction_unit`) + `llm_body_extractor` | `ast_code_extractor.to_graph_data` |
| 엔티티 이름 규칙 | self=doc_title, 대상=표시명/정규화 | 심볼=FQN `<file>::<parent>.<name>`, 파일/import=단순명 |
| SQLite embed_text | 비움(본문=임베딩입력) | 채움(meta_text, 본문≠임베딩입력) |

## 핵심 관찰 (사실 기록)

1. **LLM 의존도 비대칭**: confluence는 인덱싱 중 문서당 LLM 호출이 최대 2회(질문 생성 + 의미 그래프). git_code는 0회.
2. **청킹 입자도 철학 차이**: confluence는 문서 단위(검색 dedup이 입자도 담당), git_code는 심볼 단위(코드 검색은 심볼이 자연 단위).
3. **그래프 풍부도 비대칭**: confluence는 3경로가 한 노드로 수렴해 의미 관계까지. git_code는 구조 관계(import/contains)만, **호출 그래프와 상속 관계는 미추출**.
4. **수집 트리거**: 두 sync 함수(`sync_repository`, `import_page_via_mcp`)는 `documents`만 적재하고 파생 데이터를 만들지 않는다. `process_document`는 별도로 호출되어야 한다 (오케스트레이션은 `coordinator.py`/`sync/engine.py` 영역 — 본 분석 미포함).

## 산출물

- `_workspace/indexing-analysis/00_overview.md` (본 문서)
- `_workspace/indexing-analysis/01_confluence_mcp_indexing.md`
- `_workspace/indexing-analysis/02_git_code_indexing.md`
