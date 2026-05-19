# 검색·RAG 평가 스크립트 가이드

검색 시스템의 신뢰성을 정량 평가하기 위한 4개 스크립트의 역할·실행법·파라미터를 정리한다.

## 전체 워크플로우

```
                ┌────────────────────────────────────┐
                │  scripts/build_synthetic_gold_set  │  ← LLM 으로 골드셋 생성
                │  (생성기 + Judge 4단계 게이트)       │
                └─────────────────┬──────────────────┘
                                  │ eval/gold_set.yaml
                                  ▼
                ┌────────────────────────────────────┐
                │       scripts/eval_search          │  ← 검색 시스템 채점
                │   (Recall/Precision/MRR/nDCG/Judge) │
                └─────────────────┬──────────────────┘
                                  │ eval/runs/*.summary.json + *.csv
                                  ▼
┌──────────────────────────────────┐   ┌──────────────────────────────────┐
│   scripts/compare_runs           │   │  scripts/calibrate_graph_match    │
│   baseline vs treatment paired   │   │  그래프 τ F1 최적점 산출 + auto-tune │
│   (Wilcoxon + bootstrap CI)      │   │                                  │
└──────────────────────────────────┘   └──────────────────────────────────┘
```

세 단계 — **생성(build) → 평가(eval) → 비교(compare)**. 그래프 모드를 쓰면 평가 전에 `calibrate_graph_match` 로 임계값을 본 환경 데이터에 맞춰 튜닝한다.

---

## 1. `scripts/build_synthetic_gold_set.py` — 합성 골드셋 생성

### 역할

인덱싱된 청크/그래프에서 stratified sampling 한 후, Generator LLM 이 청크당 N개 질문을 역방향 생성하고 Judge LLM 의 4단계 게이트로 사기성·일반성 질문을 탈락시켜 (질문, 정답 문서/엔티티) 페어 YAML 골드셋을 만든다.

**핵심 안전장치 (코드로 강제됨):**
- 자기-평가 편향 차단 — Generator/Judge 가 system LLM 과 같으면 `--allow-self-eval` 없이는 종료
- 결정론적 누설 게이트 — ASCII 식별자·한국어 고유명사·지시대명사 정규식
- LLM 게이트 — 답변 가능성(a) + 유일성(d1) + distractor 비답변(d2)
- 한글 게이트의 코퍼스 학습 stopword — false positive 자동 통제
- 재현성 — Generator·Judge 호출에 seed 전달 (endpoint 지원 시)

### 파라미터

#### 핵심

| 옵션 | 기본 | 설명 |
|---|---|---|
| `--config / -c` | `~/.context-loop/config.yaml` | 사용자 config 파일 |
| `--output / -o` | `eval/gold_set.yaml` | 출력 YAML 경로. `--no-filter` 시 `.UNFILTERED` 접미사 강제 |
| `--n-chunks` | 30 | 샘플링할 청크 수 |
| `--questions-per-chunk` | 2 | 청크당 생성할 질문 수 |
| `--source-types` | (전체) | 쉼표로 구분된 `source_type` 화이트리스트 (예: `confluence_mcp,git_code`) |
| `--n-gold-sets` | 1 | 생성할 골드셋 수. >1 이면 `seed+i-1` 로 변동성 측정용 다중 빌드, 파일에 `_NNN` 접미사 |
| `--reasoning-mode` | `off` | LLM `reasoning_profiles` 키 |
| `--concurrency` | 1 | 항목(chunk/subgraph) 단위 동시 처리. 4~8 권장 |
| `--verbose / -v` | False | DEBUG 로깅 |

#### 청크 필터

| 옵션 | 기본 | 설명 |
|---|---|---|
| `--min-chars` | 200 | 최소 청크 길이 (그 미만 제외) |
| `--max-chars` | 8000 | 최대 청크 길이 (그 초과 제외) |

#### 게이트

| 옵션 | 기본 | 설명 |
|---|---|---|
| `--no-filter` | False | 4단계 게이트 비활성. **운영 골드셋 금지** — 출력 경로에 `.UNFILTERED` 접미사 강제 |
| `--n-distractors` | 2 | 일반성 게이트 (d2) 의 무관 청크 수 |

#### 자기-평가 차단

| 옵션 | 기본 | 설명 |
|---|---|---|
| `--allow-self-eval` | False | Generator/Judge 가 system LLM 과 동일해도 강행. 메타데이터에 `self_evaluation_warning=true` 기록 |
| `--generator-endpoint` | (config) | Generator 전용 endpoint URL |
| `--generator-model` | (config) | Generator 전용 모델 ID |
| `--generator-api-key` | (config) | Generator 전용 API 키 |
| `--generator-headers` | (config) | Generator 전용 HTTP 헤더 JSON |
| `--judge-endpoint` | (config) | Judge 전용 endpoint URL |
| `--judge-model` | (config) | Judge 전용 모델 ID |
| `--judge-api-key` | (config) | Judge 전용 API 키 |
| `--judge-headers` | (config) | Judge 전용 HTTP 헤더 JSON |

#### 재현성

| 옵션 | 기본 | 설명 |
|---|---|---|
| `--seed` | None | Python `random` 시드 (샘플링 결정성). N>1 일 때 i번째 골드셋은 `seed+i-1` |
| `--generator-temperature` | 0.0 | Generator sampling 온도. 다양성 필요 시 0.7. 메타데이터에 기록 |
| `--generator-seed-base` | None | Generator LLM 호출 seed base. 청크별 seed = `base + chunk_index`. OpenAI 호환 endpoint 만 효과 |

#### 그래프 모드

| 옵션 | 기본 | 설명 |
|---|---|---|
| `--include-graph-questions` | False | 그래프 subgraph 기반 질문도 생성 |
| `--n-graph-nodes` | 0 (= `--n-chunks`) | 샘플링할 subgraph 수 |
| `--min-graph-neighbors` | 1 | subgraph 후보의 1-hop 이웃 최소 수 |
| `--embed-graph-evidence` | True | description 임베딩을 골드셋에 미리 박을지 여부 |
| `--score-relations` | False | 관계(엣지) 채점용 `GraphRelationRef` emit |
| `--graph-match-threshold` | 0.78 | 평가 시 T4 (embedding) 임계값. 메타데이터 기록 |

### 메타데이터 (출력 YAML 의 `metadata`)

빌드 시 다음이 기록되어 사후 추적 가능:
- `generator_model`, `generator_endpoint`, `judge_model`, `judge_endpoint`
- `generator_configured_separately`, `judge_configured_separately`, `self_evaluation_warning`, `allow_self_eval`
- `generator_temperature`, `generator_seed_base`, `seed`
- `n_chunks_sampled`, `questions_per_chunk`, `filter_applied`, `source_types`
- `stats`: 게이트별 통과/탈락 사유 카운트
- 그래프 모드: `embedding_model`, `graph_match_threshold_default`, `score_relations`

### 실행 예시

**기본 (config 의 `llm.*` 사용):**
```bash
python scripts/build_synthetic_gold_set.py \
    --n-chunks 30 --questions-per-chunk 2 \
    --output eval/gold_set.yaml
```

**Generator/Judge 분리 + 재현성:**
```bash
python scripts/build_synthetic_gold_set.py \
    --generator-endpoint http://strong-model:8080/v1 \
    --generator-model gpt-4o \
    --judge-endpoint http://other-family:8080/v1 \
    --judge-model claude-haiku \
    --seed 42 --generator-seed-base 1000 \
    --output eval/gold_set.yaml
```

**변동성 측정용 다중 골드셋 (N=5):**
```bash
python scripts/build_synthetic_gold_set.py \
    --source-types git_code \
    --seed 42 --n-gold-sets 5 \
    --output eval/gold_sets/git_code.yaml
# → git_code_001.yaml, _002.yaml, ... _005.yaml
```

**그래프 + 관계 채점:**
```bash
python scripts/build_synthetic_gold_set.py \
    --include-graph-questions --n-graph-nodes 20 \
    --score-relations --graph-match-threshold 0.78 \
    --output eval/gold_set.yaml
```

---

## 2. `scripts/eval_search.py` — 검색 시스템 채점

### 역할

골드셋 YAML 을 받아 각 질의에 대해 `assemble_context_with_sources` 를 호출하고 정답과 비교하여 Recall@k / Precision@k / Hit@k / nDCG@k / MRR 을 계산. 옵션으로 Judge LLM 의 응답 품질 0~5 점 채점도 가능.

**핵심 보장:**
- tie-breaker 명시 — `(−similarity, document_id asc)` 으로 stable sort, 결정성 보장
- Judge 모드 선택 — `reference-free`(기본, lexical overlap 차단) / `overlap` / `entailment`
- Judge 분산 측정 — `--judge-n-samples N` 시 N회 호출 median + std
- 실패 질의 명시 — `metric_failed=True` + 메트릭 키 None, summary 에 `failure_rate`
- 자기-평가 차단 — `--judge` + 분리 endpoint 없으면 `--allow-self-judge` 강제
- 다중 골드셋 변동성 — `--gold-set-glob` 시 mean/std/min/max aggregate

### 파라미터

#### 골드셋 입력

| 옵션 | 기본 | 설명 |
|---|---|---|
| `--gold-set / -g` | (필수, glob 와 택1) | 골드셋 YAML 단일 경로 |
| `--gold-set-glob` | (택1) | 글롭 패턴. 매칭된 N개 골드셋 순차 채점 + `{label}.aggregate.summary.json` 산출 |
| `--limit` | 0 (전체) | 평가할 질의 수 제한 |

#### 출력

| 옵션 | 기본 | 설명 |
|---|---|---|
| `--label` | `run` | 출력 파일 접두 (`baseline`, `multiview` 등) |
| `--output-dir` | `eval/runs` | 결과 저장 디렉터리 |

#### 검색 설정

| 옵션 | 기본 | 설명 |
|---|---|---|
| `--top-k` | 5 | 메트릭 계산용 top-k |
| `--max-chunks` | 10 | 검색 단계 `max_chunks`. top-k 보다 크게 잡아 over-fetch |
| `--similarity-threshold` | (config) | 유사도 임계 오버라이드 |
| `--rerank` | (config) | 리랭커 사용 여부 (true/false) |
| `--hyde` | (config) | HyDE 사용 여부 |
| `--include-graph / --no-graph` | True | 그래프 컨텍스트 포함 |

#### Judge

| 옵션 | 기본 | 설명 |
|---|---|---|
| `--judge` | False | Judge LLM 채점 활성 (느림, 비용 발생) |
| `--allow-self-judge` | False | `--judge` 활성 + 분리 endpoint 없으면 필수. summary 에 `judge_is_self=true` 기록 |
| `--judge-mode` | `reference-free` | `reference-free` (권장, source 미노출) / `overlap` (legacy) / `entailment` (NLI) |
| `--judge-endpoint`, `--judge-model`, `--judge-api-key`, `--judge-headers` | (config) | Judge 전용 LLM 오버라이드 |
| `--judge-seed-base` | None | Judge 결정성 seed base. 항목별 seed = `base + hash(item.id) % 10M` |
| `--judge-n-samples` | 1 | 분산 측정용 반복 호출 수. ≥2 시 median + std/min/max 기록. 비용 N배 — 진단용 |
| `--reasoning-mode` | `off` | LLM reasoning 프로파일 |

#### 그래프

| 옵션 | 기본 | 설명 |
|---|---|---|
| `--graph-match-threshold` | 0.78 | T4 (embedding) 임계값. 골드셋 metadata 의 기본값을 무시 |
| `--graph-match-strict` | False | T2/T3/T4 모두 skip — 1차 동작(정확 비교만) 재현 |
| `--score-relations` | False | 관계 채점 메트릭 (`graph_rel_*`) 산출 |

#### 기타

| 옵션 | 기본 | 설명 |
|---|---|---|
| `--config / -c` | `~/.context-loop/config.yaml` | config 파일 |
| `--concurrency` | 1 | 골드셋 내 항목 동시 처리. 4~8 권장 |
| `--verbose / -v` | False | DEBUG 로깅 |

### 출력

- `{label}.csv` — per-question 결과 (질의·정답·retrieved·메트릭·judge_score·`source_fetch_method`·`graph_t4_disabled` 등)
- `{label}.summary.json` — 집계 메트릭 + `config_summary` + `judge_mode`/`judge_is_self`/`failure_rate` 등
- 다중 골드셋: `{label}.aggregate.summary.json` — mean/std/min/max

### 실행 예시

**기본:**
```bash
python scripts/eval_search.py --gold-set eval/gold_set.yaml --label baseline
```

**Judge + 분산 측정:**
```bash
python scripts/eval_search.py \
    --gold-set eval/gold_set.yaml --label multiview \
    --judge --judge-endpoint http://judge-model:8080/v1 \
    --judge-mode reference-free --judge-n-samples 3 \
    --judge-seed-base 42
```

**다중 골드셋 변동성:**
```bash
python scripts/eval_search.py \
    --gold-set-glob "eval/gold_sets/git_code_*.yaml" \
    --label baseline
# → baseline_git_code_001.summary.json ... + baseline.aggregate.summary.json
```

---

## 3. `scripts/compare_runs.py` — baseline ↔ treatment paired 비교

### 역할

`eval_search` 가 산출한 두 개의 `*.summary.json` + `*.csv` 를 받아 paired 비교를 수행한다.

1. **config 동치성 검증** — 두 run 이 같은 `gold_set_sha256`, `embedding_model`, `llm_model`, `top_k`, `max_chunks`, `similarity_threshold`, `rerank_enabled`, `hyde_enabled`, `judge_mode` 인지 자동 확인. 불일치 시 종료(`--allow-config-mismatch` 로 강행 가능)
2. **per-question paired diff** — `id` 기준 inner join. `metric_failed=True` 행은 자동 제외
3. **통계 검정**:
   - paired Wilcoxon signed-rank (직접 구현, scipy 미의존)
   - `paired_bootstrap` — mean / 95% CI / **p_improve** (bootstrap 샘플 중 mean diff > 0 비율) / **Cohen's d (paired)** / **p_min_effect** (사용자 정의 최소 효과 통과율)
4. **N<10 경고** — Wilcoxon 정규근사가 부정확한 표본 크기는 stdout + summary `low_sample_warning` 으로 명시

### 파라미터

| 옵션 | 기본 | 설명 |
|---|---|---|
| `--baseline` | (필수) | baseline `summary.json` |
| `--treatment` | (필수) | treatment `summary.json` |
| `--baseline-csv` | (필수) | baseline per-question CSV |
| `--treatment-csv` | (필수) | treatment per-question CSV |
| `--out` | (생략 시 저장 안 함) | 비교 결과 JSON 저장 경로 |
| `--allow-config-mismatch` | False | EQUIVALENCE_KEYS 가 달라도 강행 (위험 — 거짓 개선 가능) |
| `--bootstrap-resamples` | 1000 | bootstrap resample 횟수 |
| `--seed` | 42 | bootstrap 결정성 시드 |
| `--min-effect-size` | 0.0 | `p_min_effect` 계산용 임계. 예: 0.02 → "treatment 가 2pp 이상 개선될 확률" |
| `--verbose / -v` | False | DEBUG 로깅 |

### 출력 (stdout + JSON)

각 메트릭별:
- `mean Δ` — paired difference 평균 (treatment − baseline)
- `CI95 lo/hi` — 95% bootstrap CI
- `p_imp` — P(treatment > baseline)
- `Cohen d` — paired effect size
- `p Wilc` — Wilcoxon signed-rank p-value (양측)
- `n` — paired 표본 수 (작으면 `*` 마킹)

### 실행 예시

```bash
# 기본
python scripts/compare_runs.py \
    --baseline   eval/runs/baseline.summary.json \
    --treatment  eval/runs/multiview.summary.json \
    --baseline-csv  eval/runs/baseline.csv \
    --treatment-csv eval/runs/multiview.csv

# 최소 효과 임계 명시 (예: recall 2pp 이상 개선 확률)
python scripts/compare_runs.py \
    --baseline ... --treatment ... \
    --baseline-csv ... --treatment-csv ... \
    --min-effect-size 0.02 \
    --out eval/runs/compare_baseline_vs_multiview.json
```

### 의사결정 가이드

- `p_imp ≥ 0.95` + `CI95 lo > 0` → 의미 있는 개선
- `Cohen d` 절댓값: 0.2 작은 효과 / 0.5 중간 / 0.8 큰 효과 (paired 기준)
- `p_min_effect` — 운영 의사결정용 임계 (예: 0.02 = 2pp). 0.95 이상이면 임계 통과 거의 확실

---

## 4. `scripts/calibrate_graph_match.py` — 그래프 τ 캘리브레이션

### 역할

`DEFAULT_GRAPH_MATCH_THRESHOLD = 0.78` 이 본 환경의 인덱싱된 graph_nodes 분포에 적절한지 검증한다. 양성(alias 일 가능성) / 음성(무관 또는 type-drift) 쌍의 cosine 분포에서 F1 최대 τ 를 찾아 권장값을 산출.

**쌍 정의 (S3 보강):**
- 양성 종류:
  - `trivial-normalize` — 같은 정규화 이름 + 같은 type (T3 자명 양성, baseline)
  - `alias-only` — 같은 type 다른 정규화 이름이지만 substring 또는 prefix 4글자 공유 (실제 T4 가 잡아내야 하는 어려운 케이스)
- 음성 종류:
  - `unrelated` — 다른 이름 + 다른 type
  - `type-drift` — 같은 이름 다른 type (`system → service` 시나리오, T4 type-agnostic 흡수가 false positive 만드는 경계)

### 파라미터

| 옵션 | 기본 | 설명 |
|---|---|---|
| `--config / -c` | `~/.context-loop/config.yaml` | config 파일 |
| `--n-neg` | 1000 | `unrelated` 음성 쌍 표본 수. 너무 적으면 통계 불안정 |
| `--seed` | 42 | 음성 쌍 샘플링 시드 (결정성) |
| `--output / -o` | (생략 시 stdout 만) | 결과 JSON 저장 경로 |
| `--apply` | False | 권장 τ 를 `graph_match.py:33` 의 `DEFAULT_GRAPH_MATCH_THRESHOLD` 에 자동 반영. `|Δ| ≥ 0.005` 일 때만 갱신 |
| `--verbose / -v` | False | DEBUG 로깅 |

### 출력

- stdout — τ 격자(0.50~0.95, 0.01 간격) 별 precision/recall/F1 표 + 권장 τ + `Δ vs default 0.78`
- `--output` 지정 시 JSON — `n_pos`/`n_neg`/`recommended`/`recommended_f1`/`delta`/`table`/sim_stats

### 실행 예시

```bash
# 진단만 (도구 실행 후 graph_match.py 수동 갱신 검토)
python scripts/calibrate_graph_match.py --output eval/graph_threshold_calibration.json

# 자동 반영 — 권장값이 default 와 0.005 이상 다르면 graph_match.py:33 자동 갱신
python scripts/calibrate_graph_match.py --apply

# 음성 표본 증가 (대규모 인덱스)
python scripts/calibrate_graph_match.py --n-neg 5000 --apply
```

### 권장 운영 주기

- 신규 도메인 인덱싱 후 1회
- 인덱싱 LLM 또는 임베딩 모델 변경 후 1회
- 분기별 정기 (도메인 어휘 drift 추적)

---

## 통합 운영 워크플로우 — A/B 비교

baseline 코드 → treatment 코드 변경의 효과 측정.

```bash
# 1. (옵션) 그래프 τ 캘리브레이션 — 신규 환경이면 1회
python scripts/calibrate_graph_match.py --apply

# 2. 골드셋 빌드 — 다중(N=5) 으로 변동성 측정 가능하게
python scripts/build_synthetic_gold_set.py \
    --generator-endpoint http://strong-model:8080/v1 \
    --generator-model gpt-4o \
    --judge-endpoint http://other:8080/v1 \
    --judge-model claude-haiku \
    --seed 42 --generator-seed-base 1000 \
    --n-gold-sets 5 --concurrency 4 \
    --include-graph-questions --score-relations \
    --output eval/gold_sets/run.yaml

# 3. baseline 평가
python scripts/eval_search.py \
    --gold-set-glob "eval/gold_sets/run_*.yaml" \
    --label baseline --concurrency 4 \
    --judge --judge-endpoint http://other:8080/v1 \
    --judge-model claude-haiku --judge-mode reference-free \
    --judge-n-samples 3

# 4. (코드 변경)

# 5. treatment 평가 — 같은 골드셋·같은 옵션
python scripts/eval_search.py \
    --gold-set-glob "eval/gold_sets/run_*.yaml" \
    --label treatment --concurrency 4 \
    --judge --judge-endpoint http://other:8080/v1 \
    --judge-model claude-haiku --judge-mode reference-free \
    --judge-n-samples 3

# 6. paired 비교 — 운영 의사결정용 통계
python scripts/compare_runs.py \
    --baseline   eval/runs/baseline.aggregate.summary.json \
    --treatment  eval/runs/treatment.aggregate.summary.json \
    --baseline-csv  eval/runs/baseline_run_001.csv \
    --treatment-csv eval/runs/treatment_run_001.csv \
    --min-effect-size 0.02 \
    --out eval/runs/compare.json
```

### 출력 해석

`compare_runs` 결과에서:
- `p_imp ≥ 0.95` + `CI95 lo > 0` + `Cohen d ≥ 0.5` → **유의미한 개선, 머지 가능**
- `p_imp ∈ [0.5, 0.95]` → **개선 신호 약함, 추가 표본 또는 다른 메트릭 확인**
- `p_imp < 0.5` → **개선 없음 또는 회귀**
- N < 10 (별표 마킹) → **표본 부족, 골드셋 N 증가**
- config mismatch — baseline/treatment 가 다른 설정에서 측정됨, 비교 의미 없음

## 트러블슈팅

| 증상 | 원인 / 해결 |
|---|---|
| `build` 가 `parser.error("...self-eval...")` 로 종료 | Generator/Judge 가 system LLM 과 동일. `--generator-endpoint`/`--judge-endpoint` 분리 또는 `--allow-self-eval` |
| `eval --judge` 가 `SystemExit` | Judge 분리 endpoint 없음. `--judge-endpoint`/`--judge-model` 추가 또는 `--allow-self-judge` |
| 그래프 골드셋 통과율 매우 낮음 | `stats.fail_*` 확인. `fail_non_unique_source` 다수면 그래프 정보가 generic — `--source-types` 좁히거나 distractor 다양화 검토 |
| `compare_runs` 가 종료 (`config mismatch`) | 두 run 의 `gold_set_sha256` 또는 핵심 설정 다름. 같은 골드셋·같은 옵션으로 재실행 |
| `compare_runs` 의 `Cohen d` 가 NaN/0 | 표본이 너무 작거나 분산 0. N 증가 또는 메트릭 분포 확인 |
| `calibrate` 가 양성 쌍 0개 | 인덱싱이 alias 를 별도 노드로 만들지 않음. `_build_pair_buckets` 의 alias-only 휴리스틱 (substring/prefix 4글자) 도 0개면 그래프 인덱싱 보강 필요 |

## 관련 문서

- 설치·환경 설정: [`docs/setup.md`](./setup.md)
- 시스템 설계: `CLAUDE.md` (프로젝트 루트)
- 평가 신뢰성 감사 하네스: `.claude/skills/rag-eval-audit/SKILL.md`
- 평가 시스템 패치 하네스: `.claude/skills/rag-eval-fix/SKILL.md`
