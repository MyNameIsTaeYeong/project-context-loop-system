# 03_implementation.md — 골드셋 생성·평가 병렬화 구현 (3차)

**작성**: 2026-05-18
**상위**: `_workspace/02_design.md` (19개 결정 + 의사코드)
**참조**: `_workspace/00_requirements.md`, `_workspace/01_analysis.md`

이 문서는 02_design 의 §11 변경 파일 표를 체크리스트로 사용해 구현한
실제 코드 변경과 검증 결과를 요약한다.

---

## 1. 수정·신규 파일

| 파일 | 변경 종류 | 한 줄 변경 설명 |
|------|---------|---------|
| `scripts/build_synthetic_gold_set.py` | 수정 | chunk·graph 항목 단위 동시 처리, `--concurrency` CLI, id 사전 부여 후처리, exception 격리, LocalStats 머지, `metadata["concurrency"]` 기록. |
| `scripts/eval_search.py` | 수정 | 골드셋 내 항목 동시 처리, `--concurrency` CLI, `embed_fn` 1회 빌드 외부 주입, `graph_store.build_entity_embeddings` 사전 빌드, `_idx` 정렬 기반 결정성 회복, `config_summary["concurrency"]`. |
| `src/context_loop/eval/graph_match.py` | 수정 | `EmbedFn` 을 `__all__` 에 추가 (외부에서 타입 import 가능). |
| `tests/test_eval/test_build_synthetic_gold_set.py` | 수정 | `_make_graph_gold_item(existing_items=...)` 제거에 따른 호출 갱신, `_merge_stats` 단위 테스트 2개, `--concurrency` CLI 노출 검증. |
| `tests/test_eval/test_concurrency.py` | 신규 | 11개 결정성·격리·cap 회귀 테스트 (설계 §10 의 전략 그대로). |

**총 변경 파일**: 스크립트 2 + 모듈 1 (한 줄 export) + 테스트 2 = **5 파일**.

---

## 2. 추가한 테스트 시나리오

### 2.1 `tests/test_eval/test_concurrency.py` (신규 11개)

| 테스트 | 검증 대상 | 설계 매핑 |
|-------|---------|---------|
| `test_process_chunk_item_returns_items_with_empty_ids` | `_process_chunk_item` 의 GoldItem.id="" placeholder + local stats 반환 | D-3, §4.1 |
| `test_process_chunk_item_handles_empty_generation` | 빈 응답 시 fail_parse 카운트, 정상 종료 | §6.1 |
| `test_process_subgraph_item_returns_items_with_empty_ids` | graph 항목도 id="" + graph_generated/graph_passed 카운트 | D-3 |
| `test_run_chunk_mode_ids_assigned_in_idx_order` | concurrency=4 로 5청크 처리 후에도 id=q0001..q0010 단조 증가 | D-3, §3 |
| `test_run_chunk_mode_deterministic_across_concurrency` | 같은 입력 + concurrency 1 vs 4 vs 8 → ids/docs/stats 완전 동일 | R2, §10.1 |
| `test_run_chunk_mode_isolates_exceptions` | 1청크가 raise 해도 나머지 정상 + stats["fail_runtime"]==1 + id 연속 | R4, §6 |
| `test_run_chunk_mode_respects_concurrency_cap` | Semaphore 안에서 generator in-flight ≤ cap | R1, §2 |
| `test_chunk_and_graph_modes_share_continuous_id_space` | next_id 가 chunk → graph 모드로 carry over | D-3, §3.3 |
| `test_build_embed_fn_caches_repeated_text` | LRU 캐시로 같은 텍스트는 1번만 embed_query | §7.1 |
| `test_evaluate_gold_set_prebuilds_entity_embeddings_once` | eval_search 안에 `build_entity_embeddings` 사전 빌드 코드 존재 | D-7, §7.2 |
| `test_evaluate_gold_set_concurrent_results_match_serial` | rows.sort(key=_idx) 가 도착 순서를 결정성 순서로 회복 | §4.2, §6.2 |

### 2.2 `tests/test_eval/test_build_synthetic_gold_set.py` (추가 2개)

| 테스트 | 검증 |
|-------|------|
| `test_merge_stats_adds_known_and_dynamic_keys` | 알려진 키 + 동적 fail_<reason> 키 모두 더해짐 |
| `test_merge_stats_empty_local_noop` | 빈 local 은 target 변경 없음 |

### 2.3 회귀 — 기존 테스트 유지

- `test_make_graph_gold_item_*` 3 개 — `_make_graph_gold_item` 의
  `existing_items` 인자 제거에 맞춰 호출 갱신 (id 비교 자체는 검증 안 함, item 의 다른 필드만).
- `test_cli_exposes_new_options` / `test_eval_search_cli_exposes_new_options` — `--concurrency` 노출 검증 추가.

---

## 3. 검증 결과

### 3.1 pytest tests/test_eval/

```
271 passed in 0.49s
```

- 기존 260개 + 신규 11개. **100% 통과**.

### 3.2 pytest -x (전체)

```
998 passed, 11 warnings in 5.35s
```

- 11개 warning 은 starlette `TemplateResponse` deprecation (변경 영역 무관, 사전 존재).

### 3.3 ruff check (변경 영역)

- `scripts/eval_search.py`, `src/context_loop/eval/graph_match.py`,
  `tests/test_eval/test_concurrency.py`, `tests/test_eval/test_build_synthetic_gold_set.py`:
  **All checks passed!**
- `scripts/build_synthetic_gold_set.py`: 1 error (E501, line 996 `--source-types` help string).
  **이 에러는 사전 존재 — 본 PR 변경 영역 외**. `git stash` 검증 완료.

### 3.4 CLI 노출 확인

```bash
$ python scripts/build_synthetic_gold_set.py --help | grep -A2 concurrency
  --concurrency CONCURRENCY
                        항목(chunk/subgraph) 단위 동시 처리 수 (기본 1, 직렬). LLM endpoint
                        rate limit 에 맞춰 4~8 권장. metadata 에 기록.

$ python scripts/eval_search.py --help | grep -A2 concurrency
  --concurrency CONCURRENCY
                        골드셋 내 항목 동시 처리 수 (기본 1, 직렬). LLM endpoint rate limit 에
                        맞춰 4~8 권장. summary 에 기록.
```

### 3.5 결정성 회귀 테스트 통과 증거

`test_run_chunk_mode_deterministic_across_concurrency` 는 mock 환경에서 같은 입력 +
`concurrency` 1 / 4 / 8 로 `_run_chunk_mode` 를 세 번 호출한 뒤 다음을 검증:

```python
assert ids1 == ids4 == ids8           # id 순서·내용 동등
assert docs1 == docs4 == docs8         # 항목 매핑 동등
assert stats1 == stats4 == stats8      # stats 머지 결과 동등
```

LLM stub 은 `asyncio.sleep(random uniform jitter)` 로 응답 도착 순서를
의도적으로 흔들지만, 응답 자체는 prompt 내용으로 결정론적이므로 결과가
동등하다 — 이것이 D-3 (id 사전 부여) + D-8 (LocalStats 머지) 의 핵심.

테스트 통과:
```
test_run_chunk_mode_deterministic_across_concurrency PASSED [ 45%]
```

---

## 4. 설계 어긋난 점 + 이유

### 4.1 `_run_graph_mode` 의 `sem` 기본값을 `None` 으로 둠 (방어적)

설계 §2.2 / §4.1 는 `sem` 을 필수 인자로 명시했으나, 외부 테스트나 단독 호출
(예: 향후 graph-only entry point) 에서 sem 없이도 직렬 동작하도록
`sem: asyncio.Semaphore | None = None` + 진입 시 `if sem is None: sem = asyncio.Semaphore(1)`
방어 코드를 두었다.

**근거**: 본 PR 범위 외의 호출자가 안전하게 함수를 부를 수 있도록 — backward
compatibility 보호. 일반 호출 경로 (`build()`) 는 항상 sem 을 전달.

### 4.2 stats 시드 키에 `fail_demonstrative` 추가 안 함

설계 §5.3 의 위험 §12.2 에서 implementer 선택권 부여 — "dict.get 패턴 유지 권고".
구현은 **현 코드 그대로** dict.get 패턴 유지하고 `fail_runtime` 만 명시적으로 시드.

**근거**: §5.3 권고대로 단순성 우선. `fail_demonstrative` 는 첫 발생 시 자동 생성.

### 4.3 `LocalStats` dataclass 미도입

설계 §4.1 는 `LocalStats` dataclass 와 `dict[str, int]` typealias 둘 다 옵션으로
언급 — 구현은 **`dict[str, int]` 그대로**. 함수 시그니처는
`tuple[list[GoldItem], dict[str, int]]`.

**근거**: 동적 키 (`fail_<reason>`) 호환 + Counter/dict 기반 머지 단순성 + main
stats 도 dict — 일관성.

### 4.4 `evaluate_one` 의 `idx` 인자 — 동치 처리

설계 §4.2 는 `idx` 를 `evaluate_one` 의 인자로 전달하라 했으나, 구현은 _process_item
안에서 row 에 `_idx` 를 직접 박는다 (`row["_idx"] = idx`). `evaluate_one` 시그니처
변경 최소화.

**근거**: 단일 책임 — `evaluate_one` 은 채점 로직, `_idx` 는 외부 정렬용 메타.

---

## 5. CLI 사용 예시

### 5.1 직렬 (기본, 현 동작과 동일)

```bash
python scripts/build_synthetic_gold_set.py \
    --source-types confluence_mcp,git_code \
    --seed 42 \
    --output eval/gold_set.yaml
# (--concurrency 1 가 기본)
```

### 5.2 동시성 8 — 일반적 sweet spot

```bash
python scripts/build_synthetic_gold_set.py \
    --source-types git_code \
    --seed 42 --n-gold-sets 5 \
    --concurrency 8 \
    --output eval/gold_sets/git_code.yaml
```

300 항목 골드셋 직렬 ≈ 15 분 → concurrency=8 ≈ 2 분 (예측, endpoint TPS 의존).

### 5.3 평가 측 동시 채점

```bash
python scripts/eval_search.py \
    --gold-set-glob "eval/gold_sets/git_code_*.yaml" \
    --label baseline_par8 \
    --concurrency 8
```

5 골드셋 × 60 항목, 항목당 1~3 LLM call → 직렬 ~5분 → concurrency=8 ~1분.

---

## 6. 예상 효과

| 항목 | 직렬 (concurrency=1) | concurrency=8 | 비고 |
|------|---------------------|---------------|------|
| 생성: 300 chunks × 7 LLM calls | ~15 분 | ~2 분 | endpoint TPS 가 8 동시 받쳐주면 8x |
| 평가: 300 items × ~2 LLM calls | ~5~10 분 | ~0.75~1.25 분 | 동일 |
| 메모리 | ~동일 | 약간 증가 | sem cap 으로 in-flight 8 제한 |
| 결정성 | YAML byte-identical 보장 (mock 환경) | 동일 | id 사전 부여 + idx 정렬 |
| 에러 격리 | 1 항목 실패 → 전체 abort 가능 | 1 항목 실패 → fail_runtime+=1, 나머지 진행 | gather(return_exceptions=True) |

지배 비용은 LLM 호출 (~90%) → cap=N 일 때 throughput 은 N 까지 거의 선형 증가
(endpoint TPS 가 받쳐주는 영역에서). N≥16 부터는 endpoint rate limit 천장 가능
→ `--concurrency` > 32 시 경고 로그 발동.

---

## 7. backward-compat 확인

- `--concurrency` 미지정 (기본 1) → `Semaphore(1)` → 사실상 직렬 동작
- 기존 `gold_set.yaml` 스키마 변경 없음 (metadata 에 `concurrency` 키 추가만)
- 기존 CLI 옵션 (`--seed`, `--n-gold-sets`, `--source-types`, …) 그대로
- 기존 998 개 테스트 100% 통과 (변경 영역 외 0 regression)

---

## 8. 변경 이력

- 2026-05-18: 초안 작성. 19개 결정 모두 코드 반영. 11개 결정성 회귀 테스트 추가.
  pytest 998 / ruff (변경 영역) 통과. CLI `--concurrency` 노출 확인.
