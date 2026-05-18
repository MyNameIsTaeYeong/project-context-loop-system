# 02_design.md — 골드셋 생성·평가 병렬화 설계 (3차)

**작성**: 2026-05-18
**대상**: `scripts/build_synthetic_gold_set.py`, `scripts/eval_search.py`
**상위**: `00_requirements.md` (R1~R4), `01_analysis.md` (19개 미해결 질문)

이 문서는 implementer 가 보고 그대로 코드를 작성할 수 있을 정도로 구체화한 설계안이다. 코드 본문은 작성하지 않고, 짧은 의사코드와 시그니처만 제시한다.

---

## 0. 미해결 질문 결정 요약 (19개)

00_requirements 의 10개 + analyst 추가 발견 9개 = 19. 모두 결정 부여.

| # | 질문 | 결정 | 근거 (한 줄) |
|---|------|------|---------|
| Q1 | 동시성 제어 메커니즘 | `asyncio.Semaphore(N)` + `asyncio.gather(return_exceptions=True)` | 코드베이스에 이미 동일 패턴 3곳, 외부 의존성 0. |
| Q2 | CLI 옵션 이름·기본값 | `--concurrency N` (양쪽 스크립트 동일, 기본 1) | `mcp_sync.py` 선례와 어휘 일치. |
| Q3 | id 사전 부여 구현 | 항목별 함수에 `idx: int` 를 인자로 전달, `f"q{idx:04d}"` 직접 부여. chunk 와 graph 모드는 **연속 idx 공간**(chunk 가 1..M, graph 가 M+1..M+K) 으로 분배. | 단순·결정론적·기존 명명규칙 유지. |
| Q4 | exception 격리 | 생성·평가 모두 **`gather(return_exceptions=True)` + 결과 분리** 단일 패턴. 평가 측 기존 `try/except` 는 _process_one 내부로 이동 (이중 try 제거). | 한 곳 패턴으로 일원화. |
| Q5 | 로그 정책 | **시작 1회 (`[start i/N]` 사전 idx) + 완료 1회 (`[done i/N]` 사전 idx)** 양쪽 로깅. 순서 흔들림은 idx 가 박혀 있어 추적 가능. | 디버그 시 시작/완료 양쪽 추적 필요. |
| Q6 | SQLite read cache (평가) | **도입하지 않음**. `_fetch_source_text` / `_format_chunk_results` 의 doc fetch 는 ms 단위, LLM 비용에 묻힘. | YAGNI. 추후 측정 후 재검토. |
| Q7 | graph distractor 풀 명시 | 루프 전 1회 셔플 후 **read-only 공유 명시**. 항목 task 는 슬라이싱만. | analyst §2.4 검증 완료. |
| Q8 | stats 카운터 | 각 항목 task 가 **LocalStats dict 반환** → main 에서 일괄 머지. | race 가능성 자체 제거, 단순. |
| Q9 | 다중 골드셋 store 재사용 | **현행 유지** — stores 1세트 + 골드셋 간 직렬. 각 골드셋 내부에서만 동시성 적용. | R3 비목표. aiosqlite 한계도 직렬에서 충돌 안 함. |
| Q10 | Generator/Judge 동시성 cap 공유 | **단일 cap (`--concurrency`)** 만 두고 항목 단위로 sem 획득. Generator/Judge 가 한 항목 안에서 직렬 호출되므로 endpoint 동시 호출 수 ≤ N. 별도 cap 미도입. | YAGNI. 별도 cap 은 cap 누적·디버깅 비용↑. |
| Q11 | 항목 안 내부 병렬화 (질문×distractor) | **하지 않음**. 외곽 (chunk/subgraph) 항목 단위만 병렬. | 디버깅·로그 가독성, cap 관리 단순. |
| Q12 | `build_embed_fn` 캐시 외부화 | **외부화**. `_evaluate_gold_set` 시작 시 1회 빌드하여 `evaluate_one(embed_fn=...)` 으로 주입. 캐시 dict 공유 + 동시 write 안전성은 §7 에 명시. | 항목 동시 평가 시 캐시 효과 보존, 임베딩 호출 절감. |
| Q13 | `graph_store.build_entity_embeddings` 사전 빌드 | **`_evaluate_gold_set` 시작 직전 1회 호출**. 항목 평가 진입 후에는 더 이상 build 진입 없음. | 동시 중복 build race 회피, 단순. |
| Q14 | metadata `concurrency` 기록 | **기록함**. 생성 측 `metadata["concurrency"] = effective_concurrency`. 평가 측은 `config_summary["concurrency"]` 에 포함. | R3 명시. |
| Q15 | n_gold_sets × concurrency 결합 | 골드셋 간 직렬, 골드셋 내부만 동시. 결합 동시성 없음. | Q9 의 자연스러운 귀결. |
| Q16 | 결정성 회귀 테스트 인프라 | mock LLMClient 가 `asyncio.sleep(random)` 으로 응답 도착 순서를 의도적으로 흔들고, **같은 시드 + concurrency 1 vs 4 의 YAML byte-identical** 검증. | id 사전 부여의 핵심 회귀. |
| Q17 | `_embed_graph_item_descriptions` 호출 시점 | 현행 유지 — graph 모드 종료 후 1회. items 가 모두 모인 뒤에 호출되는 invariant 유지 (graph 모드 동시화 후에도). | 이미 비직렬 배치. |
| Q18 | 로그 idx 의 의미 | 사전 idx (sampling 순서). 완료 카운터(별도 변수) 는 추가로 `[%d/%d done]` 형태로 병기. | Q5 와 일관. |
| Q19 | chunk + graph 모드 동시화 결합 | **두 모드 분리 유지**. chunk 모드 끝나고 graph 모드 시작. 각 모드 안에서 sem 으로 동시성. | 단순성, 로그 분리 가독성. |

---

## 1. 설계 목표 (R1~R4 → 결정 ID 매핑)

| 요구 | 핵심 결정 |
|------|---------|
| R1. 항목 단위 병렬 처리 | Q1 (Semaphore+gather), Q2 (CLI), Q11 (외곽만), Q19 (모드 분리) |
| R2. 결정성·재현성 | Q3 (사전 idx → id), Q5/Q18 (idx 로그), Q8 (LocalStats 머지), Q16 (회귀 테스트) |
| R3. 백워드 호환 | Q2 (기본 1), Q9 (다중 골드셋 직렬 유지), Q14 (메타 기록), Q15 (결합 동시성 없음) |
| R4. Rate Limit·에러 분리 | Q1 (단일 Semaphore cap), Q4 (exception 격리), Q10 (단일 cap), Q11 (cap 폭주 차단) |

---

## 2. 동시성 제어 패턴

### 2.1 선례 인용

`src/context_loop/sync/mcp_sync.py:241-270`:

```python
effective_concurrency = max(1, concurrency)
sem = asyncio.Semaphore(effective_concurrency)

async def _process_one(doc_id: int) -> None:
    async with sem:
        try:
            await process_document(doc_id, ...)
            result.processed.append(doc_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("...실패: %s", exc)
            ...

await asyncio.gather(*(_process_one(d) for d in to_process))
```

본 설계도 동일 형태를 채택. 차이점은 두 가지:
- `_process_one` 이 **결과를 return** (현 선례는 외부 list 에 append). 동시 append 비결정성 회피.
- `gather(..., return_exceptions=True)` 로 호출 — 호출자가 직접 분리.

### 2.2 의사코드 (생성 측 chunk 모드)

```python
effective_concurrency = max(1, args.concurrency)
sem = asyncio.Semaphore(effective_concurrency)
total = len(sampled)
completed = 0

async def _process_chunk(idx: int, chunk: dict) -> tuple[list[GoldItem], LocalStats]:
    async with sem:
        logger.info("[chunk start %d/%d] doc=%d ...", idx, total, chunk["document_id"])
        local_items, local_stats = await _build_chunk_items(
            idx=idx, chunk=chunk,
            distractor_pool=distractor_pool,  # read-only 공유
            generator=generator, judge=judge,
            questions_per_chunk=questions_per_chunk,
            n_distractors=n_distractors,
            reasoning_mode=reasoning_mode,
            apply_filter=apply_filter,
        )
        nonlocal completed
        completed += 1                                # asyncio 단일 스레드 — race 없음
        logger.info("[chunk done %d/%d] (completed=%d)", idx, total, completed)
        return local_items, local_stats

tasks = [_process_chunk(idx, chunk) for idx, chunk in enumerate(sampled, start=1)]
results = await asyncio.gather(*tasks, return_exceptions=True)
```

### 2.3 결과 분리

```python
for idx, r in enumerate(results, start=1):
    if isinstance(r, Exception):
        logger.exception("[chunk fail %d/%d] %s", idx, total, r)
        stats["fail_runtime"] = stats.get("fail_runtime", 0) + 1
        continue
    local_items, local_stats = r
    items.extend(local_items)                          # 순서: idx 순
    _merge_stats(stats, local_stats)
```

`items.extend(local_items)` 는 `idx` 순으로 호출되므로 최종 `items` 순서는 sampling 순서와 동일 → 결정성 보장.

graph 모드도 동일 패턴 (총 idx 공간을 chunk 다음 번호부터 시작).

평가 측도 동일 패턴 (의사코드 §6 참조).

---

## 3. id 사전 부여 알고리즘

### 3.1 idx → id 매핑 규칙

- 한 항목(chunk 또는 subgraph) 은 `questions_per_chunk` (기본 2) 개의 GoldItem 을 만들 수 있다 (filter 통과 시).
- 따라서 **GoldItem.id 부여는 (chunk_idx, q_idx_within_chunk) 의 2 차원 idx 기반 단조함수** 가 되어야 한다.

**선택**: id 명명규칙은 기존(`q0001` 4 자리 zero-padded) 유지. 모드(chunk/graph) 와 chunk_idx, q_idx 를 결합해 결정론적 id 부여:

```
chunk 모드:    id = f"q{chunk_idx * Q + q_within + 1 - Q:04d}"  ← Q = questions_per_chunk
  ↓ 같은 의미를 단순하게:
  base = (chunk_idx - 1) * Q  +  q_within   ← chunk_idx, q_within 모두 1-based
  id   = f"q{base + 1:04d}"

graph 모드:    base = chunk_count * Q + (graph_idx - 1) * Q + q_within
  id          = f"q{base + 1:04d}"
```

여기서 `chunk_count = len(sampled_chunks)` (모든 chunk 가 항상 Q 개 자리를 차지하도록 예약).
**filter 탈락한 슬롯은 id 가 비게 됨**. 즉 최종 items 의 id 는 연속이 아닐 수 있다.

### 3.2 비연속 id 의 결정

- 옵션 A (연속 id, 후처리 압축): 모든 결과 모은 뒤 idx 순으로 다시 1..N 재부여. 단순하지만 idx ↔ id 의 단조 관계가 깨져 디버그 시 추적 어려움.
- 옵션 B (예약 id, 비연속): 위 의사코드처럼 chunk_idx, q_within 으로 직접 부여. id 가 비연속일 수 있음.

**결정**: **옵션 A — 연속 id**.
- 이유: 기존 골드셋 (`q0001`, `q0002`, ...) 이 연속이라 backward-compat. 또한 사용자가 골드셋을 눈으로 볼 때 비연속 id 가 혼란.
- 결정성: `gather` 결과를 idx 순으로 순회하면서 `_assign_ids(local_items, start=next_id)` 로 부여. 같은 시드 → 같은 sampling 순서 → 같은 통과/탈락 패턴 → 같은 id.

**핵심 invariant**: id 부여는 task 안에서 하지 않고 **gather 완료 후 idx 순 순회에서만** 부여한다. task 안에서는 임시 id 없이 (또는 placeholder 로) GoldItem 을 만든다.

### 3.3 의사코드

```python
def _build_chunk_items(idx: int, chunk: dict, ...) -> tuple[list[GoldItem], LocalStats]:
    """idx 는 사전 부여 idx (로그·디버그용). GoldItem.id 는 비워두고 (=""), 후처리에서 부여."""
    items: list[GoldItem] = []
    stats: LocalStats = LocalStats()
    generated = await generate_questions(...)
    stats["generated"] += len(generated)
    if not generated:
        stats["fail_parse"] += 1
        return items, stats
    for j, gq in enumerate(generated, start=1):
        if not apply_filter:
            items.append(GoldItem(id="", query=gq.query, ...))
            stats["passed"] += 1
            continue
        report = await filter_question(...)
        if not report.passed:
            key = f"fail_{report.reason}" if report.reason else "fail_parse"
            stats[key] = stats.get(key, 0) + 1
            continue
        items.append(GoldItem(id="", query=gq.query, ...))
        stats["passed"] += 1
    return items, stats
```

main 에서:
```python
results = await asyncio.gather(*(_process_chunk(i, c) for i, c in enumerate(sampled, start=1)), return_exceptions=True)
next_id = 1
for idx, r in enumerate(results, start=1):
    if isinstance(r, Exception): ...; continue
    local_items, local_stats = r
    for item in local_items:
        item.id = f"q{next_id:04d}"
        items.append(item)
        next_id += 1
    _merge_stats(stats, local_stats)
```

graph 모드도 동일 — chunk 모드 종료 후 `next_id` 가 chunk 분 만큼 진전된 상태에서 graph idx 진행.

**평가 측은 idx → id 부여를 하지 않는다**. 골드셋 항목의 id 는 이미 박혀 있으므로 평가 측은 (item.id, idx) 양쪽을 보존하면 됨.

### 3.4 `_make_graph_gold_item` 시그니처 변경

기존:
```python
def _make_graph_gold_item(sg, gq, existing_items: list[GoldItem], *, score_relations) -> GoldItem:
    return GoldItem(id=f"q{len(existing_items) + 1:04d}", ...)
```

변경:
```python
def _make_graph_gold_item(sg, gq, *, score_relations) -> GoldItem:
    """id 는 호출자가 후처리에서 부여한다. 여기서는 id="" 로 둔다."""
    return GoldItem(id="", ...)
```

세 위치 (`build_synthetic_gold_set.py:426, 456, 698`) 모두 `id=""` 로 일괄 변경.

---

## 4. 항목 처리 함수 시그니처

### 4.1 생성 측

```python
@dataclass
class LocalStats:
    """항목 1개의 통계. main 에서 일괄 머지."""
    generated: int = 0
    passed: int = 0
    fail_not_answerable: int = 0
    fail_leakage: int = 0
    fail_demonstrative: int = 0
    fail_generic: int = 0
    fail_parse: int = 0
    graph_generated: int = 0   # graph 모드만 사용
    graph_passed: int = 0      # graph 모드만 사용
    fail_runtime: int = 0      # exception 격리 시 main 에서 set
    # 동적 키 (fail_<reason>) 호환: get(name, 0) 패턴 + 머지 시 dict 변환
```

> 구현 단순화를 위해 `LocalStats` 를 `dict[str, int]` 의 typealias 로 둬도 무방 (현 코드와 일치). main 의 `stats` 도 dict 이므로 `Counter` 기반 merge 가 가장 단순.

**시그니처 (chunk 모드)**:
```python
async def _process_chunk(
    idx: int,
    chunk: dict[str, Any],
    *,
    distractor_pool: list[dict[str, Any]],
    generator: LLMClient,
    judge: LLMClient,
    questions_per_chunk: int,
    n_distractors: int,
    reasoning_mode: str,
    apply_filter: bool,
    sem: asyncio.Semaphore,
    total: int,                # 로그용
) -> tuple[list[GoldItem], dict[str, int]]:
    ...
```

**시그니처 (graph 모드)**:
```python
async def _process_subgraph(
    idx: int,
    sg: dict[str, Any],
    *,
    distractor_pool: list[dict[str, Any]],
    skip_generic_gate: bool,
    generator: LLMClient,
    judge: LLMClient,
    questions_per_chunk: int,
    n_distractors: int,
    reasoning_mode: str,
    apply_filter: bool,
    score_relations: bool,
    sem: asyncio.Semaphore,
    total: int,
) -> tuple[list[GoldItem], dict[str, int]]:
    ...
```

### 4.2 평가 측

`evaluate_one` 의 기존 시그니처는 유지하되 두 인자를 **추가**:

```python
async def evaluate_one(
    item: GoldItem,
    *,
    # ... 기존 인자들 ...
    embed_fn: Callable[[str], list[float] | None],   # NEW — 외부에서 주입
    idx: int,                                          # NEW — 로그·정렬용 (선택)
) -> dict[str, Any]:
    ...
```

내부의 `embed_fn = build_embed_fn(embedding_client, ...)` 호출 (현 `eval_search.py:208`) 은 **제거**.
`idx` 는 row dict 에 `"_idx": idx` 로 박아 두어, gather 완료 후 정렬에 사용.

`_evaluate_gold_set` 안의 `_process_item`:
```python
async def _process_item(idx: int, item: GoldItem) -> dict[str, Any]:
    async with sem:
        logger.info("[%s start %d/%d] q=%s | gold_doc=%s", label, idx, total, item.id, item.relevant_doc_ids)
        try:
            row = await evaluate_one(item, ..., embed_fn=embed_fn, idx=idx)
            row["_idx"] = idx
        except Exception as exc:
            logger.exception("질의 %s 실패: %s", item.id, exc)
            row = {"id": item.id, "query": item.query, "error": str(exc), "_idx": idx}
        nonlocal completed
        completed += 1
        logger.info("[%s done %d/%d] (completed=%d) q=%s", label, idx, total, completed, item.id)
        return row
```

`gather` 후 `rows.sort(key=lambda r: r["_idx"])` → CSV 행 순서 결정성 회복. `_idx` 는 CSV 저장 전에 pop.

---

## 5. stats 머지 패턴

### 5.1 정의

```python
def _merge_stats(target: dict[str, int], local: dict[str, int]) -> None:
    """동적 키 (fail_<reason>) 포함 전 키를 더한다."""
    for k, v in local.items():
        target[k] = target.get(k, 0) + v
```

### 5.2 사용 위치

- chunk 모드 gather 후: `for idx, r in enumerate(results, start=1): ...; _merge_stats(stats, r[1])`
- graph 모드 동일
- exception 결과는 `stats["fail_runtime"] += 1`

### 5.3 초기 stats 시드 키

기존 (`build_synthetic_gold_set.py:380-389`) + `fail_runtime` 추가:

```python
stats: dict[str, int] = {
    "generated": 0,
    "passed": 0,
    "fail_not_answerable": 0,
    "fail_leakage": 0,
    "fail_demonstrative": 0,    # 기존 코드 누락된 가능성 — 확인 후 추가
    "fail_generic": 0,
    "fail_parse": 0,
    "fail_runtime": 0,           # NEW
    "graph_generated": 0,
    "graph_passed": 0,
}
```

> `fail_demonstrative` 키는 현 코드의 `filter_question.reason` 값(`synth.py:613` 의 `"demonstrative"`) 과 일치하는지 확인 필요. analyst §5.2 의 키 목록에는 없음 — 동적 키 (`fail_<reason>`) 가 처음 등장할 때 `get(key, 0) + 1` 로 자동 생성되므로 시드에 빠져도 동작은 OK.

---

## 6. exception 격리

### 6.1 생성 측 (신규 도입)

```python
results = await asyncio.gather(*tasks, return_exceptions=True)
for idx, r in enumerate(results, start=1):
    if isinstance(r, Exception):
        logger.exception("[chunk fail %d/%d] %s", idx, total, r)
        stats["fail_runtime"] = stats.get("fail_runtime", 0) + 1
        continue
    local_items, local_stats = r
    for it in local_items:
        it.id = f"q{next_id:04d}"; next_id += 1
        items.append(it)
    _merge_stats(stats, local_stats)
```

### 6.2 평가 측 (기존 try/except 통합)

기존 (`eval_search.py:610-641`) 의 try/except 를 `_process_item` 안으로 **이동**. main 루프에서는 `gather(return_exceptions=True)` 결과를 idx 로 정렬만 한다.

```python
# _process_item 안에서 try/except (위 §4.2 의사코드 참조)
results = await asyncio.gather(*(_process_item(i, it) for i, it in enumerate(gold.items, start=1)), return_exceptions=True)
rows: list[dict[str, Any]] = []
for idx, r in enumerate(results, start=1):
    if isinstance(r, Exception):
        # _process_item 안에서 잡혔어야 함. 여기 도달하면 코드 버그 → 로깅 후 fallback row.
        logger.error("예외가 _process_item 밖으로 새어 나옴 (버그 가능성): idx=%d, exc=%s", idx, r)
        rows.append({"id": f"_idx{idx}", "error": str(r), "_idx": idx})
    else:
        rows.append(r)
rows.sort(key=lambda r: r.get("_idx", 0))
for r in rows:
    r.pop("_idx", None)
```

**원칙**: 한 곳에서만 try/except (`_process_item` 안). main 의 외층은 방어 코드만.

---

## 7. build_embed_fn 공유 + graph_store 사전 빌드

### 7.1 build_embed_fn 외부화

기존:
```python
# evaluate_one 안에서 매 호출마다 새로 생성 → 캐시 효과 사라짐
embed_fn = build_embed_fn(embedding_client, model_id=embedding_model_id)
```

변경 — `_evaluate_gold_set` 시작 시 1회:
```python
embed_fn = build_embed_fn(embedding_client, model_id=embedding_model_id)
```
그리고 `evaluate_one(..., embed_fn=embed_fn)` 으로 주입.

**동시 write race**: `build_embed_fn` 내부 closure dict 캐시 (`graph_match.py:201-216`) 는 lock 이 없다. 같은 텍스트를 두 task 가 동시 임베딩 호출하면 — `embed_fn` 안의 `if text in cache:` 체크 후 await embedding → cache 저장. await 중 다른 task 가 같은 체크를 통과해 중복 호출 가능.
- **결정**: 중복 호출은 idempotent (같은 텍스트 → 같은 벡터). 캐시 마지막 쓰기가 이김. **데이터 손실 없음, 비용은 약간 증가**. lock 추가는 비대화 — 도입 안 함.
- analyst §2.6 와 일치.

### 7.2 graph_store.build_entity_embeddings 사전 빌드

`_evaluate_gold_set` 시작 시 (gold 로드 직후, 항목 평가 시작 전):

```python
if args.include_graph:
    if graph_store.entity_embedding_count == 0:
        logger.info("entity embedding 사전 빌드 시작 — %d 노드", graph_store.entity_count)
        await graph_store.build_entity_embeddings(embedding_client)
        logger.info("entity embedding 사전 빌드 완료")
```

이후 항목 평가가 진입했을 때 `assemble_context_with_sources` 안의 `entity_embedding_count == 0` 체크는 항상 False → 동시 build race 자체가 발생하지 않음.

**대응 위치**: `eval_search.py` 의 `_evaluate_gold_set` 함수 진입부 (gold 로드 직후, embed_fn 빌드와 같은 영역).

### 7.3 의사코드 (평가 측 진입부)

```python
async def _evaluate_gold_set(gold_path, *, ..., args, ...):
    gold = load_gold_set(gold_path)
    if not gold.items: return None
    if args.limit: gold.items = gold.items[:args.limit]

    # NEW: 사전 빌드
    embed_fn = build_embed_fn(embedding_client, model_id=embedding_model_id)
    if args.include_graph and graph_store.entity_embedding_count == 0:
        await graph_store.build_entity_embeddings(embedding_client)

    # 동시성 루프
    effective_concurrency = max(1, args.concurrency)
    sem = asyncio.Semaphore(effective_concurrency)
    total = len(gold.items)
    completed = 0
    async def _process_item(idx, item): ...   # §4.2
    results = await asyncio.gather(*(_process_item(i, it) for i, it in enumerate(gold.items, start=1)), return_exceptions=True)
    # 정렬·CSV 저장 (§6.2)
```

---

## 8. CLI 옵션 표

| 옵션 | 스크립트 | 기본값 | 의미 | 비고 |
|------|---------|--------|------|------|
| `--concurrency N` | `build_synthetic_gold_set.py` | 1 | chunk 모드·graph 모드 항목 동시 처리 수 | N=1 시 현 동작과 동일. metadata 에 기록. |
| `--concurrency N` | `eval_search.py` | 1 | 골드셋 내 항목 동시 처리 수 | N=1 시 현 동작과 동일. summary config_summary 에 기록. |

**환경변수 / config 옵션**: 도입 안 함 (00_requirements 의 결정 1·2 와 일치). 명시적 CLI 만 사용.

**검증**:
- `argparse` 단에서 `type=int`, `default=1`. 음수 / 0 입력 시 `max(1, N)` 으로 보정. `> 32` 시 경고 로그.
- 두 스크립트 모두 `--help` 출력에 "기본 1 (직렬). LLM endpoint rate limit 에 맞춰 4~8 권장." 문구.

---

## 9. 로그 정책

### 9.1 로그 메시지 형식

| 시점 | 메시지 | 키 정보 |
|------|--------|--------|
| 시작 | `[chunk start %d/%d] doc=%d, chunk_index=%d, source_type=%s` | idx, total, doc, chunk_index, source_type |
| 완료 | `[chunk done %d/%d] (completed=%d)` | idx (사전), total, completed (실행 시점 완료 수) |
| 통과 | `  q%d 통과 — query=%s` (기존 유지) | 질문 인덱스, query 80자 |
| 탈락 | `  q%d 탈락 — reason=%s, query=%s` (기존 유지) | 질문 인덱스, reason, query 80자 |
| 실패 (gather) | `[chunk fail %d/%d] %s` | idx, total, exception |
| graph 모드 동일 패턴: `[graph start ...]`, `[graph done ...]`, `[graph fail ...]` |
| 평가 측: `[%s start %d/%d] q=%s | gold_doc=%s` / `[%s done %d/%d] (completed=%d) q=%s` |

### 9.2 진행률 카운터

`completed` 는 nonlocal int 변수. asyncio 단일 스레드라 `+=` race 없음. 단, "현재 시점까지 완료된 수" 의 의미라 사전 idx 와 다를 수 있음 (병렬화 효과 가시화).

### 9.3 경고 / 에러

- `fail_runtime > 0` 이면 build 종료 시 요약에 명시: `logger.warning("런타임 예외로 실패한 항목: %d", stats["fail_runtime"])`.
- 동시성 > 32: `logger.warning("--concurrency=%d 는 endpoint rate limit 초과 위험. 4~8 권장.", N)`.

---

## 10. 결정성 회귀 테스트 전략

### 10.1 핵심 가설

> 같은 시드 + 같은 코퍼스 + 다른 `--concurrency` (1 vs 4 vs 8) → 골드셋 YAML 이 **byte-identical**.

### 10.2 테스트 인프라

`tests/test_eval/test_build_synthetic_gold_set.py` 에 신규 테스트 추가.

**mock LLMClient**:
- `generator.complete(...)` 가 입력 prompt 의 hash 를 받아 결정적 응답을 반환 (= 같은 청크 → 항상 같은 질문).
- `judge.complete(...)` 도 결정적 응답.
- **응답 도착 순서를 의도적으로 흔들기 위해**: `await asyncio.sleep(random.Random(hash(prompt)).uniform(0, 0.05))` 로 지연. 단 응답 자체는 결정적.

**테스트 시나리오**:

```python
@pytest.mark.asyncio
async def test_goldset_deterministic_across_concurrency(tmp_path, mock_clients):
    paths: list[Path] = []
    for n in [1, 4, 8]:
        out = tmp_path / f"gold_n{n}.yaml"
        await build_with_args(["--seed", "42", "--concurrency", str(n), "--out", str(out), ...])
        paths.append(out)
    contents = [p.read_bytes() for p in paths]
    assert contents[0] == contents[1] == contents[2], "동시성 변화 시 골드셋 결정성 깨짐"
```

### 10.3 추가 테스트

- **stats merge**: `_merge_stats(target, local)` 가 모든 키를 올바르게 합산.
- **id 부여**: 같은 시드 → 같은 id 순서.
- **exception 격리**: 한 청크의 `generator.complete` 가 raise → 다른 청크는 정상 처리되고 stats["fail_runtime"] == 1.
- **동시성 cap**: Semaphore 가 실제로 N 개를 동시에 허용하는지 (`asyncio.Lock` 으로 진입 카운터 측정 — concurrent_max ≤ N 확인).

### 10.4 평가 측 테스트

`scripts/eval_search.py` 는 eval 전용 테스트가 없는 영역 (analyst §10 의 매트릭스). 신규 작성 권장하되 implementer 가 시간 제약 시 다음 한 가지만:
- `test_evaluate_one_with_shared_embed_fn`: `embed_fn` 이 외부 주입되었을 때 (a) 동작 정상 (b) 캐시 dict 이 항목 간 공유되는지.

---

## 11. 변경 파일 목록

| 파일 | 변경 종류 | 핵심 변경 |
|------|---------|---------|
| `scripts/build_synthetic_gold_set.py` | 수정 | `_process_chunk` / `_process_subgraph` 신규. `build()` 와 `_run_graph_mode()` 가 task 생성·gather·결과 분리·id 부여·stats 머지. `_make_graph_gold_item` 시그니처에서 `existing_items` 제거. `--concurrency` CLI 옵션 추가. `metadata["concurrency"]` 기록. |
| `scripts/eval_search.py` | 수정 | `_evaluate_gold_set` 안에 `_process_item` 신규. `gather(return_exceptions=True)` + idx 정렬. `embed_fn` / `graph_store.build_entity_embeddings` 사전 빌드. `evaluate_one(embed_fn=..., idx=...)` 추가 인자. `--concurrency` CLI 옵션 추가. `config_summary["concurrency"]` 기록. |
| `tests/test_eval/test_build_synthetic_gold_set.py` | 수정/추가 | 결정성 회귀 테스트 (concurrency 1 vs 4 vs 8 byte-identical), id 결정성 테스트, exception 격리 테스트, stats merge 단위 테스트. mock LLMClient 의 `asyncio.sleep` jitter 추가. |
| `tests/test_eval/test_eval_search.py` _(신규 가능)_ | 추가 | `embed_fn` 외부 주입·공유 캐시 동작, sem cap 동작 (시간 허락 시). 최소 1~2 케이스. |

**총 변경 파일**: 스크립트 2 + 테스트 1~2 = **3 ~ 4 파일**.

---

## 12. 위험 / 미해결 (implementer 가 마주칠 결정점)

### 12.1 LLMClient 의 idempotency 가정

mock 이 아닌 실 endpoint 는 응답이 100% 결정적이지 않을 수 있다 (temperature > 0, 또는 endpoint 측 비결정성). 즉 결정성 회귀 테스트는 **mock 환경에서만 보장**.
- **권고**: 실 환경에서는 같은 시드여도 골드셋이 약간 달라질 수 있다는 점을 metadata 에 명시 (이미 generator.temperature 가 metadata 에 박혀 있음 — 추가 작업 없음).

### 12.2 `fail_demonstrative` 키 누락

§5.3 에서 언급. implementer 가 `filter_question.reason` 의 모든 enum 값을 stats 초기 키에 시드할지, dict.get 패턴에 맡길지 선택. **권고**: dict.get 패턴 유지 (현 코드 그대로).

### 12.3 `embed_fn` 캐시 race 와 비용

§7.1 의 중복 호출은 N 이 큰 환경 (N=16+) 에서 임베딩 비용을 측정 가능 수준으로 증가시킬 수 있음. 측정 후 필요 시 `asyncio.Lock` 또는 `asyncio.Event` 로 race 차단 검토.

### 12.4 ChromaDB / aiosqlite 동기 호출의 이벤트 루프 블로킹

analyst §3.3, §6.1 — ChromaDB `search` 는 동기. N 이 큰 환경에서 누적 블로킹 시간이 보이면 `asyncio.to_thread(vector_store.search, ...)` 로 감싸는 후속 작업 필요. 본 PR 범위 밖.

### 12.5 generator/judge 가 다른 endpoint 인 경우

Q10 의 결정은 "단일 cap". 두 endpoint 가 각자 rate limit 이 다르면 사용자가 보수적으로 작은 N 을 선택해야 함. metadata 에 endpoint 정보가 기록되어 있어 사후 진단 가능 (현 generator/judge metadata 보존).

### 12.6 다중 골드셋 평가 시 메모리

n_gold_sets × concurrency 의 task 동시 in-flight 는 골드셋 간 직렬이라 max(concurrency) 만큼만. 메모리 부담은 단일 골드셋 시나리오와 동일.

### 12.7 id 부여 시 chunk 와 graph 의 경계

§3.3 의 `next_id` 가 chunk 모드 종료 후 graph 모드 시작 전에 carry over. graph 모드 안에서도 같은 next_id 가 진전. 시드 일관성 보장 핵심 — implementer 는 chunk 모드 종료 시점에 `next_id` 가 어디까지 갔는지 확인하는 sanity assert 를 권고 (debug 빌드만).

---

## 변경 이력

- 2026-05-18: 초안 작성.
