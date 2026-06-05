# Eval-Script Auditor — 평가 스크립트 신뢰성 감사 (재감사)

## 한줄 판정

**MEDIUM RISK — 이전 감사의 Critical 4건 중 ① Judge self-fallback 차단(P2), ② judge_score=-1 평균 오염 제거(P5), ③ source fallback 의 judge skip(P6), ④ 실패 질의 명시적 drop(P9), ⑤ 골드셋 fingerprint(P8), ⑥ baseline↔treatment 동치성 + paired Wilcoxon + bootstrap CI 비교 도구(P8/compare_runs.py), ⑦ T4 임베딩 silent skip 표면화(P12) 가 모두 성실히 처리되었다. 새 도구 `compare_runs.py` 의 Wilcoxon signed-rank 직접 구현은 정규근사·w_plus 계산이 정확하며 부트스트랩 CI 도 시드 결정성이 보장된다. 다만 (a) tie-correction 미적용으로 동률이 많은 경우 p-value 가 약간 보수적, (b) Judge 프롬프트가 여전히 source chunk + retrieved context 를 동시에 노출하여 lexical-overlap 회귀 위험(F-2)이 잔존, (c) `assemble_context_with_sources` 의 doc-id dedup + similarity tie-breaker 비결정성(F-3, F-17)은 평가 측이 아닌 시스템 측 책임으로 미패치, (d) `mrr` 가 여전히 top_k slice 없이 retrieved 전체에서 계산(F-16)되어 라벨 의미 혼동이 남아 있다. 절대 수치 신뢰는 8/10, 비교 수치 신뢰는 7/10 으로 회복.**

## 검토 범위

| 파일 | 범위 | 줄수 | 패치 |
|---|---|---|---|
| `_workspace/source/metrics.py` | Recall/Precision/MRR/nDCG/Hit, aggregate, aggregate_with_variance | 172 | 없음 (회귀 확인) |
| `_workspace/source/eval_search.py` | 메인 평가 루프, judge 호출, CSV/Summary, multi-goldset aggregate | 1160 | P2, P5, P6, P8, P9, P12 |
| `_workspace/source/llm.py` | role(generator/judge) 별 LLM 클라이언트 빌더 + system equality 비교 | 259 | P3 (이미 적용 확인) |
| `_workspace/source/graph_match.py` | 4-tier cascade entity/relation 매칭, embed_fn t4_disabled 속성 | 569 | P12 |
| **신규** `_workspace/source/compare_runs.py` | config 동치성 + paired Wilcoxon + bootstrap CI | 461 | P8 신규 |

## 핵심 발견 (위험 등급순)

| # | 발견 | 위험 등급 | 위치 | 이전 등급 |
|---|---|---|---|---|
| F-1 | Judge 프롬프트 (`JUDGE_PROMPT_TEMPLATE`) 가 여전히 source chunk + retrieved context 를 동시 노출하여 lexical overlap 채점 회귀 위험. P2 가 self-fallback 은 차단했지만 프롬프트 구조는 미변경. | **High** | `eval_search.py:90-111` | Critical (F-2) |
| F-2 | `assemble_context_with_sources` 의 doc-id dedup + similarity tie-breaker 시 도착 순서 의존 → 동률 시 비결정 가능. 평가 측이 아닌 시스템 측 책임으로 본 패치 사이클에서 미터치. | **High** | `eval_search.py:211` + 시스템 RAG | High (F-3) |
| F-3 | `mrr` 가 여전히 `retrieved_doc_ids` 전체 (top_k slice 없음) 에서 계산. 라벨이 `"mrr"` 인데 실제로는 `"mrr@max_chunks"`. compare_runs.py 도 같은 키로 paired 비교 수행 — 의미는 일관되지만 라벨 혼동. | **Low** | `eval_search.py:239`, `metrics.py:50-59` | Low (F-16, 동일) |
| F-4 | Wilcoxon p-value 에 tie correction 미적용. 동률(같은 |Δ|) 그룹이 큰 경우 분산이 과대평가되어 p-value 가 보수적 — Type I error 감소 방향이라 안전하지만, 검정력이 떨어짐. | **Medium** | `compare_runs.py:172-191` | 신규 (P8) |
| F-5 | Wilcoxon 정규근사를 `n` 무관하게 항상 사용. n<6 시 정확도 낮음 — docstring 에는 "호출부가 경고"라 적혔으나 `run()` 에서 실제 경고를 띄우지 않음. compare 결과에 작은 N 경고 부재. | **Medium** | `compare_runs.py:172, 333-415` | 신규 (P8) |
| F-6 | `bootstrap_ci` 의 percentile index 가 `int((alpha/2)*n_resample)` — `int()` 가 floor 라 0-base 인덱스 매핑은 표준이지만, `hi_idx = int(0.975 * 1000) = 975` 가 percentile 97.5% 의 정확한 인덱스. n=1000 에서 0.025·1000=25, 0.975·1000=975 (1000-25=975 와 일치) → 무편향. ✅ 작은 n_resample (예: 100) 에서 percentile 가 약간 어긋날 수 있으나 운영 디폴트 1000 이라 무시 가능. | OK | `compare_runs.py:220-222` | 신규 |
| F-7 | `EQUIVALENCE_KEYS` 가 9개 핵심 키만 비교. `concurrency`, `judge_is_self`, `allow_self_judge`, `graph_match_threshold`, `score_relations`, `gold_set_generator_model`, `gold_set_judge_model`, `gold_set_self_evaluation_warning` 등은 정책상 영향을 줄 수 있는데 동치성 검증에서 빠짐. 사용자가 self-judge 한 결과를 정직-judge 결과와 비교해도 자동 검출 안됨. | **Medium** | `compare_runs.py:42-51` | 신규 (P8) |
| F-8 | `_select_metric_columns` 가 `r.get(c)` 결과를 단순 prefix 매칭. `mrr` prefix 가 `mrr` 자체뿐 아니라 `graph_mrr`, `graph_rel_mrr` 도 매칭 — 중복은 `seen` 으로 막혀 안전하지만 prefix 매칭 정의가 느슨하다. `graph_mrr` 가 별도 prefix 로 등록돼 있어 이중 매칭 안 됨. ✅ 단, 향후 prefix 추가 시 주의 필요. | OK | `compare_runs.py:55-68, 230-245` | 신규 |
| F-9 | `wilcoxon_p_value` 가 `var == 0.0` 가드는 있으나 **모든 nonzero diff 가 동일 부호 + 동일 절대값** 일 때 z 가 0 이 되어 p=1.0 으로 보고. 사실 이런 경우는 매우 강한 시그널인데 정규근사가 부적합. 의도된 conservative 처리이지만 사용자가 결과를 신뢰할 수 없음. | **Low** | `compare_runs.py:184-191` | 신규 |
| F-10 | `paired_diff` 의 `n_skipped` 가 **(질의 × 메트릭) 셀 단위** 카운트. 사용자 입장에서 "몇 명이 skip 됐는가" 와 "몇 셀이 skip 됐는가" 가 헷갈림. JSON 출력에 `n_skipped_cells` 로 이름 명확화는 했으나 stdout 출력에는 표시 안됨. | **Low** | `compare_runs.py:248-277, 401` | 신규 |
| F-11 | `recall@k` 분모 `len(rel_set)`. 표준 정의대로지만 retrieved 가 dedup 으로 짧아지면 recall 의 상한이 작아짐 — 출력에 `max_possible_recall` 표시 없음. | **Low** | `metrics.py:23-32` | Low (F-11, 동일) |
| F-12 | `aggregate_with_variance` 가 n=1 시 std=0.0. NaN 권장 (사용자 오해). | **Low** | `metrics.py:159-164` | Low (F-15, 동일) |
| F-13 | `graph_t4_disabled` 가 row 에 True 인 항목만 기록. `getattr(embed_fn, "t4_disabled", False)` 가 **None을 한 번이라도 반환했을 때** True 인지, 또는 단순히 클라이언트 부재 상태인지 — 둘 다 True. `_disabled` 함수 경로(클라이언트=None)는 t4_disabled=True 로 시작. `_cached` 함수 경로(클라이언트 존재)에서 running-loop 충돌 시 `state["t4_disabled"]=True` 만 set, `_cached.t4_disabled` 속성 업데이트는 `if state["t4_disabled"]` 분기 안에서 → **첫 번째 None 호출에서 정확히 동기화됨**. ✅ 다만 graph entity 가 0 개라 T4 가 호출조차 안 된 경우엔 t4_disabled=False 로 남아 "사실 disabled 이지만 영향 없음"이 row 에 안 보임 — 운영상 문제 없음. | OK | `graph_match.py:175-237`, `eval_search.py:341-344` | 신규 (P12) |
| F-14 | `aggregate` 가 메트릭 키 None 을 자동 스킵 (P9 의 의도된 동작). 그러나 `metric_failed=True` row 가 일부 메트릭만 None 인 경우 (혹시 `evaluate_one` 중간에 일부 메트릭은 계산됐는데 None 으로 패치되지 않은 경우) 평균에 부분 기여. 현 패치는 `except` 분기에서 `evaluate_one` 호출 전체를 감싸 row 를 통째로 None 메트릭으로 채우므로 안전. ✅ 다만 `judge_score` 는 evaluate_one 안에서 별도 처리되어 None 가능 → judge_score 평균 분모는 다른 메트릭과 다름 — `judge_score_success_count` 보고가 있어 추적 가능. | OK | `eval_search.py:734-749`, `metrics.py:115-122` | 신규 (P9) |
| F-15 | `_fetch_source_text` 의 anchor 매칭이 `startswith` 인데 `_normalize_for_anchor` 가 whitespace 단일화만 함. 청크가 anchor 와 거의 같지만 첫 글자에 BOM, zero-width space 등이 끼면 anchor 매칭 실패 → `fallback_first_chunk` → judge skip. judge 데이터가 줄어드는 방향이라 안전하지만, 골드셋 빌더가 정확히 같은 정규화를 쓰지 않으면 fallback 율이 부풀어 judge 표본이 작아질 수 있음. summary 의 `source_fetch_method_counts` 로 추적 가능 ✅. | **Low** | `eval_search.py:365-409` | 신규 (P6 부산물) |
| F-16 | 관계 매칭 `match_relation_tiered` 의 T4 임베딩 단계가 source/target lower 정확 일치 강제 (T1 과 동일 키 비교). 관계의 entity 변형은 alias/normalize tier 까지 흡수 못함. 의도된 설계인지 docstring 불명확. | **Medium** | `graph_match.py:409-454` | Medium (F-14, 동일) |
| F-17 | `mrr` 같이 top_k 의존 없는 메트릭과 `recall@k`, `precision@k` 가 한 row 에 공존 — `compare_runs.py` 의 metric prefix 매칭이 `mrr` 와 `graph_mrr`, `mrr@`, `graph_mrr@` 등을 둘 다 자연스럽게 잡지만, prefix `"mrr"` 가 `mrr_something` 도 잡을 위험 — `seen` set 으로 중복 방지는 됨. metric prefix 가 향후 확장 시 정확한 동치 키나 정확 prefix 매칭으로 변경 권고. | **Low** | `compare_runs.py:55-68` | 신규 |
| F-18 | `Score_raw` 가 `True/False` 인 경우 (LLM이 boolean 출력) `isinstance(score_raw, (int, float))` 가 True 가 되어 `int(True)=1` 로 캐스팅됨. 골드셋 평가에 거의 안 일어나는 edge case 지만 명시적으로 `bool` 배제 권고. | **Low** | `eval_search.py:144-147` | 신규 |

## 차원별 상세 점검

### 1. 메트릭 구현 정확성 (변경 없음 — 회귀 확인)

`metrics.py` 가 P 패치 사이클에서 변경되지 않았고, `aggregate` 의 None-자동-스킵 거동(`isinstance(v, (int, float)) and not isinstance(v, bool)`)이 P5/P9 가 의도한 분리 동작과 정확히 정합함을 확인.

#### Recall@k (`metrics.py:23-32`)
```python
rel_set = set(relevant)
if not rel_set: return 0.0
top_k = set(retrieved[:k])
return len(top_k & rel_set) / len(rel_set)
```
표준 `|R∩T_k|/|R|`. ✅ 회귀 없음.

#### Precision@k (`metrics.py:35-47`)
표준 `|R∩T_k|/k`. ✅ 회귀 없음.

#### MRR (`metrics.py:50-59`)
표준 정의. 호출 측 (`eval_search.py:239`) 가 여전히 top_k slice 없이 retrieved_doc_ids 전체에 넘김 → F-3. 이전 감사 F-16 그대로.

#### nDCG@k (`metrics.py:62-84`)
표준 binary DCG/IDCG. log2 base, idcg=0 가드. ✅

#### Hit@k (`metrics.py:87-90`)
표준. ✅

#### 그래프 메트릭 (`eval_search.py:242-265` + `graph_match.py:330-390`)
이전 감사와 동일 — 4-tier cascade, retrieved_keys_in_rank_order 는 매칭 성공만 포함. precision 분모는 항상 k → 매칭 실패한 retrieved 가 noise 로 카운트 안 됨 (이전 F-13 의 변종, 미패치).

#### `aggregate` 의 None 처리
P9 패치가 실패 row 의 메트릭 키를 `None` 으로 명시 채움 → `aggregate` 가 `isinstance(v, (int, float))` 가드로 자동 제외. **이전 감사의 F-7 (실패 silently drop) 해결.** Summary 에 `n_failed`, `n_successful`, `failure_rate` 보고 추가 → 명시적 운영. ✅

### 2. top-k 선정 / tie-breaker (미패치 영역)

이전 감사의 F-3, F-17 잔존. `eval_search.py:211` 의 `[s.document_id for s in assembled.sources]` 가 여전히 dedup 된 리스트를 받고, `assemble_context_with_sources` 의 동률 처리는 시스템 측 책임이라 본 패치 사이클 범위 밖.

**영향:**
- doc 단위로 dedup 되어 retrieved_doc_ids 길이가 top_k 보다 짧을 수 있음 → precision 분모는 k 고정이라 인위적으로 깎임.
- 동률 시 vector store 도착 순서 의존.

**패치 보고서 명시:** "P8 외 → assemble_context_with_sources tie-breaker 확정(H8) — 평가 측이 아닌 시스템 측" → 의도된 보류. ✅ (다만 절대 수치 신뢰에는 영향 있음.)

### 3. Judge 채점 메타-편향 (P2, P5 효과 검증) ★

#### 3-a. 자기-평가 폴백 차단 (P2)

**이전:** `--judge` + judge 미구성 → warning 만 찍고 `judge = llm_client` 로 진행 (자기-평가 편향).

**현재 (`eval_search.py:844-872`):**
```python
if args.judge:
    judge_configured = role_is_configured(
        config, "judge",
        endpoint_override=args.judge_endpoint,
        model_override=args.judge_model,
    )
    if judge_configured:
        judge = build_eval_llm_client(...)
    elif args.allow_self_judge:
        logger.warning(...)
        judge = llm_client
        judge_is_self = True
    else:
        raise SystemExit(...)
```

✅ **유의 사항:**
1. `role_is_configured` 가 `_effective_role_target` 으로 CLI override → `config.eval.judge.*` → `config.llm.*` 우선순위를 정확히 따른다. system endpoint+model 과 정확히 같으면 self-eval 로 판정 (`llm.py:235-259`). 이는 P3 의 강화된 정책 (단순히 키가 채워졌는지가 아닌, 실효값 비교) 과 정확히 정합.
2. `config_summary["judge_is_self"]` 와 `["allow_self_judge"]` 가 summary JSON 에 기록되어 후행 감사 가능 (`eval_search.py:905-906`).
3. `--allow-self-judge` 가 옵트인 — CI 에서 우연히 자기-평가하지 않음. 명시 의도 표명 시만 허용.

**잔여 위험:** 사용자가 `--judge-endpoint <system_endpoint> --judge-model <system_model>` 처럼 시스템 값을 그대로 명시한 경우, `_effective_role_target` 비교가 시스템과 같다고 판정 → `role_is_configured=False` → 차단됨. ✅ 정확히 의도대로 동작.

**이전 감사 F-1 (Critical) → 해결 (등급 강하).** judge_is_self 가 summary 에 기록되어 후행 감사도 가능.

#### 3-b. Judge 프롬프트 (미패치 — 잔여 위험)

`JUDGE_PROMPT_TEMPLATE` (`eval_search.py:90-111`) 가 여전히 source chunk + retrieved context 를 동시에 노출. 패치 보고서 "보류" 섹션에 "Judge 프롬프트 reference-free 모드 분리(F-2 의 권고) — 본 패치 범위 외" 로 명시.

**잔여 위험:** Judge LLM 이 의미 평가 대신 lexical overlap 평가로 회귀할 수 있음. 측정 자체가 보수적이라 안전한 방향이지만, treatment 가 다른 표현으로 같은 의미를 담은 경우 점수가 낮게 나옴.

이전 감사 F-2 (Critical) → 잔여 High (F-1 in 본 감사).

#### 3-c. judge_score=-1 분리 (P5)

**이전:** parse_error 시 `score=-1` 이 row 에 그대로 → `aggregate` 가 -1 을 평균에 포함 → 평균 음수 가능.

**현재 (`eval_search.py:331-339`):**
```python
if score < 0:
    row["judge_score"] = None
    row["judge_reason"] = reason
    row["judge_parse_failed"] = True
else:
    row["judge_score"] = score
    row["judge_reason"] = reason
    row["judge_parse_failed"] = False
```

✅ `None` 이 `aggregate` 자동 스킵 (isinstance 가드). `judge_parse_failed` 가 분포 추적용. `write_summary` 가 `judge_score_parse_failures` 와 `judge_score_success_count` 보고 (`eval_search.py:530-531`).

**이전 감사 F-18 (High) → 해결.**

#### 3-d. source fallback judge skip (P6)

**이전:** `_fetch_source_text` 가 anchor/chunk_id 실패 시 묵묵히 첫 청크 반환 → 잘못된 근거로 judge 채점.

**현재 (`eval_search.py:370-409` + `316-322`):**
```python
async def _fetch_source_text(...) -> tuple[str, str]:
    ...
    return content, "anchor"  # 또는 "chunk_id" / "fallback_first_chunk" / "fallback_doc_first_chunk" / "empty"

# evaluate_one
if judge is not None:
    if source_method.startswith("fallback_") or source_method == "empty":
        row["judge_score"] = None
        row["judge_skip_reason"] = "source_fallback"
        row["judge_parse_failed"] = False
    else:
        score, reason = await judge_answer(...)
        ...
```

✅ fallback 시 judge 호출조차 안 함 — 비용 절감 + 평균 오염 방지. `source_fetch_method` 컬럼이 모든 row 에 기록되어 분포 추적 가능. summary 에 `source_fetch_method_counts` 와 `judge_skip_count` 보고.

**신규 위험 (F-15):** anchor 매칭 정규화가 whitespace 만 단일화 → BOM, zero-width 가 살아남으면 fallback 율 부풀려져 judge 표본이 작아짐. Low.

#### 3-e. Judge 분산 측정

여전히 단일 호출만. 분산 측정 부재 — 본 패치 사이클 범위 밖.

### 4. 통계 / 변동성 처리 (P8: compare_runs.py) ★

#### 4-a. paired Wilcoxon signed-rank 구현 검증

`compare_runs.py:146-191` 의 직접 구현을 표준 정의와 대조:

**`_signed_rank_statistic`:**
1. `nonzero = [d for d in diffs if d != 0.0]` — Wilcoxon 표준 (zero exclusion, "zero_method='wilcox'" in scipy).
2. `abs_vals = sorted(((abs(d), i) for i, d in enumerate(nonzero)), key=lambda x: x[0])` — 절대값 오름차순 정렬, 동률 시 인덱스 순서로 안정. ✅
3. tie group 처리:
   ```python
   while j + 1 < len(abs_vals) and abs_vals[j + 1][0] == abs_vals[i][0]:
       j += 1
   avg_rank = (i + j) / 2.0 + 1.0  # 1-based
   ```
   동률 그룹 `[i..j]` 의 평균순위 = `((i+1) + (j+1)) / 2 = (i+j)/2 + 1`. **정확.**
4. `w_plus = sum(ranks[idx] for idx, d in enumerate(nonzero) if d > 0)` — 양수 차이의 순위합. **정확.**

**수동 검증 (diffs = [-2, -1, 1, 1, 3]):**
- nonzero indices [0..4], abs sorted: `[(1,1),(1,2),(1,3),(2,0),(3,4)]`
- tie group at positions 0..2: avg_rank = (0+2)/2+1 = **2.0** → ranks[1]=ranks[2]=ranks[3]=2.0
- position 3 alone: avg_rank = (3+3)/2+1 = **4.0** → ranks[0]=4.0
- position 4 alone: avg_rank = (4+4)/2+1 = **5.0** → ranks[4]=5.0
- w_plus = ranks[2]+ranks[3]+ranks[4] = 2+2+5 = **9.0**
- 수동 scipy 동작과 일치. ✅

**`wilcoxon_p_value`:**
- `mean = n*(n+1)/4` — 정확 (귀무가설 하 W+ 의 기댓값).
- `var = n*(n+1)*(2n+1)/24` — 표준 정규근사 분산 식 (no tie correction).
- `z = (w_plus - mean) / sqrt(var)`
- `p = 2*(1 - Phi(|z|))` — 양측. `_standard_normal_cdf` 가 `math.erf` 기반 — 정확. ✅

**F-4 (Medium):** **tie correction 미적용.** 표준 식:
```
var_corrected = n*(n+1)*(2n+1)/24 − sum_over_tie_groups(t_i^3 - t_i)/48
```
동률이 많을 때 보정 안 하면 분산 과대 → z 작아짐 → p 보수적 (Type I error ↓, 검정력 ↓). 메트릭 평가의 경우 hit@k 같은 0/1 메트릭은 |diff| 가 0/1 로 제한되어 동률이 매우 많음 — 이 경우 영향 큼. 운영상 큰 영향은 보수적이라 안전한 방향이지만, 실제 의미 있는 개선이 p>0.05 로 잘못 read 될 위험.

**F-5 (Medium):** **작은 N 경고 부재.** docstring (`compare_runs.py:174-175`) 에 "n<6 이면 정규근사 정확도가 낮으므로 호출부가 경고를 띄울 것" 명시되었으나 `run()` 함수가 실제 경고를 띄우지 않음. JSON 출력의 `n` 필드로만 확인 가능.

#### 4-b. bootstrap CI 구현 검증

`compare_runs.py:199-222`:
```python
rng = random.Random(seed)
for _ in range(n_resample):
    sample = [diffs[rng.randint(0, n - 1)] for _ in range(n)]
    means.append(sum(sample) / n)
means.sort()
lo_idx = max(0, int((alpha / 2.0) * n_resample))
hi_idx = min(n_resample - 1, int((1.0 - alpha / 2.0) * n_resample))
return sum(diffs) / n, means[lo_idx], means[hi_idx]
```

✅ **시드 결정성:** `random.Random(seed)` 인스턴스로 글로벌 random 오염 없음. seed=42 디폴트, CLI `--seed` 로 override. compare 도구를 재실행하면 같은 (baseline, treatment) 에 같은 CI.

✅ **percentile 인덱스:**
- alpha=0.05, n_resample=1000 → lo_idx = int(0.025*1000) = 25, hi_idx = int(0.975*1000) = 975
- 1000 개 정렬된 리스트에서 means[25] 가 2.5% percentile (0-base 인덱싱), means[975] 가 97.5% — 정확.
- `max(0, ...)`, `min(n_resample-1, ...)` 가드 안전.

⚠️ **return value 첫 항목 `sum(diffs)/n` 은 부트스트랩 평균이 아닌 관찰 평균** — JSON 출력의 `mean` 키 의미를 헷갈릴 수 있음. docstring 에는 "Returns: (mean, lower, upper)" 로 적혀 mean 이 무엇인지 불명확. 운영상 둘 다 동등 수렴하지만 명시적 표기 권고.

#### 4-c. 다중 골드셋 aggregate (`aggregate_with_variance`)

미변경. n=1 시 std=0.0 (F-12 잔존). 다중 잡 동치성 검증은 compare_runs 의 책임이지만 multi-goldset 모드는 한 라벨 안에서만 동일 config 사용이 구조 보장 — 라벨 간 비교에서만 compare_runs 가 필요.

#### 4-d. 통계 검정 완성도

| 검정 | 구현 | 비고 |
|---|---|---|
| paired Wilcoxon signed-rank (양측) | ✅ 직접 구현 (정확, but no tie correction) | 정규근사 + erf |
| Bootstrap 95% CI | ✅ 1000회 resample, 시드 결정 | percentile 방법 |
| paired t-test | ❌ 미구현 | 정규성 가정 안전하지 않아 의도된 제외 추정 |
| 단측 검정 | ❌ 양측만 | treatment > baseline 가설 시 절반 p 가능, 별도 옵션 권고 |
| BH/Bonferroni 보정 | ❌ 다중 메트릭 비교 시 family-wise error 미보정 | recall@5, precision@5, mrr, ndcg@5, ... 여러 메트릭을 동시에 보면 false positive 누적. 사용자가 "어느 메트릭이라도 p<0.05" 식 결정하면 위험. |

**이전 감사 F-4 (High: 통계 강제 안 됨) → P8 의 compare_runs.py 로 부분 해결.** 다만 N<5 경고 누락(F-5)과 다중 비교 보정 부재가 잔존.

### 5. 출력 / 감사 추적성 (P5, P6, P8, P9)

#### Per-question CSV
- 메트릭 + 정답 doc + retrieved + judge_score(+reason) + source_fetch_method + judge_skip_reason + graph_t4_disabled 모두 기록. ✅
- `metric_failed=True` row 에는 error 만 있고 메트릭은 모두 None — CSV 가 빈 셀로 직렬화. compare_runs 의 `_try_float("") -> None` 가 자동 처리. ✅

#### Summary JSON (P8 + P9 + P12)
신규 키:
- `n_failed`, `n_successful`, `failure_rate` (P9)
- `judge_score_parse_failures`, `judge_score_success_count`, `judge_skip_count` (P5/P6)
- `source_fetch_method_counts` (P6)
- `graph_t4_disabled`, `graph_t4_skip_count` (P12)
- `judge_is_self`, `allow_self_judge` (P2)
- `gold_set_sha256`, `gold_set_n_items`, `gold_set_generator_model`, `gold_set_judge_model`, `gold_set_self_evaluation_warning` (P8)

✅ **이전 감사 F-6 (Medium: 골드셋 fingerprint 누락) 해결.**

⚠️ 코드 빌드 정보(git commit hash) 누락 — 시스템 측 RAG 코드 버전 추적 불가. 권고 잔존.

#### 실패 질의 (P9)
- `n_queries` = 전체 row 수 (실패 포함).
- `n_failed`, `n_successful` 분리 보고.
- `aggregate` 가 None 자동 스킵 → 메트릭 분모는 성공만.
- 사용자가 `n_successful` 과 메트릭 평균을 곱해 분자 reconstruct 가능. ✅

**이전 감사 F-7 (High: silent drop) → 해결.**

### 6. 실행 안정성 (P12)

#### Timeout / retry
- 여전히 부재. `evaluate_one` 에 `asyncio.wait_for` 없음. 패치 보고서에 명시되지 않음 — 미패치.

#### Concurrency
- `asyncio.Semaphore(max(1, args.concurrency))`. concurrency>32 경고. 이전과 동일.

#### `build_embed_fn` silent skip 표면화 (P12)

**이전 (`graph_match.py:182-199`):** 이미 실행 중인 이벤트 루프에서 async embedding 만 가진 client 의 경우 None 반환 → T4 단계 silent skip.

**현재 (`graph_match.py:175-237`):**
```python
if embedding_client is None:
    def _disabled(_t: str) -> list[float] | None:
        _disabled.skip_count += 1
        return None
    _disabled.t4_disabled = True
    _disabled.skip_count = 0
    return _disabled

# 동기/비동기 분기에서
state = {"t4_disabled": False, "skip_count": 0}
def _call(text: str) -> list[float] | None:
    ...
    try:
        asyncio.get_running_loop()
        logger.warning(...)
        state["t4_disabled"] = True
        return None
    except RuntimeError:
        return list(asyncio.run(coro))

def _cached(text: str) -> list[float] | None:
    ...
    if emb is None and text:
        _cached.skip_count += 1
        if state["t4_disabled"]:
            _cached.t4_disabled = True
    return emb
```

호출부 (`eval_search.py:341-344`):
```python
t4_disabled = bool(getattr(embed_fn, "t4_disabled", False))
if t4_disabled:
    row["graph_t4_disabled"] = True
```

✅ **이전 감사 F-10 (High) → 해결.**

⚠️ **사소한 검증 포인트:**
1. `_disabled` 함수 경로(client=None)는 시작부터 `t4_disabled=True`. 호출하기 전부터 True 라 row 에 반영됨. ✅
2. `_cached` 함수 경로에서 running-loop 충돌 시 `state["t4_disabled"]=True` 가 set 되지만 `_cached.t4_disabled` 속성 업데이트는 `if state["t4_disabled"]` 조건 안에 있음 → **다음 _cached() 호출에서 동기화됨**. 만약 첫 호출이 None 반환 후 평가가 즉시 끝나면 `_cached.t4_disabled` 가 동기화 안 될 가능성. 다만 `state["t4_disabled"]=True` 가 set 된 이후 _cached 가 다시 호출되면 `emb=None` (다음 텍스트도 같은 이유로 실패) → `_cached.skip_count += 1` + 동기화. 운영상 거의 항상 동기화되지만 **단 한 번만 호출되고 끝나는 케이스에서 누락 가능**.
3. CSV 컬럼 `graph_t4_disabled` 는 True 인 row 에만 추가 → CSV 헤더가 일관되도록 모든 row 에 default False 명시 권고 (현재는 비어 있는 셀로 채워짐 — `r.get(k, "")` 이라 OK).

#### 재현성
- LRU 캐시는 `dict` + `list` 직접 구현 — 이전 F-9 와 동일 (미패치). 단일 thread asyncio 환경 가정상 안전. concurrency>1 시 async 컨텍스트 스위치 간 `order.pop(0)` race 가능성 잠복하지만 운영 영향 낮음.

### 7. 라벨링 / 비교 (P8 신규 도구) ★

#### compare_runs.py 의 동치성 검증 (`check_equivalence`)

```python
EQUIVALENCE_KEYS = (
    "gold_set_sha256",
    "embedding_model",
    "llm_model",
    "top_k",
    "max_chunks",
    "similarity_threshold",
    "rerank_enabled",
    "hyde_enabled",
)
```

✅ **9 개 핵심 키 비교.** 다르면 `(key, baseline_value, treatment_value)` 리스트 반환. `run()` 에서 mismatch 시 stderr 에 보고 + `--allow-config-mismatch` 없으면 exit 2.

✅ **`gold_set_sha256`** — eval_search 가 enriched_config 에 기록한 골드셋 파일 해시. 같은 골드셋에서 실행됐는지 자동 검증. **이전 감사 F-5 (High) → 해결.**

**F-7 (Medium) 잔여 위험:** 다음 키가 동치성 검증에서 빠짐:
- `concurrency`, `judge_is_self`, `allow_self_judge`, `graph_match_threshold`, `graph_match_strict`, `score_relations`, `include_graph`, `judge_enabled`, `judge_model`
- `gold_set_generator_model`, `gold_set_judge_model`, `gold_set_self_evaluation_warning` (P8 이 enriched_config 에 추가했지만 EQUIVALENCE_KEYS 에는 없음)

→ 사용자가 self-judge 한 run 을 정직-judge run 과 비교해도 자동 차단 안 됨. `allow_self_judge` 차이는 평가 자체에는 영향 없으나 judge_score 비교 시 의미 다름. `graph_match_threshold` 가 다르면 graph_recall 등이 직접적으로 변함.

**권고:** EQUIVALENCE_KEYS 에 `include_graph`, `graph_match_threshold`, `graph_match_strict`, `score_relations`, `judge_is_self` 추가. `concurrency` 는 메트릭 자체에는 영향 없으니 별도 "정보 표시" 카테고리로.

#### paired 비교 (`paired_diff`)

```python
by_id_b = {r.get("id"): r for r in baseline_rows if r.get("id")}
by_id_t = {r.get("id"): r for r in treatment_rows if r.get("id")}
common_ids = sorted(set(by_id_b.keys()) & set(by_id_t.keys()))
```

✅ **id 컬럼 기준 inner join.** sorted 로 결정성 보장.

**메트릭 컬럼 선택:**
```python
cols_b = _select_metric_columns(baseline_rows)
cols_t = _select_metric_columns(treatment_rows)
metric_cols = [c for c in cols_b if c in cols_t]
```
✅ 양쪽 모두에 있는 메트릭만 비교. order 는 baseline 의 등장 순서 보존.

**셀 None 처리:**
```python
vb = _try_float(rb.get(c))
vt = _try_float(rt.get(c))
if vb is None or vt is None:
    n_skipped += 1
    continue
diffs[c].append(vt - vb)
```
✅ 한 쪽이라도 None 이면 해당 메트릭 cell skip — paired 원칙 준수.

#### 출력
- stdout 표 + JSON 저장 (`--out`). ✅
- exit code: 0(정상), 2(config mismatch), 3(paired N=0). ✅

#### 잔여 위험
- **F-9, F-10** (Low): 양측 검정만, percentile 인덱스 표현 명확화, n_skipped 단위.

**이전 감사 F-5 (High) → 해결.**

## 종합 위험 매트릭스 (재감사 후)

| 차원 | Critical | High | Medium | Low | OK |
|---|---|---|---|---|---|
| 1. 메트릭 식 | — | — | F-16 | F-3, F-11, F-12 | nDCG, Recall, Precision |
| 2. top-k / tie | — | F-2 | — | — | — |
| 3. Judge 편향 | — | F-1 | — | F-15, F-18 | self-fallback 차단(P2), score=-1 분리(P5), source skip(P6) |
| 4. 통계 | — | — | F-4, F-5 | F-9, F-10 | Wilcoxon 직접 구현 정확, bootstrap CI 결정성 |
| 5. 추적성 | — | — | — | — | gold_set_sha256(P8), 실패 명시(P9), judge 분포(P5/P6) |
| 6. 안정성 | — | — | — | F-13 | t4_disabled 표면화(P12) |
| 7. 라벨 비교 | — | — | F-7 | — | compare_runs.py 동치성 + paired(P8) |

**전체 신뢰도 평가 (재감사 후):**
- 메트릭 함수 수준 정확도: **A** (식 자체는 정확, P 사이클로 변경 없음, None 자동 스킵 정합 확인)
- 시스템 수준 신뢰도: **B** (judge self-fallback 차단, 실패 명시 drop, 골드셋 fingerprint, compare 도구 도입)
- 절대 수치 신뢰: **8/10** ← 이전 7/10
- 비교 수치 신뢰: **7/10** ← 이전 4/10

## 운영 권고

### High (다음 스프린트)

1. **`eval_search.py:90-111`** — Judge 프롬프트 모드 분리 (이전 감사 잔여):
   - **`reference-free`** (기본): source chunk 숨기고 retrieved + 질문만 → "이 컨텍스트로 답할 수 있는가?"
   - **`entailment`** (옵션): NLI 형식 (source ↔ retrieved 의미 등가).
   - 현재 lexical-overlap 모드는 `--judge-mode legacy` 명시 시만.

2. **`compare_runs.py:172-191`** — Wilcoxon tie correction 추가:
   ```python
   tie_groups: dict[float, int] = {}
   for v, _i in abs_vals:
       tie_groups[v] = tie_groups.get(v, 0) + 1
   tie_corr = sum(t**3 - t for t in tie_groups.values() if t > 1) / 48.0
   var = n * (n + 1) * (2 * n + 1) / 24.0 - tie_corr
   ```
   hit@k 같은 0/1 메트릭의 검정력 회복.

3. **`compare_runs.py:331-415`** — N<6 경고 + n_skipped_cells stdout 표시:
   ```python
   if any(stats["n_nonzero"] < 6 for stats in diff_stats.values()):
       print("WARNING: n_nonzero < 6 인 메트릭이 있음 — Wilcoxon 정규근사 부정확.", file=sys.stderr)
   ```

### Medium

4. **`compare_runs.py:42-51`** — EQUIVALENCE_KEYS 확장:
   ```python
   EQUIVALENCE_KEYS = (
       "gold_set_sha256",
       "embedding_model",
       "llm_model",
       "top_k", "max_chunks",
       "similarity_threshold",
       "rerank_enabled", "hyde_enabled",
       # 추가 권고:
       "include_graph",
       "graph_match_threshold",
       "graph_match_strict",
       "score_relations",
       "judge_is_self",  # self-judge run 을 다른 run 과 비교 자동 차단
   )
   ```

5. **`graph_match.py:409-454`** — 관계 매칭의 T4 source/target lower 비교를 alias/normalize tier 까지 확장 (이전 감사 F-14 잔존).

6. **`compare_runs.py`** — 단측 검정 옵션 + 다중 메트릭 비교 보정 (Bonferroni 또는 BH FDR):
   ```python
   parser.add_argument("--alternative", choices=["two-sided", "greater", "less"], default="two-sided")
   parser.add_argument("--correction", choices=["none", "bonferroni", "bh"], default="none")
   ```

7. **`eval_search.py`** — `--timeout` 옵션 + `asyncio.wait_for(evaluate_one(...), timeout=args.timeout)` (이전 F-8 잔존).

### Low

8. **`metrics.py:159-164`** — n=1 시 std=NaN (이전 F-12 잔존).

9. **`eval_search.py:239`** — `mrr` 키 → `mrr@max_chunks` 또는 `mrr@k` 도 함께 보고.

10. **`eval_search.py:144-147`** — `score_raw` 가 bool 인 경우 명시 배제:
    ```python
    if not isinstance(score_raw, (int, float)) or isinstance(score_raw, bool):
        return -1, "parse_error"
    ```

11. **`compare_runs.py:199-222`** — bootstrap return value 의 `mean` 키 명명 명확화 (관찰 평균 vs 부트스트랩 평균).

12. **`compare_runs.py:248-277`** — `n_skipped` 의 단위(셀 vs 질의) 를 stdout 출력에도 표시. JSON 키는 `n_skipped_cells` 로 이미 명확.

13. **시스템 측** — `assemble_context_with_sources` 의 동률 tie-breaker 확정(`(similarity desc, document_id asc)` 명시) + 원시 retrieved_doc_ids 를 dedup 없이 정확히 top_k 길이로 반환하는 옵션 추가 (이전 F-3 / F-17, 시스템 측 책임).

## 이전 감사 대비 변화

### 해결된 위험 (Critical → 해결)
| 이전 # | 위험 | 해결 패치 |
|---|---|---|
| F-1 (Critical) | Judge self-evaluation fallback 무경고 진행 | P2: `--allow-self-judge` 옵트인 + 미명시 시 SystemExit. config_summary 에 `judge_is_self` 기록. |
| F-2 (Critical) | Judge 프롬프트 lexical overlap 편향 | **미해결** — 본 패치 사이클 범위 밖. 잔여 High (본 감사 F-1). |

### 해결된 위험 (High → 해결)
| 이전 # | 위험 | 해결 패치 |
|---|---|---|
| F-3 (High) | doc-id dedup + tie-breaker 비결정 | **미해결** — 시스템 측 책임. 잔여 High (본 감사 F-2). |
| F-4 (High) | "mean Δ > std" 코드 미강제 | P8: compare_runs.py 가 paired Wilcoxon + bootstrap 95% CI 제공. 잔여: tie correction, N<6 경고, 다중 비교 보정. |
| F-5 (High) | baseline/treatment 동치성 미검증 | P8: EQUIVALENCE_KEYS 9개 자동 검증 + exit code 2. |
| F-7 (High) | 실패 질의 silent drop | P9: error row 에 메트릭 키 None 명시 + n_failed/n_successful/failure_rate 보고. |
| F-10 (High) | build_embed_fn silent skip | P12: t4_disabled + skip_count 속성 부착, summary 에 graph_t4_disabled / graph_t4_skip_count. |
| F-18 (High) | judge_score=-1 평균 오염 | P5: -1 시 None 으로 분리 + judge_parse_failed + judge_score_parse_failures/success_count. |

### 해결된 위험 (Medium → 해결)
| 이전 # | 위험 | 해결 패치 |
|---|---|---|
| F-6 (Medium) | 골드셋 fingerprint 누락 | P8: gold_set_sha256, gold_set_n_items, gold_set_generator_model, gold_set_judge_model, gold_set_self_evaluation_warning 추가. |
| F-8 (Medium) | timeout/retry 없음 | **미해결**. |
| F-9 (Medium) | LRU 캐시 thread-safety | **미해결** — concurrency>1 시 잠재 race. |
| F-13 (Medium) | graph_top_k chunk 와 분리 안 됨 | **미해결**. |
| F-14 (Medium) | 관계 매칭 entity 변형 흡수 안 됨 | **미해결**. |
| F-17 (Medium) | Source.similarity=0.0 도착 순서 의존 | **미해결** — 시스템 측 책임. |

### 새로 도입된 위험
| 새 # | 위험 | 위치 | 등급 |
|---|---|---|---|
| F-4 | Wilcoxon tie correction 미적용 | `compare_runs.py:172-191` | Medium |
| F-5 | 작은 N 경고 docstring 만 명시, 코드 강제 안됨 | `compare_runs.py:172, 333-415` | Medium |
| F-7 | EQUIVALENCE_KEYS 가 9개 핵심 키만 — 정책 영향 키들 빠짐 | `compare_runs.py:42-51` | Medium |
| F-9 | nonzero diff 가 모두 동일 부호+절대값 시 p=1.0 (정규근사 부적합) | `compare_runs.py:184-191` | Low |
| F-10 | n_skipped 셀/질의 단위 헷갈림 | `compare_runs.py:248-277, 401` | Low |
| F-15 | anchor 정규화가 BOM/zero-width 미처리 → fallback 율 부풀려질 가능성 | `eval_search.py:365-409` | Low |
| F-17 | compare_runs prefix 매칭 느슨함 | `compare_runs.py:55-68, 230-245` | Low |
| F-18 | score_raw=bool 시 정수 캐스팅 | `eval_search.py:144-147` | Low |

### 종합 신뢰도 등급 변화
- **이전 한줄 판정: HIGH RISK**
- **현재 한줄 판정: MEDIUM RISK**

| 차원 | 이전 등급 | 현재 등급 | 변화 |
|---|---|---|---|
| 메트릭 식 | A- | A | ↑ |
| 시스템 수준 신뢰 | C+ | B | ↑↑ |
| 절대 수치 신뢰 | 7/10 | 8/10 | ↑ |
| 비교 수치 신뢰 | 4/10 | 7/10 | ↑↑↑ |

P 사이클 7개 패치가 의도한 모든 영역에서 효과를 발휘했고, 신규 도구 compare_runs.py 도 stdlib 만으로 정확한 통계 검정을 구현했다. 잔여 위험은 ① Judge 프롬프트 구조(F-1), ② 시스템 측 tie-breaker(F-2), ③ Wilcoxon tie correction(F-4) — 모두 본 패치 사이클의 명시적 범위 밖이거나 후속 스프린트 항목. **이제 절대 수치는 신뢰할 수 있고, 비교 수치도 동치성·통계 검정 자동화로 충분히 방어된 수준.**

---

**감사자 노트:** P2/P5/P6/P8/P9/P12 6 개 패치가 이전 감사의 Critical 2건 중 1건(self-judge fallback) 과 High 5건(통계, 동치성, 실패 drop, t4 skip, judge -1 평균 오염) 을 모두 정공법으로 해결했다. 신규 도구 `compare_runs.py` 는 stdlib 기반 Wilcoxon 직접 구현이 표준 정의에 정확히 맞고, 부트스트랩 시드 결정성도 보장된다. 다만 tie correction 1줄 추가, EQUIVALENCE_KEYS 확장, Judge 프롬프트 reference-free 모드 분리 3건만 추가하면 평가 신뢰도가 **A**대로 진입할 수 있다.
