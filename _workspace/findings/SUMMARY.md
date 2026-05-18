# RAG 평가 신뢰성 감사 — 종합 보고

대상: origin/main 의 `scripts/build_synthetic_gold_set.py` + `scripts/eval_search.py` + 의존 모듈(`eval/synth.py`, `eval/metrics.py`, `eval/llm.py`, `eval/graph_match.py`)

세 독립 감사(메인 + gold-set-auditor + eval-script-auditor)의 합의 + 보완 발견을 통합. 세부는 `00_main_audit.md`, `01_gold_set_audit.md`, `02_eval_script_audit.md` 참조.

## TL;DR 판정

**현 상태로는 운영 의사결정의 단독 근거로 사용 불가.** 메트릭 식과 결정론적 누설 게이트(영문 식별자, 지시대명사)는 표준대로 잘 만들어졌지만, **(1) 자기-평가 편향 차단 부재, (2) 정답 누설의 한국어·그래프 경로, (3) 채점 결과 통계 오염, (4) 평가/시스템 결합부의 silent 위험**이 누적되어 측정된 메트릭이 체계적으로 부풀려져 있을 가능성이 높다.

**가능한 용도:** 동일 환경 회귀 검출(메트릭이 0.6 → 0.3 같은 큰 변동을 잡는 데). **불가능한 용도:** 외부 벤치마크 발표, 모델 교체 의사결정, 운영 출시 게이트, 미세 개선(Δ<0.05) 판정.

세 감사가 공통으로 합의한 핵심 위험은 5개의 Critical과 10개의 High이며, 그 중 절반은 1~2일 안에 패치 가능한 작은 코드 변경이다.

## Top 위험 (등급순, 합치도 표시)

`[★★★]` = 3개 분석 모두 / `[★★]` = 2개 / `[★]` = 1개 고유 발견

### CRITICAL

| # | 위험 | 합치 | 증거 | 영향 |
|---|------|---|------|------|
| C1 | **자기-평가 fall-through가 경고만 출력하고 진행** — Generator/Judge 미설정 시 system LLM과 같은 클라이언트로 떨어지며 실행 차단 없음. `role_is_configured`는 같은 model을 명시해도 True로 판정. | ★★★ | build:1126-1131, eval:758-763, llm:209-226 | 사용자가 stderr 안 보면 self-eval로 만든 골드셋·평가가 그대로 운영. 메트릭이 체계적으로 부풀려짐. |
| C2 | **골드셋 YAML 메타데이터에 사용된 생성/판정 LLM 모델 ID·endpoint가 기록되지 않음** | ★★★ | build:445-462 | 사후에 어떤 모델로 빌드한 골드셋인지 추적 불가. 자기-평가 빌드인지 확인 불가. |
| C3 | **`_fetch_source_text` 의 첫 청크 fallback이 Judge 채점을 silently 오염** — anchor·chunk_id 매칭 실패 시 문서의 첫 청크를 정답 근거로 가정. | ★ (메인 고유) | eval:346-375 (특히 369-370) | 문서가 N개 청크면 잘못된 근거 확률 (N-1)/N. judge_score 평균이 거짓. |
| C4 | **judge_score=-1 (parse_error)이 평균에 그대로 섞임** | ★ (서브2 고유) | eval:136-146, metrics:107-123 | parse 실패 1건이 평균 judge_score를 음수로 끌어내림. 일부 골드 항목 응답 깨지면 보고가 거짓이 됨. |
| C5 | **답변가능성(a)와 일반성(d) 게이트가 동일한 yes/no 프롬프트** — `ANSWERABLE_PROMPT_TEMPLATE` 과 `GENERIC_PROMPT_TEMPLATE` 본문이 사실상 같다. | ★ (서브1 고유) | synth:123-146 | 청크를 거의 베껴 만든 질문은 (a) 출처에서 yes 받고, distractor에선 no 받아 양 게이트 모두 통과. 게이트가 누설 탐지에 무력. |

### HIGH

| # | 위험 | 합치 | 증거 | 영향 |
|---|------|---|------|------|
| H1 | **식별자 누설 게이트가 ASCII 전용** — `_IDENT_RE`가 한글을 안 잡음. | ★★ | synth:299 | 사내 한국어 도메인 용어("결제서비스" 등)가 청크에 있고 질문에도 그대로 나오면 누설 미탐지. lexical overlap만으로 검색 1위 → 검색 품질 과대평가. |
| H2 | **재현성 부분만 보장** — `--seed`는 샘플링만 결정, LLM 호출은 `temperature=0.7` 비결정적, endpoint에 seed 미전달. metadata에 `seed`만 기록되어 사용자가 재현 가능하다고 오해. | ★★ | synth:513, build:350, build:1099-1114 | 같은 seed로 재빌드해도 다른 골드셋. `--n-gold-sets` 의 std는 검색 시스템 변동성이 아닌 LLM 노이즈를 측정. |
| H3 | **Judge 프롬프트가 source chunk + retrieved context 동시 노출** — 본질적으로 lexical/semantic overlap 채점. 단일 호출, 분산 측정 없음. | ★★ | eval:88-109 | 사용자가 "독립 답변 평가"로 오해. 실제로는 ROUGE-style 회귀. |
| H4 | **baseline vs treatment 라벨 동치성 미검증** — 두 라벨이 같은 골드셋·같은 시스템 config로 실행됐는지 자동 확인 없음. | ★★ | eval main 전반, eval:784-801 | A/B 비교가 가장 흔한 사용 시나리오인데 옵션 1개 차이로 거짓 개선 보고 가능. |
| H5 | **그래프 매칭 임계값 0.78의 캘리브레이션 근거 부재** — T4(embedding)는 type 일치 미요구. | ★★ | graph_match:33, 8 | false positive·false negative 어느 쪽인지 불명. 평가 측에서 다른 τ를 써도 강제 일치 검사 없음. |
| H6 | **그래프 evidence_description의 DB fallback** — LLM이 비우면 graph_store의 원본 description을 그대로 정답으로 사용. T4 매칭이 사실상 ID 매칭이 됨. | ★ (서브1 고유) | build:866 | T4 cosine이 trivially 1.0인 hit이 만연 → 그래프 시스템 강건성 측정 무의미. |
| H7 | **`--no-filter` 결과 골드셋이 운영 골드셋과 시각적으로 구분 안 됨** — 같은 파일 경로에 덮어쓰기 가능. | ★ (서브1 고유) | build:1010-1012, metadata:447-455 | 디버그 빌드를 잊고 CI가 가져가면 self-eval + 필터 부재 이중 오류. |
| H8 | **`assemble_context_with_sources` dedup으로 retrieved_doc_ids 길이 < top_k 가능** — doc 기준 dedup + similarity 동률 시 vector store 도착 순서 의존. | ★ (서브2 고유) | context_assembler.py, eval:209 | recall/precision의 분모가 인위적으로 깎이고, 동률 처리가 비결정적이라 메트릭이 매 실행 흔들릴 수 있음. |
| H9 | **실패 질의가 aggregate에서 silently 제외** — error row에 메트릭 키가 없으므로 평균 분모는 성공만, 그러나 `n_queries`는 실패 포함. | ★ (서브2 고유) | eval:665-678, metrics:107-123 | 실패율이 보이지 않고 평균이 낙관적으로 부풀려짐. 사용자가 metrics × n_queries로 재구성 못함. |
| H10 | **`build_embed_fn`의 비동기 경로가 silent skip 가능** — running loop 안에서 None 반환 → T4 embedding이 조용히 건너뜀. | ★ (서브2 고유) | graph_match:182-199 | graph_recall이 깎이는 방향이지만 summary에 표시 없음. 메트릭의 의미가 사용자 모르게 바뀜. |

### MEDIUM 요약

(합치 ★★ 이상만 표기, 나머지는 세부 보고서 참조)

- **샘플링 stratification이 source_type 한 축만** — 토픽/길이/난이도/그래프 degree 편향 무방비 (메인+서브1)
- **Distractor 풀의 다양성 보장 부족** — `rng.shuffle` 1회 후 `[:N]`, 청크별 동일 distractor 가능 (메인+서브1)
- **통계적 검정 미구현** — paired t-test, Wilcoxon, bootstrap CI 없음. "mean Δ > std면 유의" 기준이 코드로 enforced 아님 (메인+서브2)
- **N=1 시 std=0.0 반환** — 사용자가 "변동성 없음"으로 오해 가능. NaN 권장 (서브2)
- **timeout/retry 부재** — 한 질의가 hang 되면 평가 전체 정체 (서브2)

## 사용 가능 / 사용 금지 매트릭스

| 의사결정 유형 | 사용 가능? | 단서 |
|---|---|---|
| 동일 환경에서 큰 회귀 검출 (Δ ≥ 0.1) | **YES** | C5(게이트 무력)에도 불구하고 큰 변화는 잡힘. 단 같은 골드셋·같은 시스템 config 사람이 직접 확인. |
| 동일 환경 A/B 미세 개선 측정 (Δ < 0.05) | **NO** | H2(재현성 한계) + H8(비결정성) + 통계 검정 부재 |
| 외부 벤치마크 / 경쟁 분석 / 절대 점수 발표 | **NO** | C1·C2로 빌드 조건 검증 불가, H1(한글 누설)·H6(그래프 trivial)으로 부풀림 |
| 모델 교체 (Generator/Embedding 변경) 의사결정 | **NO (현 메타데이터로는)** | C2 패치 후 가능. base 골드셋의 self-eval 여부 사후 추적이 전제 |
| 운영 출시 게이트 (CI에서 자동 통과/실패 판정) | **NO** | C3·C4·H7로 채점 결과 신뢰성 무너짐. self-eval 골드셋이 게이트를 통과시킬 수 있음 |
| 검색 알고리즘 연구 발표 자료 | **NO** | H2(재현성), H5(임계값 근거)로 외부 검증 불가능 |
| 내부 디버그·탐색 (어떤 질의가 어떻게 처리되는지 보기) | **YES** | 메트릭 절대값보다 per-question CSV 정성 검토에 가치 |

## 우선순위 개선 권고 (코드 패치 단위)

### S0 — 즉시 (1~2일, 합쳐서 1 커밋 가능)

| # | 변경 위치 | 변경 내용 | 해결 위험 |
|---|----------|----------|----------|
| P1 | `build_synthetic_gold_set.py:1126-1131` | fall-through 시 `--allow-self-eval` 없으면 `parser.error()` 강제 종료. metadata에 `generator_model`, `judge_model`, `generator_endpoint`, `judge_endpoint`, `self_evaluation_warning` 추가. | C1, C2 |
| P2 | `eval_search.py:758-763` | 같은 분기 적용. `--allow-self-judge` 없으면 종료. `config_summary["judge_is_self"]` 기록. | C1 |
| P3 | `llm.py:209-226` `role_is_configured` | endpoint+model이 system과 동일하면 False 반환하도록 보강. | C1 |
| P4 | `synth.py:136-146` `GENERIC_PROMPT_TEMPLATE` | `ANSWERABLE_PROMPT_TEMPLATE`과 다른 프롬프트로 교체 — "이 문맥이 위 질문의 **유일한 정답 출처**인가? 다른 일반 문서에서도 답할 수 있다면 'no'". | C5 |
| P5 | `eval_search.py:136-146` `judge_answer` | `score < 0` 인 row의 `judge_score`를 `None`으로 분리. summary에 `judge_score_parse_failures` 카운트 추가. | C4 |
| P6 | `eval_search.py:369-370` `_fetch_source_text` | fallback 사용 시 row에 `source_fetch_method` 기록. fallback 항목은 judge 채점에서 제외 또는 별도 보고. | C3 |

### S1 — 1주 내

| # | 변경 위치 | 변경 내용 | 해결 위험 |
|---|----------|----------|----------|
| P7 | `synth.py:299` `_IDENT_RE` | 한글 패턴 추가: `[A-Za-z_][A-Za-z0-9_]{3,}\|[가-힣]{2,}`. + 청크-질문 4-gram(글자) Jaccard ≥ τ 보조 게이트. | H1 |
| P8 | `eval_search.py` summary/aggregate | 골드셋 SHA256, 항목 수, generator/judge 모델 ID를 summary에 기록. baseline/treatment 비교용 `scripts/compare_runs.py` 신설(라벨 간 config 일치 검증 + paired test). | H4 |
| P9 | `eval_search.py:665-678` 실패 처리 | error row에 메트릭 키를 `None`으로 명시 채움. summary에 `n_failed`, `n_successful`, `failure_rate` 보고. | H9 |
| P10 | `build_synthetic_gold_set.py:866` | LLM이 evidence_description을 비우면 description 자체를 비우고 T4 skip. DB fallback 금지. | H6 |
| P11 | `build_synthetic_gold_set.py:1010-1012` `--no-filter` | 출력 경로에 `.UNFILTERED` 접미사 강제. `eval_search.py`도 `metadata.filter_applied=False` 골드셋에 대해 `--allow-unfiltered-goldset` 없이는 실행 거부. | H7 |
| P12 | `graph_match.py:182-199` | async fallback이 None 반환할 때 row에 `graph_t4_disabled=True` 플래그 기록, summary에 비율 보고. | H10 |

### S2 — 2주 내

| # | 변경 위치 | 변경 내용 | 해결 위험 |
|---|----------|----------|----------|
| P13 | `synth.py:510-555` + `LLMClient.complete` | endpoint가 지원하면 `seed=` 인자 전달. metadata에 `temperature`, `llm_seed_supported` 기록. 권장: Generator temperature를 0.0으로 변경하거나 `--temperature` CLI 인자 추가. | H2 |
| P14 | `context_assembler.py` + `eval_search.py:209` | `assemble_context_with_sources`에 `return_top_k_doc_ids` 옵션 추가. tie-breaker를 `(similarity desc, document_id asc)` 명시. retrieved_doc_ids 길이를 top_k로 보장. | H8 |
| P15 | `graph_match.py:33` 0.78 캘리브레이션 | alias 쌍 vs 무관 쌍의 cosine 분포로 F1 최적점 산출하는 스크립트 신설. 결과를 docstring에 박는다. 빌드/평가 τ 불일치 시 에러. | H5 |
| P16 | `eval_search.py` 통계 검정 | `compare_runs.py`에 paired Wilcoxon + bootstrap 95% CI 구현. summary에 `improvement_significant` boolean. N<5/N<10에 경고. | F-4 (메트릭 검정 부재) |

### S3 — 추가 강건화

- 청크 `content_hash`를 GoldItem에 기록 → 인덱스 변경 후 stale 골드셋 검출
- 다중 stratification (source_type × length_bucket × difficulty)
- 그래프 노드 degree 버킷 stratification (hub 편향 회피)
- Judge 프롬프트 분리: reference-free 기본, entailment NLI, lexical-overlap 옵션
- 인덱싱 LLM ID를 메타에 기록 → 인덱싱-생성 self-loop 검출

## 진단 흐름 (사용자가 바로 확인 가능한 점검)

현 코드 상태로 운영 골드셋이 신뢰할 만한지 빠르게 점검하는 방법:

1. **메타데이터 점검:** `eval/gold_set.yaml`을 열어 `metadata` 블록 확인.
   - `seed`만 있고 `generator_model`/`judge_model`이 없으면 C2 해당. 운영 사용 보류.
   - `filter_applied: false`면 H7 해당. 운영 사용 금지.

2. **빌드 시 stderr 로그 확인:** `"Generator/Judge 모두 system LLM ... 동일"` 경고가 보였다면 C1 해당. 그 골드셋은 self-eval 빌드.

3. **평가 summary JSON 점검:** `judge_model`이 `llm_model`과 같은 값이면 C1 해당.

4. **per-question CSV 점검:** `error` 컬럼이 있는 행 수를 세고, summary의 `n_queries`와 비교. 차이가 5% 이상이면 H9로 보고 평균 부풀림 가능.

5. **샘플 질의 정성 검토 (10개):** 질문에 청크 본문의 한국어 명사구가 그대로 들어 있는 비율이 50%+면 H1 해당.

## 종합 신뢰도 점수

| 차원 | 점수 | 코멘트 |
|---|---|---|
| 메트릭 함수 정확성 | **A−** | 식 자체는 표준대로. 단 호출부에서 retrieved 길이 < k 같은 함정 |
| 결정론적 게이트(영문/지시대명사) | **A−** | 영문 식별자/지시대명사 게이트는 실제로 작동 |
| 결정론적 게이트(한글/그래프) | **D** | 한글 누설 미탐지, 그래프 evidence trivial 매칭 |
| LLM 게이트(Judge 4단계) | **C** | (a)≡(d) 동일 프롬프트, reasoning 미요구, distractor 정적 |
| 자기-평가 편향 차단 | **F** | 경고 한 줄, 메타 기록 없음 |
| 재현성 | **D** | 샘플링만 결정, LLM 비결정, seed 미전달 |
| 통계 처리 | **C+** | 표본 분산 ddof=1은 좋음. 검정·CI 부재 |
| 감사 추적성 | **C−** | 골드셋 메타에 모델 ID 없음, gold_set fingerprint 미기록 |
| 평가/시스템 결합 안정성 | **C+** | dedup·tie-breaker·silent skip 위험 |
| 운영 안전장치 (실패율, fallback 추적) | **D+** | 실패 silent drop, fallback silent fallback |
| **전체** | **C** | "숫자는 맞지만 그 숫자가 의미하는 것은 신뢰하기 어렵다" |

**S0(즉시 6건)만 패치하면 C → B+ 회복, S1까지 완료하면 B+ → A−.**

## 후속 재감사 권장 트리거

- 모델 교체(Generator/Embedding/Judge 중 하나라도) → C1·C2 메타데이터 재검증
- 골드셋 갱신 후 metric 0.05+ 변동 → C1·H1·H6 확인
- 새 source_type 추가 → distractor 풀 적정성·stratification 재검토
- `assemble_context_with_sources` 시그니처 변경 → H8 tie-breaker 재감사
- 평가 스크립트에 새 메트릭 추가 → 표준 정의 대조 (`metrics.py` 패턴 재현)

## 부록 — 세부 보고서

- `_workspace/findings/00_main_audit.md` — 메인 직접 감사 (8 Top 위험, 7개 차원)
- `_workspace/findings/01_gold_set_audit.md` — gold-set-auditor (Critical 1 + High 4 + Medium 4)
- `_workspace/findings/02_eval_script_audit.md` — eval-script-auditor (Critical 2 + High 4 + Medium 4)
