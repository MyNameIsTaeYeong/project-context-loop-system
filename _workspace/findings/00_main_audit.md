# RAG 평가 신뢰성 — 메인 직접 감사

작성: 메인 (서브 감사관 결과와 별도)
대상: origin/main 의 `scripts/build_synthetic_gold_set.py` (1197줄) + `scripts/eval_search.py` (1042줄) + 의존 모듈 4개 (`eval/synth.py` 673줄, `eval/metrics.py` 172줄, `eval/llm.py` 226줄, `eval/graph_match.py` 548줄)

## TL;DR

이 시스템은 **동일 환경에서의 A/B 비교**에는 신뢰할 만하다. 메트릭 구현이 표준이고, 결정론적 누설 게이트(식별자/지시대명사 정규식)와 일반성 게이트(distractor 청크)가 실제로 코드로 강제된다. 그러나 다음 한계가 있어 **외부 벤치마크 절대 점수**, **모델 교체 의사결정의 단독 근거**, **운영 출시 게이트**로는 부적합하다:

1. 재현성 — `--seed`로 샘플링만 결정되고 LLM 호출은 `temperature=0.7` 비결정적 (synth.py:513). 같은 seed로 재빌드해도 다른 골드셋.
2. 감사 추적성 — 골드셋 YAML 메타데이터에 **사용된 generator/judge 모델 ID가 기록되지 않는다** (build:445-462). 사후에 self-eval 빌드인지 확인 불가.
3. 한글 누설 — 식별자 누설 게이트가 ASCII만 검사 (synth.py:299). 사내 한국어 문서의 팀명·시스템명·서비스명 같은 한글 고유명사는 누설되어도 통과.
4. Judge fall-through — 사용자가 옵션 미지정 시 system LLM과 같은 모델이 Judge가 되어 자기 답을 자기가 채점. 경고 로그는 뜨지만 진행은 막지 않고, 메타데이터에도 명시 플래그가 없다.

## Top 8 위험 (등급순)

### [CRITICAL] C1. 골드셋 메타데이터에 생성 LLM 정보 누락
- **증거:** `build_synthetic_gold_set.py:445-462` — `metadata` dict에 `generated_at`, `seed`, `stats`, `embedding_model`(graph 모드만), `graph_match_threshold` 등은 기록되지만 **`generator_model`, `generator_endpoint`, `judge_model`, `judge_endpoint` 키가 없다**.
- **영향:** 생성된 골드셋이 self-eval 조건(generator=judge=system LLM)에서 빌드되었는지 사후 확인 불가. 누군가 옵션 없이 빌드한 골드셋과 분리 설정으로 빌드한 골드셋을 메타데이터로 구분할 방법이 없다. 평가 결과의 재현성·감사 추적성의 핵심.
- **권고:** `build:445`의 metadata에 다음 키 추가
  ```python
  "generator_model": config.get("eval.generator.model") or config.get("llm.model"),
  "generator_endpoint": config.get("eval.generator.endpoint") or config.get("llm.endpoint"),
  "judge_model": ...,
  "judge_endpoint": ...,
  "generator_configured_separately": gen_configured,  # bool — main 함수에서 이미 계산됨
  "judge_configured_separately": judge_configured,
  ```

### [CRITICAL] C2. `_fetch_source_text`의 첫 청크 fallback이 Judge 채점 오염
- **증거:** `eval_search.py:346-375` — `source_text_anchor` prefix 매칭 실패 + `source_chunk_id` 매칭 실패 → `if chunks: return chunks[0].get("content") or ""` (L369-370). Judge가 정답 청크가 아닌 임의 첫 청크를 정답 근거로 받음.
- **영향:** 문서가 N개 청크로 나뉘면 정답이 첫 청크가 아닐 확률 (N-1)/N. anchor 매칭이 어떤 사유로 실패한 항목들은 Judge가 잘못된 근거로 채점하고, `judge_score` 평균이 거짓.
- **권고:** fallback 발생 시 row에 `source_fetch_method="anchor"|"chunk_id"|"fallback_first_chunk"` 플래그 추가, fallback 항목은 `judge_score`에 `None` 기록 + summary에서 fallback 비율 보고.

### [HIGH] H1. 식별자 누설 게이트가 ASCII 전용 — 한글 고유명사 미검출
- **증거:** `synth.py:299` — `_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")`. 한글 문자가 포함된 식별자는 추출 자체가 안 됨. `has_identifier_leakage`는 한글 누설을 항상 False로 판정.
- **영향:** 사내 한국어 문서에서 흔한 "결제서비스", "이커머스팀", "주문플랫폼" 같은 한글 고유명사가 청크에 있고 질문에도 그대로 들어가도 leakage 게이트 통과. lexical match만으로 검색 시스템이 항상 1위 가능 → 검색 품질을 과대평가.
- **권고:** `_IDENT_RE`에 한글 조합(가-힣) + 영문 혼합 패턴 추가, 또는 별도 `has_korean_proper_noun_leakage` 게이트 추가. 청크에서 추출한 한글 명사구 중 빈도 1회(=고유명사 가능성)이고 길이 ≥ 2자인 토큰을 누설 후보로 검사.

### [HIGH] H2. 재현성 주장과 실제 동작의 불일치 (temperature=0.7)
- **증거:** `synth.py:513` `temperature=0.7` (Generator), `build_synthetic_gold_set.py:999-1002` 의 `--seed` 도움말은 "재현성"을 명시. 하지만 LLM 호출은 비결정적.
- **영향:** 같은 seed + 같은 입력 청크 + 같은 모델로 다시 빌드해도 다른 골드셋이 나온다. `--n-gold-sets`로 측정하는 std가 "골드셋 변동성"이 아닌 "LLM noise + 샘플링 차이" 혼합이므로 검색 시스템의 안정성 판단에 거짓 신호.
- **권고:** 두 가지 중 하나
  - (a) Generator 호출에 `temperature=0.0` 옵션 추가하고 기본값을 0.0으로. 다양성은 prompt + question count로 확보.
  - (b) `--seed` 도움말을 "샘플링과 distractor 순서만 결정. LLM 호출 자체는 비결정적이므로 재빌드는 다른 골드셋을 만든다"로 수정 + 메타데이터에 `lm_determinism=False` 명시.

### [HIGH] H3. Judge fall-through가 메타데이터에 자동 플래그되지 않음
- **증거:** `build_synthetic_gold_set.py:1126-1131` (build), `eval_search.py:759-763` (eval). 둘 다 `logger.warning` 후 진행. Build의 metadata에는 fallback 사실이 안 기록되고 (C1 참조), Eval의 `config_summary["judge_model"]`(L794)에는 `args.judge_model or config.get("llm.model")`로 채워져 system LLM과 동일 ID가 들어가지만, **명시적 self-eval 플래그가 없으므로 사용자가 두 키를 비교해야 알아챔**.
- **영향:** 사용자가 stderr 로그를 안 보면 self-eval 결과가 그대로 운영 의사결정에 흘러감.
- **권고:** 두 스크립트 모두 (a) fall-through 시 stdout에 빨간색 경고 박스 출력, (b) 메타데이터/summary JSON에 `self_evaluation_risk: true` 명시 기록, (c) 환경 변수 `RAG_AUDIT_STRICT=1`이면 fall-through 시 sys.exit(1) 옵션 제공.

### [HIGH] H4. Baseline vs Treatment 라벨 동치성 미검증
- **증거:** `eval_search.py:719-848` `run` 함수 어디에도 "이전 라벨과 같은 골드셋·같은 config로 실행되었는가" 자동 확인 없음. 사용자가 `--label baseline`을 골드셋 A로 실행한 뒤 `--label multiview`를 골드셋 B(혹은 같은 골드셋이지만 `--rerank` 옵션 차이)로 실행해도 막지 않음.
- **영향:** A/B 비교가 가장 흔한 사용 시나리오인데 동치성 보장이 사람 의존. 옵션 1개 차이로 baseline mrr=0.45 → treatment mrr=0.62 같은 거짓 개선 보고 가능.
- **권고:** summary JSON에 `gold_set_hash`(yaml content hash) + `runtime_options_hash`(rerank/hyde/top_k/max_chunks/include_graph fingerprint) 기록. `eval_search.py compare baseline.summary.json multiview.summary.json` 같은 별도 비교 명령을 추가하여 두 hash가 다르면 경고.

### [MEDIUM] M1. 다중 골드셋 변동성의 의미가 제한적
- **증거:** `build:1161` `seed_i = base_seed + i - 1`. 같은 청크 풀에서 seed만 다르게 stratified_sample 재실행 + Generator 비결정성 합산.
- **영향:** 측정된 std는 (a) 청크 풀 자체 편향, (b) LLM 비결정성, (c) Judge 비결정성의 혼합. "검색 시스템의 변동성"을 측정하는 게 아니라 "골드셋 생성 노이즈"를 측정. 이걸 운영 안정성 신호로 오해하면 위험.
- **권고:** `eval/runs/{label}.aggregate.summary.json`(L869)에 `variance_source: "gold_set_generation_noise"` 명시. 운영 안정성 측정은 같은 골드셋을 N번 평가(검색 시스템이 비결정적인 경우)로 별도 측정.

### [MEDIUM] M2. Distractor 풀의 토픽 다양성·관련성 보장 부족
- **증거:** `build:518-525` — `[c for c in distractor_pool if c["source_type"] == chunk["source_type"]][:n_distractors]`. `rng.shuffle(distractor_pool)`은 한 번만 호출되어 풀 전역 순서를 고정. 같은 source_type을 가진 청크가 풀 앞쪽에 몰리면 모든 chunk가 같은 distractor[0..1]를 받음. `n_distractors` 기본 2.
- **영향:** 일반성 게이트가 "특정 distractor에만 답 가능"한 질문을 통과시키거나 탈락시키는 편향. distractor가 우연히 source와 무관 토픽이면 게이트가 너무 관대 → false positive 다발.
- **권고:** 청크별로 distractor를 (a) 같은 source_type에서, (b) source 문서와 다른 문서에서, (c) 키워드 다양성(예: section_path 다른 것 우선)으로 N개 별도 샘플링.

## 차원별 상세

### 1. 메트릭 구현 정확성 — ✅ 표준 준수

`metrics.py:23-90` 의 함수들이 정통 정의 그대로 구현됨:
- `recall_at_k`: `|retrieved[:k] ∩ relevant| / |relevant|`, 정답 0개 → 0.0
- `precision_at_k`: 분모 k, k=0 → 0.0
- `mrr`: 첫 정답 등수 역수, 없으면 0.0
- `ndcg@k`: binary relevance, log2(i+1) DCG, IDCG는 ideal hits를 1~m 위 배치, idcg=0 가드 있음
- `aggregate_with_variance:161`: ddof=1 표본 분산 (n≥2일 때만, n=1이면 std=0.0)

작은 주의점: `recall_at_k:31` `set(retrieved[:k])` — retrieved에 중복 doc_id가 있으면 중복 제거. `retrieved_doc_ids`(eval:209)가 청크 단위로 문서ID를 추출하므로 한 문서의 여러 청크가 top-k에 들면 set 변환 후 1개로 계산됨. 의도된 동작이고 docstring과 일치.

### 2. top-k 선정 / tie-breaker — ⚠️ 외부 의존

`eval_search.py:209` `retrieved_doc_ids = [s.document_id for s in assembled.sources]` — `assemble_context_with_sources` 반환 순서에 의존. 이 함수의 정렬·tie-breaker는 분석 대상 범위 밖이지만, **만약 거기서 동률 처리가 비결정적이면 평가 메트릭도 비결정적**. context_assembler.py를 별도 확인 필요.

긍정: `rows.sort(key=lambda r: r.get("_idx", 0))` (eval:700) — async 결과를 결정론적 idx 순으로 재정렬. 동시성 영향 차단.

### 3. Judge 채점의 의미 — ⚠️ Lexical/Semantic Overlap 추정

`eval_search.py:88-109` JUDGE_PROMPT — Judge에게 source_chunk + retrieved_context 모두 보여주고 "retrieved가 source의 핵심을 담는지" 0~5점. 이는 본질적으로 **overlap 추정**이지 RAG 답변 품질이 아니다. 사용자가 "독립 답변 평가"로 오해하면 안 됨.

긍정: `temperature=0.0` (eval:132), `max_tokens=256` — 채점 일관성 확보 시도.
한계: `score_raw`가 int/float 아니면 -1 반환(eval:143), `max(0, min(5, int(score_raw)))` 클램핑. 즉 Judge가 4.5 같은 소수를 줘도 int 캐스팅(=4)으로 손실. graded scale 의미가 작음.

### 4. 통계 처리 — ⚠️ 표본 검정 미구현

`aggregate_with_variance`(metrics:126)이 mean/std/min/max/n을 산출. 다만:
- bootstrap CI, paired t-test, Wilcoxon 같은 통계 검정 없음
- "mean Δ > std면 유의" docstring 기준(eval:22)이 코드로 enforced 아님 — 사용자가 눈으로 비교
- `n_gold_sets_evaluated`(eval:868)와 `n_gold_sets_requested`(L867)를 둘 다 기록 → 실패율은 사후 확인 가능

### 5. 결정성 / 재현성 — ⚠️ 부분 결정성

- Build: `stratified_sample`은 seed 기반 셔플(synth:654), `distractor_pool` 셔플(build:382) 결정적. **그러나 Generator/Judge LLM 호출이 비결정** (synth:513 temperature=0.7).
- Eval: `embed_fn` LRU 캐시(eval:609), `rows` idx 정렬(eval:700) 결정적. 단 `assemble_context_with_sources` 자체 결정성은 외부 의존.

### 6. 정답 누설 방지 — ✅ 의외로 강함, ⚠️ 한글 빈틈

긍정 (synth.py):
- `GENERATE_PROMPT_TEMPLATE`(L88-120): "식별자 그대로 베끼지 말 것", "지시대명사 금지" 명시 + 좋은/나쁜 예시
- `has_identifier_leakage`(L330): 청크의 4자+ ASCII 식별자 추출 → 질문에 word boundary 매칭. 결정론적 게이트.
- `has_demonstrative_reference`(L373): 한글/영어 지시대명사 정교 정규식. 한글은 잘 처리됨.
- `filter_question`(L583)의 4단계 게이트가 모두 강제됨 (apply_filter=True일 때).
- distractor를 같은 source_type에서 우선 선정 (build:518-525) — 일반성 게이트의 검출력 강화.

빈틈:
- H1: 식별자 누설 한글 미검출 (위 참조).
- `_COMMON_WORDS`(synth:302)에 도메인 영단어(endpoint, service, manager, config 등) 미포함 → 청크의 평범한 영단어가 누설 토큰으로 잘못 추출되어 질문 탈락(false positive). 단 누설 검출보다는 다양성 손실 위험.
- `parse_yes_no:488`: "yes"로 시작만 하면 True. Judge의 "yes, but only if..." 같은 신중한 답을 무조건 yes로 해석.

### 7. 그래프 매칭 — ⚠️ 임계값 근거 외부 의존, T4 type 미요구

- `DEFAULT_GRAPH_MATCH_THRESHOLD = 0.78` (graph_match.py:33). 주석은 "설계 §2.2 결정값"이라고만 함 — 코드에 검증 근거 없음. 외부 설계 문서 확인 필요.
- T4(embedding) tier가 type 일치를 요구하지 않음(graph_match.py:8). `system → service` 같은 명칭 변경에는 강하지만, 무관 타입 엔티티가 description 우연 유사할 때 false positive 위험.
- `tier_counts` 보고(eval:265) — 어느 tier에서 매칭이 일어났는지 추적 가능. 좋음.
- 그래프 description 임베딩이 인덱싱 임베딩과 같은 모델 사용 (build:1149-1151의 `_build_embedding_client` 재사용) — 인위적 self-similarity 가능성. 메타데이터에 `embedding_model` 기록은 됨.

## 종합 위험 매트릭스

| 차원 | 등급 | 핵심 이슈 |
|------|------|-----------|
| 메트릭 구현 정확성 | Low | 표준 준수 |
| top-k tie-breaker | Medium | `assemble_context_with_sources` 결정성 외부 의존 |
| Judge 메타-편향 | High | fall-through 시 메타 자동 플래그 없음(H3) |
| Judge 의미 | Medium | overlap 추정 — RAG 답변 품질 아님 |
| 통계 검정 | Medium | mean/std만, 검정 미구현(M1) |
| 결정성/재현성 | High | temperature=0.7, seed 효과 제한적(H2) |
| 정답 누설 방지 (영문) | Low | 결정론 게이트 강함 |
| 정답 누설 방지 (한글) | High | ASCII 전용(H1) |
| 메타데이터 추적성 | Critical | 생성 모델 정보 누락(C1) |
| Source text fetch | Critical | 첫 청크 fallback 위험(C2) |
| 라벨 동치성 | High | 자동 검증 없음(H4) |
| Distractor 다양성 | Medium | 풀 셔플 1회 + `[:N]`(M2) |
| 그래프 매칭 임계값 | Medium | 0.78 근거 외부 의존 |

## 사용 가능 / 사용 금지 매트릭스

| 의사결정 유형 | 사용 가능? | 단서 |
|---|---|---|
| 동일 시스템 내 코드 변경 전후 A/B (같은 골드셋 사용) | **YES** | gold_set_hash·config hash를 사람이 직접 대조. mean Δ 가 std 보다 명백히 클 때만 신뢰. |
| 명백한 회귀 검출 (recall@5가 0.6 → 0.3 같은 급격 변화) | **YES** | 거짓 신호일 가능성 낮음 |
| 미세 개선 측정 (Δ < 0.05) | **NO** | H2(재현성) + M1(통계 검정 부재)로 잡음 구분 불가 |
| 외부 벤치마크 절대 점수 (다른 RAG 시스템과 비교) | **NO** | 합성 골드셋이 자기 시스템의 청크 공간에 fit되어 있음. 외부 시스템에 그대로 적용 불가. |
| 모델 교체 의사결정 (Generator/Embedding 변경) | **CONDITIONAL** | C1(메타 누락), H3(fall-through 미플래그)으로 base 골드셋의 self-eval 위험 사후 확인 불가. 메타데이터 정비 후 가능. |
| 운영 출시 게이트 | **NO** | C2(첫 청크 fallback)로 Judge 채점 오염 가능 + 한글 누설 미검출(H1). 게이트로 쓸 수 없음. |
| 검색 알고리즘 연구 발표 자료 | **NO** | 재현성(H2) 부족으로 외부 검증 불가능. |

## 우선순위 개선 권고 (코드 패치 단위)

1. **C1 — 메타데이터에 생성 모델 정보 기록.** `build_synthetic_gold_set.py:445` metadata dict 확장. 1시간 미만 작업. **즉시 수행 권장.**

2. **H3 — Fall-through 플래그를 메타데이터/summary에 기록.** `build:1126`과 `eval:759`의 경고 분기에서 결과 파일에 `self_evaluation_risk: true` 명시. + `RAG_AUDIT_STRICT=1` 환경변수로 fail-fast. C1과 함께 묶어 작업.

3. **C2 — `_fetch_source_text` fallback 추적.** `eval_search.py:346` 에서 fallback 경로 식별 + row에 `source_fetch_method` 기록 + summary에 fallback 비율. judge_score를 fallback 항목에서 제외.

4. **H1 — 한글 식별자 누설 게이트.** `synth.py:299` `_IDENT_RE` 확장 또는 새 함수 `has_korean_proper_noun_leakage` 추가. 한글 명사구 추출 + 청크 빈도 1회 토큰 매칭.

5. **H4 — 라벨 동치성 검증.** summary JSON에 `gold_set_hash`(YAML content sha256) + `runtime_fingerprint` 추가. 별도 `compare` 명령은 후속.

6. **H2 — 재현성 명확화.** 옵션 2가지 중 택 1:
   - (a) `--temperature` CLI 인자 추가, 기본 0.0. (안전한 변경)
   - (b) `--seed` 도움말 수정 + 메타데이터 `lm_determinism=False`. (현 동작 유지)

7. **M1 — `variance_source` 명시.** aggregate summary에 `variance_source: "gold_set_generation_noise"` 키 추가. 1줄 변경.

8. **M2 — Distractor 다양성.** `build:518-525`의 단순 `[:N]`을 청크별 `rng.sample(same_type, N)` 으로 교체. 1줄 변경.

## 후속 검토 권장 트리거

- 모델 변경 (Generator 또는 Embedding 교체) → 재감사
- 골드셋 갱신 후 메트릭이 0.05+ 변동 → C1·H3 확인
- 새 source_type 추가 → distractor 풀 적정성 확인
- `assemble_context_with_sources` 시그니처 변경 → tie-breaker 결정성 재검토 (context_assembler.py 별도 감사)
