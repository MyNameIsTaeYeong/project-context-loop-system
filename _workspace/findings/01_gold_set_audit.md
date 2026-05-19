# Gold-Set Auditor — 합성 골드셋 생성 신뢰성 재감사 (S0/S1 패치 후)

## 한줄 판정

**MEDIUM 위험** — 운영 의사결정용 절대 메트릭의 단독 발표는 여전히 권장하지 않으나, S0/S1 패치(P1·P3·P4·P7·P10·P11) 로 이전 감사의 **Critical 1건 + High 4건이 실질적으로 해소**되었다. 자기 평가 편향은 CLI 옵트인 없이는 차단되며(P1+P3), 한국어 누설·그래프 trivial 매칭·`--no-filter` 오염도 결정론적 또는 경로 분리로 막혔다. 잔여 위험은 (1) Judge reasoning 미요구, (2) 정적 distractor pool, (3) 그래프 τ=0.78 캘리브레이션 부재, (4) LLM seed 미전달의 4건. 시스템 내부 회귀 테스트(A vs B) 와 함께 골드셋 metadata 의 `self_evaluation_warning=false` 가 확인된 경우 운영 메트릭 보조 근거로 사용 가능. 단독 인용은 여전히 금지.

## 검토 범위

| 파일 | 패치 전 줄 수 | 패치 후 줄 수 | 주요 진입점 |
| --- | --- | --- | --- |
| `_workspace/source/build_synthetic_gold_set.py` | 1197 | **1330** | `main()` → `build()` → `_run_chunk_mode()` / `_run_graph_mode()` |
| `_workspace/source/synth.py` | 673 | **835** | `generate_questions()`, `filter_question()`, `is_unique_source()` (NEW), `has_korean_proper_noun_leakage()` (NEW), `stratified_sample()` |
| `_workspace/source/llm.py` | 226 | **259** | `build_eval_llm_client()`, `role_is_configured()`, `_effective_role_target()` (NEW) |
| `_workspace/source/graph_match.py` | 548 | **569** | `match_entity_tiered()` (T1~T4), `build_embed_fn()` (T4 skip 메타 부착) |

증가된 줄 수: +470 (≈16% 증가). 신규 헬퍼 함수 7개 추가.

---

## 핵심 발견 (위험 등급순)

### [HIGH] 정적 distractor pool — 청크별 동일 distractor 2개로 (d2) 게이트가 형식화될 위험 (이전과 동일, 잔여)

- **증거**:
  - `build_synthetic_gold_set.py:401-404` — `distractor_pool` 을 한 번만 `rng.shuffle()`, 모든 청크가 같은 풀 사용.
  - `build_synthetic_gold_set.py:552-558` — 청크별로 `same_type_distractors = [c for c in distractor_pool if c["source_type"] == chunk["source_type"]][:n_distractors]`. 같은 source_type 내 첫 N=2개. **모든 청크가 같은 distractor 쌍**.
  - `synth.py:778-782` (d2 게이트) — `for distractor in distractors: if is_answerable(...): generic`.
- **이전 등급**: HIGH (H2 의 일부)
- **패치 적용**: 부분적. P4 가 (d1) `is_unique_source` 를 추가해 distractor 의존도를 줄였으나, (d2) distractor 다양성 자체는 미패치.
- **현재 등급**: HIGH (영향 축소 — P4 로 1차 검증선이 LLM 으로 분리됨)
- **잔여 영향**: 첫 distractor 가 우연히 정답 청크와 가까운 주제(같은 가이드 문서의 다른 청크)면 모든 청크에서 일괄 generic 판정 → 표본 다양성 손실. 반대로 거리가 멀면 (d2) 가 형식적이 된다. (d1) 가 추가됐기에 self-bias 위험은 분산됐으나, (d2) 자체의 정보 가치는 여전히 낮다.
- **권고**:
  1. `build_synthetic_gold_set.py:552-558` — 청크별로 `rng.sample(distractor_pool_same_type, k=n_distractors)` 로 매번 다시 추출 (rng 시드 결정성 유지).
  2. 또는 청크 BM25 거리 기준으로 "중간 거리" distractor 를 청크별 선별.

---

### [HIGH] Judge reasoning 미요구 — yes/no 1토큰 응답으로 self-bias 검증 불가 (이전과 동일, 잔여)

- **증거**:
  - `synth.py:677-684` `is_answerable()` — `max_tokens=64`, `temperature=0.0`, reasoning 없이 yes/no.
  - `synth.py:706-713` `is_unique_source()` (P4 신규) — 같은 패턴 (max_tokens=64, yes/no).
  - `synth.py:578-596` `parse_yes_no()` — `<think>` 태그는 처리하나 reasoning_mode 가 off 면 모델이 reasoning 을 생성하지 않음.
- **이전 등급**: HIGH (H2 의 일부)
- **패치 적용**: **미패치**. P4 가 프롬프트만 분리했고 reasoning 요구는 도입 안 함.
- **현재 등급**: HIGH (영향 축소 — P4 가 두 게이트의 의미를 분리해 self-bias 가 한쪽으로만 작용)
- **잔여 영향**: Judge 가 명확하지 않은 케이스에 yes/no 한쪽으로 치우쳐 판정해도 reasoning 부재로 검증 불가. 자기 평가 편향 차단(P1)은 분리 강제만 다루지 Judge 판단 품질은 별개.
- **권고**:
  1. `is_answerable` / `is_unique_source` 에 `reasoning_mode="on"` 옵션 추가 → endpoint 가 `<think>...</think>` 출력 후 결론 도출.
  2. 또는 두 게이트의 출력 포맷을 `{"reasoning": "...", "answer": "yes/no"}` JSON 으로 변경. `extract_json` 헬퍼 기 사용.

---

### [HIGH] 그래프 매칭 τ=0.78 캘리브레이션 근거 부재 (이전과 동일, 잔여 — S2 P15 로 이연)

- **증거**:
  - `graph_match.py:33-34` — `DEFAULT_GRAPH_MATCH_THRESHOLD = 0.78` + 주석 `"설계 §2.2 결정값."` 그대로.
  - `graph_match.py:299-322` — T4 가 `description` cosine 만으로 매칭(type-agnostic). 정상 paraphrase 도 false miss / 무관 서비스 description 유사도도 false hit 가능.
- **이전 등급**: HIGH (H4)
- **패치 적용**: **미패치**. 패치 SUMMARY 에 따르면 S2 P15 작업으로 이연. 단, P10 으로 trivial 매칭 경로는 차단(아래 별개 항목).
- **현재 등급**: HIGH (변화 없음)
- **잔여 영향**: T4 hit 의 score 분포가 임계값 부근에 몰리면 메트릭이 임계값 1%p 변동에 민감해진다. 빌드자(metadata 의 `graph_match_threshold_default`) 와 평가자 τ 가 다르면 silent 메트릭 불일치.
- **권고**:
  1. `scripts/calibrate_graph_match.py` 신규 작성 — alias 쌍 vs 무관 쌍의 description cosine 분포에서 F1 최적점 산출.
  2. 평가 측에서 `metadata.graph_match_threshold_default` 와 CLI `--threshold` 불일치 시 경고/에러.

---

### [MEDIUM] LLM seed 미전달 — `--seed` 가 샘플링만 결정. 같은 seed 라도 골드셋 내용이 흔들림 (이전과 동일, 잔여 — S2 P13 로 이연)

- **증거**:
  - `synth.py:614-621` `generator.complete()` 호출 — `temperature=0.7`, seed 인자 없음.
  - `synth.py:677-684` Judge 호출도 동일.
  - `build_synthetic_gold_set.py:372` `rng = random.Random(seed)` — Python random 만.
  - `llm.py` 전체 — seed 키 검색 hit 없음.
- **이전 등급**: MEDIUM
- **패치 적용**: **미패치** (S2 P13). 패치 SUMMARY 에 명시.
- **현재 등급**: MEDIUM (변화 없음)
- **잔여 영향**: 같은 `--seed 42` 두 번 빌드 → 골드셋 항목이 미세하게 달라짐. CI 회귀 테스트의 diff 가 항상 dirty. `metadata.seed=42` 가 재현성 환상을 만든다.
- **권고**:
  1. `LLMClient.complete()` 시그니처에 `seed=` 추가, OpenAI/vLLM endpoint 에 전달.
  2. `metadata.llm_seed_supported`, `metadata.generator_temperature`, `metadata.judge_temperature` 키 추가 — 결정성 보장 여부 자기 선언.

---

### [MEDIUM] 샘플링이 source_type 한 차원만 — 길이/난이도/degree 편향 (이전과 동일, 잔여)

- **증거**:
  - `build_synthetic_gold_set.py:392-394` — `stratified_sample(candidates, n_total=n_chunks, key="source_type", rng=rng)`. **단일 키**.
  - `synth.py:792-834` `stratified_sample()` — group key 별 round-robin. 그룹 자체는 1차원.
  - 길이 필터(`build_synthetic_gold_set.py:1111-1117`) — `[min_chars=200, max_chars=8000]` 외 균등화 없음.
  - 그래프 노드 `min_graph_neighbors >= 1` (`:1134`) 만 — degree 편향 미고려.
- **이전 등급**: MEDIUM
- **패치 적용**: **미패치**.
- **현재 등급**: MEDIUM (변화 없음)
- **잔여 영향**: 단일 source_type 빌드 시 stratification 효과 무력. 짧은/긴 청크, hub/leaf 노드가 같은 가중치로 샘플링 — 길이·degree 의 영향을 측정 못함.
- **권고**: 이전 감사의 권고 그대로 유지 (`len_bucket` 추가 stratification 키, degree 버킷).

---

### [LOW→MEDIUM] 메타데이터 chunk fingerprint 부재 — 인덱스 변경 후 stale 검증 불가 (이전과 동일, 잔여)

- **증거**:
  - `build_synthetic_gold_set.py:470-495` metadata dict — 8개 신규 키(P1) 가 추가됐지만 청크 hash/fingerprint 는 없음.
- **이전 등급**: LOW
- **패치 적용**: **부분적**. P8(별도 워크트리, `eval_search.py`) 으로 골드셋 전체 sha256 은 기록되나 청크 단위 hash 는 미적용.
- **현재 등급**: LOW (변화 없음 — 골드셋 전체 fingerprint 는 P8 로 도입)
- **잔여 영향**: 인덱싱 측 chunk_size 변경 시 같은 `source_document_id` 라도 본문 달라짐. anchor (앞 200자) 만으로는 검증 불완전.
- **권고**: `GoldItem.source_content_hash` 필드 추가.

---

### [LOW] 인덱싱-생성 LLM 동일 가능성 (이전과 동일, 잔여)

- **증거**:
  - `build_synthetic_gold_set.py` graph mode 의 `load_candidate_subgraphs()` 가 graph_store 에서 인덱싱 시 추출된 entity 를 그대로 읽음.
  - Generator/Judge 가 system LLM 으로 fall-through 시 (이제 `--allow-self-eval` 강제) 인덱싱 LLM 과 같을 수 있음.
- **이전 등급**: LOW
- **패치 적용**: 간접. P1 으로 `--allow-self-eval` 옵트인 시에만 fall-through 가능 + metadata 에 self-eval 표시. 그러나 인덱싱 LLM ID 와 비교 로직은 없음.
- **현재 등급**: LOW (변화 없음)
- **잔여 영향**: 사용자가 `--allow-self-eval` 명시한 경우 인덱싱·생성 LLM 동일성 위험이 표면화됨.
- **권고**: `metadata.indexing_llm_models` 키 추가 — `processing_history` 에서 사용된 LLM ID 들의 set.

---

### 신규 도입된 위험 (패치 트레이드오프)

#### [LOW] (d1) `is_unique_source` 추가로 인한 LLM 호출 증가 — 빌드 시간 +20~30%

- **증거**:
  - `synth.py:716-784` `filter_question()` — (a) `is_answerable` + (d1) `is_unique_source` + (d2) `is_answerable` × N_distractor = **2 + N** 회 호출. 이전: 1 + N.
- **영향**: 청크당 LLM 호출 1회 증가. `concurrency` 로 완화 가능. 새로운 누락 모드 없음 — `parse_error` 처리 경로 동일.
- **권고**: 운영 사용자에게 사전 공지 (패치 SUMMARY 에 이미 명시).

#### [LOW] (d1) `is_unique_source` 와 (d2) 의 의미 중복 가능성

- **증거**: (d1) "유일한 정답 출처인지" — 일반 위키/매뉴얼로도 답할 수 있다면 no. (d2) "무관 청크로도 답할 수 있는가" — yes 면 generic. **두 게이트 모두 "정답 청크 외 출처가 있는지" 를 다른 각도로 검증**.
- **영향**: (d1) 통과 + (d2) 탈락 의 비중이 stats 에서 보고되어야 게이트 단계간 정보 가치를 모니터링 가능. 현재는 `fail_non_unique_source` vs `fail_generic` 카운트가 분리되어 있어 사후 분석 가능 (`build_synthetic_gold_set.py:407-420` stats dict).
- **권고**: 운영 후 N회 빌드의 `fail_non_unique_source` 대비 `fail_generic` 분포를 확인. (d2) 가 거의 0 이면 (d1) 으로 충분 — (d2) 비활성화 옵션 검토.

#### [LOW] P10 의 description fallback 제거로 그래프 recall 감소 가능

- **증거**: `build_synthetic_gold_set.py:906` — LLM 이 evidence_description 빈 응답이면 description 도 빈 문자열. `graph_match.py:300-301` 에서 T4 skip.
- **영향**: 의도된 보수적 측정. trivial 1.0 hit 제거로 metric 절대값이 낮아질 수 있으나, 회귀 테스트(A vs B) 비교에는 영향 없음.
- **권고**: 평가 측에서 `description` 비율을 stats 로 보고 (eval-script-patcher 영역, P12 일부).

---

## 차원별 상세 점검

### 1. 샘플링 편향

- **변화 없음** (S0/S1 범위 밖). `stratified_sample` 의 stratification 키는 여전히 `source_type` 단일.
- 그래프 노드의 `min_graph_neighbors >= 1` 만 적용, degree 균등화 없음.
- **판정**: MEDIUM (변화 없음)

### 2. 역방향 생성의 정답 누설 (★ 가장 중요)

- **P7 검증 (한글 누설)**:
  - `synth.py:364-368` `_KOREAN_NOUN_RE = re.compile(r"[가-힣]{4,}")` — 4자 이상 연속 한글 매칭.
  - `synth.py:370-379` `_KOREAN_COMMON_NOUNS` — 흔한 어휘 화이트리스트("프로젝트", "데이터베이스" 등 화이트리스트 보강).
  - `synth.py:382-385` `_KOREAN_JOSA_RE` — 한국어 조사 정규식 — 토큰 stem 추출.
  - `synth.py:403-428` `extract_korean_proper_noun_candidates()` — 4자 이상 + 조사 stripping + 빈도 ≤ max_freq(=1) + stopword 제외.
  - `synth.py:431-448` `has_korean_proper_noun_leakage()` — stem substring 매칭 (조사 변화에 강건).
  - `synth.py:758-759` `filter_question()` 의 (b2) 단계로 통합.
- **P10 검증 (그래프 evidence DB fallback 제거)**:
  - `build_synthetic_gold_set.py:906` — `description = gq.evidence_description` (이전: `gq.evidence_description or str(sg.get("entity_description") or "")`). DB 폴백 제거 확인.
  - `graph_match.py:300-301` — `if not golden.description: return None` — T4 자동 skip 검증.
- **검출 범위 확인**: 한국어 사내 도메인(팀명·시스템명) → P7 게이트로 잡힘. 그래프 trivial 매칭 → P10 으로 차단.
- **잔여**: 영문/숫자 합성어, 2~3자 한글 고유명사("AWS", "QA팀") 는 `_IDENT_RE` 의 4자 이상 + `_KOREAN_NOUN_RE` 의 4자 이상 컷오프로 미커버. 그러나 짧은 토큰은 위양성 비용이 크므로 길이 컷오프는 합리적.
- **판정**: HIGH → **MEDIUM** (P7+P10 으로 주요 누설 경로 차단. 잔여 미커버 영역은 비주류 케이스)

### 3. Judge 4단계 게이트 효과성

- **P4 검증 (GENERIC 프롬프트 분리)**:
  - `synth.py:142-158` `GENERIC_PROMPT_TEMPLATE` — "유일한 정답 출처인지" 묻는 새 프롬프트.
  - `synth.py:129-139` `ANSWERABLE_PROMPT_TEMPLATE` — "답할 수 있는가" 묻는 기존 프롬프트. **두 프롬프트가 명확히 다른 의미**.
  - `synth.py:687-713` `is_unique_source()` 헬퍼 — `GENERIC_PROMPT_TEMPLATE` 사용, `purpose="goldset_judge_unique_source"`.
  - `synth.py:770-776` `filter_question()` 의 (d1) — `is_unique_source` 호출 → `non_unique_source` 사유로 탈락.
  - `synth.py:778-782` (d2) — distractor 보조 검증으로 남음.
- **게이트 단계 (5단계)**: (a) answerable → (b1) leakage → (b2) korean_leakage → (c) demonstrative → (d1) unique_source → (d2) generic.
- **잔여**: Judge reasoning 미요구 (max_tokens=64, yes/no 1단어) — 자기 친화 bias 가 여전히 작용 가능. P4 가 의미 분리로 영향을 분산했지만 근본 해결은 아님.
- **판정**: HIGH → **MEDIUM** (P4 로 (a)·(d1) 의미 분리, self-bias 한쪽 게이트 통과만으로는 충분치 않게 됨)

### 4. Generator/Judge 분리 강제성 ★

- **P1 검증 (self-eval 차단)**:
  - `build_synthetic_gold_set.py:1099-1105` — `--allow-self-eval` CLI 플래그 신규.
  - `build_synthetic_gold_set.py:1209-1218` — `role_is_configured()` 양쪽 체크.
  - `build_synthetic_gold_set.py:1219-1226` — `self_evaluation_warning and not args.allow_self_eval` 시 `parser.error()` 종료 (exit code 2).
  - `build_synthetic_gold_set.py:1227-1232` — 옵트인 시에만 warning 후 진행.
- **P3 검증 (role_is_configured 보강)**:
  - `llm.py:209-232` `_effective_role_target()` 신규 — CLI/eval/{role}/llm 폴백 체인 재현.
  - `llm.py:235-259` `role_is_configured()` — 단순 endpoint/model 채워짐 검사가 아닌 `(role_endpoint, role_model) != (system_endpoint, system_model)` 비교. **같은 endpoint+model 명시도 False 반환**.
- **P1 메타데이터 검증 (8개 신규 키)**:
  - `build_synthetic_gold_set.py:480-487` — `generator_model`, `generator_endpoint`, `judge_model`, `judge_endpoint`, `generator_configured_separately`, `judge_configured_separately`, `self_evaluation_warning`, `allow_self_eval` 8개 키 모두 기록.
  - `build_synthetic_gold_set.py:1234-1247` — `_resolve_eval_role_identity()` 로 effective model/endpoint 해석.
  - `build_synthetic_gold_set.py:1316-1323` `build()` 호출부에 8개 키 전달.
- **잔여**: 사용자가 `--allow-self-eval` 명시한 경우 fall-through 가능 — 의도적. metadata `self_evaluation_warning=true` 로 사후 추적 가능.
- **판정**: CRITICAL → **LOW** (자기 평가 편향 차단 + 메타데이터 추적성 + endpoint+model 동일성 검사 모두 도입)

### 5. 결정성 / 재현성

- **변화 없음** (S0/S1 범위 밖, S2 P13 로 이연).
- Python `random.Random(seed)` 만 시드, LLM seed 미전달, temperature 비기록.
- **판정**: MEDIUM (변화 없음)

### 6. 그래프 질문

- **P10 검증**: 위 차원 2 와 핵심 발견 [HIGH→해소→LOW] 항목 참고.
- **τ=0.78 캘리브레이션**: 잔여 (S2 P15).
- **alias 채점**: Generator 가 채운 aliases (`synth.py:545-553` 파싱) — 인덱싱 LLM 의 aliases 와 매칭. 두 LLM 이 같으면 self-loop 위험 — `--allow-self-eval` 차단으로 부분 완화.
- **T4 description 매칭**: P10 으로 LLM 이 빈 evidence 출력 시 T4 skip. trivial 매칭 경로 차단.
- **신규 graph_t4_disabled 표기 (P12 — graph_match.py)**:
  - `graph_match.py:158-181` `build_embed_fn()` 반환 함수에 `t4_disabled`, `skip_count` 속성 부착. 평가 시 T4 skip 여부 명시 추적.
- **판정**: HIGH → **MEDIUM** (P10 으로 trivial 매칭 차단, P12 로 T4 skip 표면화. τ 캘리브레이션은 잔여)

### 7. 메타데이터 / 추적성

- **P1 검증 (8개 신규 메타 키)**: 차원 4 참고.
- **P11 검증 (`--no-filter` 출력 분리)**:
  - `build_synthetic_gold_set.py:991-1006` `_unfiltered_output_path()` — `.UNFILTERED` 접미사 강제. 이중 접미사 방지.
  - `build_synthetic_gold_set.py:1258-1266` `main()` 에서 `args.no_filter` 시 경로 강제 변환 + stderr 경고.
  - `_numbered_output_path` 와 호환: `gold_set.UNFILTERED.yaml` → `gold_set.UNFILTERED_001.yaml`.
- **추가 (P8, 별도 워크트리)**: 골드셋 전체 sha256 (`eval_search.py` 측). `compare_runs.py` 신규로 동치성 검증.
- **잔여 누락**:
  - 청크별 content_hash (LOW)
  - 인덱싱 LLM ID (LOW — `--allow-self-eval` 케이스 대비)
  - `llm_seed_supported`, `temperature` (재현성 — S2 P13)
- **판정**: MEDIUM → **LOW** (P1 + P11 + P8 으로 핵심 추적성 확보)

---

## 종합 위험 매트릭스

| 차원 | 패치 전 등급 | 패치 후 등급 | 핵심 변화 |
| --- | --- | --- | --- |
| 1. 샘플링 편향 | MEDIUM | MEDIUM | 변화 없음 — source_type 단일 stratification 유지 |
| 2. 역방향 생성 정답 누설 | HIGH | **MEDIUM** | P7 (한글) + P10 (그래프 trivial) 으로 주요 경로 차단 |
| 3. Judge 게이트 효과성 | HIGH | **MEDIUM** | P4 로 (a)·(d1) 의미 분리, self-bias 한쪽으로만 작용. Judge reasoning 미요구 잔여 |
| 4. Generator/Judge 분리 강제 | **CRITICAL** | **LOW** | P1+P3 으로 차단 + 8개 메타키 기록 + 동일 endpoint/model 검사 |
| 5. 결정성/재현성 | MEDIUM | MEDIUM | 변화 없음 (S2 P13 이연) |
| 6. 그래프 질문 | HIGH | **MEDIUM** | P10 으로 trivial 매칭 차단. τ 캘리브레이션 잔여 |
| 7. 메타데이터/추적성 | MEDIUM | **LOW** | P1 + P11 + (P8 별도) 으로 핵심 추적성 확보 |

---

## 이전 감사 대비 변화 (Before/After)

| 항목 | 이전 등급 | 패치 인용 | 코드 검증 | 현재 등급 | 잔여 위험 |
| --- | --- | --- | --- | --- | --- |
| Self-eval fall-through 차단 | **CRITICAL** | P1: `parser.error()` + `--allow-self-eval` | `build_synthetic_gold_set.py:1220-1226` `parser.error()` 강제 종료 확인 | **LOW** | 옵트인 시 fall-through 가능 (의도) — metadata 로 추적 |
| 메타데이터 모델 ID 기록 | (CRITICAL 일부) | P1: 8개 신규 키 | `build_synthetic_gold_set.py:480-487` 8키 모두 기록 확인 | **LOW** | 인덱싱 LLM ID 미포함 |
| `role_is_configured` 보강 | (CRITICAL 일부) | P3: endpoint+model 동일성 검사 | `llm.py:235-259` `_effective_role_target` 추가, `(role_endpoint, role_model) != (system...)` 비교 | **LOW** | 없음 |
| 한국어 누설 검출 | **HIGH** | P7: `has_korean_proper_noun_leakage` + 조사 stripping | `synth.py:364-448` 신규 함수 + (b2) 단계 통합 (`:758`) | **MEDIUM** | 2~3자 한글 미커버 (길이 컷오프 트레이드오프) |
| GENERIC 프롬프트 분리 | **HIGH** | P4: `is_unique_source` + `non_unique_source` reason | `synth.py:142-158` 새 프롬프트, `:687-713` 새 함수, `:770-776` (d1) 추가 | **MEDIUM** | Judge reasoning 미요구 |
| 그래프 evidence DB fallback | **HIGH** | P10: `description = gq.evidence_description` (fallback 제거) | `build_synthetic_gold_set.py:906` 단일 표현식 확인. T4 자동 skip (`graph_match.py:300-301`) | **MEDIUM** | description 누락률 평가 측 보고 필요 (eval-script-patcher 영역) |
| `--no-filter` 출력 분리 | **HIGH** | P11: `.UNFILTERED` 접미사 강제 | `build_synthetic_gold_set.py:991-1006` 헬퍼 + `:1258-1266` 호출 | **LOW** | `_numbered_output_path` 호환 확인됨 |
| 그래프 τ=0.78 캘리브레이션 | **HIGH** | (S2 P15 이연) | `graph_match.py:33-34` 변경 없음 | **HIGH** | S2 작업 대기 |
| Judge reasoning 미요구 | **HIGH** | 미패치 | `synth.py:677-684`, `:706-713` max_tokens=64 yes/no 유지 | **HIGH** | S2 권고 |
| 정적 distractor pool | (HIGH 일부) | 미패치 | `build_synthetic_gold_set.py:401-404` 한 번만 shuffle. `:552-558` 첫 N개 | **HIGH** | S2 권고 |
| 길이/난이도/degree 편향 | **MEDIUM** | 미패치 | 변화 없음 | **MEDIUM** | S2 권고 |
| LLM seed 미전달 | **MEDIUM** | (S2 P13 이연) | `llm.py` seed 키 없음 | **MEDIUM** | S2 작업 대기 |
| concurrency id 비결정성 | **MEDIUM** | 부분 (gather 순서 결정적, LLM 응답만 흔들림) | `build_synthetic_gold_set.py:649-664` gather + idx 순 id 부여 | **MEDIUM** | LLM seed 와 함께 처리 |
| chunk fingerprint 부재 | **LOW** | (P8 골드셋 전체 sha256 만) | 청크 단위 hash 미적용 | **LOW** | S2/S3 권고 |
| 인덱싱·생성 LLM 동일 | **LOW** | 간접 (P1 옵트인 차단) | `--allow-self-eval` 명시 시에만 가능 | **LOW** | 변화 없음 |

**신규 도입된 위험 (패치 트레이드오프):**

| 항목 | 등급 | 영향 |
| --- | --- | --- |
| (d1) `is_unique_source` LLM 호출 증가 (+20~30% 빌드 시간) | **LOW** | 운영 사용자 사전 공지 필요 |
| (d1)/(d2) 의미 중복 가능성 | **LOW** | stats 분포 모니터링으로 충분 |
| P10 description fallback 제거로 graph recall 절대값 감소 | **LOW** | 의도된 보수적 측정 — 회귀 비교는 영향 없음 |

---

## 운영 권고

### 사용 가능 범위 (이전 대비 확대)

- **이전과 동일**: 시스템 내부 회귀 테스트 (A vs B), chunk-only 모드 difficulty=easy 정성 리뷰.
- **확대 (조건부)**: 운영 메트릭의 **보조 근거**. 단, 다음 조건 모두 충족 시:
  1. 골드셋 metadata 의 `self_evaluation_warning == false` (Generator/Judge 가 별도 LLM 으로 분리).
  2. `filter_applied == true` (파일명에 `.UNFILTERED` 없음).
  3. `compare_runs.py` 의 `gold_set_sha256` 동치성 확인됨.
  4. 그래프 메트릭은 `graph_t4_disabled == false` 확인.

### 사용 금지 범위 (이전과 동일)

- **단독 절대 메트릭 발표** ("우리 검색 정확도 X%") — Judge reasoning 부재 + τ 캘리브레이션 부재로 절대값 신뢰도 부족. 회귀 비교 또는 보조 근거로만.
- **외부 벤치마크/경쟁 분석** — 자기 평가 편향 차단(P1) 적용했더라도 LLM seed 미전달로 재현성이 보장되지 않아 외부 검증 불가.
- **`--no-filter` 생성 골드셋의 운영 사용** — P11 으로 파일명 분리됐으나 `metadata.filter_applied=False` 명시 확인 필요.
- **`--allow-self-eval` 명시 빌드의 운영 사용** — metadata `self_evaluation_warning=true` 시 회귀 테스트 외 용도 금지.

### 보완 작업 우선순위 (이전 보고서 대비 재정렬)

1. **[즉시]** Judge reasoning 도입 — `is_answerable` / `is_unique_source` 에 `reasoning_mode="on"` 또는 JSON 출력. (잔여 HIGH)
2. **[즉시]** Distractor pool 청크별 dynamic 추출 — `build_synthetic_gold_set.py:552` `rng.sample(...)` 패턴. (잔여 HIGH)
3. **[1주 내]** S2 P15 — 그래프 τ 캘리브레이션 스크립트. (잔여 HIGH)
4. **[1주 내]** S2 P13 — LLM endpoint seed 전달 인프라. (잔여 MEDIUM)
5. **[2주 내]** stratified_sample 다중 키 (`source_type`, `len_bucket`, degree). (잔여 MEDIUM)
6. **[1개월 내]** 청크 단위 content_hash + 인덱싱 LLM ID metadata. (잔여 LOW)
7. **[1개월 내]** (d1)/(d2) 통과율 분포 모니터링 → 필요 시 (d2) 비활성화 옵션. (트레이드오프 검토)

---

## 신뢰도 등급 변화 요약

| 등급 | 패치 전 (이전 감사) | 패치 후 (현 감사) |
| --- | --- | --- |
| Critical | 1 | 0 |
| High | 4 | 3 (Judge reasoning, distractor pool, τ 캘리브레이션) |
| Medium | 3 | 3 (샘플링, 재현성, distractor 의미 중복 신규 LOW) |
| Low | 2 | 4 (메타데이터 미세 누락 + 신규 트레이드오프 3건) |
| **전체** | **HIGH 위험** | **MEDIUM 위험** |

S0/S1 12건 패치 중 본 감사 범위(P1·P3·P4·P7·P10·P11) 6건이 모두 코드에서 실제로 적용·동작함을 확인. 자기 평가 편향(Critical 1건) + 정답 누설 방어 부재(High 2건) + Judge 게이트 우회 가능성(High 1건의 일부) 가 해소되어 **운영 의사결정용 보조 근거로 조건부 사용 가능** 수준에 도달. 단독 절대 메트릭 발표는 여전히 제한.
