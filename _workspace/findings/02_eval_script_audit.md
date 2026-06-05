# Eval-Script Auditor — 평가 스크립트 신뢰성 감사 (그래프 한정)

## 한줄 판정

**현 그래프 채점·통계 경로로 산출한 `graph_*` 메트릭은 A/B 개선 판단에 그대로 쓸 수 없다.** 두 개의 구조적 결함이 동시에 존재한다 — (1) T4 임베딩 tier가 type을 무시하고 0.65라는 느슨한 임계값으로 false-positive를 정답으로 흡수해 단일 `graph_recall`을 부풀리며 결정론(T1–T3)과 fuzzy(T4) 기여가 분리 리포팅되지 않고, (2) `graph_*` 메트릭은 bootstrap CI 계산에서 명시적으로 제외(`eval_search.py:778`)되어 델타의 signal/noise를 통계적으로 구분할 수단이 전혀 없다. 추가로 단일 엔티티 gold에서 recall/mrr/ndcg가 동률(0/1) 디그레이드되어 분산 정보가 거의 없는데도 그 한계가 출력에 명시되지 않는다.

## 검토 범위

- `graph_match.py` 전체 (4-tier cascade, `run_entity_matching`, `run_relation_matching`, `cosine_similarity`, `build_embed_fn`)
- `eval_search.py` 그래프 채점부 (`evaluate_one` ~427–522), 통계부 (`_chunk_metric_cis` 764–789, `write_summary` 792–897, `_write_aggregate` 1401–1462), 실패 행 처리 (1090–1135), `check_absolute_mode_requirements` 729–761
- `metrics.py` 전체 (`recall_at_k`/`precision_at_k`/`mrr`/`ndcg_at_k`/`hit_at_k`, `bootstrap_ci_mean`, `aggregate`, `aggregate_with_variance`)
- `gold_set.py` (`GraphEntityRef`/`GraphRelationRef`/`GoldItem`)
- 범위 외(chunk-only judge 채점 경로)는 그래프와 무관한 부분 제외.

---

## 핵심 발견 (위험 등급순)

| # | 위험 | 등급 | 핵심 증거 |
|---|------|------|-----------|
| F1 | T4 type-무시 + τ=0.65 → false-positive를 정답 처리, recall 부풀림 | **치명(Critical)** | `graph_match.py:33`, `:305`, `:330-332` |
| F2 | `graph_*`가 bootstrap CI에서 제외 → 델타 유의성 판정 불가 | **치명(Critical)** | `eval_search.py:778`, `:771`, `:893` |
| F3 | 결정론(T1–T3)과 fuzzy(T4) 기여가 단일 `graph_recall`로 혼합, tier 분리 메트릭 부재 | **높음(High)** | `eval_search.py:457-461`, `:482` |
| F4 | 단일 엔티티 gold에서 recall/mrr/ndcg가 0/1 이진 디그레이드 → 분산 소실, 출력에 한계 미명시 | **높음(High)** | `metrics.py:26-87`, `eval_search.py:457-480` |
| F-PREC | `graph_precision@k` 분모가 k 고정 + 분자에 매칭 골든만 → false-positive 패널티 0, 해석 불가 | **높음(High)** | `metrics.py:50`, `eval_search.py:462-466`, `graph_match.py:393-399` |
| F5 | 그래프 실패 질의가 0점이 아니라 **집계에서 무성(無聲) 제외** (chunk와 처리 불일치) | **중(Medium)** | `eval_search.py:1100-1106`, `metrics.py:111-126` |
| F6 | T4 임베딩이 시스템/인덱스와 동일 embedding 클라이언트 사용 → 매칭 메타-편향 | **중(Medium)** | `eval_search.py:1031`, `:1038` |
| F7 | 다중 골드셋 비교(`--label`)에서 동일 골드셋·동일 조건(threshold/임베딩) 검증 장치 부재 | **중(Medium)** | `eval_search.py:1414-1431`, `graph_match.py:36-39` |
| F8 | `aggregate_with_variance`가 그래프에 적용되지만 N(골드셋 수)이 작아 std 무의미, "mean Δ > std" 기준 미강제 | **중(Medium)** | `metrics.py:234-239`, `eval_search.py:1414` |

---

## 차원별 상세 점검 (그래프 맥락)

### 1. 메트릭 구현 정확성

매칭 산출물(`retrieved_keys_in_rank_order`, `all_relevant_keys`)을 generic `metrics.*`에 넣는 구조 자체는 표준 정의에 부합하나, **그래프 입력의 특성 때문에 정의가 디그레이드**된다.

**graph_recall@k** (`eval_search.py:457-461`): `recall_at_k(retrieved_keys_in_rank_order, all_relevant_keys, top_k)`. `metrics.py:35` = `len(top_k ∩ rel_set)/len(rel_set)`. 분모는 `all_relevant_keys`(골든 전체, `graph_match.py:370-373`), 분자는 매칭된 키 — **분모/분자 처리는 정통**. 단, `retrieved_keys_in_rank_order`는 이미 "매칭 성공한 골든의 키"만 담으므로(`graph_match.py:393-399`), 여기서 `top_k` slice는 retrieved 엔티티의 원본 rank가 아니라 **매칭된 골든 수**에 대한 slice다. 골든이 보통 1–3개이고 top_k=5라면 slice가 사실상 무효 → recall은 "몇 개 매칭됐나"의 비율로 환원. 정의상 틀리진 않으나 **k의 의미가 chunk recall과 다르다**.

**graph_precision@k** (`:462-466`): `precision_at_k(retrieved_keys_in_rank_order, all_relevant_keys, top_k)` = `hits/k` (`metrics.py:50`). **분모가 k(=5) 고정**인데 `retrieved_keys_in_rank_order`에는 매칭 성공 골든만 들어가므로 분자 ≤ 골든 수. 골든 1개·매칭 1개면 precision = 1/5 = 0.2. **이것은 "top-5 retrieved 중 정답 비율"이 아니라 "골든 매칭 수 / 5"라는 무의미한 수치**다. retrieved 그래프 엔티티의 실제 개수가 분모에 전혀 반영되지 않아 graph_precision은 **해석 불가 메트릭**(false-positive 패널티가 0). 등급: 높음.

**graph_mrr** (`:472-475`): 입력 list가 "매칭된 골든 키를 retrieved rank로 정렬"한 것이므로 첫 원소는 항상 매칭 → 사실상 `1/(첫 매칭의 retrieved rank)`. 단일 골든이면 0 또는 1/r로 이진화.

**graph_ndcg@k** (`:476-480`): `ndcg_at_k`(`metrics.py:65-87`). `idcg==0` 가드 존재, binary relevance, log2(i+1) 정통. **구현 자체는 정확.** 단 단일 골든에서 거의 이진.

**graph_hit@k** (`:467-471`): 표준. 매칭 키가 하나라도 있으면 1.

**관계 메트릭(`graph_rel_*`, `:508-522`)**: 동일 패턴. precision 동일 결함.

**판정**: nDCG/recall 정의는 표준 함수로는 정확하나, **그래프 입력 형태 때문에 precision은 의미를 잃고 recall/mrr/ndcg는 단일 골든에서 이진화**된다. `metrics.py`는 죄가 없고 `eval_search.py:457-480`의 입력 구성이 문제.

### 2. top-k 선정 / tie-breaker (결정성)

- `hits.sort(key=lambda t: t[0])` (`graph_match.py:391`)은 retrieved_index만 키 → 동일 index에 복수 골든 매칭 시 Python stable sort(=골든 입력 순서)에 의존. 골든 순서는 YAML 로드 순서로 안정 → **결정성 OK**.
- **T4 best 선택(`graph_match.py:331`)**: `sim > best.score` 엄격 부등호 → 동률 cosine이면 먼저 등장한 retrieved 유지. 결정적.
- **동시성/LRU 캐시**: `build_embed_fn` 캐시는 lock 없는 dict+list(`graph_match.py:222-239`). 동시 쓰기 race 가능하나 동일 텍스트→동일 값이라 메트릭 비결정성으로 거의 안 이어짐(eviction 순서만). 등급: 낮음.

### 3. Judge 채점 메타-편향 (그래프 = T4 임베딩 매칭이 fuzzy judge)

- T4 임베딩 클라이언트는 `_build_embedding_client(config)`(`eval_search.py:958-964`)로 생성되어 `embed_fn`에 주입(`:1031`)되고, **동시에 `graph_store.build_entity_embeddings(embedding_client)`(`:1038`)에도 같은 클라이언트 사용.** 검색 인덱싱 임베딩과 평가 매칭 임베딩이 동일 endpoint/family일 위험이 강제 분리되어 있지 않음 → **"자기 답을 자기가 칭찬"** 구조로 T4 매칭률↑. 등급: 중.
- 골드셋 생성도 같은 임베딩으로 `description_embedding`을 박을 수 있음(`gold_set.py:70-72`).
- T4는 type 무시(`:305-308`)이고 variance 측정 없이 단일 cosine 결과 신뢰. judge variance에 해당하는 N-sample 안전장치 없음.

### 4. 통계 / 변동성 처리 — **핵심 결함 F2/F8**

- **`_chunk_metric_cis`가 `graph_`로 시작하는 모든 키를 명시 제외**(`eval_search.py:778`: `if k.startswith("graph_"): continue`). bootstrap CI는 chunk 메트릭에만 적용. `bootstrap_ci_mean`(`metrics.py:129`)은 그래프에 한 번도 호출되지 않음. → `absolute_mode`의 `metric_ci`(`:893`)에 graph_* CI 부재.
- 두 라벨의 graph_recall 델타가 noise인지 signal인지 **통계적으로 판정할 근거가 출력에 전무**.
- `aggregate_with_variance`(`metrics.py:182`)는 graph_* 키도 처리(키 필터 없음, `:216`)하나, (a) ddof=1 std는 N≥2에서만(`:234`), 실무 N 보통 1–5라 불안정, (b) 부트스트랩·CI·paired t-test 같은 정식 검정 없음, (c) "mean Δ > std면 유의" 기준은 코드 미강제(사용자 눈대중).
- **단일 골드셋·단일 실행**(가장 흔함)에서 그래프 메트릭에 불확실성 정량치 전무.

### 5. 출력 / 감사 추적성

- per-question row에 `graph_match_tiers`, `graph_match_score_avg/min/max`(`:482-485`) 기록 — tier 분포 행 단위 추적 가능(긍정). `graph_t4_disabled`(`:585`), `graph_t4_skip_count`(`:869-883`)도 존재.
- 그러나 **"어떤 retrieved 엔티티가 어떤 골든과 cosine 몇으로 매칭됐는지"의 per-pair 증거 부재.** `MatchResult`(retrieved_index, tier, score)가 row에 평탄화되지 않아 T4 false-positive 사후 수동 검증 불가. 등급: 중.
- summary JSON에 임베딩 모델 ID, `graph_store_sha256`(`:1282`,`:1332`), `graph_match_threshold`(`:1274`) 기록 — 양호.

### 6. 실행 안정성 (실패 질의의 그래프 집계 영향) — **F5**

- 질의 예외 시 fallback row(`:1094-1106`)는 chunk 키만 `None`으로 채우고 **`graph_*` 키는 아예 안 넣음.** `aggregate`(`metrics.py:111-126`)는 키 없는 행 자동 제외 → **실패한 그래프 질의는 0점이 아니라 평균에서 무성 제외.** 검색 자주 실패 시 그래프 메트릭은 "성공한 질의만"의 낙관적 평균. `failure_rate`(`:850`)와 교차검증 안 하면 과대평가. 등급: 중.
- T4 임베딩 실패 → 해당 골든 T4 skip(`graph_match.py:317-318`) → 미매칭 강등(recall↓). 임베딩 인프라 흔들리면 recall 흔들려 재현성 위협. `graph_t4_skip_count`로 사후 탐지 가능.
- 재현성: T1–T3 순수 결정적. T4는 `description_embedding`이 골드셋에 박혀 있으면 결정적, 아니면 lazy 임베딩(`:314-316`) → 임베딩 API 비결정성 노출.

### 7. 라벨링 / 비교 — **F7**

- `_write_aggregate`(`:1401`)는 동일 label 다중 골드셋을 mean±std로 묶고 `per_gold_set` 경로 기록(`:1426`)하나, **baseline vs treatment가 동일 골드셋·동일 `graph_match_threshold`·동일 임베딩 모델로 돌았는지 자동 대조 장치 없음.** 다른 threshold(예 baseline τ=0.78, treatment τ=0.65)로 돌리면 그래프 차이가 **시스템 개선이 아니라 채점 기준 변경**일 수 있음 — `DEFAULT_GRAPH_MATCH_THRESHOLD` 0.78→0.65 완화 docstring 경고(`graph_match.py:36-39`)가 바로 이 위험 시사. 등급: 중.

---

## 출발점 위협 검증 (정량 확정)

### 위협 (1): T4 type-무시 + τ=0.65 → false-positive 정답 처리, recall 부풀림 — **확정, Critical**
- `graph_match.py:33` `DEFAULT_GRAPH_MATCH_THRESHOLD = 0.65` (docstring `:36-39` 0.78→0.65 완화가 "골드셋 신뢰성에 영향이 큰 변경"이라 자인).
- `:305` "T4 — embedding (type-agnostic)" + `:330-332` cosine 비교에 **type 비교 없음.** T1–T3는 모두 `r_type == g_type` 요구(`:279`,`:293`,`:302`).
- gold/retrieved 양측 description→name fallback(`:310`, `:322`). description 비면 이름 임베딩 매칭 → 짧은 이름끼리 cosine 비특이적으로 높아 0.65 쉽게 초과.
- 정량 영향: 단일 골든이 주류라 graph_recall은 "T1∨T2∨T3∨T4 중 하나 매칭" 이진값. T4가 가장 관대(type 무시+name fallback+τ=0.65)해 T1–T3에서 진짜 틀린 케이스를 흡수. cosine 0.65는 "느슨하게 관련" 수준이라 서로 다른 엔티티("인증 서비스" vs "인증 토큰")도 통과 가능 → false-positive가 hit=1/recall=1로 집계되어 **graph_recall 체계적 상향**. 현재 단일 수치라 tier 분리 불가(F3).

### 위협 (2): graph_* CI 제외 → signal/noise 구분 불가 — **확정, Critical**
- `:778` `if k.startswith("graph_"): continue`로 `_chunk_metric_cis`가 graph 키 전량 배제.
- `bootstrap_ci_mean`(`metrics.py:129`) 호출처는 `:788` 단 1곳, chunk 전용.
- `absolute_mode` `metric_ci`(`:893`)에 graph_* 부재. `check_absolute_mode_requirements`(`:729`)도 그래프 CI 미점검(graph는 planner_seed만, `:755`).

---

## 결론: 이 채점·통계로 만든 그래프 메트릭을 A/B 개선 판단에 쓸 수 있는가?

**아니오 — 현 상태로는 불가.** 두 치명 결함(F1 부풀림, F2 CI 부재)이 결합해, graph_recall이 올라가도 (a) 진짜 검색 개선인지, (b) T4 false-positive 증가인지, (c) 단순 noise인지 구분 불가. 아래 단서가 갖춰지면 제한적 사용 가능:

1. **tier 분리 리포팅 강제 (F1/F3)**: `eval_search.py:457` 부근에 `graph_recall_surface@k`(T1–T3만)와 `graph_recall@k`(T4 포함)를 **별도 컬럼 동시 산출**. 개선 판단은 surface 1차 기준, fuzzy 보조. `graph_match_tiers`의 `embedding` 비중이 일정 % 초과 시 경고.
2. **T4 type 일치 옵션 + 임계값 보수화**: `:330` cosine 비교에 type 게이트 플래그 추가, τ 0.65→0.78 복원 또는 type-무시 매칭을 별도 tier(`embedding_typecross`)로 격리. name-fallback 매칭은 별도 플래그로 표시.
3. **graph_* 부트스트랩 CI 추가 (F2)**: `_chunk_metric_cis`(`:764`)의 graph 제외(`:778`) 해제, graph_recall/hit/ndcg/mrr per-query에 `bootstrap_ci_mean` 적용. `absolute_mode` `metric_ci`에 graph 포함. **델타가 두 라벨 CI 비중첩일 때만 "개선" 판정.**
4. **precision 재정의 (F-PREC)**: `graph_precision@k` 분모를 k 고정이 아니라 실제 retrieved 그래프 엔티티 수로, 또는 미매칭 retrieved(false-positive)를 분모에 반영. 현 정의 폐기 권장.
5. **per-pair 매칭 증거 CSV (F5 추적성)**: `MatchResult`(golden_key, retrieved_index, tier, cosine)를 행에 JSON 평탄화해 T4 spot-check 가능하게.
6. **실패 그래프 질의 처리 통일 (F5)**: `:1100` fallback row에 graph_* 키도 `None` 명시 추가, 또는 0점 정책 명문화.
7. **라벨 비교 가드 (F7)**: `_write_aggregate`에서 비교 라벨 간 `graph_match_threshold`·임베딩 모델 ID·골드셋 fingerprint 동일성 assert, 불일치 시 비교 무효 경고.

**핵심 한 줄: strict(T1–T3) graph_recall + per-query bootstrap CI 두 가지가 갖춰지기 전까지 그래프 메트릭 델타는 방향 참고용일 뿐 의사결정 근거로 삼지 말 것.**
