# 그래프 측정 신뢰성 감사 — 종합 보고 (SUMMARY)

> 범위: 그래프(graph) 골드셋 생성 + 그래프 채점/통계. 목표: "변경(예: 코드↔지식
> 브리지)이 그래프 검색을 개선했는지 **객관적으로 측정**" 가능 여부 판정.
> 출처: `01_gold_set_audit.md`(생성), `02_eval_script_audit.md`(채점), `03_cross_bias_analysis.md`(교차).
> 기준 코드: `origin/main` 스테이징본(`_workspace/source/`).

## TL;DR 판정

**현재 그래프 메트릭(`graph_*`)을 "개선 여부"의 객관적 근거로 쓸 수 없다 — HIGH→CRITICAL.**
그래프 entity recall이 올라가도 그것이 (a) 진짜 검색 개선인지, (b) T4 임베딩 false-positive
증가인지, (c) 통계적 noise인지 **구분할 수단이 코드에 없다.** 단, 아래 S0 패치(표면 tier 분리 +
그래프 CI + cross-doc 골드 + τ/임베딩 고정)를 적용하면 **"동일 인덱스 위에서 retriever/planner
변경의 방향성"** 만큼은 신뢰성 있게 측정 가능하다. 인덱싱/임베딩을 바꾸는 A/B와 그래프 절대값
보고는 패치 후에도 금지.

## Top 위험 (등급순, 3개 보고서 통합)

| # | 위험 | 등급 | 증거 |
|---|------|------|------|
| **R1** | **graph_* 메트릭이 bootstrap CI에서 제외** → 델타의 signal/noise 통계 판정 불가 | **CRITICAL** | `eval_search.py:778`(`if k.startswith("graph_"): continue`), `:893` |
| **R2** | **T4 임베딩 tier가 type-무시 + τ=0.65 + name-fallback** → 틀린 엔티티를 정답 처리해 recall 부풀림. 결정론(T1–T3)과 fuzzy(T4)가 단일 수치로 혼합 | **CRITICAL** | `graph_match.py:33`,`:305`,`:330-332`; `eval_search.py:457-461`,`:482` |
| **R3** | **self-fitting** — 골드 entity name/type가 평가 대상 인덱스 노드에서 그대로 복사(추출 LLM 동일 여부와 무관) → T1 exact 자명 통과. "검색 품질이 아니라 골드셋 생성 방식을 측정" | **CRITICAL** | `build_synthetic_gold_set.py:1426-1427` ← `:237`/`:240`; retrieved 동일 인덱스 `eval_search.py:431`; `graph_match.py:276-280` |
| **R4** | **3중 임베딩 순환** — 골드 evidence 임베딩 = 검색 인덱스 임베딩 = T4 채점 임베딩이 모두 동일 모델. "자기 임베딩으로 회수하고 자기 임베딩으로 채점"(R2와 곱해짐) | **HIGH** (cross로 Medium→High 재등급) | `build:1889-1891`, `eval:1038`, `eval:1031` |
| **R5** | **alias/description 누설** — generator 작성 aliases/evidence가 누설 게이트 없이 골드에 직박힘 → T2/T4 trivial 통과. 누설 게이트는 query에만 적용 | **HIGH** | `build:1428`, `synth.py:198-199`, 게이트 `build:1054`(query-only) |
| **R6** | **단일 엔티티 0/1 채점** — per-item recall ∈ {0,1}, 분모 항상 1. 기본 N 작아 소표본 분산 큼(N≥150 권고) | **HIGH** | `build:1406-1407,1451`, `graph_match.py:370-373` |
| **R7** | **graph_precision@k 분모=k 고정** + 분자=매칭 골든 수 → false-positive 패널티 0, 해석 불가 | **HIGH** | `metrics.py:50`, `eval_search.py:462-466` |
| **R8** | **브리지 미자극** — 표준 graph-mode는 단일 노드 질문이라 cross-source(confluence 이름→코드 FQN)를 자극 못함. cross-doc 모드가 메움 | **HIGH** | `synth.py:175-176`; cross-doc `build:1461,1479-1480` |
| **R9** | **Channel C (신규)** — generator LLM이 시스템 planner/HyDE LLM과 동일 가능(fall-through). `role_is_configured`는 2-way만 검사 | **MEDIUM** | `eval:388-394`, `llm.py:166,222,235` |
| **R10** | **Channel E (신규, 브리지 측정 직격)** — "동일 인덱스 ⇒ self-fit 대칭" 가정은 **변경이 retrieved set을 바꾸면 깨짐**. 브리지 on/off는 회수 집합을 바꾸므로 R3의 T1 windfall이 비대칭 분배 | **HIGH** | `03` Channel E; `graph_match.py:276-280` |
| R11 | 실패 그래프 질의가 0점이 아니라 집계에서 무성 제외(chunk와 불일치) | MEDIUM | `eval:1100-1106`, `metrics.py:111-126` |
| R12 | 라벨 비교 가드 부재 — A/B 두 arm이 동일 τ·임베딩·골드셋인지 자동 대조 없음 | MEDIUM | `eval:1414-1431`, `graph_match.py:36-39` |

## 사용 가능 / 사용 금지 매트릭스 (4 시나리오)

| 의사결정 유형 | 현재 | S0 패치 후 | 단서 |
|---|---|---|---|
| **(1) retriever/planner만 변경 (인덱스 고정) A/B** — 예: 코드↔지식 브리지 | ❌ | **조건부 YES** | 표면(T1–T3) 메트릭 + 그래프 CI + cross-doc 골드 + τ/임베딩/골드셋 고정. **방향성만**, 절대값 금지 |
| **(2) 인덱싱/추출 변경 A/B** | ❌ | ❌ | self-fitting이 비대칭화(R3+R10) → 그래프 메트릭 무효 |
| **(3) 임베딩 모델 교체** | ❌ | ❌ | 3중 임베딩 순환(R4)이 채점 기준 자체를 바꿈 |
| **(4) 외부 벤치마크/절대값 SLA 보고** | ❌ | ❌ | 합성 self-fit 골드셋은 외부 비교 자격 없음 |

## 우선순위 개선 권고 (S0 = 객관 측정의 전제, S1 = 신뢰 보강)

### S0 — 이게 없으면 그래프 A/B를 신뢰하지 말 것
- **S0-1 (R2/R3 분리 리포팅)**: `eval_search.py:457` 부근에서 **표면 메트릭 `graph_recall_surface@k`(T1–T3만)** 와 기존 `graph_recall@k`(T4 포함)를 **별도 컬럼 동시 산출**. `run_entity_matching`에 T4 제외 경로 추가(기존 `strict`는 T1만이라 부족 → T1–T3 허용·T4 제외 모드 신설). 개선 판단은 surface를 1차 기준으로. `graph_match_tiers`의 `embedding` 비중 임계 초과 시 경고.
- **S0-2 (R1 그래프 CI)**: `_chunk_metric_cis`(`eval_search.py:764`)의 `graph_` 제외(`:778`) 해제 또는 `_graph_metric_cis` 신설 → graph_recall/hit/ndcg/surface per-query에 `bootstrap_ci_mean`(`metrics.py:129`) 적용. summary/`metric_ci`에 graph 포함. **델타가 두 arm CI 비중첩일 때만 "개선" 판정.** `test_absolute_mode.py` 기대값 갱신.
- **S0-3 (R8 브리지 자극)**: 그래프 A/B 골드셋은 `--enable-cross-doc --source-types confluence_mcp git_code`로 생성해 **cross-source(문서 이름↔코드 FQN) 항목 포함**. 안 하면 브리지 효과가 0으로 안 움직임.
- **S0-4 (R12 비교 가드)**: `_write_aggregate`에서 비교 라벨 간 `graph_match_threshold`·임베딩 모델 ID·골드셋 fingerprint(`graph_store_sha256`) 동일성 assert, 불일치 시 비교 무효 경고.

### S1 — 신뢰 보강
- **S1-1 (R7 precision 재정의)**: `graph_precision@k` 분모를 실제 retrieved 그래프 엔티티 수로, 또는 미매칭 retrieved(false-positive)를 반영. 현 정의 폐기.
- **S1-2 (R5 누설 게이트 확장)**: generator 작성 `aliases`/`evidence_description`에도 누설 게이트 적용(`build:1054`를 query 외로 확장). 또는 aliases를 채점에서 제외 옵션.
- **S1-3 (R6 표본/해상도)**: 그래프 골드 N≥150 권고를 빌드 메타·문서에 명시, 출력에 "단일 엔티티 0/1 채점" 한계 경고.
- **S1-4 (R4/R9 분리 기록)**: 추출(인덱싱) LLM ID + 골드/검색/채점 임베딩 모델 ID를 summary에 기록하고 동일성 경고. `role_is_configured`(`llm.py:235`)를 3-way(generator/judge/system)로 확장.
- **S1-5 (R11 실패 처리 통일)**: `eval:1100` fallback row에 graph_* 키도 `None` 명시, 또는 0점 정책 명문화.
- **S1-6 (R2 추적성)**: per-pair 매칭 증거(golden_key, retrieved_index, tier, cosine)를 행에 JSON 평탄화해 T4 spot-check 가능하게.

## 브리지(코드↔지식) 측정 레시피 — 위 패치를 어떻게 쓰는가

R10(Channel E)이 핵심: 브리지는 **retrieved set을 바꾸므로** self-fit 대칭이 깨진다. 그러나 이는
브리지 측정에 **유리하게** 작용한다 — 브리지의 목적이 정확히 "정답 코드 FQN 노드를 회수하게
만들기"이므로, gold 노드가 baseline에선 미회수(recall 0)·브리지에선 회수(recall 1)로 바뀌는 것은
**정당한 신호**다(self-fitting은 "회수되면 매칭된다"는 보장일 뿐). 따라서:
1. S0-3로 cross-source 골드 생성(브리지가 자극되는 항목 확보).
2. S0-1의 **`graph_recall_surface@k`(T1–T3)** 를 1차 지표로 — T4 false-positive(R2)가 델타를 오염시키지 않게.
3. S0-2의 **그래프 CI** 로 baseline vs 브리지 델타가 CI를 넘는지 확인(R6 소표본이라 N 충분 필수).
4. S0-4로 양 arm이 동일 인덱스·τ·임베딩·골드셋임을 보증(브리지는 검색 코드만 바꾸므로 충족).

## 부록

- 세부 보고서: `_workspace/findings/01_gold_set_audit.md`, `02_eval_script_audit.md`, `03_cross_bias_analysis.md`
- 이전 감사: `_workspace/findings_prev/`
- 재감사 트리거: 임베딩 모델 교체, 그래프 추출 로직 변경, 골드셋 생성 방식 변경, τ 변경 시.
- 다음 단계(별도 실행 영역): S0/S1 패치는 `rag-eval-fix` 스킬(eval-script-patcher + gold-set-build-patcher)로 적용 가능.
