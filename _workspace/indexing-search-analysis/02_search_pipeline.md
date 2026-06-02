# 검색/질의(Query) 파이프라인 분석 — 현재 동작(as-is)

> 목적: 사용자 질의가 들어왔을 때 **현재 코드가 어떻게 검색·컨텍스트를 조립하는가**를
> 함수·파일경로·파라미터 단위로 서술한다. 개선 제안이 아니라 동작 기술이다.
> 기준 커밋: `claude/indexing-search-analysis-tq5Is` (2026-06-02 시점).

---

## 0. 한눈에 보는 전체 흐름

```
                          ┌─ MCP tool: search_context (mcp/tools.py)
질의(query) 진입점 ────────┼─ Web API: POST /api/chat (web/api/chat.py)
                          └─ (모두) mcp/context_assembler.py
                                   assemble_context() / assemble_context_with_sources()
                                       │
        ┌──────────────────────────────┼───────────────────────────────┐
        ▼                                                                ▼
  [1] 쿼리 임베딩                                              (llm_client 없으면 그래프 스킵)
   - 기본: _embed_query()
   - HyDE: expand_query_embedding()
        │
        ▼
  [2] 벡터 검색  _search_chunks()
   - vector_store.search(n_results = max_chunks * 6)   ← over-fetch
   - document_id 단위 dedup
   - similarity_threshold 필터 (1 - distance)
        │
        ▼
  [3] 리랭킹 + 그래프 탐색을 asyncio.gather 로 병렬  _rerank_and_search_graph()
        ├── _maybe_rerank()  → reranker.rerank()  (cross-encoder, optional)
        └── _maybe_graph()   → _search_graph_with_llm()
                                   ├ plan_graph_search()      (LLM 탐색 계획 JSON)
                                   └ execute_graph_search()   (엔티티 매칭 → 양방향 BFS → 포맷)
        │
        ▼
  [4] 그래프 도달 문서 보강  _search_graph_sourced_chunks()
   - 그래프가 도달했지만 벡터가 못 찾은 문서의 본문 청크 인출
   - max_graph_docs / max_graph_tokens 예산 가드
        │
        ▼
  [5] (옵션) 원본 소스 코드 첨부  _fetch_and_format_source_code()
        │
        ▼
  [6] 섹션 조립 → str(assemble_context) 또는 AssembledContext(assemble_context_with_sources)
```

---

## 1. 진입점 (Entry Points)

세 경로 모두 결국 `src/context_loop/mcp/context_assembler.py` 의
`assemble_context()`(텍스트 반환) 또는 `assemble_context_with_sources()`
(구조화 반환)로 수렴한다.

### 1.1 MCP 도구
- 파일: `src/context_loop/mcp/tools.py`
- `register_tools()` 내 `@mcp.tool() async def search_context(query, max_chunks=10, include_graph=True, include_source_code=True)` (tools.py:19-66)
- 동작: `mcp/server.py` 의 전역 스토어/클라이언트(`_get_stores()`, `_embedding_client`, `_llm_client`, `_reranker_client`, `_config`)를 가져와 `assemble_context()` 호출. 검색 관련 모든 설정값을 `_config.get("search.*")`, `_config.get("mcp.*")` 로 읽어 인자로 전달한다.
- 그 외 MCP 도구: `list_documents`, `get_document(format=original|chunks|graph)`, `get_graph_context(entity_name, depth)`. 마지막 도구는 `assemble_context` 를 거치지 않고 `graph_store.get_neighbors()` + `get_edges_between()` 를 직접 노출한다 (tools.py:158-197).

### 1.2 웹 API
- 파일: `src/context_loop/web/api/chat.py` — `POST /api/chat`. 스트리밍 응답(NDJSON), 출처(`sources`) 포함. 내부적으로 `assemble_context_with_sources()` 사용.

### 1.3 CLI / MCP 서버 부트스트랩
- 파일: `src/context_loop/cli.py` → `main()` → `context-loop mcp serve [--transport stdio|sse] [--port]`
- `src/context_loop/mcp/server.py` → `run_stdio()` / `run_sse(port)`, `_initialize()` 에서 MetadataStore/VectorStore/GraphStore/EmbeddingClient/LLMClient/RerankerClient 초기화.

---

## 2. [1단계] 쿼리 임베딩 생성

`assemble_context()` / `assemble_context_with_sources()` 진입 직후(context_assembler.py:106-109, 519-522):

- **기본 경로** `_embed_query(query, embedding_client)` (context_assembler.py:171-180): `embedding_client.aembed_query(query)` 호출. 실패 시 `None` 반환(이후 벡터 검색은 빈 결과로 graceful degradation).
- **HyDE 경로** (`hyde_enabled=True` 이고 `llm_client` 존재 시): `expand_query_embedding(query, llm_client, embedding_client)` (query_expander.py:64-101)
  - `generate_hypothetical_document()` (query_expander.py:34-61): LLM에게 "질문에 대한 가상의 사내 기술 문서 단락(3~5문장)"을 생성하게 한다. `max_tokens=32768, temperature=0.7, reasoning_mode="off", purpose="hyde_query_expansion"`.
  - 원본 쿼리 임베딩과 가상 문서 임베딩을 `_average_embeddings()` (단순 산술 평균, query_expander.py:104-109)으로 합쳐 반환.
  - 가상 문서 생성 실패 시 원본 쿼리 임베딩으로 폴백.
- 설정: `search.hyde_enabled` (config/default.yaml: 기본 `false`).

---

## 3. [2단계] 벡터 유사도 검색 — `_search_chunks()`

파일: `context_assembler.py:183-234`. 저장소: `storage/vector_store.py` (`VectorStore.search()`, ChromaDB 래퍼).

핵심 동작:
1. `vector_store.count() == 0` 이거나 `query_embedding is None` 이면 즉시 `[]`.
2. **Over-fetch**: `vector_store.search(query_embedding, n_results=max_chunks * 6)` (context_assembler.py:211). 배수 6은 R3 멀티뷰 인덱싱(body/meta/question, 한 문서가 여러 view로 임베딩) + 섹션당 가상질문 3~5개를 흡수하기 위함.
3. **`document_id` 단위 dedup** (context_assembler.py:212-225): ChromaDB가 distance 오름차순으로 반환하므로, 문서별 첫 등장(=최소 distance=최고 유사도) 항목만 채택. dedup 키 우선순위: `metadata.document_id` → `metadata.logical_chunk_id` → `id`. `max_chunks` 도달 시 중단.
4. **threshold 필터** (context_assembler.py:226-230): `similarity_threshold > 0` 이면 `(1 - distance) >= threshold` 인 항목만 유지. 설정 `search.similarity_threshold` (config 기본 `0.3`; 함수 기본값은 `0.0`).
5. 예외 발생 시 `[]` 반환 (graceful degradation).

`VectorStore.search()` (vector_store.py:79-): ChromaDB `collection.query(query_embeddings, n_results, where=...)` 호출, `{id, document, metadata, distance}` 리스트로 정규화.

> 멀티뷰의 의미: 같은 청크가 `view ∈ {body, meta, question}` 로 여러 번 임베딩되어
> 있고, dedup 후 살아남은 결과의 `metadata.view`/`section_path`/`question_text`
> 가 출처 라벨로 보존된다 — `view == "question"` 이면 "매칭 질문" 으로 표기.

---

## 4. [3단계] 리랭킹 + 그래프 탐색 병렬 실행 — `_rerank_and_search_graph()`

파일: `context_assembler.py:425-466`. 리랭킹은 `chunk_results` 에만, 그래프 계획은 `query_embedding` 에만 의존하므로 **`asyncio.gather()` 로 동시 실행**하여 외부 모델 호출 지연을 겹친다.

### 4.1 리랭킹 — `_maybe_rerank()` → `reranker.rerank()`
- 활성 조건: `chunk_results` 존재 AND `rerank_enabled` AND `reranker_client` 존재.
- `rerank(query, chunks, reranker_client, top_k)` (reranker.py:17-65):
  - 각 청크 본문을 `_truncate(limit=2000)` 로 잘라 cross-encoder 리랭커에 전달.
  - `reranker_client.rerank(query, documents)` → 점수 리스트. 각 청크에 `rerank_score` 부여 후 내림차순 정렬, `top_k` 상한.
  - 호출 실패 시 원본 순서 유지(점수 `1.0 - i*0.001`) — graceful degradation.
- 이후 `rerank_score_threshold > 0` 이면 점수 미달 청크 제거 (context_assembler.py:450-454).
- 설정: `search.reranker_enabled`(기본 false), `reranker_top_k`(기본 5), `reranker_score_threshold`(기본 0.3).

### 4.2 그래프 탐색 — `_maybe_graph()` → `_search_graph_with_llm()`
- 활성 조건: `include_graph` AND `llm_client` 존재.
- `_search_graph_with_llm()` (context_assembler.py:385-422):
  1. 엔티티 임베딩 캐시가 비어 있으면(`graph_store.entity_embedding_count == 0`) `graph_store.build_entity_embeddings(embedding_client)` 로 최초 1회 구축 (graph_store.py:834-857; 모든 노드 이름을 `aembed_documents` 로 임베딩).
  2. `plan_graph_search()` 로 탐색 계획 수립 → `plan.should_search == False` 면 `None`.
  3. `execute_graph_search()` 로 실제 탐색.

---

## 5. [3단계-상세] 그래프 검색 플래너

파일: `src/context_loop/processor/graph_search_planner.py`.

### 5.1 탐색 계획 수립 — `plan_graph_search()` (planner.py:190-238)
1. 그래프가 비면(`graph_store.stats()["nodes"] == 0`) `should_search=False`.
2. **스키마 컨텍스트 선택**:
   - `query_embedding` 있고 엔티티 임베딩 구축됨 → `format_query_relevant_schema_for_llm(query_embedding)` (쿼리 유사 서브그래프 스키마만; graph_store.py:788-830, 시드 엔티티+유형별 목록+관계 유형/예시).
   - 아니면 `format_schema_for_llm()` (전체 스키마 요약).
3. **LLM 호출** (planner.py:225-232): 시스템 프롬프트(`_render_system_prompt()`)는 `graph_vocabulary` 단일 출처에서 entity types / relation types / intent mapping 을 주입한다. `max_tokens=32768, temperature=0.0, reasoning_mode="off", purpose="graph_search_planner"`. 큰 `max_tokens` 는 reasoning 모델의 thinking 예산으로 JSON이 잘리는 것을 방지하기 위함.
4. **응답 파싱** `_parse_plan()` (planner.py:241-304): JSON에서
   - `should_search`, `reasoning`
   - **`target_entities`** (R3 1차 신호): `{name, type}` 최대 5개
   - **`target_relations`** (R3 1차 신호): `{source, target, relation_type}` 방향성 보존, 최대 5개. 끝점 미상은 빈 문자열 허용.
   - `search_steps` (R2 이하 후방 호환): `{entity_name, depth(1~2), focus_relations}` 최대 3개.

> 프롬프트 설계 의도: **인덱싱 시점과 같은 어휘·같은 방향성**으로 정답 엔티티/
> 관계를 명시하게 한다. 스키마에 실제 존재하는 이름을 "글자 단위로 정확히 복사"
> 하도록 강제(공백/케이스/하이픈/언더스코어 보존). 어휘 외 relation_type 금지.

### 5.2 탐색 실행 — `execute_graph_search()` (planner.py:307-684)

엔티티 매칭과 시드 보강이 핵심이다. LLM이 추측한 이름이 인덱스 표기와 달라 표면 매칭이 0개가 되면 그래프 메트릭이 0%로 떨어지는 funnel 손실을 막기 위해 다단계 보강을 둔다.

순서:
1. **`target_entities` 처리** (planner.py:380-400): 각 후보를 `graph_store.get_neighbors(name, depth=1, embedding_fallback=<name 임베딩>)` 로 조회. 시드 자기 자신은 `priority=True` 로 표시(rank-1 보장 목적).
2. **`target_relations` 처리** (planner.py:402-424): 각 관계의 source/target 끝점을 시드로 추가, 끝점 자신은 `priority`.
3. **`search_steps` 처리** (planner.py:429-447): R2 호환 경로. `depth`(1~2)로 BFS.
4. **R2 always-on query 임베딩 시드 보강** (planner.py:454-477): `query_embedding` 이 있으면 `search_entities_by_embedding(threshold=0.6, top_k=3)` 으로 유사 노드를 항상 union 보강하고, 각 보강 노드에서 `get_neighbors_from_node_id(depth=1)`. LLM이 sink 이웃만 시드로 골라 retrieved 가 sink 자신만 담기는 케이스를 보완.
5. **전체 실패 시 최종 폴백** (planner.py:480-500): 노드가 0개면 `search_entities_by_embedding(threshold=0.5, top_k=5)` 로 더 낮은 임계값 폴백.
6. **엣지 수집** `get_edges_between(all_node_ids)` (planner.py:505) + `focus_relations` 필터(planner.py:507-516).
7. **document_id 수집** (planner.py:518-528): 노드의 `document_ids`(정규 노드는 set) 합집합.
8. **텍스트 포맷팅** (planner.py:530-554): `## 관련 그래프 컨텍스트` + 탐색 근거 + **엔티티** 목록(`- name (type) * — desc`, `*` 는 시드) + **관계** 목록(`- src --[rel]--> tgt (label)`).
9. **평가용 구조화 출력** (planner.py:556-684):
   - `entities: list[GraphEntityRef]` — priority 노드를 앞순위로 정렬(MRR/NDCG rank 민감 대응). description이 비면 1-hop 관계를 자연어 문장으로 풀어 `_natural_description()` 으로 채움(T4 임베딩 매칭의 비특이성 완화).
   - `relations: list[GraphRelationRef]` — `--score-relations` 평가용 1-hop 엣지.

### 5.3 그래프 스토어 탐색 메커니즘 — `storage/graph_store.py`
- **시드 해석** `_resolve_seed_nodes()` (graph_store.py:402-450): 4단계 폴백
  1. 완전 일치(`entity_name.lower()`)
  2. 파일 범위 제거 일치(`_extract_scoped_name`, 예 `UserService.create_user`)
  3. 짧은 이름 일치(`_extract_short_name`, 예 `create_user`)
  4. 임베딩 fallback (`search_entities_by_embedding`, threshold 0.5, top_k 3)
- **양방향 BFS** `_bidirectional_bfs(sources, depth)` (graph_store.py:370-400): DiGraph의 successor-only 한계를 보완 — successor+predecessor 모두 따라가 depth-hop 방문 집합 반환. "X를 누가 사용하나?" 류에서 sink 시드가 자기 자신만 반환하던 funnel 손실(F-SRCH-R2-01) 대응.
- `get_neighbors()` (graph_store.py:526-581), `get_neighbors_from_node_id()` (583-605): 시드 해석 → 양방향 BFS → 노드 dict 목록.
- `search_entities_by_embedding(query_embedding, threshold, top_k)` (graph_store.py:859-886): 캐시된 엔티티 임베딩과 코사인 유사도 비교 후 상위 반환.

---

## 6. [4단계] 그래프 도달 문서 본문 보강 — `_search_graph_sourced_chunks()`

파일: `context_assembler.py:289-351` (설계 A).

- 목적: 그래프가 관계로 **도달한 문서**(`graph_result.document_ids`) 중 **벡터 검색이 못 찾은 것**(`existing_doc_ids` 제외)의 가장 관련된 청크 본문을 별도 섹션으로 첨부. 임베딩으로는 안 닿지만 관계로 연결된 문서를 실제 산문으로 LLM에 전달.
- 동작:
  - 신규 doc_id만 추림, `_MAX_GRAPH_DOC_FILTER=50` 가드.
  - `vector_store.search(query_embedding, n_results=len(new_doc_ids)*6, where={"document_id": {"$in": new_doc_ids}})` — 멀티뷰 over-fetch + dedup.
  - `document_id` dedup + **개수 상한 `max_graph_docs`** + **토큰 상한 `max_graph_tokens`**(`count_tokens()` 누적) 동시 적용.
- 설정: `mcp.max_graph_context_docs`(기본 3), `mcp.max_graph_context_tokens`(기본 6000). 0이면 기능 off.

---

## 7. [5단계] 원본 소스 코드 첨부(옵션) — `_fetch_and_format_source_code()`

파일: `context_assembler.py:649-696` (Phase 9.7).

- 조건: `include_source_code=True` AND 벡터 결과 존재.
- 동작: 히트 문서 중 `source_type ∈ {code_doc, code_summary}` 인 것의 `document_sources` 연결을 통해 원본 git_code 문서의 `original_content` 를 조회, 파일 확장자 기반 언어 힌트로 코드블록 포맷팅. 헤더 `## 원본 소스 코드 (검증용)`.

---

## 8. [6단계] 최종 컨텍스트 조립 & 반환

섹션은 `"\n\n---\n\n"` 로 결합된다. 가능한 섹션 순서:
1. `## 관련 문서` — 벡터 검색 청크 (`_format_chunk_results`, context_assembler.py:237-267). 헤더에 유사도, 섹션 경로, (질문뷰면) 매칭 질문.
2. `## 관련 그래프 컨텍스트` — `graph_result.text`.
3. `## 그래프 연결 문서` — 그래프 도달 보강 청크 (`_format_graph_chunk_results`, 354-382).
4. `## 원본 소스 코드 (검증용)` — 옵션.

### 8.1 두 반환 형태
- `assemble_context()` → **`str`** (context_assembler.py:58-168). MCP `search_context` 가 사용. 빈 결과면 `"관련 컨텍스트를 찾을 수 없습니다."`.
- `assemble_context_with_sources()` → **`AssembledContext`** (context_assembler.py:469-631). 웹 API/평가가 사용.
  ```python
  @dataclass
  class AssembledContext:
      context_text: str
      sources: list[Source]                          # {document_id, title, similarity}
      retrieved_graph_entities: list[GraphEntityRef] # 노드 (description 포함)
      retrieved_graph_relations: list[GraphRelationRef] # 1-hop 엣지
  ```
  - `sources` 는 벡터 히트 + (본문이 실제 첨부된) 그래프 도달 문서만 포함, similarity 내림차순 정렬(context_assembler.py:580-618). 출처 목록을 실제 컨텍스트에 들어간 문서와 일치시킨다.
  - `Source`/`GraphEntityRef`/`GraphRelationRef` 정의는 `mcp/context_assembler.py` 및 `eval/gold_set.py`.

---

## 9. 관련 설정값 요약 (config/default.yaml)

| 키 | 기본값 | 역할 |
|----|--------|------|
| `search.similarity_threshold` | 0.3 | 벡터 청크 최소 코사인 유사도 (1 - distance) |
| `search.reranker_enabled` | false | cross-encoder 리랭킹 사용 |
| `search.reranker_top_k` | 5 | 리랭킹 후 반환 청크 수 |
| `search.reranker_score_threshold` | 0.3 | 리랭크 점수 최소값 |
| `search.hyde_enabled` | false | HyDE 쿼리 확장 사용 |
| `mcp.max_context_chunks` | 10 | 반환 청크(문서) 상한 |
| `mcp.include_graph_by_default` | true | 그래프 컨텍스트 포함 기본값 |
| `mcp.context_max_tokens` | 32768 | 컨텍스트 토큰 상한 |
| `mcp.max_graph_context_docs` | 3 | 그래프 도달 문서 첨부 개수 상한 |
| `mcp.max_graph_context_tokens` | 6000 | 그래프 도달 문서 첨부 토큰 상한 |

---

## 10. 핵심 컴포넌트 맵

| 컴포넌트 | 파일 | 핵심 함수/클래스 |
|---------|------|-----------------|
| 컨텍스트 조립 | `mcp/context_assembler.py` | `assemble_context`, `assemble_context_with_sources`, `_search_chunks`, `_rerank_and_search_graph`, `_search_graph_sourced_chunks` |
| 쿼리 확장(HyDE) | `processor/query_expander.py` | `expand_query_embedding`, `generate_hypothetical_document` |
| 리랭커 | `processor/reranker.py` | `rerank`, `_truncate` |
| 그래프 플래너 | `processor/graph_search_planner.py` | `plan_graph_search`, `execute_graph_search`, `_parse_plan` |
| 그래프 스토어 | `storage/graph_store.py` | `get_neighbors`, `_bidirectional_bfs`, `_resolve_seed_nodes`, `search_entities_by_embedding`, `format_query_relevant_schema_for_llm`, `build_entity_embeddings` |
| 벡터 스토어 | `storage/vector_store.py` | `search`, `count`, `add_chunks` (ChromaDB) |
| MCP 도구 | `mcp/tools.py` | `search_context`, `get_graph_context`, `get_document`, `list_documents` |
| 어휘 | `processor/graph_vocabulary.py` | `format_entity_types_for_prompt` 등 |

---

## 11. 검색-인덱싱 정렬 포인트 (요약, 상세는 00_overview.md)

- **멀티뷰 임베딩(body/meta/question)** ↔ 벡터 검색 over-fetch×6 + view 라벨 보존.
- **가상 질문 인덱싱(question_generator)** ↔ HyDE 가상 문서 + question view 매칭.
- **link/body/LLM 그래프 추출** ↔ 그래프 플래너가 동일 어휘(`graph_vocabulary`)·동일 방향성으로 탐색.
- **엔티티 정규화(D룰 병합, graph_merge_log)** ↔ 동명 엔티티 단일 노드 → 크로스-문서 관계 탐색이 자연스럽게 동작(`get_neighbors` 주석).
- **code_doc/code_summary ↔ git_code 원본**(`document_sources`) ↔ `include_source_code` 첨부.
