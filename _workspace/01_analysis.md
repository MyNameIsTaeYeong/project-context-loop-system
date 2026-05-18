# 01_analysis.md — 골드셋 생성·평가 병렬화 정밀 분석 (3차)

**작성**: 2026-05-18
**대상 코드**: 워크트리 `sweet-chaplygin-e3c81b`
**범위**: 사실만 기록. 설계·구현은 designer 가 결정.

---

## 1. 현재 직렬 처리 지점 정밀 매핑

### 1.1 생성 측 — chunk 모드 루프

위치: `scripts/build_synthetic_gold_set.py:391-469` — `build()` 내부 `for i, chunk in enumerate(sampled):` 단일 루프.

한 반복(`chunk` 1개) 안에서 발생하는 `await` 순서:

1. `await generate_questions(...)` (`scripts/build_synthetic_gold_set.py:398`)
   → 내부적으로 `await generator.complete(prompt, ..., purpose="goldset_generate")` 1회 (`src/context_loop/eval/synth.py:510`).
2. 생성된 질문 수 만큼 (보통 `questions-per-chunk=2`) 다시 직렬 루프 `for j, gq in enumerate(generated):` (`scripts/build_synthetic_gold_set.py:423`).
   각 질문당 — `apply_filter=True` 일 때 — `await filter_question(...)` 호출 (`build_synthetic_gold_set.py:439`).
3. `filter_question` 내부 (`src/context_loop/eval/synth.py:583-622`):
   - (a) `await is_answerable(question, source_chunk, ...)` — `judge.complete` 1회 (`synth.py:600`)
   - (b) 결정론적 `has_identifier_leakage` (`synth.py:607`)
   - (c) 결정론적 `has_demonstrative_reference` (`synth.py:613`)
   - (d) distractor 수(`n_distractors`, 기본 2)만큼 다시 직렬 루프 — 각 `await is_answerable(question, distractor, ...)` (`synth.py:618`)

요약: 한 chunk 당 최악의 경우 LLM 호출 수 = `1(생성) + questions_per_chunk × (1(answerable) + n_distractors(generic))` ≈ `1 + 2*(1+2) = 7`회. 모든 호출이 **단일 await 체인** 으로 직렬.

### 1.2 생성 측 — graph 모드 루프

위치: `scripts/build_synthetic_gold_set.py:587-654` — `_run_graph_mode()` 내부 `for i, sg in enumerate(sampled_sg):` 단일 루프.

한 반복(`sg` 1개) 안에서 발생하는 `await`:

1. `await generate_graph_questions(sg, ..., generator=...)` (`build_synthetic_gold_set.py:594`) — generator.complete 1회 (`synth.py:549`).
2. `for j, gq in enumerate(generated):` 직렬 루프 (`build_synthetic_gold_set.py:622`).
3. 각 질문당 `await filter_question(...)` (`build_synthetic_gold_set.py:631`) — chunk 모드와 동일한 (a)-(d) 게이트.

루프 종료 후 일괄로 (이미 비직렬):
- `await _embed_graph_item_descriptions(items, embedding_client)` (`build_synthetic_gold_set.py:658`) — 내부에서 `aembed_with_client` 가 모든 description 을 한 번에 배치 처리 (`src/context_loop/eval/graph_match.py:498-531`).

### 1.3 평가 측 항목 루프

위치: `scripts/eval_search.py:605-641` — `_evaluate_gold_set()` 내부 `for i, item in enumerate(gold.items):`.

한 반복(`item` 1개) 안의 `await`:

1. `await evaluate_one(item, ...)` (`eval_search.py:611`) — 내부에서:
   - `await assemble_context_with_sources(item.query, ...)` (`eval_search.py:185`) — 다단계 호출. 다음 1.3.1 참조.
   - `embed_fn = build_embed_fn(embedding_client, ...)` (`eval_search.py:208`) — **반복마다 동기 재생성** (캐시는 closure 안 dict 라 매 반복 초기화).
   - `entity_report = run_entity_matching(...)` (`eval_search.py:211`) — `run_entity_matching` 내부는 동기. 단, `match_entity_tiered` 의 T4 분기에서 `embed_fn(text)` 가 호출되면 그 안에서 `asyncio.run(coro)` 가 일어날 수 있음 (`graph_match.py:196`) — 단 평가 측은 이미 running loop 내부라 `_call` 의 분기 (`graph_match.py:185-194`) 에서 경고 후 `None` 반환. 즉 **평가 측 동기 embed_fn 은 T4 에서 사실상 작동 안 함**.
   - `score_relations` 분기 시 `run_relation_matching(...)` (`eval_search.py:271`) — 동기.
   - `judge` 분기 시 `await _fetch_source_text(item, meta_store)` (`eval_search.py:308`) + `await judge_answer(...)` (`eval_search.py:309`) — SQLite read 1회 + LLM 호출 1회.

#### 1.3.1 `assemble_context_with_sources` 내부 await 체인 (단일 항목당)

`src/context_loop/mcp/context_assembler.py:323-451`:

1. `query_embedding = await _embed_query(query, embedding_client)` (`context_assembler.py:374`) — `embedding_client.aembed_query` 1회. `hyde_enabled` 시 `await expand_query_embedding(query, llm_client, embedding_client)` (`context_assembler.py:372`) — LLM 1회 + 임베딩 1회.
2. `chunk_results = await _search_chunks(...)` (`context_assembler.py:377`) — `vector_store.count()` + `vector_store.search(...)` 두 동기 호출 (`context_assembler.py:193, 196`). ChromaDB. 비동기 메서드 아님.
3. `chunk_results, graph_result = await _rerank_and_search_graph(...)` (`context_assembler.py:383`) — 내부에서 `await asyncio.gather(_maybe_rerank(), _maybe_graph())` (`context_assembler.py:320`):
   - `_maybe_rerank` → `rerank(query, chunk_results, reranker_client, ...)` (`context_assembler.py:301`) — reranker LLM 호출.
   - `_maybe_graph` → `_search_graph_with_llm` (`context_assembler.py:314`) → `plan_graph_search` (LLM 1회) + `execute_graph_search` (SQLite/그래프 read). 추가로 `graph_store.entity_embedding_count == 0` 이면 `await graph_store.build_entity_embeddings(embedding_client)` (`context_assembler.py:264`) — 전체 노드명 임베딩 (대량 배치 1회).
4. 청크 결과 포맷 단계에서 `await meta_store.get_document(doc_id)` 반복 (`context_assembler.py:401`) — 단일 항목당 최대 `len(chunk_results)` 회 SQLite read.
5. 그래프 결과의 출처 추출에서도 `await meta_store.get_document(doc_id)` 반복 (`context_assembler.py:423`).

요약: 한 골드 항목 평가 = (query 임베딩 1회 + Chroma search 1회 + rerank 1회 + graph plan LLM 1회 + 결과 build 시 doc fetch 다수회) + (optional judge LLM 1회).

### 1.4 다중 골드셋 평가 루프

위치: `scripts/eval_search.py:743-769` — `for gold_path in gold_paths:` 최상위 루프. 골드셋 1개씩 직렬 호출:

```python
for gold_path in gold_paths:
    summary = await _evaluate_gold_set(gold_path, ..., meta_store=..., vector_store=..., ...)
```

stores/clients 는 호출자(`run`) 가 한 번 초기화해 모든 잡에 재사용 (`eval_search.py:671-678`). 골드셋 간 직렬은 R3 의 비목표("다중 골드셋 간 병렬화 — out of scope")로 명시되어 있음.

생성 측은 `scripts/build_synthetic_gold_set.py:958-989` 의 `_run_all()` 에서 `for i in range(1, args.n_gold_sets + 1):` 가 `await build(...)` 를 직렬 호출.

---

## 2. 공유 상태 / Race Condition 후보

### 2.1 `items: list[GoldItem]` (생성 측)

- 위치: `build_synthetic_gold_set.py:379` 에서 `[]` 로 생성.
- 쓰기 지점:
  - chunk 모드 `items.append(GoldItem(...))` `build_synthetic_gold_set.py:425, 455`
  - graph 모드 `items.append(_make_graph_gold_item(...))` `build_synthetic_gold_set.py:624, 647`
- 읽기 지점:
  - **id 부여** — `f"q{len(items) + 1:04d}"` `build_synthetic_gold_set.py:426, 456, 698`. `_make_graph_gold_item(sg, gq, items, ...)` 안에서 `len(existing_items) + 1` (`build_synthetic_gold_set.py:698`).
  - 모드 통합 후 `_embed_graph_item_descriptions(items, ...)` 가 `items` 를 enumerate 하며 graph entity/relation 의 description 임베딩을 채움 (`build_synthetic_gold_set.py:711-745`).
  - 최종 `gold = GoldSet(version=1, items=items, metadata=metadata)` (`build_synthetic_gold_set.py:515`).

**동시 변경 영향**:
- `list.append` 자체는 CPython GIL 보장으로 데이터 손실 없음. 그러나 **id 가 append 순서에 의존** 하므로 동시 append 시 id 가 LLM 응답 도착 순서로 부여돼 결정성 깨짐 (R2 위반).
- chunk + graph 모드를 한 items 리스트에 머지하므로 두 모드의 id 충돌도 가능.

### 2.2 `stats: dict[str, int]` (생성 측)

- 위치: `build_synthetic_gold_set.py:380-389` 에서 8개 키 dict 로 초기화.
- 쓰기 지점:
  - chunk 모드: `stats["generated"] += len(generated)` (`build_synthetic_gold_set.py:404`), `stats["fail_parse"] += 1` (`408`), `stats[key] = stats.get(key, 0) + 1` (`448`), `stats["passed"] += 1` (`436, 466`).
  - graph 모드: `stats["graph_generated"] += len(generated)` (`600`), `stats["generated"] += len(generated)` (`601`), `stats["fail_parse"] += 1` (`605`), `stats[key] = stats.get(key, 0) + 1` (`640`), `stats["passed"] += 1` / `stats["graph_passed"] += 1` (`627-628, 650-651`).

**동시 변경 영향**:
- `stats[k] += 1` 은 read-modify-write 비원자. async 코루틴 사이라도 단일 스레드긴 하지만, `await` 점에서 다른 코루틴이 끼어들면 race 가능. 다만 `stats[k] = stats[k] + 1` 의 그 한 줄은 await 없이 실행되므로 **CPython single-thread asyncio 환경에서는 race 없음** (다른 코루틴이 같은 줄 사이에 끼어들지 못함). 그러나 한 함수 안에서 `await` 직후 stats 를 갱신하는 패턴이 여러 군데라, 동시성 cap 패턴이 항목 단위로 stats 를 만들고 일괄 머지하는 쪽이 안전.

### 2.3 `len(items) + 1` 기반 id 부여 — 비결정성 핵심

위 2.1 항목에서 인용. 세 위치:
- `build_synthetic_gold_set.py:426` (chunk no-filter)
- `build_synthetic_gold_set.py:456` (chunk filter pass)
- `build_synthetic_gold_set.py:698` (`_make_graph_gold_item` — graph)

00_requirements R2 의 핵심 명시 — "사전 인덱스 기반으로" 부여하라.

### 2.4 distractor_pool — read-only 공유

- chunk 모드: `distractor_pool = [c for c in candidates if ...]` (`build_synthetic_gold_set.py:374-377`). 루프 안에서는 `c for c in distractor_pool if c["source_type"] == chunk["source_type"]` 슬라이싱만 (`411-419`).
- graph 모드: `distractor_pool` 동일 패턴 (`build_synthetic_gold_set.py:574-578`). 루프 안 `s for s in distractor_pool if s["source_type"] == sg["source_type"]` 슬라이싱 (`608-615`).

**동시 변경 영향**: 루프 전에 한 번 셔플 (`rng.shuffle`) 후 read-only. `rng.shuffle` 도 루프 전에만 호출 (`377, 578`). 동시 read 안전 — 공유 OK.

### 2.5 `seen_keys` / `sampled_keys` (graph 모드)

- `sampled_keys = {...}` (`build_synthetic_gold_set.py:571-573`) — 루프 전 1회 계산, read-only.
- distractor 풀 구성 직후 한 번만 만들고 이후 변경 없음.

### 2.6 LRU 캐시 — `build_embed_fn`

위치: `src/context_loop/eval/graph_match.py:146-216`.

- `build_embed_fn(embedding_client, ...)` 호출 시 **closure 안 dict 캐시** 를 생성 (`graph_match.py:201-216` — `cache: dict[...] = {}`, `order: list[...] = []`).
- `evaluate_one` 에서 **매 항목마다 새로 호출** (`eval_search.py:208`) — 따라서 캐시는 항목 간 공유되지 않음.
- 캐시는 항목 1개 안에서 같은 텍스트 임베딩 중복 호출만 막음.
- `functools.lru_cache(maxsize=4096)` 가 적용된 곳은 `_normalize(text)` (`graph_match.py:48`) — 순수 문자열 변환, 동시 호출 안전.

**동시 변경 영향**:
- `build_embed_fn` 안 closure dict 는 한 항목의 직렬 호출 안에서만 쓰임 — 외부 동시성과 무관.
- 단, **여러 항목이 동시에 평가될 경우** 같은 텍스트(예: 동일 retrieved entity 의 description)를 여러 task 가 각자 임베딩 호출 — 캐시 효과 사라짐. designer 가 캐시를 항목 외부로 끌어올릴지 결정 필요.
- 만약 평가 측에서 캐시를 항목 외부에 두려면 dict + lock 또는 항목 시작 전 사전 임베딩이 필요. 현재 캐시는 **동시 쓰기에 대해 lock 이 없음** (`graph_match.py:204-214`) — 외부화 시 race 가능.

### 2.7 `rows: list[dict[str, Any]]` (평가 측)

위치: `eval_search.py:604` 에서 `[]` 로 시작. `rows.append(row)` (`634`) 또는 에러 시 `rows.append({"id": ..., "error": ...})` (`637`) 직렬 append.

**동시 변경 영향**: append 자체는 GIL 안전. 순서는 LLM 응답 도착 순으로 뒤바뀜 → CSV/Summary 의 행 순서는 결정성을 위해 정렬 필요. 평가 결과는 항목 id 별 dict 이므로 id 로 정렬하면 복원 가능.

### 2.8 `graph_store._entity_embeddings` (평가 측 공유)

위치: `src/context_loop/storage/graph_store.py:86` — `dict[int, tuple[str, list[float]]]`. 동시 평가 시 여러 task 의 `assemble_context_with_sources` 가 거의 동시에 `entity_embedding_count == 0` 을 체크 → 동시에 `await graph_store.build_entity_embeddings(embedding_client)` 진입 가능 (`context_assembler.py:263`).

`build_entity_embeddings` 자체 (`graph_store.py:591-614`):
- `missing` 리스트를 만든 뒤 `await embedding_client.aembed_documents(names)` 1회 호출.
- 응답을 받고 `self._entity_embeddings[node_id] = (name, emb)` 로 채움.

**동시 변경 영향**: 두 task 가 동시에 `count == 0` 을 보고 동시에 build 진입하면 중복 임베딩 호출 + 같은 dict 에 동시 쓰기. CPython 에서 dict 쓰기는 GIL 보호되지만, 두 번 await 후 동일 키 덮어쓰기 — 데이터 손실은 없으나 비용이 두 배. **첫 항목 평가 전에 사전 빌드** 하면 회피 가능 (designer 결정).

---

## 3. async 인프라 가용성

### 3.1 LLMClient 모든 호출이 async 인가

`src/context_loop/processor/llm_client.py`:

- `LLMClient` 추상 베이스 `async def complete(...)` (`llm_client.py:26-37`).
- `AnthropicClient.complete` async (`llm_client.py:134-176`).
- `OpenAIClient.complete` async (`llm_client.py:191-231`).
- `EndpointLLMClient.complete` async (`llm_client.py:285-308`) — 내부적으로 `stream()` 을 async iteration.

eval 측 generator/judge 도 `build_eval_llm_client(...)` → `build_llm_client(...)` → `web.app._build_llm_client` 가 위 세 클라이언트 중 하나를 반환 (`src/context_loop/eval/llm.py:84-117`). 모두 async.

**결론**: 모든 LLM 호출은 await 가능 — 동시화에 인프라 장벽 없음.

### 3.2 MetadataStore — aiosqlite 단일 connection 동시 read

`src/context_loop/storage/metadata_store.py:142-194`:

- `self._db: aiosqlite.Connection | None = None` (`metadata_store.py:144`).
- `initialize()` 에서 **단 한 개** 의 connection 을 열고 (`metadata_store.py:149`) `self._db` 에 저장. `WAL` 모드 + `foreign_keys=ON`.
- 모든 read 메서드 (`list_documents`, `get_chunks_by_document`, `get_all_graph_nodes`, `get_node_document_ids`, `get_document` 등) 가 `self.db.execute(...)` → `await cursor.fetchall()` 패턴.

**aiosqlite 의 동시 동작**: aiosqlite 는 한 connection 당 **단일 백그라운드 스레드** 에서 queue 로 직렬 실행 (`aiosqlite.Connection` 내부 `_tx` queue). 같은 connection 으로 동시에 두 query 가 await 되면 — `execute` 는 즉시 queue 에 enqueue 되어 future 를 받지만, 백그라운드 스레드는 **하나씩** 처리. 따라서:
- 데이터 무결성 안전 (race 없음).
- 진정한 병렬 read 불가 — 백그라운드 스레드 1개가 직렬 처리. SQLite WAL 모드의 다중 reader 이점을 활용 못 함.
- N=16 동시성으로 항목을 돌려도 SQLite read 는 사실상 직렬화 — LLM 호출이 대부분 시간이므로 영향은 작음.

**커밋 영향**: 평가 측은 read-only 라 commit 없음. 생성 측 코드는 `metadata_store` 를 read 전용으로만 호출 (`load_candidate_chunks`, `load_candidate_subgraphs`) — 동시 read 가능하나 직렬화됨. 안전하지만 throughput 제약.

### 3.3 VectorStore (ChromaDB) 동시 호출 안전성

`src/context_loop/storage/vector_store.py:17-118`:

- 메서드들이 **모두 동기**: `initialize()`, `count()`, `search(query_embedding, n_results, ...)`, `add_chunks(...)`, `delete_by_document(...)`.
- ChromaDB `PersistentClient` 는 한 프로세스 안에서 single instance 공유 권장. 동기 메서드 직접 호출.
- 평가 측 `_search_chunks` 가 `vector_store.search(...)` 를 await 없이 호출 (`context_assembler.py:196`) — 즉 같은 task 안에서 동기 블로킹.

**동시 호출 영향**:
- 평가 항목이 동시에 N개 돌면 각 task 가 `vector_store.search(...)` 를 동기로 호출 — 이벤트 루프 스레드를 블로킹. 하지만 await 점 (`_search_chunks` 가 자체로는 `await` 가 없음 — 다만 외부에서 `await _search_chunks` 가 호출됨) 이전에 즉시 반환. 즉 search 한 번이 빠르면 (보통 ms 단위) 큰 문제 없으나, 큰 컬렉션에서는 이벤트 루프 블로킹 누적.
- ChromaDB 자체는 thread-safe 라고 문서화돼 있으나, embedded DuckDB/SQLite 백엔드의 동시성은 보장 약함. 다만 평가는 read-only.

**결론**: read-only 동시 호출은 안전하지만 동기 호출이 루프를 블로킹하므로, 동시성 cap N 이 커도 throughput 은 LLM 호출 대기로 인해 SearchTime / N 으로 amortize.

### 3.4 EmbeddingClient 동시 호출 안전성

`src/context_loop/processor/embedder.py`:

- `EndpointEmbeddingClient.aembed_query / aembed_documents` (`embedder.py:81-119`):
  - **매 호출마다 새로운 `httpx.AsyncClient` 를 `async with` 로 생성** (`embedder.py:96`). 즉 connection pool 공유 안 됨 — 호출마다 connection 새로.
  - 그래서 동시 호출은 안전하나 connection 비용이 매번 발생.
  - 내부 상태(rate limit token 등) 없음.
- `LocalEmbeddingClient.aembed_documents`: `loop.run_in_executor(None, self.embed_documents, ...)` (`embedder.py:171`) — default executor 큐. 동시 호출은 안전하지만 sentence-transformers 모델 자체가 thread-safe 여부에 따라 직렬화될 수 있음.

**평가 시 query 임베딩** — 항목 1개당 `aembed_query` 1회 = `aembed_documents([text])` 호출 = httpx connection 1회 신규 생성. N=16 으로 동시 돌리면 endpoint 에 동시 connection 16 — endpoint 가 받쳐주면 N배 throughput.

### 3.5 `assemble_context_with_sources` 내부 추가 동시 호출

이미 `_rerank_and_search_graph` 가 `asyncio.gather` 로 reranker + graph planner 를 병렬 (`context_assembler.py:320`). 이는 항목 1개 안의 내부 병렬 — 외부 항목 단위 병렬화와 별도.

`assemble_context_with_sources` 가 한 번 호출되면 LLM 호출 수:
- HyDE 활성: 1회 (HyDE)
- Graph plan: 1회
- Rerank: 1회 (optional)
- 즉 항목 1개 ≈ 1~3 LLM 호출 + 임베딩 1회 + 다수 SQLite read.

---

## 4. 동시성 cap 의 효과 예측

### 4.1 비용 지배 분석

**한 chunk 모드 항목 (questions_per_chunk=2, n_distractors=2):**
- 생성: 1 LLM call (Generator).
- 게이트: per-question (a) 1 + (d) 2 = 3 calls/q × 2 = 6 calls (Judge).
- 합계: 1 + 6 = **7 LLM calls / chunk** (apply_filter=True).

**한 graph 모드 항목 (동일 파라미터):**
- 동일하게 7 calls + 임베딩은 루프 종료 후 배치 1회 (이미 비직렬).

**한 평가 항목 (judge OFF, rerank ON, graph ON, hyde OFF):**
- query embed: 1 call.
- ChromaDB search: 동기, ms 단위.
- rerank: 1 reranker call.
- graph plan: 1 LLM call.
- SQLite reads: 다수, ms 단위.
- judge ON 추가: 1 LLM call.

LLM 호출이 전체 시간의 ~90% 이상 (300 항목 골드셋 직렬 ~15분 추정과 일치).

### 4.2 N 별 예측

| N | 생성 측 chunk 모드 (300 chunks × 7 calls) | 평가 측 (300 items × 2~3 calls) | 비고 |
|---|---|---|---|
| 1 | 직렬 sum (~15분) | 직렬 sum (~5~10분) | 현 상태 |
| 4 | LLM endpoint 가 4 동시 받쳐주면 ~4분 | ~1.5~2.5분 | 일반적 sweet spot |
| 8 | endpoint TPS 가 받쳐주면 ~2분 | ~0.75~1.25분 | rate limit 위험 시작 |
| 16 | TPS 천장에 닿으면 평탄화 | 동일 | endpoint 의존 |

지배 비용: LLM > 임베딩 > SQLite > ChromaDB. SQLite/ChromaDB 의 직렬화는 LLM 대기 시간에 가려져 무시 가능.

### 4.3 평탄화 지점

- Generator/Judge 가 같은 endpoint 면 동시 호출 cap 이 공유됨 — endpoint 의 N_max 가 진짜 천장.
- 별도 endpoint 면 각각 독립적으로 N. 단 prompt cache(있다면) 효과는 응답 도착 순서가 흔들리면 약화.

---

## 5. 결정성을 유지하기 위해 필요한 코드 변경 지점

### 5.1 id 부여 위치들

3 위치 — 모두 `f"q{len(items) + 1:04d}"`:
1. `build_synthetic_gold_set.py:426` — chunk no-filter
2. `build_synthetic_gold_set.py:456` — chunk filter pass
3. `build_synthetic_gold_set.py:698` — `_make_graph_gold_item`

**필요 변경**: sampling 직후 인덱스를 fix 하고, 항목별 함수에 `idx` 를 인자로 전달하여 `f"q{idx:04d}"` 식으로 부여. 단, chunk + graph 모드가 한 items 리스트를 공유하므로 idx 명명규칙을 두 모드에 걸쳐 충돌 없이 부여해야 함 (예: chunk 는 q0001~qNNNN, graph 는 qNNNN+1~ 이어 붙이거나 prefix 분리).

### 5.2 stats 머지 패턴

stats 는 항목 단위로 누적하는데, 한 함수 안에서 + 순회 안의 += 만 — 다른 코루틴이 끼어드는 위치가 await 직후뿐.

**가장 단순한 패턴**: 각 항목 task 가 **로컬 stats dict** 를 만들어 반환 → gather 후 메인에서 합산. 합산 함수 한 줄 (`merge_stats(local_dicts) -> dict`).

키 종류:
- chunk: `generated, passed, fail_not_answerable, fail_leakage, fail_demonstrative, fail_generic, fail_parse`
- graph: 위 + `graph_generated, graph_passed`

### 5.3 로그 진행률 위치

직렬 로그:
- chunk: `logger.info("[%d/%d] 질문 생성 — ...", i+1, len(sampled), ...)` (`build_synthetic_gold_set.py:392`).
- chunk inner: `logger.info("  q%d 통과 / 탈락 — query=%s", ...)` (`450, 467`).
- graph: 동일 패턴 (`588, 642, 652`).
- 평가: `logger.info("[%s | %d/%d] q=%s | gold_doc=%s", ...)` (`eval_search.py:606`).

병렬화 시 도착 순서가 시작 순서와 다름 — 00_requirements 의 비기능 요구사항에서 **"완료 카운터 또는 그대로 둠 — 사용자 결정"** 으로 designer 에게 위임.

### 5.4 sampling 과 distractor 풀 구성 시점

- chunk sampling: `build_synthetic_gold_set.py:365` — 루프 전 1회 (`stratified_sample`).
- chunk distractor 풀: `build_synthetic_gold_set.py:374-377` — 루프 전 1회 (`rng.shuffle(distractor_pool)`).
- graph subgraph 샘플링: `build_synthetic_gold_set.py:562` — 루프 전 1회.
- graph distractor 풀: `build_synthetic_gold_set.py:574-578` — 루프 전 1회.

**모두 루프 전 단발성** — 동시화와 무관, 변경 불요.

`rng = random.Random(seed)` (`build_synthetic_gold_set.py:345`) 는 메인 함수 진입 직후 한 번만 사용 (sampling + shuffle) → 항목 task 안에서는 rng 접근 없음. **rng 공유 race 없음**.

---

## 6. SQLite·VectorStore 동시 접근 안전성

### 6.1 aiosqlite 동시 read 거동 (실제 코드 인용)

`metadata_store.py:142-194` — `MetadataStore` 클래스는 **단일 `aiosqlite.Connection`** 을 `self._db` 에 보관. 모든 read 메서드 (`list_documents:248`, `get_chunks_by_document:351`, `get_all_graph_nodes:398`, `get_node_document_ids:443`, `get_document:228`) 가 같은 connection 으로 `await self.db.execute(...)` → `await cursor.fetchall()`.

aiosqlite 의 single-connection 동작:
- 내부적으로 connection 당 single worker thread 가 sqlite 호출을 직렬 처리.
- 여러 task 가 `await execute(...)` 를 동시 호출하면 future 들이 queue 에 쌓이고 worker 가 순차 처리.
- **데이터 무결성은 안전** (cursor 끼리 섞이지 않음) — fetchall 도 future 단위로 처리.
- 진정한 동시성 없음 — read throughput 은 single-thread sqlite read 속도가 천장.

**위험성**: 없음. 단, throughput 이점도 없음 — N=16 동시성에서 SQLite read 는 직렬화. 다만 평가 측은 LLM 대기가 SQLite read 시간을 압도하므로 영향 작음.

**대응 권고 (designer 결정)**:
- (a) 그대로 둠 — LLM 비용이 지배적이라 충분.
- (b) 평가 측에서 `get_chunks_by_document` 가 `_fetch_source_text` 와 `_format_chunk_results` 양쪽에서 호출됨 (같은 doc_id 반복). 사전 일괄 적재 캐시면 SQLite hit 수 N→1 감소.
- (c) connection pool — aiosqlite 가 native 지원 없음. 직접 풀 구현은 코드 비대화. 권장 안 함.

### 6.2 VectorStore (ChromaDB) 동시 호출

`vector_store.py:79-118` — `search` 는 동기. 평가 측에서 `await _search_chunks` 안에서 `vector_store.search(query_embedding, n_results=...)` 를 동기 호출.

- 동기 함수가 이벤트 루프 안에서 호출되면 그 동안 다른 task 진행 불가 (block).
- ChromaDB read 자체는 thread-safe (문서 보장). 단, 동시 호출 시 각 task 가 이벤트 루프를 ms 단위로 잡고 놓음 — N 이 큼에 비례해 루프 책임률 만큼 sequential 화.
- 단일 query 가 ms~수십ms 면 LLM 호출 비용에 비해 무시 가능.

**대응 권고**: 그대로 둠. 큰 컬렉션이거나 vector search 시간이 LLM 호출 시간과 비슷해지면 designer 가 `asyncio.to_thread(vector_store.search, ...)` 로 감싸는 옵션 검토.

---

## 7. LLMClient 와 EmbeddingClient 의 동시 호출 안전성

### 7.1 LLMClient 내부 상태

- `EndpointLLMClient.__init__` 에서 `AsyncOpenAI(...)` 인스턴스 1개 생성 (`llm_client.py:281`).
- `AsyncOpenAI` 는 내부적으로 `httpx.AsyncClient` 를 보유 — connection pool 공유. 동시 호출 안전 (httpx 의 design).
- `_reasoning_profiles` 는 dict, read-only.
- 다른 내부 상태(rate limit token bucket 등) 없음.

**결론**: `EndpointLLMClient` 인스턴스 1개를 N개 동시 호출해도 안전. 단 endpoint 자체의 rate limit 은 외부 제약.

`AnthropicClient`, `OpenAIClient` 동일 — `AsyncAnthropic` / `AsyncOpenAI` SDK 가 동시 호출 안전.

### 7.2 EmbeddingClient

- `EndpointEmbeddingClient.aembed_documents` (`embedder.py:81-114`):
  - **httpx.AsyncClient 를 호출마다 새로** (`async with httpx.AsyncClient(timeout=...) as client:` `embedder.py:96`). 즉 connection pool 미공유 — 호출 비용 약간 증가.
  - 내부 상태 없음.
- **결론**: 동시 호출 안전.

### 7.3 에러·재시도 로직

현 코드는 LLM/임베딩 클라이언트 모두 **재시도 로직 없음** — 한 번 호출 → 실패 시 raise. 호출자가 try/except 로 처리. 동시 호출 시 race 가능성 없음 (각자 독립 호출).

---

## 8. exception 격리 시 영향

### 8.1 한 항목 실패 시 정보 손실

**생성 측 chunk 모드** 직렬 코드에서 항목 1개의 실패:
- `generate_questions` 가 raise 하면 try/except 없이 위로 — 전체 build 가 죽음. 다만 `generate_questions` 내부 `parse_generated_questions` 가 빈 응답을 silent 처리 → `stats["fail_parse"] += 1` 만 발생.
- `filter_question` 내부 `is_answerable` 의 LLM call 이 raise 하면 위로 전파 → 전체 build 사망.

→ **현재 코드는 LLM raise 에 대한 명시적 격리 없음**. 운영에서는 httpx 의 ConnectionError, timeout 등이 발생하면 전체가 죽는다.

**평가 측**: 이미 격리 있음 — `eval_search.py:610-641`:
```python
try:
    row = await evaluate_one(item, ...)
    rows.append(row)
except Exception as exc:
    logger.exception("질의 %s 실패: %s", item.id, exc)
    rows.append({"id": item.id, "query": item.query, "error": str(exc)})
```
실패해도 다음 항목 진행. 병렬화 후에도 같은 패턴 유지 가능.

### 8.2 gather(return_exceptions=True) 사용 패턴

`asyncio.gather(*tasks, return_exceptions=True)` 결과는 `list[GoldItem | Exception]` 또는 `list[dict | Exception]`. 분리 패턴 예시:

```python
results = await asyncio.gather(*tasks, return_exceptions=True)
for idx, r in enumerate(results):
    if isinstance(r, Exception):
        logger.exception("idx=%d 실패: %s", idx, r)
        local_stats["fail_runtime"] = local_stats.get("fail_runtime", 0) + 1
    else:
        ...
```

이 패턴이 이미 `coordinator.py:193` 에 사용 — designer 가 그대로 차용 가능.

---

## 9. 가용 동시성 cap 추정 (외부 endpoint)

### 9.1 config 에 rate limit 설정이 있는가

- `config/default.yaml` 전체에서 `rate_limit`, `max_concurrent`, `max_concurrency` 키워드는 찾은 곳:
  - `config/default.yaml:92` — `max_concurrent_workers: 10` (이건 다른 영역, 아마도 worker agent).
- `eval.generator`, `eval.judge` 섹션에 rate limit 키 **없음**.
- `llm.*` 섹션에도 rate limit 키 **없음**.

→ designer 는 새 CLI/config 옵션 `--concurrency` (또는 `--parallel`) 로 사용자 명시 받아야 함 (R1 의 요구).

### 9.2 기존 코드의 동시성 hint

같은 코드베이스에서 이미 Semaphore 패턴 채택:
- `src/context_loop/sync/mcp_sync.py:241-270` — `concurrency` 인자, `asyncio.Semaphore(effective_concurrency)`, `asyncio.gather(*(_process_one(d) for d in to_process))`. **이 패턴이 가장 가까운 선례**.
- `src/context_loop/processor/llm_body_extractor.py:176-189` — `cfg.max_concurrency`, `asyncio.Semaphore(...)`, `asyncio.gather(...)`. LLM 호출 묶음에 대한 패턴.
- `src/context_loop/ingestion/coordinator.py:182-193` — `_MAX_FILE_CONCURRENCY` 상수, `Semaphore`, `gather(*, return_exceptions=True)`.

이 세 위치 중 가장 닮은 것은 `mcp_sync.py:241-270` — designer 가 동일 형태로 차용 가능.

`chat.py` 의 병렬 호출은 grep 결과 (`asyncio.gather` 위치 목록) 에 없음 — chat 측은 단발성 한 사용자 질의 처리라 병렬화 무관.

---

## 10. 영향 파일·테스트 매트릭스

| 변경 영역 | 영향 파일 | 영향 테스트 |
|---------|---------|----------|
| 생성 측 chunk 모드 병렬화 | `scripts/build_synthetic_gold_set.py` (`build()` 391-469, `main()` CLI 추가) | `tests/test_eval/test_build_synthetic_gold_set.py` |
| 생성 측 graph 모드 병렬화 | `scripts/build_synthetic_gold_set.py` (`_run_graph_mode()` 527-654) | `tests/test_eval/test_build_synthetic_gold_set.py` |
| 평가 측 항목 병렬화 | `scripts/eval_search.py` (`_evaluate_gold_set()` 569-655, `main()` CLI 추가) | (eval_search 전용 테스트 부재 — 신규 작성 검토) |
| id 사전 부여 | `scripts/build_synthetic_gold_set.py` (3 위치: 426, 456, 698) — `_make_graph_gold_item` 시그니처 변경 가능 | `tests/test_eval/test_build_synthetic_gold_set.py` (id 결정성 회귀 테스트 추가) |
| stats 머지 패턴 | `scripts/build_synthetic_gold_set.py:380-389, 404, 408, 436, 448, 466, 600-601, 605, 627-628, 640, 650-651` | 신규 stats merge 단위 테스트 |
| exception 격리 (생성 측) | `scripts/build_synthetic_gold_set.py` (현 try/except 없음 — 추가 필요) | 항목 1개 실패 시 다른 항목 영향 없음 테스트 |
| concurrency CLI 옵션 | `scripts/build_synthetic_gold_set.py:786-893`, `scripts/eval_search.py:865-959` | CLI smoke 테스트 (옵션 인식) |
| 진행률 로그 정책 | 위 두 스크립트의 `logger.info("[i/n] ...")` 라인 | 로그 회귀 테스트 일반적으로 없음 |
| graph entity embedding 사전 빌드 (옵션) | `scripts/eval_search.py:678` 부근에 사전 빌드 한 줄 추가 가능 | 신규 — 동시 호출 시 중복 임베딩 없음 검증 |

**테스트 인프라 측**: `tests/test_eval/` 에는 현재 `concurrency`, `parallel`, `Semaphore` 관련 테스트 없음 (grep 결과 빈 출력). 신규 작성 영역.

**Mock**: 기존 `test_build_synthetic_gold_set.py` / `test_synth.py` 가 LLMClient 를 mock 으로 테스트하는 패턴이 이미 있을 것 — 그대로 차용. 결정성 테스트는 같은 seed 로 두 번 돌려 items 가 동일한지 비교.

---

## 11. designer 에게 넘길 미해결 질문

00_requirements.md 의 10개 + 추가 발견:

### 00_requirements 의 10개 (재게시)

1. **동시성 제어 메커니즘** — `asyncio.Semaphore(N)` vs `asyncio.gather` 단순 + chunk batch vs `aiometer` (외부 의존). 기존 코드는 모두 (1) Semaphore + gather 패턴 → 차용 권장. designer 확정.
2. **CLI 옵션 이름·기본값** — 생성·평가 양쪽 일관성 (`--concurrency N` 기본 1 권고).
3. **id 사전 부여 구현** — sampling 직후 인덱스 → id 매핑 dict, 또는 항목별 함수가 idx 를 인자로 받기. chunk + graph 가 한 items 리스트를 공유하므로 prefix 분리/연속 부여 둘 중 선택.
4. **exception 격리 전략** — `asyncio.gather(return_exceptions=True)` + 결과 분리 vs Semaphore 안에서 try/except 후 None 반환. 평가 측은 후자가 더 단순 (현 try/except 패턴 유지).
5. **로그 정책** — 시작/완료/둘 다 중 선택.
6. **SQLite read cache** — 평가 측 `_fetch_source_text` + `_format_chunk_results` 가 `get_chunks_by_document(same_doc)` 중복 호출. 사전 일괄 적재 가치 평가 필요. 측정 데이터: doc 수와 query 수에 따라.
7. **graph distractor 풀 명시** — read-only 공유 OK (§2.4 확인). designer 가 명시.
8. **stats 카운터** — 로컬 누적 + 일괄 머지 권고 (§5.2).
9. **다중 골드셋 평가 측 store 재사용** — 한 stores 인스턴스 + 골드셋 간 직렬 (R3 비목표) 그대로. 단 각 골드셋 안의 동시성이 store/connection 을 share 함은 §6.1 의 한계.
10. **Judge LLM 의 동시 호출 안전성** — Generator/Judge 같은 endpoint 인지에 따라 cap share 여부. 별도 cap 두면 더 안전 — designer 가 한 cap (`--concurrency`) vs 두 cap (`--generator-concurrency` / `--judge-concurrency`) 결정.

### 추가 발견 질문

11. **chunk 모드 안의 "questions × distractors" 내부 직렬 루프** — 한 chunk 안의 `for j, gq in enumerate(generated)` (`build_synthetic_gold_set.py:423`) 와 그 안의 distractor loop 도 직렬. 항목 단위 병렬화 위에 또 한 단계 병렬(인내 안 chunk 단위 + 안의 질문 단위 동시) 까지 들어갈지, 청크 외곽만 병렬화할지. **추천**: 청크 외곽 단계만 — 내부 중첩 동시성은 cap 관리·디버깅이 어렵고, 청크 단위가 이미 적정 단위.
12. **`build_embed_fn` 캐시 외부화 여부** — 항목 동시 평가 시 캐시 효과 사라짐 (§2.6). 외부화하려면 lock 또는 동시 접근 안전 dict 필요. 평가 측에서 retrieved entity 의 description 임베딩이 자주 반복되지 않으면 그대로 둬도 OK.
13. **graph_store.build_entity_embeddings 동시 진입** — N≥2 평가에서 첫 항목들이 동시에 빈 캐시를 보고 동시 build 진입 가능 (§2.8). 평가 시작 직전에 `await graph_store.build_entity_embeddings(embedding_client)` 를 1회 호출하는 명시적 사전 단계 추가가 단순 해법.
14. **metadata 에 `concurrency` 기록** — R3 의 `concurrency` 메타. 생성 측 metadata dict 에 `concurrency` 키 추가 위치는 `build_synthetic_gold_set.py:497-513` 의 `metadata` 빌더.
15. **n_gold_sets > 1 + 항목 단위 병렬화 결합** — 골드셋 5개 × 항목 100개 × concurrency 8 → 동시 호출 8 (골드셋 간 직렬). 두 단계 동시성 결합은 없음. designer 가 명시.
16. **결정성 회귀 테스트 인프라** — mock LLM 이 의도적으로 응답 순서를 비결정적으로 (랜덤 sleep) 만들어도 같은 seed → 같은 id 가 부여되는지 검증. 현 mock 패턴이 그걸 지원하는지 designer 가 확인.
17. **graph 모드의 `_embed_graph_item_descriptions` 호출 시점** — 현재 `_run_graph_mode` 종료 후 1회 (`build_synthetic_gold_set.py:658`). 그대로 두면 됨 (이미 비직렬, items 전체 입력). 단 병렬 graph 모드 후에도 모든 graph 항목이 items 에 모인 다음 호출이라는 invariant 유지.
18. **로그의 `[%d/%d]` index — 사전 idx vs 완료 카운터** — `[start 5/100]` 의 `5` 는 사전 idx (samping 순서) 인가 완료 카운터인가. designer 결정.
19. **graph 모드 + chunk 모드 동시화 결합** — 현재 `build()` 가 chunk 모드 끝난 뒤 graph 모드 호출 (`build_synthetic_gold_set.py:472`). 두 모드를 한꺼번에 묶어 outer 항목 리스트(chunk + subgraph)를 만들고 그 위에서 동시성 cap 1개로 돌릴 수도 있음. 단 단순성을 위해 두 단계 분리 유지가 권고.

---

## 요약

영향 파일 **8개** (스크립트 2, 신규 테스트 추가 영역), 미해결 질문 **19개** (R1~R4 의 10개 + 추가 9개), 가장 큰 제약은 **LLM endpoint 의 외부 rate limit — designer 는 사용자가 `--concurrency` 로 명시하는 정책을 권장하며 코드 측 자동 추정은 비목표**.
