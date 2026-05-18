# Gold-Set Auditor — 합성 골드셋 생성 신뢰성 감사

## 한줄 판정

**HIGH 위험** — 운영 의사결정용 절대 메트릭(예: "검색 정확도 X% 달성") 도출에 단독 사용 금지. 자기 평가 편향 차단 부재(Critical 1건), 정답 누설 방어 미흡(High 2건), Judge 게이트 우회 가능(High 1건) 때문. 시스템·튜닝 간 **상대 비교**(A vs B regression test) 용도로는 사용 가능. 단, 같은 골드셋에서 같은 임베딩/LLM family 가 평가될 때만 유효.

## 검토 범위

| 파일 | 줄 수 | 주요 진입점 |
| --- | --- | --- |
| `_workspace/source/build_synthetic_gold_set.py` | 1197 | `main()` → `build()` → `_run_chunk_mode()` / `_run_graph_mode()` |
| `_workspace/source/synth.py` | 673 | `generate_questions()`, `filter_question()`, `stratified_sample()`, `has_identifier_leakage()`, `has_demonstrative_reference()` |
| `_workspace/source/llm.py` | 226 | `build_eval_llm_client()`, `role_is_configured()` |
| `_workspace/source/graph_match.py` | 548 | `match_entity_tiered()` (T1~T4 cascade), `DEFAULT_GRAPH_MATCH_THRESHOLD=0.78` |

보조 참조: `gold_set.py` (다른 워크트리, 같은 모듈) — `GoldSet.metadata` 구조 확인.

---

## 핵심 발견 (위험 등급순)

### [CRITICAL] Self-evaluation 편향이 경고만 출력될 뿐, 실제로는 그대로 진행되며 메타데이터에도 기록되지 않는다

- **증거**:
  - `build_synthetic_gold_set.py:1116-1131` — Generator/Judge 가 모두 `llm.*` 로 fall-through 시 `logger.warning()` 단 한 줄. 실행은 그대로 진행.
  - `build_synthetic_gold_set.py:445-455` — `metadata` dict 에 Generator/Judge 모델 ID, endpoint, "self-eval 여부" 가 **하나도** 들어가지 않는다.
  - `llm.py:137-141` (docstring) — "role 별 설정과 CLI 오버라이드가 모두 비면 system LLM (`llm.*`) 과 같은 클라이언트가 생성된다 (자기 평가 편향 가능 — 호출부에서 경고)" — 호출부 경고는 stdout/stderr 한 줄로 끝.
- **영향**: 사용자가 `--generator-model` / `--judge-model` 을 지정하지 않으면 같은 모델이 질문도 만들고 자기 질문이 좋은지 평가까지 한다. 4단계 게이트(특히 (a) 답변가능성, (d) 일반성)는 양쪽 모두 같은 분포의 모델이 판정하므로, **Generator 가 만든 모호한 질문도 Judge 가 통과시킬 가능성이 매우 높다** (자기 친화 bias). 게다가 골드셋 YAML 에 "이 골드셋은 self-eval 위험" flag 가 없어, 평가 단계에서 이 사실이 사라진다. 운영 의사결정자가 "골드셋 메트릭 95%"를 보고 안심하지만 실제 측정값일 가능성이 있다.
- **권고**:
  1. `build_synthetic_gold_set.py:1126` 의 `if not (gen_configured or judge_configured):` 분기에서 `logger.warning` 다음에 `parser.error("--allow-self-eval 없이는 진행 불가")` 형식으로 강제 종료하거나, `args.allow_self_eval=True` 명시 시에만 허용.
  2. `build_synthetic_gold_set.py:445-455` 의 `metadata` 에 다음 필드 추가:
     ```python
     "generator_model": <effective model id>,
     "judge_model": <effective model id>,
     "generator_endpoint": <effective endpoint>,
     "judge_endpoint": <effective endpoint>,
     "self_evaluation_warning": (not (gen_configured or judge_configured)),
     ```
  3. `llm.py:209-226` `role_is_configured()` 은 endpoint/model 만 체크 — 같은 endpoint 에 같은 model 을 양쪽에 명시한 경우도 "configured=True" 로 잘못 판정한다. `generator==judge` (endpoint+model 동일) 비교 로직 추가 필요.

---

### [HIGH] 역방향 생성의 정답 누설 방어가 ASCII 식별자에 한정 — 한글 도메인 용어는 그대로 누설된다

- **증거**:
  - `synth.py:299` — `_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")` — **ASCII only**. `re` 모듈 기본 동작상 `[A-Za-z]` 는 한글에 매칭되지 않으며, `re.UNICODE` 도 영향 없다.
  - `synth.py:311-327` `extract_unique_tokens()` — 같은 ASCII-only 정규식 사용. 결국 한글 청크 본문에서 추출되는 토큰 = 영문 식별자뿐.
  - `synth.py:88-120` `GENERATE_PROMPT_TEMPLATE` — 한국어 프롬프트로 "식별자를 그대로 베끼지 말라"고 안내하나, 청크 본문 자체가 인라인 삽입(`{chunk_content}`) 되어 Generator 가 직접 읽음.
  - `synth.py:104` 예시 "`_clamp_max_per_tenant 함수가 뭔가요?`" → "테넌트별 최대치 제한 로직은 어떻게 동작하나요?" — **영문 식별자 → 한국어 의미 풀어쓰기** 시나리오에만 가이드.
- **영향**: 청크가 한국어 도메인 문서(예: Confluence "결제 한도 처리 가이드")일 때, 청크에 "**결제 한도 처리**"가 있고 LLM 이 질문에 "**결제 한도 처리**는 어떻게 동작하나요?"를 그대로 적어도, `has_identifier_leakage()` 는 한글 토큰을 인식하지 못해 False 를 반환한다. BM25/임베딩 검색은 어휘 중첩으로 100% hit → 검색 품질이 거의 항상 과대평가.
- **권고**:
  1. `synth.py:299` 의 `_IDENT_RE` 를 multi-script 토큰 정규식으로 교체. 예:
     ```python
     _IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}|[가-힣]{2,}")
     ```
  2. 별도로 한국어 명사구(n-gram) 중첩률을 계산하는 함수 추가: 질문과 청크의 4-gram(글자 단위) Jaccard 가 임계 이상이면 leakage. 임계는 distractor 쌍의 baseline n-gram Jaccard 분포에서 95th percentile 로 캘리브레이션.
  3. `synth.py:88-120` 프롬프트에 한국어 도메인 어휘 패러프레이즈 예시 추가 — "결제 한도 처리 → 거래 금액 상한 검증" 같은 의역 강제.

---

### [HIGH] Judge 의 답변가능성·일반성 게이트가 동일한 yes/no 프롬프트로 평가 — Generator 가 청크 거의 그대로 베껴 만든 질문은 항상 통과한다

- **증거**:
  - `synth.py:123-133` `ANSWERABLE_PROMPT_TEMPLATE` 와 `synth.py:136-146` `GENERIC_PROMPT_TEMPLATE` — **두 프롬프트가 완전히 동일하다**. 단지 호출 시 정답 청크 vs distractor 청크를 바꿔 넣을 뿐.
  - `synth.py:559-580` `is_answerable()` — Judge 가 답변가능성에만 yes/no 답하며 **reasoning 요구 없음**. `max_tokens=64`, `temperature=0.0`.
  - `synth.py:617-621` 일반성 게이트 — distractor 들에 대해 같은 `is_answerable()` 을 호출, 하나라도 yes 이면 generic 으로 탈락.
  - 일반성 게이트 distractor 풀: `build_synthetic_gold_set.py:519-525` — "같은 source_type 의 distractor_pool 첫 N개"를 정적으로 가져옴. **모든 chunk 가 같은 distractor 2개로 평가됨** (n_distractors 기본 2).
- **영향**:
  1. 청크와 질문이 어휘적으로 거의 동일하면 Judge 는 "yes, 답할 수 있다"를 명백히 출력 → 답변가능성 통과. 동시에 distractor 청크와는 무관하므로 "no" → 일반성 통과. 즉 Judge 는 **누설된(베낀) 질문을 가장 잘 통과시키는 게이트**다.
  2. distractor 가 정적 첫 N 개라, 만약 첫 distractor 가 우연히 정답 청크와 가까운 주제(예: 같은 가이드 문서의 다른 청크)면 일반성 게이트가 과도하게 탈락시켜 표본 다양성 손실. 반대로 거리가 멀면(랜덤 셔플 후 첫 2개) generic 게이트가 거의 형식적이 된다.
  3. Judge 가 reasoning 없이 yes/no 단 1개 토큰 출력 — `synth.py:478-492` `parse_yes_no()` 는 "yes/y/true/예/네" 또는 "no/n/false/아니오/아니요"만 인정. **답이 yes 인지 명확하지 않을 때 None 반환** 후 `synth.py:601-602` 에서 `parse_error` 로 탈락. 이 동작 자체는 안전하나, Judge 가 분명한 self-bias 로 "yes" 만 답하면 reasoning 부재로 그 판단을 검증할 수 없다.
- **권고**:
  1. `synth.py:136-146` `GENERIC_PROMPT_TEMPLATE` 을 다음과 같이 변경 — distractor 의 일반화 가능성을 더 적극 탐지:
     ```
     "이 문맥이 위 질문에 대한 **유일한 정답 출처**라고 단정할 수 있는가? 
      문맥에 명시되지 않은 정보로 답해야 한다면 'no'. 
      다른 일반 문서에서도 같은 답을 얻을 수 있다면 'no'.
      yes/no 한 단어로만 답하라."
     ```
  2. `synth.py:599-622` `filter_question()` 에 게이트 (e) 추가: "**역방향 매칭 게이트**" — Judge 에게 "이 질문이 청크 본문의 표현을 그대로 베낀 것 같은가?" 를 묻고 yes 면 탈락.
  3. `build_synthetic_gold_set.py:519-525` distractor 선택 로직을 청크별 셔플로 변경 (rng 재사용). 또는 청크당 distractor 를 정답 청크와의 의미적 거리 기준으로 선별 (BM25 거리 중간 수준).
  4. `synth.py:573-579` `is_answerable()` 의 `max_tokens=64` 는 reasoning 여지 없음. reasoning_mode 사용 시 chain-of-thought 후 결론을 yes/no 로 forcing 하도록 프롬프트 변경.

---

### [HIGH] 그래프 매칭 임계값 0.78 의 근거가 코드/주석 어디에도 없으며, 평가 시 변경에 대한 결정성 보장이 없다

- **증거**:
  - `graph_match.py:33-34` — `DEFAULT_GRAPH_MATCH_THRESHOLD = 0.78` + `"T4 임베딩 cosine 임계값 기본. 설계 §2.2 결정값."` 주석. 설계 문서 §2.2 가 어디인지 코드 안에서 추적 불가.
  - `graph_match.py:278-301` T4 단계 — `description` 임베딩 cosine 이 `threshold` 이상이면 매칭. **type 일치 무시**(line 9 docstring). 즉 description 만으로 매칭됨.
  - `build_synthetic_gold_set.py:1056-1061` — CLI 로 `--graph-match-threshold` 변경 가능. 골드셋 metadata 에 기록되긴 함(line 460: `metadata["graph_match_threshold_default"] = graph_match_threshold`). 그러나 **평가 코드가 평가 시 다른 τ 를 받아도 강제로 metadata 의 값을 따르지 않는다** — eval_search.py 코드를 들여다보지 못했으나, build 측이 강제하는 코드는 없음.
- **영향**: 0.78 이 너무 낮으면 description 이 비슷한 다른 엔티티(예: "결제 서비스" description 과 "주문 서비스" description 이 비슷한 시스템)가 false hit → recall 과대평가. 0.78 이 너무 높으면 정상 패러프레이즈가 false miss → recall 과소평가. 둘 다 임계값 캘리브레이션 증거가 없으므로, 어느 쪽인지 측정자도 모르는 채 메트릭이 나온다. 평가자와 빌드자가 다른 τ 를 쓰면 (예: 빌드 시 0.78, 평가 시 0.80) silent 한 메트릭 일치성 손실.
- **권고**:
  1. `graph_match.py:33-34` 에 캘리브레이션 절차 docstring 명시: "본 임계값은 N개 alias 쌍 vs N개 무관 쌍 임베딩 cosine 분포에서 F1 최적점으로 정해졌다 — 데이터 변경 시 `scripts/calibrate_graph_match.py` 로 재계산." 캘리브레이션 스크립트가 없으면 만들고, 결과 표를 docstring 에 박는다.
  2. `graph_match.py:288-301` T4 에 type 가중치 추가 옵션: `r_type == g_type` 이면 score, 다르면 score - 0.05. 동의어 시나리오는 alias 로 잡고, type 가변 시나리오는 별도 flag (`--allow-type-drift`) 로 명시 활성화.
  3. 평가 코드(`eval_search.py`)에서 골드셋 metadata 의 `graph_match_threshold_default` 와 CLI `--threshold` 불일치 시 에러 출력 — 빌드/평가 τ 분리 차단.

---

### [HIGH] Generator 가 청크 일부를 paraphrase 없이 그대로 evidence_description 으로 채워주는 그래프 모드 — 정답 누설의 두 번째 경로

- **증거**:
  - `synth.py:149-202` `GRAPH_GENERATE_PROMPT_TEMPLATE` — Generator 에게 `evidence_description` 을 "1~2 문장의 자연어로 풀어쓴 evidence"로 요청. 그러나 청크 본문(또는 subgraph snippet) 에 그대로 등장하는 description 을 LLM 이 그대로 베껴도 검출 장치 없음.
  - `build_synthetic_gold_set.py:866` `_make_graph_gold_item()` — `description = gq.evidence_description or str(sg.get("entity_description") or "")`. LLM 이 비워두면 **DB 의 원본 entity_description 을 그대로 사용**. 즉 그래프 인덱싱 시 추출된 description = 골드셋 evidence. 평가 시 검색 결과의 description 도 같은 출처 → cosine ~1.0 hit (자동 통과).
  - `graph_match.py:282-301` T4 매칭 — golden.description 의 임베딩과 retrieved.description 의 임베딩 cosine. 둘이 같은 텍스트면 1.0.
- **영향**: 그래프 모드 골드셋에서 T4 (embedding) tier hit 의 대부분은 "골든과 retrieved 가 같은 description text 를 공유"하는 사실상 ID 매칭이다. 실제 그래프 인덱싱이 **표기 변형/타입 변경/패러프레이즈** 에 강건한지 측정하는 게 목적인데, 측정값이 trivially 1.0 으로 부풀려져 인덱싱 시스템 평가가 무의미해진다.
- **권고**:
  1. `build_synthetic_gold_set.py:866` evidence_description fallback 을 제거. LLM 이 비웠으면 description 자체를 비우고 T4 skip 시키는 게 정직함.
  2. `synth.py:178-180` 프롬프트에 강제: "evidence_description 은 청크에 등장하지 않는 다른 표현으로 작성. 청크에 등장한 명사구를 사용하지 말 것."
  3. 평가 단계에서 `golden.description` 과 `retrieved.description` 의 문자 단위 Levenshtein < 5% 이면 그 hit 을 "trivial" 로 표시하고 별도 metric (`recall@k_nontrivial`) 으로 분리 보고.

---

### [HIGH] `--no-filter` 플래그가 모든 게이트를 무력화하나, 결과 골드셋이 운영용과 시각적으로 구분되지 않는다

- **증거**:
  - `build_synthetic_gold_set.py:1010-1012` CLI 옵션 `--no-filter` (디버그/탐색용).
  - `build_synthetic_gold_set.py:530-543` — `apply_filter=False` 면 모든 생성 질문이 그대로 `GoldItem` 으로 push. 게이트 4단계 모두 skip.
  - `build_synthetic_gold_set.py:447-455` metadata 에 `"filter_applied": apply_filter` 는 기록되나, 파일명/디렉토리 규칙으로 분리되지 않음. 같은 `eval/gold_set.yaml` 경로에 덮어쓸 수 있음.
- **영향**: 사용자가 한 번 `--no-filter` 로 디버그 실행 후 잊고 평가 파이프라인이 이 골드셋을 가져가면 자기 평가 편향+필터 부재의 이중 오류가 누적된다. CI/CD 에서 골드셋을 자동으로 끌어쓸 때 특히 위험.
- **권고**:
  1. `build_synthetic_gold_set.py:1010-1012` `--no-filter` 플래그 처리 시 출력 경로에 `.UNFILTERED` 접미사 강제 부여. 예: `eval/gold_set.yaml` → `eval/gold_set.UNFILTERED.yaml`.
  2. `eval_search.py` 같은 평가 스크립트에서 골드셋 로드 시 `metadata.filter_applied is False` 면 명시적 `--allow-unfiltered-goldset` 플래그 없이는 실행 거부.

---

### [MEDIUM] 샘플링 균등화가 source_type 만 — 토픽/길이/난이도 편향 노출

- **증거**:
  - `build_synthetic_gold_set.py:370-372` — `stratified_sample(candidates, n_total=n_chunks, key="source_type", rng=rng)`. **유일한 stratification 키가 source_type**.
  - `synth.py:630-673` `stratified_sample()` — group key 별 round-robin. 그룹 내 셔플은 가능하나 그룹 자체는 source_type 만.
  - 길이 필터: `build_synthetic_gold_set.py:157` — `[min_chars, max_chars]` 범위만 통과. 그 안에서 길이 균등 분포 보장 없음.
  - 난이도: `synth.py:113` 프롬프트에 "난이도 골고루 분포" 텍스트로 LLM 에게 부탁만 함. 빌드 측에서 difficulty 분포를 확인·재조정하는 코드 없음.
  - 그래프 노드: `build_synthetic_gold_set.py:777-779` — 같은 `stratified_sample` 을 source_type 기준으로만. degree(이웃 수) 편향 없음. `min_neighbors` (`min_graph_neighbors`) 1 이상 필터만 있음 — hub 노드가 도배될 가능성.
- **영향**:
  1. source_type 가 1개(예: `git_code` 단일 빌드)면 stratification 효과 자체가 무력. round-robin 으로 그룹 내 첫 청크들만 N 개 뽑힘.
  2. 짧은 청크(200~500자) vs 긴 청크(5000자+) 가 mix 되면, 긴 청크는 정보량이 많아 Generator 의 질문 품질도 다르다. 길이 편향 측정 불가.
  3. hub 노드(이웃 50+ 개)와 leaf 노드(이웃 1개) 가 같은 가중치로 샘플링 — 그래프 채점이 hub 위주가 되어 graph 시스템의 leaf 처리 능력을 측정 못함.
- **권고**:
  1. `synth.py:630` `stratified_sample()` 시그니처 확장: `keys: list[str] = ["source_type"]` 로 다중 stratification. 길이 버킷(`len_bucket`)을 사전 계산해 추가 키로.
  2. `build_synthetic_gold_set.py:155-167` 청크 로드 시 길이 버킷 부여:
     ```python
     out.append({..., "len_bucket": "short" if len(content) < 1000 else "medium" if len(content) < 3000 else "long"})
     ```
  3. 그래프 노드 샘플링 시 degree 버킷 추가 — `_workspace/source/build_synthetic_gold_set.py:248` `neighbor_ids` 길이로 `low/med/high` 분류.
  4. 빌드 후 metadata 에 `difficulty_distribution: {easy: 12, medium: 18, hard: 5}` 보고 — 사용자가 편향을 즉시 인지하도록.

---

### [MEDIUM] Generator 의 temperature=0.7 + 재현 시드 미주입 — 같은 seed 라도 같은 골드셋이 만들어지지 않을 수 있다

- **증거**:
  - `build_synthetic_gold_set.py:350` — `rng = random.Random(seed)`. Python `random` 만 시드.
  - `synth.py:510-516` `generate_questions()` — `generator.complete(..., temperature=0.7, ...)`. LLM 호출에 seed 전달 인자 없음.
  - `synth.py:549-555` `generate_graph_questions()` — 동일.
  - `synth.py:573-579` `is_answerable()` — `temperature=0.0` 사용하나 seed 무전달.
  - `build_synthetic_gold_set.py:1099-1114` `build_eval_llm_client()` — endpoint/model/api_key/headers 외에 seed 전달 인자 없음.
  - `llm.py` 전체 — seed 관련 키 검색 결과 없음.
- **영향**: 같은 `--seed 42` 로 두 번 빌드해도 LLM 응답 분포가 달라 골드셋 항목이 달라진다. metadata 에 `"seed": 42` 가 기록되지만 **실제로는 결정성 보장 없음** — 사용자가 재현 가능하다고 오해할 위험. multi-seed 빌드(`--n-gold-sets 5`)도 LLM 변동성이 stratified 샘플링 변동성과 섞여 분리 불가.
- **권고**:
  1. `synth.py:500-516` `generate_questions()` 의 `generator.complete()` 호출에 `seed=` 인자 추가 (OpenAI/vLLM/대부분의 endpoint 가 지원). `LLMClient` 인터페이스에 seed 파라미터 노출.
  2. `build_synthetic_gold_set.py:402` `_run_chunk_mode()` 진입 시 청크별 deterministic seed 계산: `chunk_seed = (base_seed * 31 + chunk_index)`.
  3. seed 가 전달되지 않거나 endpoint 가 지원하지 않으면 `metadata["llm_seed_supported"] = False` 로 기록 — 결정성 미보장임을 골드셋이 자기 선언.
  4. temperature 도 metadata 에 기록.

---

### [MEDIUM] `concurrency > 1` 시 결과 순서가 비결정적 → id 부여가 seed 와 무관해진다

- **증거**:
  - `build_synthetic_gold_set.py:616` — `results = await asyncio.gather(*tasks, return_exceptions=True)`. gather 는 입력 순서를 보존하므로 결과 list 순서는 결정적.
  - `build_synthetic_gold_set.py:626-631` `_run_chunk_mode()` — `for idx, r in enumerate(results, start=1): ... item.id = f"q{next_id:04d}"`. id 부여는 idx 순서, 즉 결정적.
  - **그러나** 동일 청크 내 LLM 응답 자체의 비결정성(위 MEDIUM 참고)이 results 내용을 흔든다. id 자체는 idx 순서지만 실제 GoldItem 내용은 청크 처리 동시성과 무관하게 흔들림.
  - `synth.py:617` — 일반성 게이트의 distractor 루프는 결정적 (list 순회).
- **영향**: id 순서는 보장되나 같은 id 에 다른 question 이 매핑될 수 있어 골드셋 diff 가 항상 dirty. CI 에서 골드셋 변경 감지 어려움.
- **권고**:
  1. `build_synthetic_gold_set.py:629` id 를 `next_id` 기반이 아닌 `hash(query) + chunk_index` 의 deterministic hash 로 부여. 청크와 질문이 같으면 id 도 같음.
  2. 위 [MEDIUM] LLM seed 와 함께 처리하면 완전한 결정성 도달.

---

### [LOW] 메타데이터에 chunk fingerprint(원본 hash) 가 기록되지 않음 — 인덱스 변경 후 골드셋 stale 검증 불가

- **증거**:
  - `build_synthetic_gold_set.py:445-455` metadata dict — `generated_at`, `n_chunks_sampled`, `seed`, `source_types`, `stats` 등이 있으나 **개별 청크의 hash/fingerprint 없음**.
  - 개별 GoldItem(`gold_set.py:185-215`) 에는 `source_document_id`, `source_text_anchor` (앞 200자 prefix) 가 들어가나 청크 본문 hash 는 없음.
  - 그래프 모드도 동일: `build_synthetic_gold_set.py:886-897` `_make_graph_gold_item()` 에 `primary_document_id` 만, entity description 의 hash 없음.
- **영향**: 인덱싱 파이프라인이 청크 분할 알고리즘(chunk_size, overlap) 을 변경하면 같은 `source_document_id` 라도 청크 본문이 달라진다. 골드셋의 `source_text_anchor` 만 봐서는 검증 불완전. 평가 측에서 stale 골드셋 검출 장치가 약함.
- **권고**:
  1. `build_synthetic_gold_set.py:159-166` 청크 로드 시 `content_hash = hashlib.sha1(content.encode()).hexdigest()[:16]` 추가.
  2. `GoldItem` 에 `source_content_hash: str = ""` 필드 추가 (`gold_set.py:158-183`).
  3. 평가 시 hash mismatch 면 warning + 매칭률 별도 보고.

---

### [LOW] 그래프 인덱싱·골드셋 생성·평가가 모두 같은 LLM 으로 돌면 모든 단계에 같은 편향이 직접 누적

- **증거**:
  - `build_synthetic_gold_set.py:182-298` `load_candidate_subgraphs()` — graph_store 에서 노드/엣지/description 을 그대로 읽어옴. 이 노드/description 은 인덱싱 시점에 `llm.*` (default LLM) 가 추출한 것.
  - `build_synthetic_gold_set.py:1099-1107` Generator 는 같은 `llm.*` 로 fall-through 가능.
  - `graph_match.py:283-301` 평가 T4 cosine 매칭의 embedding 도 같은 임베딩 모델 (`processor.embedding_model`).
- **영향**: 3중 self-loop — "LLM A 가 추출한 entity → LLM A 가 질문 생성 → LLM A 가 Judge → 임베딩 모델 B 가 매칭" 구조에서, LLM A 의 entity 추출 편향(예: 영문 표기 선호)이 골드셋과 평가 모두에 동시에 들어간다. 시스템 외 사용자가 다른 LLM 으로 검색하는 시나리오를 측정 불가.
- **권고**:
  1. 인덱싱 시 사용된 LLM 모델 ID 를 `processing_history` (CLAUDE.md 의 메타 DB) 와 비교해 골드셋 metadata 의 generator/judge 와 비교 — 같으면 경고.
  2. 골드셋 metadata 에 `"indexing_llm_models": [...]` 필드 추가 — 인덱싱 LLM 들의 ID 를 모은 set.

---

## 차원별 상세 점검

### 1. 샘플링 편향

- `stratified_sample` (`synth.py:630-673`) 은 **source_type 한 차원만** 균등화. 그룹 내 round-robin + `rng.shuffle(g)` 만 적용.
- 길이 필터(`build_synthetic_gold_set.py:157` `[min_chars=200, max_chars=8000]`) 는 outlier 제거일 뿐 분포 균등화 아님.
- 청크당 N개 질문 생성(`questions_per_chunk` 기본 2) — `synth.py:113` 프롬프트가 "난이도 골고루 분포"만 부탁하고 코드 측의 강제·검증 없음. **다양성 보장은 LLM 자율에 의존**.
- 그래프 노드 샘플링(`build_synthetic_gold_set.py:763-779`) — `min_neighbors >= 1` 만 보장. degree 균등화 없음 → hub 편향 가능.
- **판정**: MEDIUM. 단일 source_type 빌드 시 stratification 효과 자체가 없음. 길이/난이도/degree 편향에 코드가 무방비.

### 2. 역방향 생성의 정답 누설 (★ 가장 중요)

- **누설 방지 의도**: `synth.py:88-120` 프롬프트의 "식별자 그대로 베끼지 말 것" + `synth.py:299-344` `has_identifier_leakage()` + `synth.py:330-344` 단어 경계 검사.
- **실제 동작**:
  - `_IDENT_RE = r"[A-Za-z_][A-Za-z0-9_]{3,}"` 는 **ASCII 한정**. 한글 도메인 용어 누설을 전혀 검출 못함.
  - `_COMMON_WORDS` 화이트리스트는 영어 stopwords 28개. 한국어 stopwords 없음.
  - 그래프 모드 evidence_description 은 LLM 이 비우면 DB description 그대로 fallback (`build_synthetic_gold_set.py:866`) — 누설 직통 경로.
  - 답변가능성 게이트(`synth.py:559-580`) 와 일반성 게이트(`synth.py:617-621`) 모두 yes/no 단답 — Generator 가 청크를 그대로 베끼면 Judge 도 yes 만 답함.
- **판정**: HIGH. 한국어 청크에 대해 누설 검출이 사실상 작동하지 않는다. 그래프 모드는 별도로 trivial 매칭 경로가 열려 있다.

### 3. Judge 게이트 효과성

- 4단계 gates: (a) answerable (LLM yes/no), (b) identifier leakage (regex), (c) demonstrative reference (regex), (d) generic (LLM yes/no on distractors).
- (a)와 (d)는 동일 프롬프트(`synth.py:123-146`) — Judge 가 self-bias 면 양쪽 동시에 흐트러짐.
- (b)는 ASCII 식별자만, (c)는 한국어 지시어를 잘 잡으나 단순 정규식 — `synth.py:352-370` `_DEMONSTRATIVE_RE`. "이/위/아래/다음/해당/본 + 화이트리스트" 패턴은 합리적이지만 false negative 사례 다수 가능 (예: "그 함수의 동작은"의 "그"는 미커버).
- (d) distractor 풀이 정적 첫 N 개 (`build_synthetic_gold_set.py:519-525`) — 청크별 다양성 없음.
- 통과율 통계: `build_synthetic_gold_set.py:385-395` stats dict 에 `fail_*` 카운트 기록. 운영 시 통과율 50% 미만 또는 95% 초과면 게이트 캘리브레이션 문제 — 그러나 자동 경고 없음.
- Judge reasoning 미요구 — `synth.py:573-579` `max_tokens=64`, raw yes/no 만.
- **판정**: HIGH. (a)+(d) 의 self-bias 취약성 + Judge reasoning 부재 + distractor 다양성 부족.

### 4. Generator/Judge 분리 강제성 ★

- CLI 미지정 시: `build_synthetic_gold_set.py:1101-1114` `build_eval_llm_client(config, "generator"/"judge")` 가 `config.eval.{role}` → `config.llm.*` 순서로 폴백 (`llm.py:159-167`).
- `role_is_configured()` (`llm.py:209-226`) 는 endpoint/model 채워졌는지만 봄. **같은 endpoint+model 을 generator/judge 양쪽에 명시한 경우도 True 로 판정**.
- 양쪽 모두 fall-through 시 `build_synthetic_gold_set.py:1126-1131` `logger.warning(...)` 한 줄. 실행 차단 없음.
- 메타데이터 기록 없음(`build_synthetic_gold_set.py:445-455`) — 사용자가 경고를 못 봤다면 영구히 사실 소실.
- **판정**: CRITICAL. 분리 강제 메커니즘이 사실상 없음.

### 5. 결정성 / 재현성

- Python `random.Random(seed)` 만 시드 — `build_synthetic_gold_set.py:350`.
- LLM 호출에 seed 전달 없음 (`synth.py:510-555`). temperature=0.7 (생성), 0.0 (판단).
- `--n-gold-sets N` 다중 빌드: `build_synthetic_gold_set.py:1161` — `seed_i = base_seed + i - 1`. seed 차이는 작음 (1, 2, 3...) — Python random 은 충분히 다른 stream 이지만 LLM 의 sampling 까지는 영향 없음.
- 동시성: `concurrency > 1` 시 LLM rate 가변성으로 결과 도착 순서가 흔들릴 수 있으나 `gather` 가 입력 순서 보존 → id 부여는 결정적. 그러나 LLM 응답 내용 자체가 비결정적이므로 별 의미 없음.
- temperature, model, seed 가 metadata 에 기록 안 됨.
- **판정**: MEDIUM. 샘플링 단계는 결정적이나 LLM 단계 비결정적 → 실용적으로 재현 불가.

### 6. 그래프 질문

- 임계값 0.78: `graph_match.py:33-34`. 근거 주석 "설계 §2.2" — 코드 내 추적 불가. 캘리브레이션 스크립트 없음.
- T4 (`graph_match.py:278-301`) 는 type 무시 — 시나리오 ("system → service") 흡수 목적이나, 그만큼 false positive 위험 증가.
- alias 채점(T2, `graph_match.py:259-267`) 은 골드셋의 `aliases` 에 의존. aliases 는 Generator 가 채워줌(`synth.py:441-449`). **Generator 가 만든 alias 가 검색 결과의 alias 와 잘 매칭되도록 만들면 self-loop** — 그러나 검색 결과의 entity 는 인덱싱 LLM 이 만든 것이므로 두 LLM 이 같으면 누설.
- description 의존: T4 가 retrieved.description 의 embedding 을 lazy 계산(`graph_match.py:295`). retrieved description 이 golden description 과 동일 출처(같은 인덱싱 LLM)면 cosine 1.0 — trivial hit.
- 그래프 노드/엣지 추출 LLM = 골드셋 생성 LLM 가능성 매우 높음(둘 다 `llm.*` 폴백).
- **판정**: HIGH. 임계값 캘리브레이션 부재 + description trivial 매칭 + 인덱싱-생성 LLM 동일 가능성.

### 7. 메타데이터 / 추적성

- 기록되는 것: `generated_at`, `n_chunks_sampled`, `questions_per_chunk`, `filter_applied`, `seed`, `source_types`, `generation_modes`, `concurrency`, `stats` (`build_synthetic_gold_set.py:445-455`).
- 그래프 모드 추가: `embedding_model`, `graph_match_threshold_default`, `score_relations`, `embed_graph_evidence` (line 459-462).
- **누락된 중요 정보**:
  - Generator/Judge model ID, endpoint (자기 평가 편향 추적 불가).
  - Generator temperature, LLM seed (재현성 추적 불가).
  - 각 청크의 content_hash (인덱스 변경 후 stale 검출 불가).
  - 인덱싱 LLM ID (인덱싱-생성 self-loop 검출 불가).
- 골드셋 자체에 "self-eval 위험" flag 없음 → 평가 단계가 못 봄.
- **판정**: MEDIUM. 기록은 일부 있지만 결정적인 self-evaluation/재현성/stale 검증 메타가 빠져 있음.

---

## 종합 위험 매트릭스

| 차원 | 위험 등급 | 핵심 이슈 |
| --- | --- | --- |
| 1. 샘플링 편향 | MEDIUM | source_type 단일 stratification. 길이/난이도/degree 편향 무방비. |
| 2. 역방향 생성 정답 누설 | HIGH | ASCII-only 정규식으로 한글 누설 미검출. 그래프 evidence fallback 으로 trivial 매칭 경로 존재. |
| 3. Judge 게이트 효과성 | HIGH | (a)/(d) 동일 프롬프트, reasoning 미요구. distractor 정적·소량. self-bias 에 취약. |
| 4. Generator/Judge 분리 강제 | **CRITICAL** | fall-through 시 경고 한 줄로 실행. 메타데이터 누락. 같은 model 명시도 "configured" 로 통과. |
| 5. 결정성/재현성 | MEDIUM | LLM seed 미전달. temperature 비기록. 같은 `--seed` 라도 LLM 응답 변동. |
| 6. 그래프 질문 | HIGH | τ=0.78 캘리브레이션 근거 부재. description trivial 매칭. 인덱싱·생성 LLM 동일 가능. |
| 7. 메타데이터/추적성 | MEDIUM | Generator/Judge model 미기록. content_hash 부재. self-eval flag 부재. |

---

## 운영 권고

### 사용 가능 범위

- **시스템 내부 회귀 테스트 (A vs B)**: 같은 골드셋으로 두 검색 시스템 변형 (예: 청크 크기 512 vs 768) 의 메트릭 차이를 비교하는 용도. 절대값이 부풀려져 있어도 상대 차이는 의미 있을 가능성 있음. 단 self-eval 편향이 한쪽에 유리한 방식으로 작용하지 않는다는 가정 하.
- **chunk-only 모드의 difficulty=easy 항목 정성 리뷰**: 사람이 골드셋을 샘플링해 매뉴얼 검증한 뒤 small batch 자동 평가.

### 사용 금지 범위

- **운영 의사결정용 절대 메트릭 발표** ("우리 검색 정확도 92%") — Critical/High 이슈들이 누적되어 메트릭 부풀림이 확실시됨.
- **외부 비교 (벤치마크 발표, 경쟁 분석)** — 자기 평가 편향 차단 없는 골드셋으로 측정한 값은 외부 신뢰성 0.
- **그래프 시스템의 "강건성" 주장 근거** — τ=0.78 캘리브레이션 부재 + description trivial 매칭으로 strict_t1 외의 모든 tier 메트릭 무효.
- **`--no-filter` 로 생성된 골드셋의 운영 사용** — 파일명 분리 없이 덮어쓰일 수 있어 우발적 사용 위험.

### 보완 작업 (우선순위 순)

1. **[즉시]** `build_synthetic_gold_set.py:1126` self-eval fall-through 차단:
   - `--allow-self-eval` 명시 없으면 `parser.error()` 강제 종료.
   - 같은 model+endpoint 가 generator/judge 에 모두 매핑된 경우도 차단.
   - metadata 에 `generator_model`, `judge_model`, `self_evaluation_warning` 필드 추가.

2. **[즉시]** `synth.py:299` `_IDENT_RE` 를 한글 포함 multi-script 정규식으로 교체. `has_identifier_leakage()` 의 unit test 추가 (한국어 청크 + 베낀 질문 → True 반환 확인).

3. **[1주 내]** `build_synthetic_gold_set.py:866` 그래프 evidence_description 의 DB fallback 제거. 또는 fallback 사용 시 `description_was_db_fallback=True` 마커를 GoldItem 에 박고, 평가 메트릭 보고에서 이 항목 비율을 노출.

4. **[1주 내]** `synth.py:136-146` `GENERIC_PROMPT_TEMPLATE` 을 "유일한 정답 출처인지 묻는" 더 엄격한 프롬프트로 교체. distractor pool 을 청크별 dynamic 선별로 변경.

5. **[2주 내]** `graph_match.py:33` τ 캘리브레이션 스크립트 작성. alias 쌍 vs 무관 쌍 cosine 분포로 F1 최적점 계산. 결과를 docstring 에 박는다.

6. **[2주 내]** LLM seed 전달 인프라 구축: `LLMClient.complete()` 시그니처에 `seed` 추가, endpoint 지원 시 전달. metadata 에 `llm_seed_supported`, `temperature` 기록.

7. **[1개월 내]** `--no-filter` 출력 경로에 `.UNFILTERED` 접미사 강제. `eval_search.py` 측에서 `metadata.filter_applied is False` 면 명시적 플래그 없이 실행 거부.

8. **[1개월 내]** 청크 `content_hash` 를 GoldItem 에 박고, 평가 시 인덱싱된 청크와 hash 비교해 stale 검출.

이 작업들이 완료될 때까지, **이 골드셋으로 측정한 메트릭은 모두 "내부 회귀 비교용"으로만 표시**하고 운영 의사결정 회의 자료에서 단독 인용을 금지한다.
