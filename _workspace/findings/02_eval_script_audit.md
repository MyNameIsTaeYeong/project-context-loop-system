# Eval-Script Auditor — 평가 스크립트 신뢰성 감사

## 한줄 판정

**HIGH RISK — 메트릭 식 자체는 대체로 정확하나, ① Judge가 시스템 LLM과 같은 클라이언트로 자동 폴백되는 자기-평가 편향 경로, ② Judge 프롬프트가 source chunk + retrieved context를 동시에 노출해 lexical overlap 채점이 되는 메타 편향, ③ `sources` dedup 으로 인한 retrieved_doc_ids 길이 < top_k 위험과 동률(similarity tie) 시 vector store 도착 순서에 의존하는 비결정성, ④ "mean Δ > std" 유의성 기준이 코드로 강제되지 않고 docstring 뿐, ⑤ baseline/treatment 라벨이 같은 골드셋·시스템 설정에서 실행됐는지 자동 검증하는 장치 부재가 결합되어, 절대 수치는 신뢰할 수 있어도 비교 결론은 흔들릴 수 있다.**

## 검토 범위

| 파일 | 범위 | 줄수 |
|---|---|---|
| `_workspace/source/metrics.py` | Recall/Precision/MRR/nDCG/Hit, aggregate, aggregate_with_variance | 172 |
| `_workspace/source/eval_search.py` | 메인 평가 루프, judge 호출, CSV/Summary, multi-goldset aggregate | 1042 |
| `_workspace/source/llm.py` | role(generator/judge) 별 LLM 클라이언트 빌더, 분리 강제 로직 | 226 |
| `_workspace/source/graph_match.py` | 4-tier cascade entity/relation 매칭, embed_fn 캐시 | 548 |
| `git show origin/main:src/context_loop/mcp/context_assembler.py` | 시스템 RAG (`assemble_context_with_sources`) — top-k/dedup/sort 로직 | 참조 |

## 핵심 발견 (위험 등급순)

| # | 발견 | 위험 등급 | 위치 |
|---|---|---|---|
| F-1 | `--judge` 활성 시 별도 endpoint/model이 없으면 시스템 LLM을 그대로 Judge로 재사용. 경고 로그만 찍고 진행 — **자기-평가 편향**. CI에서 차단되지 않음. | **Critical** | `eval_search.py:758-763` |
| F-2 | Judge 프롬프트가 source chunk + retrieved context를 함께 노출. 모델이 의미 평가보다 lexical overlap 평가로 회귀 — **Judge 메타 편향**. 또한 단일 호출 결과만 신뢰, variance/calibration 측정 없음. | **Critical** | `eval_search.py:88-109`, `evaluate_one()` 단일 호출만 |
| F-3 | `assemble_context_with_sources` 가 `sources` 를 `doc_id` 기반으로 dedup 한 뒤 `similarity` desc 정렬. 같은 doc의 여러 청크가 1개 source로 합쳐져 **`retrieved_doc_ids` 길이가 top_k 보다 작아질 수 있음** → recall/precision 분모 왜곡. 동률(same similarity)은 stable sort라 vector store 도착 순서에 의존 → **비결정적 tie-breaker**. | **High** | `context_assembler.py` (Source dedup + sort), `eval_search.py:209` |
| F-4 | "mean Δ > std면 유의미한 개선"은 docstring 권고일 뿐, 코드에서 강제·표시·경고하지 않음. paired t-test/bootstrap CI/Wilcoxon 없음. N=2-5인 경우 std 자체가 매우 불안정. | **High** | `eval_search.py:23`, `metrics.py:126-172`, `_write_aggregate()` |
| F-5 | baseline vs treatment 비교 자동화·검증 부재. 두 라벨이 **같은 골드셋, 같은 시스템 config (embedding_model, llm_model, similarity_threshold, rerank, hyde, top_k 등) 로 실행됐는지** 자동 체크하는 장치 없음. config_summary 가 저장될 뿐 비교 시 cross-check가 안됨. | **High** | `eval_search.py:784-801` (config_summary 저장만), `_write_aggregate()` 무점검 |
| F-6 | 골드셋 fingerprint(파일 hash/항목 수/seed)가 summary JSON 에 기록되지 않음. config_summary 에 `gold_set` 경로 문자열만 추가. 골드셋 변경 후 같은 라벨로 덮어쓰면 추적 불가. | **Medium** | `eval_search.py:707-712`, `write_summary` |
| F-7 | 실패한 질의는 row에 `error` 만 채우고 메트릭 키 자체가 없음. `aggregate` 가 해당 키 없는 row를 자동으로 무시 → **실패 질의가 silently 평균에서 빠짐** (자동 0점도 아니고 명시적 제외도 아님). 통계가 낙관적으로 부풀려질 수 있음. | **High** | `eval_search.py:665-678`, `metrics.py:107-123` |
| F-8 | `evaluate_one` / judge 호출에 timeout/retry 없음. `assemble_context_with_sources` 또는 judge LLM 이 응답을 영원히 안 주면 한 항목이 전체 평가를 멈춤. async gather 라서 한 항목 실패가 cancel 전파는 안되지만 hang은 가능. | **Medium** | `eval_search.py:632-678` |
| F-9 | `build_embed_fn` 의 LRU 캐시가 `dict` + `list` 로 직접 구현됨 (`graph_match.py:201-216`). 동시성 환경에서 `order.pop(0)` / `cache.pop` 가 **non-thread-safe**. asyncio 단일 스레드 가정이라 운영상 안전하지만 `--concurrency>1` 시 async 컨텍스트 스위치 사이의 동시 호출에서 cache state 가 꼬일 가능성. 또한 `lru_cache(maxsize=4096)` (graph_match.py:48 `_normalize`) 는 함수 자체 캐시라 평가 간 격리 안됨. | **Medium** | `graph_match.py:48-54, 201-216` |
| F-10 | `build_embed_fn` 의 비동기 경로 (`graph_match.py:182-199`): 평가가 이미 `asyncio.run()` 안에서 실행 중인데 `asyncio.get_running_loop()` 가 성공하면 그냥 None 반환 → **T4 embedding 단계가 조용히 skip**. 운영 평가가 정확히 이 경로를 탈 위험 — graph_recall 이 부풀려지지 않고 깎이는 방향이지만 그 사실이 메트릭에 표시되지 않음. | **High** | `graph_match.py:182-199` |
| F-11 | `recall@k` 분모가 `len(rel_set)`. 골드셋 한 항목이 relevant_doc_ids 를 N개 갖고 검색이 top_k=5 인데 N=10이면 recall@5의 상한이 0.5. 이건 표준 정의대로지만 mode="chunk" 항목에서 흔히 발생 — 사용자가 결과 해석 시 함정. **표준 정의 맞음, 다만 출력에 max_possible_recall 표시 없음**. | **Low** | `metrics.py:23-32` |
| F-12 | nDCG 가 binary relevance만 다룸 — 명시적이라 문제 없음. `idcg=0.0` 가드 있음. ✅ | OK | `metrics.py:62-84` |
| F-13 | `graph_recall@k` 등이 top_k 같은 chunk 메트릭의 k 값을 그대로 씀. 그래프 entity 가 항상 chunk 와 같은 cardinality 라는 보장 없음. graph 검색에서 entity가 50개 반환되면 k=5는 매우 빡빡한 컷오프. **k 의 도메인 분리 권고**. | **Medium** | `eval_search.py:240-263` |
| F-14 | `relation_matching` 의 T4 임베딩 단계가 (source, target) lower 정확 일치를 **강제**. T1 도 같은 키 정확 비교. → 관계의 entity 자체가 alias/normalize/embedding 으로 살짝 변형되면 T4도 0. **관계 매칭은 사실상 T1+description 임베딩만 다루고 entity 변형은 흡수 못함**. 의도된 설계인지 불명확. | **Medium** | `graph_match.py:399-433` |
| F-15 | `aggregate_with_variance` 가 `n=1` 일 때 std=0.0 반환. mean Δ 와 비교하는 사람 입장에서 std=0 은 "변동성 없음"으로 오해 가능. 최소한 std=NaN/None 권장. | **Low** | `metrics.py:159-164` |
| F-16 | `mrr` 의 정의: 첫 정답의 1/rank. 정답이 retrieved 전체(top_k 가 아님!) 안에 없으면 0. ★ `evaluate_one` 에서 `mrr(retrieved_doc_ids, relevant)` — retrieved_doc_ids 는 dedup 된 source 리스트 전체. 한편 `recall@k` 등은 `[:k]` slice. **`mrr` 만 top_k slice 안 함** → 사실상 `mrr@max_chunks` 평가. 라벨이 `mrr` 인 게 살짝 오해 소지. | **Low** | `eval_search.py:237`, `metrics.py:50-59` |
| F-17 | `Source` 가 `similarity=0.0` 디폴트 — 그래프 탐색 결과로 들어온 source 들은 모두 0 으로 들어와 `sources.sort(key=lambda s: s.similarity, reverse=True)` 시 **벡터 검색 결과 뒤에 일괄 배치 + 자기들 사이는 stable=원래 등장 순서**. 도착 순서 의존. | **Medium** | `context_assembler.py` Source dataclass |
| F-18 | judge 채점 결과 `(-1, "parse_error")` 가 row 에 그대로 들어가서 `aggregate` 에서 평균에 -1 이 섞임. 평균 `judge_score` 가 음수일 수 있음 → 보고 왜곡. | **High** | `eval_search.py:136-146`, `metrics.py:107-123` |

## 차원별 상세 점검

### 1. 메트릭 구현 정확성

#### Recall@k (`metrics.py:23-32`)

```python
def recall_at_k(retrieved, relevant, k):
    rel_set = set(relevant)
    if not rel_set: return 0.0
    top_k = set(retrieved[:k])
    return len(top_k & rel_set) / len(rel_set)
```

표준 정의 `|R∩T_k|/|R|` 정확. 분모는 정답 총 개수 (top-k 개수 아님). ✅
- 가드: `rel_set` 비면 0.0 — 정의 불가 → 0 처리. 일관됨.
- 미관찰 위험: 호출처 (`eval_search.py:233`)가 `retrieved_doc_ids` 를 넘기는데 이 리스트가 `Source.document_id` 의 dedup된 리스트라 **한 doc에서 여러 청크를 다 맞춰도 1개로 카운트**. recall이 평소보다 짜진다. — F-3 참조.

#### Precision@k (`metrics.py:35-47`)

```python
hits = sum(1 for r in top_k if r in rel_set)
return hits / k
```

표준 정의 `|R∩T_k|/k`. ✅ 단, `top_k` 가 `list(retrieved[:k])` 이고 정답이 list 내 중복을 통해 보너스를 못 받음 (set 변환 안 함) — 정의대로. `k<=0`/빈 top_k 가드 있음. ✅
- 다만 `top_k = list(retrieved[:k])` 인데 retrieved 길이가 k 미만이어도 분모는 **k** 고정 (top_k length 아님). 표준 정의 그대로지만 retrieved < k 인 경우 precision 이 인위적으로 깎임 — F-3 의 dedup 으로 자주 발생할 수 있음.

#### MRR (`metrics.py:50-59`)

```python
for i, r in enumerate(retrieved, start=1):
    if r in rel_set: return 1.0 / i
return 0.0
```

첫 정답의 1/rank, 미발견 시 0. ✅
- ⚠️ F-16: `evaluate_one:237` 에서 `mrr(retrieved_doc_ids, relevant)` — top_k slice 없음. retrieved 전체에서 첫 정답을 찾음. 표준 정의는 "랭킹 리스트 전체"라 옳지만, 다른 메트릭은 `@k` slice 라 일관성 없음. 메트릭 라벨이 `"mrr"` 인 게 사실상 `"mrr@max_chunks"` 의미.

#### nDCG@k (`metrics.py:62-84`)

```python
dcg = 0.0
for i, r in enumerate(retrieved[:k], start=1):
    if r in rel_set: dcg += 1.0 / math.log2(i + 1)
ideal_hits = min(len(rel_set), k)
idcg = sum(1.0 / math.log2(i + 1) for i in range(1, ideal_hits + 1))
if idcg == 0.0: return 0.0
return dcg / idcg
```

표준 binary DCG = `sum(rel_i / log2(i+1))`, IDCG = 정답을 1..m 에 배치. log2 base 사용. ✅
- gain = 1/0 (binary). 명시적 docstring. ✅
- `idcg=0` 가드 있음. ✅
- 단, `len(rel_set) > k` 면 IDCG 가 k 위치에서 잘림 → **상한이 1.0 인 것 확인됨**. 정의 정확.
- graded relevance 미지원. 골드셋이 future 에 weight 도입 시 확장 필요.

#### Hit@k (`metrics.py:87-90`)

```python
return any(r in rel_set for r in retrieved[:k])
```

`bool`. 호출처에서 `int(...)` 캐스팅. ✅

#### 그래프 메트릭 (`eval_search.py:240-263`, `graph_match.py:309-369`)

`run_entity_matching` 이 4-tier 매칭으로 `retrieved_keys_in_rank_order`, `all_relevant_keys` 두 시퀀스를 만들고 그대로 chunk용 generic metric 함수에 넘김. 즉:
- recall 분모 = `|all_relevant_keys|` = 골든 entity 총 수. ✅
- recall 분자 = top_k 안에서 매칭된 retrieved 키 수. ✅

다만:
1. ⚠️ `retrieved_keys_in_rank_order` 는 **매칭에 성공한 retrieved 의 키들만** rank 순으로. 즉 retrieved entity 가 100개여도 매칭 안 된 것은 빠짐 — recall 입장에서는 OK지만, **precision 분모는 항상 k 라서 매칭 못한 것이 모두 noise 로 안 셈해짐**. precision 평가 의미가 chunk-level과 달라짐 (chunk 는 retrieved 안에 매칭 없으면 noise → precision 깎임, graph 는 매칭 없는 retrieved 가 아예 리스트에서 빠짐 → precision 인위적 부풀림 가능). F-13 의 변종.
2. ⚠️ rank 보존이 `hits.sort(key=lambda t: t[0])` (retrieved_index 오름차순). 같은 retrieved_index 에 다른 golden이 매칭된 경우의 tie 처리가 stable sort 의존 — 결정적이지만 의도된 동작인지 docstring 확인 안 됨.
3. T4 embedding tier 의 best 선택이 `sim > best.score` 인데 **같은 sim 이면 첫 항목 유지**. 결정성은 OK이지만 retrieved entity 의 source 순서가 graph_store 도착 순서에 의존하면 비결정성 잠복.

#### entity match 의 의미 정확성

- T1 exact: name.lower + type 정확. ✅
- T2 alias: 골드 측 aliases vs retrieved name. type 정확 요구. ✅
- T3 normalize: NFKC + 공백/하이픈/언더스코어/점 제거. type 정확 요구. **escape 문자열 `r"[\s\-\_\.]+"` 의 `\-`/`\_` 가 character class 안에서 불필요한 escape (warning 아닌 실수일 가능성)**. 의도는 명확. ✅
- T4 embedding: description embedding cosine ≥ τ. type-agnostic. cosine 분모 0 가드 있음. ✅

### 2. top-k 선정 / tie-breaker

**비결정성 핵심:**

| 위치 | 문제 |
|---|---|
| `context_assembler.py` `_search_chunks` | `vector_store.search(query_embedding, n_results=max_chunks*2)` 결과 순서 = ChromaDB distance 오름차순. distance tie 시 ChromaDB 내부 ID 순서에 의존 — 동률 시 비결정적일 수 있음 (DB 버전·인덱스 빌드 순서 의존). |
| `context_assembler.py` Source dedup | `sources.append` 가 `doc_id not in {s.document_id for s in sources}` 체크 후 append. 같은 doc의 더 낮은 similarity 청크가 먼저 들어가면 그 값이 박힘. |
| `context_assembler.py` `sources.sort(key=lambda s: s.similarity, reverse=True)` | Python sort 는 stable. tie 시 insertion 순서 보존 → vector_store 도착 순서 의존. **결정성을 vector store 가 결정성을 보장할 때만 보장**. |
| 그래프 결과 추가 시 `similarity=0.0` 디폴트 | 그래프 source 들이 모두 동률 0 → 자기들끼리는 graph_result.document_ids iteration 순서대로 (set 일 수 있음 → 비결정성). 코드상 `graph_result.document_ids` 가 list 인지 set 인지에 따라 다름. |
| `eval_search.py:209` `[s.document_id for s in assembled.sources]` | 위 sort 결과를 그대로 사용. retrieved_doc_ids 가 top_k 보다 짧을 수 있음 (dedup 영향) → recall 분자 부족, precision 분모는 k 고정이라 인위적 깎임. |

**개선:** vector_store 측에서 secondary key (chunk_id) 까지 명시적 sort, 또는 `assemble_context_with_sources` 가 `(similarity, document_id)` 튜플 비교로 정렬. eval_search 측에서 `retrieved_doc_ids` 가 정확히 top_k 길이가 되도록 보장 (chunk-level retrieval 결과를 dedup 없이 받는 별도 API 추가).

### 3. Judge 채점 메타-편향

**가장 위험. 두 가지 결합:**

#### 3-a. 자기 평가 폴백 (F-1)

```python
# eval_search.py:743-763
if args.judge:
    judge_configured = role_is_configured(config, "judge", ...)
    if judge_configured:
        judge = build_eval_llm_client(config, "judge", ...)
    else:
        logger.warning(
            "--judge 가 켜져 있지만 config.eval.judge / --judge-* 가 비어 있어 "
            "system llm_client 를 Judge 로 재사용합니다 (자기 평가 편향 가능).",
        )
        judge = llm_client
```

→ 경고 로그뿐. CI/평가 결과에서 자기-평가 여부가 보이지 않음. `config_summary["judge_model"]` 도 `args.judge_model or config.get("llm.model") if args.judge else None` 라서 **자기 평가일 때도 model 이름이 시스템 LLM 이름으로 채워짐 — 결과 파일에서 후행 감사가 어려움**.

#### 3-b. Judge 프롬프트의 lexical overlap 편향 (F-2)

```python
JUDGE_PROMPT_TEMPLATE = """\
질문: {query}
정답 근거 (출처 청크):
---
{source_chunk}
---
검색 시스템이 반환한 컨텍스트:
---
{retrieved_context}
---
검색된 컨텍스트가 정답 근거의 핵심 내용을 담고 있는지 0~5점으로 평가하라.
```

Judge가 **source chunk 와 retrieved context 둘 다 본다**. LLM은 의미 평가가 비싸고 토큰 overlap 평가가 싸기 때문에, 사실상 ROUGE-style overlap 채점에 회귀할 위험이 매우 높다.
- 진짜 의미 평가는 source chunk 를 가리고 retrieved 만 보여주고 "이 컨텍스트로 질문에 답할 수 있는가?" 형식의 reference-free judge 가 더 robust.
- 또는 source chunk vs retrieved context 의 의미적 등가성을 `entailment` 형태로 묻기 (NLI). 단순히 "담고 있는지" 는 lexical overlap 으로 회귀.

추가로:
- temperature=0 + 단일 호출만 (`eval_search.py:129-135`). **분산 측정 안함**. Judge가 매번 같은 답을 준다고 가정하지만 운영 LLM(특히 endpoint)에서 cache miss 시 흔들림.
- score=-1 (parse_error) 처리: F-18 의 평균 오염. F-18 의 위험은 critical에 가까움. `metrics.py:aggregate` 가 -1 도 숫자로 받아 평균에 포함.

#### 3-c. Generator 분리

`llm.py:209-226` `role_is_configured` 는 generator 도 같은 함수로 처리. eval_search 는 generator 를 직접 호출하지 않음 (golden set은 외부 `build_synthetic_gold_set.py` 가 만듦). 하지만 generator 가 system LLM 과 같은 model 로 생성된 골드셋을 system LLM 으로 평가하고 judge 도 같으면 **3중 자기-참조**. eval_search 자체는 generator 동일성 체크를 안함.

### 4. 통계 / 변동성 처리

| 항목 | 상태 |
|---|---|
| mean, std, min, max, n 계산 | ✅ 표본 표준편차 (ddof=1) 사용 `metrics.py:161` — 작은 N 편향 회피 의도 명시 |
| n=1 시 std=0.0 | ⚠️ NaN 권장. 사용자가 "variance 없음"으로 오해 |
| 표본 크기 경고 | ❌ N<5 일 때 결과를 신뢰하지 말라는 경고 없음 |
| "mean Δ > std면 유의미" docstring 기준 | ❌ 코드 강제 안됨. `_write_aggregate` 가 baseline/treatment 두 라벨을 받지도 않음 (단일 라벨만) |
| 부트스트랩/페어드 t-test/Wilcoxon | ❌ 없음 |
| paired comparison | ❌ baseline 과 treatment 의 per-question 점수를 짝지어 비교하는 함수 없음 |

권고:
- N≥10 권고 + 그 미만은 summary에 `warning: n<10, variance unreliable` 추가
- `compare.py` 도구 추가 — baseline.csv + treatment.csv 를 per-question paired (id 기준) 비교, paired t-test / Wilcoxon signed-rank, bootstrap 95% CI 산출

### 5. 출력 / 감사 추적성

**Per-question CSV (`write_csv` `eval_search.py:383-409`):**
- 질의, 정답 doc, retrieved_doc_ids (top_k slice), hit 여부, 점수 모두 기록. ✅
- 다만 `assembled.sources` 의 similarity 값은 row 에 없음 — 디버그 시 점수와 hit 의 mismatch 추적 어려움.
- judge_score, judge_reason 도 row 에 있음. ✅

**Summary JSON (`write_summary` + `run` 의 `config_summary`):**
- `config_summary` 에 top_k, max_chunks, similarity_threshold, rerank_enabled, hyde_enabled, include_graph, embedding_model, llm_model, judge_enabled, judge_model, graph_match_*, concurrency 기록.
- ⚠️ **judge_model 이 자기 평가 폴백 시 시스템 LLM 이름으로 채워짐** — 후행 감사에서 분리 여부 추적 불가. F-1 의 결과.
- ❌ 골드셋 fingerprint (파일 sha256, 항목 수, generator 모델, 빌드 timestamp) 없음. `gold_set` 경로 string만 enriched_config 에 추가.
- ❌ 시스템 RAG의 코드 commit hash / model embedding signature 없음.
- ❌ 평가 스크립트 자신의 buildinfo 없음.

**실패 질의 집계 (`eval_search.py:665-678`):**
```python
except Exception as exc:
    row = {"id": item.id, "query": item.query, "error": str(exc), "_idx": idx}
```
→ 메트릭 키가 row 에 없음. `aggregate` (metrics.py:107) 는 키가 있는 row 만 평균에 포함 → **실패 질의가 평균에서 silently 제외**.
이게 자동 0점도 아니고 명시적 exclude 도 아닌 회색지대. summary 의 `n_queries=len(rows)` 는 실패 포함하지만 **메트릭 평균 분모는 성공만**. → 사용자가 metrics를 N으로 곱해서 reconstruct 못함.

권고: summary 에 `n_failed`, `n_successful`, `metric_mean = sum/n_successful` 명시. 실패율도 summary 에 추가.

### 6. 실행 안정성

- **Timeout**: 없음. `assemble_context_with_sources` 가 LLM 호출(graph planner, reranker, judge)이 hang 되면 `_process_item` 도 hang. Semaphore 가 다른 항목을 막아 평가 전체 정체.
- **Retry**: 없음. 1회 실패 → row.error.
- **Concurrency**: `asyncio.Semaphore(max(1, args.concurrency))`. concurrency>32 경고 (`eval_search.py:1032-1036`).
- **재현성**:
  - sort 이후 idx 복원 (`eval_search.py:700`) ✅
  - 그러나 LLM endpoint 가 stateless 가 아니거나 temperature>0 이면 동일 입력에서 다른 결과. judge temperature=0 이지만 plan_graph_search 같은 내부 LLM 호출의 temperature 는 system config 의존.
  - embedding cache 가 평가 실행마다 새로 빌드 → 첫 N개 항목과 마지막 N개 항목의 latency 가 다름 (캐시 hit 율 차이). 정확도엔 영향 없으나 elapsed_ms 의 분포가 왜곡.
- **Deterministic ordering**: `_process_item` 결과를 `_idx` 로 정렬. ✅
- **F-10 의 silent skip**: `build_embed_fn` 이 running loop 안에서 async embedding 만 가진 client 의 경우 None 반환 → graph 메트릭 영향. logger.warning 만 있고 summary 에 표시 안됨.

### 7. 라벨링 / 비교

`eval_search.py` 는 **단일 라벨 한번에 한 골드셋(또는 글롭) 만 실행**. baseline 과 treatment 비교는 사용자가 두 번 실행한 뒤 결과 파일을 눈으로 비교하는 워크플로우.

- ❌ baseline vs treatment 자동 비교 도구/스크립트 부재.
- ❌ 두 라벨의 `config_summary` 가 같은 골드셋·같은 핵심 설정(embedding_model 등) 인지 cross-check 자동화 없음.
- ❌ 라벨 결과 파일이 같은 골드셋 fingerprint 를 가졌는지 확인 안함.
- ✅ multi-goldset 모드에서 `--label baseline` 한 번 실행 시 모든 goldset 에 대해 같은 config 로 돈다 (구조적으로 보장됨).

권고: `scripts/compare_runs.py baseline.summary.json treatment.summary.json` — 같은 골드셋, 같은 top_k, 같은 embedding_model 확인 + per-question paired diff + paired test.

## 종합 위험 매트릭스

| 차원 | Critical | High | Medium | Low |
|---|---|---|---|---|
| 1. 메트릭 식 | — | — | F-13, F-14 | F-11, F-12, F-15, F-16 |
| 2. top-k / tie | — | F-3 | F-17 | — |
| 3. Judge 편향 | F-1, F-2 | F-18 | — | — |
| 4. 통계 | — | F-4 | — | F-15 |
| 5. 추적성 | — | F-7 | F-6 | — |
| 6. 안정성 | — | F-10 | F-8, F-9 | — |
| 7. 라벨 비교 | — | F-5 | — | — |

**전체 신뢰도 평가:**
- 메트릭 함수 수준 정확도: **A-** (식 자체는 정확. nDCG/Recall/Precision/MRR 모두 표준)
- 시스템 수준 신뢰도: **C+** (Judge 자기-평가 폴백, top-k tie 비결정성, 실패 silent drop, 라벨 비교 미검증)
- 절대 수치 신뢰: 7/10 — 같은 설정에서 같은 결과 재현 가능 (vector store 결정성 가정 시).
- 비교 수치 신뢰: 4/10 — baseline ↔ treatment Δ 의 통계적 유의성을 확신할 수 없다.

## 운영 권고

### Critical (즉시 패치)

1. **`eval_search.py:758-763`** — `--judge` 가 켜졌는데 judge 가 분리 구성 안된 경우 **에러로 종료** 또는 최소한 `--allow-self-judge` 플래그 강제. config_summary 에 `judge_is_self=true` 명시 기록.
   ```python
   else:
       if not args.allow_self_judge:
           raise SystemExit("--judge requires --judge-model or config.eval.judge.* — "
                            "use --allow-self-judge to override")
       judge = llm_client
       config_summary["judge_is_self"] = True
   ```

2. **`eval_search.py:88-109`** — Judge 프롬프트 두 가지 모드:
   - **`reference-free`** (기본): source chunk 숨기고 retrieved 만 + 질문 → "이 컨텍스트로 답할 수 있는가?"
   - **`entailment`** (옵션): source chunk vs retrieved 의미 entailment 0/1.
   현재 lexical-overlap 모드는 `--judge-mode lexical` 명시할 때만.

3. **`eval_search.py:319-320`** — judge_score=-1 을 row 에 그대로 두면 `aggregate` 가 평균에 -1 포함. 별도 `judge_score_parse_failures` 카운트로 빼고, `judge_score` 는 성공 값만.

4. **`eval_search.py:632-678`** — 실패 row 에 `metric_failed=True` + 모든 표준 메트릭 키를 `None` 으로 채워 `aggregate` 가 명시적으로 drop 하도록. summary 에 `n_failed`, `n_successful`, `failure_rate` 보고.

### High (다음 스프린트)

5. **`context_assembler.py`** + **`eval_search.py:209`** — `assemble_context_with_sources` 에 `return_top_k_doc_ids: int | None` 파라미터 추가, dedup 안된 원시 retrieved_doc_ids 를 정확히 top_k 길이로 반환. sort tie-breaker 로 `(similarity desc, document_id asc)` 명시.

6. **`metrics.py`** + 새 파일 `scripts/compare_runs.py` — baseline.csv + treatment.csv 를 받아 per-question paired diff, paired Wilcoxon, bootstrap 95% CI 계산. baseline.summary.json 과 treatment.summary.json 의 `config_summary` 가 일치 (gold_set, embedding_model, top_k, max_chunks 등) 함을 자동 확인 → mismatch 시 에러.

7. **`eval_search.py:707-712`** — `enriched_config` 에 `gold_set_sha256`, `gold_set_n_items`, `gold_set_generator_model` 추가. summary JSON 에 buildinfo (git commit hash) 추가.

8. **`graph_match.py:182-199`** — async embedding fallback이 None을 반환할 때 evaluate_one 의 결과에 `graph_t4_disabled=True` 플래그를 표시. summary 에서 graph_recall 이 깎였을 가능성을 사용자에게 명시.

9. **`eval_search.py:632-678`** — `asyncio.wait_for(evaluate_one(...), timeout=args.timeout)` 추가. 기본 60s, CLI 로 조정 가능. timeout 도 retry 1회.

### Medium

10. **`eval_search.py:23` + `_write_aggregate`** — "mean Δ > std" docstring 을 실제 검증 함수로 추출. multi-goldset 결과를 받았을 때 `improvement_significant` boolean 키를 summary 에 추가. N<5 일 때 `warning_low_sample_size` 명시.

11. **`graph_match.py:201-216`** — `OrderedDict` + `move_to_end` 로 LRU 재구현, 또는 `functools.lru_cache` 사용 (단 클로저 안에서 가능하게 `(model_id, text)` 키).

12. **`eval_search.py:240-263`** — `graph_top_k` 별도 CLI 인자 도입. 그래프 entity는 chunk 와 별개의 cardinality.

13. **`graph_match.py:399-433`** — 관계 매칭의 source/target lower 정확 비교를 alias/normalize tier 까지 확장.

### Low

14. **`metrics.py:159-164`** — n=1 시 std=NaN, summary 에 `"n=1, std undefined"` 표기.

15. **`eval_search.py:237`** — `mrr` 키를 `mrr@max_chunks` 로 명명 정확화. 또는 `mrr@k` 도 함께 보고.

16. **`graph_match.py:45`** — regex `r"[\s\-\_\.]+"` 의 `\-`/`\_` 는 character class 안에서 불필요한 escape. `r"[\s\-_.]+"` 권장. 동작 영향 없음.

17. **CSV 컬럼 확장** — `assembled.sources` 의 `(doc_id, similarity)` 페어를 column 으로 추가하여 디버깅성 향상.

18. **재현성 시드** — `random.seed`/`numpy.random.seed` 미사용이라 OK이지만 향후 sampling 도입 시 `--seed` CLI 인자 + summary 기록.

---

**감사자 노트:** 메트릭 함수 자체는 견고하다. 그러나 메트릭이 받는 입력 (retrieved_doc_ids) 의 구성 과정, judge 의 분리 보장, 통계 비교 자동화 세 곳에 구조적 결함이 있어 **"숫자는 맞지만 그 숫자가 의미하는 것은 신뢰하기 어렵다"** 가 종합 판정. 위 Critical 4건만 처리해도 신뢰도가 B+ 수준으로 회복된다.
