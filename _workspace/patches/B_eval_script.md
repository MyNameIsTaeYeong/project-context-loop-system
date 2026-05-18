# B — Eval Script Patches

`_workspace/findings/SUMMARY.md` 의 평가 측 (Eval-Script-Patcher 책임) 패치 7건을
실제 워크트리에 적용한 결과 로그.

| ID | 위험 | 변경 파일 | 상태 |
|---|---|---|---|
| P2 | C1 (judge fall-through) | `scripts/eval_search.py` | applied |
| P3 | C1 보조 (role_is_configured) | `src/context_loop/eval/llm.py` | pre-existed (감사 시점 → 현 워크트리에 이미 반영됨). 정책 일치 확인. |
| P5 | C4 (judge_score=-1 평균 오염) | `scripts/eval_search.py` | applied |
| P6 | C3 (`_fetch_source_text` 첫 청크 fallback) | `scripts/eval_search.py` | applied |
| P8 | H4 (baseline/treatment 동치성) | `scripts/eval_search.py` + `scripts/compare_runs.py` (신규) | applied |
| P9 | H9 (실패 질의 silent drop) | `scripts/eval_search.py` | applied |
| P12 | H10 (`build_embed_fn` silent skip) | `src/context_loop/eval/graph_match.py` + `scripts/eval_search.py` | applied |

## P2 — judge fall-through 차단

**위치:** `scripts/eval_search.py:run()` Judge 클라이언트 생성 블록.

**변경 전:** Judge 가 별도로 구성되지 않으면 `logger.warning` 로 알리고 `judge = llm_client` 로 진행.

**변경 후:**
- `judge_configured = role_is_configured(...)` 가 `False` 일 때:
  - `--allow-self-judge` 가 있으면 self-judge 로 진행하고 `judge_is_self = True` 마킹.
  - 없으면 `SystemExit` 으로 즉시 종료. 메시지는 옵션 명시.
- `config_summary` 에 `judge_is_self: bool`, `allow_self_judge: bool` 두 키 추가.
- 새 CLI 옵션: `--allow-self-judge` (`store_true`, 기본 False) — 옵트인 플래그.

**호환성:** Judge 미사용(`--judge` 안 켬) 흐름은 영향 없음. Judge 분리 구성이 정상이면 옵션도 무관.

## P3 — role_is_configured 보강

**위치:** `src/context_loop/eval/llm.py` `role_is_configured`.

**현 상태:** 감사 시점 보고서의 `eval/llm.py:209-226` 와 달리 현 워크트리는 이미
`_effective_role_target` 헬퍼와 함께 system-equality 비교가 적용된 상태였다.
함수가 다음 정책을 만족하는지 확인:

- `(role_endpoint, role_model)` 의 최종 적용값을 CLI override > `config.eval.{role}.*` > `config.llm.*` 순으로 결정.
- system `(config.llm.endpoint, config.llm.model)` 과 정확히 동일하면 `False` 반환.
- 분리되지 않은 자기-참조 구성을 같은 모델 명시 케이스에서도 잡아냄.

**호환성:** 시그니처 동일 (`endpoint_override`, `model_override` keyword-only).
호출부(`eval_search.py`, `build_synthetic_gold_set.py`) 코드 변경 불필요. 단
정책이 더 엄격해져 `gold-set-build-patcher` 의 P1 (generator 분리 판정) 동작도
같은 기준으로 self-eval 을 판정한다.

## P5 — judge_score=-1 (parse_error) 분리

**위치:** `scripts/eval_search.py:evaluate_one()` 의 judge 호출부.

**변경 전:** `judge_answer` 가 `(-1, "parse_error")` 반환 시 그대로 `row["judge_score"] = -1`. `aggregate` 가 -1 을 평균에 포함 → 평균이 음수로 끌어내려질 수 있음.

**변경 후:**
- `score < 0` 이면 `row["judge_score"] = None` 으로 분리. `aggregate` 의 `isinstance(v, (int, float))` 가드는 None 을 자동 제외.
- `row["judge_parse_failed"] = True` 도 함께 기록.
- 정상 score 도 `judge_parse_failed = False` 로 명시 (분포 추적용).
- `write_summary` 에 다음 키 추가:
  - `judge_score_parse_failures: int`
  - `judge_score_success_count: int`

## P6 — _fetch_source_text fallback 추적 + judge skip

**위치:** `scripts/eval_search.py` `_fetch_source_text`, `evaluate_one`.

**변경 전:** anchor / chunk_id 매칭 실패 시 묵묵히 첫 청크를 반환. 잘못된 근거로 judge 채점 → 평균 오염.

**변경 후:**
- `_fetch_source_text` 의 반환을 `tuple[str, str]` 로 변경 — `(content, method)`.
- method 값: `"anchor" | "chunk_id" | "fallback_first_chunk" | "fallback_doc_first_chunk" | "empty"`.
- `evaluate_one` 은 method 가 `"fallback_*"` 또는 `"empty"` 이면 `judge_answer` 호출 skip:
  - `row["judge_score"] = None`
  - `row["judge_skip_reason"] = "source_fallback"`
- `row["source_fetch_method"]` 컬럼 추가 (모든 row).
- `write_summary` 에 `source_fetch_method_counts: dict`, `judge_skip_count: int` 추가.

## P8 — gold_set fingerprint + compare_runs.py

**위치:** `scripts/eval_search.py` `_evaluate_gold_set()` enriched_config + 신규 `scripts/compare_runs.py`.

**eval_search.py 변경:**
- `import hashlib`, `from collections import Counter` 추가.
- `_evaluate_gold_set` 에서 enriched_config 에 다음 키 추가:
  - `gold_set_sha256: str` — 골드셋 파일 SHA256
  - `gold_set_n_items: int`
  - `gold_set_generator_model: str` — `gold.metadata["generator_model"]`
  - `gold_set_judge_model: str` — `gold.metadata["judge_model"]`
  - `gold_set_self_evaluation_warning` — `gold.metadata["self_evaluation_warning"]` 원본 값 (없으면 None)
- 골드셋 파일 IO 실패 시 sha256 는 빈 문자열로.

**신규 `scripts/compare_runs.py`:**
독립 entrypoint. baseline/treatment 두 summary.json + 두 CSV 를 받아 동치성 검증과 paired 비교 산출.

핵심 기능:
1. **config 동치성** — `gold_set_sha256`, `embedding_model`, `llm_model`, `top_k`, `max_chunks`, `similarity_threshold`, `rerank_enabled`, `hyde_enabled` 가 모두 같은지 확인. 다르면 어느 키가 다른지 출력 + `--allow-config-mismatch` 없으면 exit code 2.
2. **paired diff** — 두 CSV 를 `id` 컬럼으로 inner join. 메트릭 컬럼은 `recall@`, `precision@`, `hit@`, `ndcg@`, `mrr`, `graph_*`, `judge_score`, `elapsed_ms` prefix 로 자동 선택. 한 쪽에 None 이 있으면 해당 셀 skip.
3. **통계** — paired Wilcoxon signed-rank (직접 구현, scipy 미의존; 정규근사 양측 p-value) + bootstrap 95% CI (1000 resample, seed=42).
4. **출력** — stdout 표 + `--out` 지정 시 JSON 저장.

stdlib 만 사용 (random, math, csv, json, hashlib). scipy 미의존.

### 사용법

```bash
python scripts/compare_runs.py \
    --baseline   eval/runs/baseline.summary.json \
    --treatment  eval/runs/multiview.summary.json \
    --baseline-csv  eval/runs/baseline.csv \
    --treatment-csv eval/runs/multiview.csv \
    --out eval/runs/compare_baseline_vs_multiview.json
```

종료 코드:
- `0` — 비교 완료 (config 일치 또는 --allow-config-mismatch).
- `2` — config 동치성 위반, --allow 없음.
- `3` — paired 항목 0 (id 매칭 실패).

## P9 — 실패 질의 명시

**위치:** `scripts/eval_search.py` `_process_item` except 블록 + asyncio.gather 사후 처리.

**변경 전:** error row 가 `{"id", "query", "error", "_idx"}` 만. `aggregate` 가 메트릭 키 없는 row 를 무시 → 실패가 silently 빠짐. summary 에는 표시 없음.

**변경 후:**
- error row 에 다음 키를 명시 채움:
  - `metric_failed: True`
  - 표준 메트릭 키 (`recall@k`, `precision@k`, `hit@k`, `ndcg@k`, `mrr`) 를 `None` 으로 명시.
- bare/외부 예외 catch 블록도 같은 패턴 적용.
- `write_summary` 에 다음 키 추가:
  - `n_failed: int`
  - `n_successful: int`
  - `failure_rate: float`
- `n_queries` 는 기존대로 전체 row 수 (실패 포함). 사용자는 summary 만 보고 분모를 재구성 가능.

## P12 — graph_t4_disabled 표기

**위치:**
- `src/context_loop/eval/graph_match.py` `build_embed_fn`.
- `scripts/eval_search.py` `evaluate_one`, `write_summary`.

**graph_match.py 변경:**
- `build_embed_fn` 이 반환하는 callable 에 두 속성 부착:
  - `t4_disabled: bool` — 클라이언트 없음 / 이벤트 루프 충돌로 async 실패한 적 있음.
  - `skip_count: int` — T4 임베딩 시도가 None 으로 떨어진 누적 횟수.
- `embedding_client is None` 분기는 t4_disabled=True 로 시작.
- async fallback 의 running-loop 충돌 경로는 state["t4_disabled"]=True 마킹.

**시그니처 호환:** 반환 타입 자체는 그대로 `EmbedFn = Callable[[str], list[float] | None]`. 속성은 부가. 기존 호출부(`match_entity_tiered`, `match_relation_tiered`) 영향 없음.

**eval_search.py 호출부:**
- `evaluate_one` 종료 직전 `getattr(embed_fn, "t4_disabled", False)` 검사 →
  True 면 `row["graph_t4_disabled"] = True` 기록.
- `write_summary` 에 다음 키 추가:
  - `graph_t4_disabled: bool` — 한 row 라도 disabled 면 True.
  - `graph_t4_skip_count: int` — disabled 마킹된 row 수.

## 협업 / 후속 영향

- **gold-set-build-patcher 의 P1 의존:** `gold_set_generator_model` /
  `gold_set_judge_model` / `gold_set_self_evaluation_warning` 은 골드셋
  metadata 에 P1 패치 후에만 채워진다. 그 전엔 모두 빈 문자열 / None 으로
  기록되며, compare_runs.py 동치성 검증의 `gold_set_sha256` 은 P1 무관하게 동작.
- **P3 정책 변경의 부작용:** `role_is_configured` 가 더 엄격해져 generator/judge 가
  system 과 같은 endpoint+model 명시 케이스를 self-eval 로 판정. build_synthetic_gold_set.py 의
  P1 (gold-set-build-patcher) 분기에서 같은 차단이 발생.
- **CLI 후방 호환:** P2 의 `--allow-self-judge` 는 옵트인. Judge 미사용 / 정상 분리 사용 흐름은 변화 없음. 그러나
  config.eval.judge / --judge-* 가 비어있고 `--judge` 만 켜서 system LLM 으로 자동 fallback 받던 기존 워크플로우는 깨진다 — 명시적 옵션 추가 필요.

## 보류 / 미적용 항목

해당 패처 책임 범위(P2/P3/P5/P6/P8/P9/P12) 외:
- P1, P4, P7, P10, P11, P13~P16 — `gold-set-build-patcher` 또는 별도 책임.
- Judge 프롬프트 reference-free 모드 분리 (F-2 의 권고) — 본 패치 범위 외.
- `assemble_context_with_sources` tie-breaker 확정 (H8) — 평가 측이 아닌 시스템 측.

## 검증 메모

- syntax 확인은 bash 실행 권한이 차단되어 직접 수행하지 못함. 정적 코드 리뷰로 다음을 확인:
  - 신규 import (`hashlib`, `Counter`) 가 파일 상단에 올바르게 배치.
  - 신규 키워드/속성이 기존 코드 흐름과 충돌하지 않음.
  - DictWriter 가 None 값을 빈 문자열로 직렬화하는 표준 동작에 의존.
- 실제 동작 검증은 사용자가 `python scripts/eval_search.py --gold-set ... --judge ...` 로 1회 회귀 실행 권장.
