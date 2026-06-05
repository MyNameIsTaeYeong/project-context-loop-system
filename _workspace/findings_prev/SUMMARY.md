# RAG 평가 신뢰성 재감사 — 종합 보고 (v2: S0/S1 패치 후)

이전 감사(v1, `_workspace/findings_prev/SUMMARY.md` — 종합 등급 **C**)에서 식별된 Critical 5건 + High 10건 + Medium 다수에 대해 PR #53 으로 S0/S1 12건 패치가 적용되었다. 본 재감사는 패치된 코드를 다시 동일 차원(7축)으로 점검하여 등급 회복 정도를 측정한다.

## TL;DR 판정

**종합 신뢰도: C → B+ (회복 확인)**

- Critical 5건 모두 코드 수준에서 해소 — 옵트인 플래그 없이는 fall-through 차단됨
- High 10건 중 6건 해소(P7·P8·P9·P10·P11·P12), 4건은 S2 범위로 명시(재현성·tie-breaker·임계값 캘리브레이션 등 미패치)
- 의도된 동작 변경(`--allow-self-eval`/`--allow-self-judge`/`.UNFILTERED` 강제) 외에 새로 도입된 위험 없음
- `compare_runs.py` 신규 도구가 baseline↔treatment 동치성 + 통계 검정 자동화 — 통계 차원이 가장 큰 등급 회복

이전 결론 "동일 환경 큰 회귀 검출에만 사용 가능"이 **"동일 환경 A/B 미세 개선 판정도 가능(N≥10 표본 + compare_runs 사용 시)"**으로 확장됐다. 단 **외부 벤치마크·운영 출시 게이트는 여전히 부적합** (재현성·임계값 근거·tie-breaker 미해결).

## Critical 위험 재평가

| # | 이전 위험 | 적용 패치 | 코드 인용 | 현 등급 | 잔여 |
|---|---|---|---|---|---|
| **C1** | self-eval fall-through 미차단 | P1+P2+P3 | `build:1194-1205` `parser.error`, `eval:843-869` `--allow-self-judge`, `llm:235-259` endpoint+model 동일성 | ✅ 해소 | 옵트인 사용자는 fallback 가능 — `self_evaluation_warning=true` 로 metadata 기록 |
| **C2** | 메타데이터 모델 ID 누락 | P1 | `build:480-487` — generator/judge × {model, endpoint, configured_separately} + warning + allow 플래그 = 8 키 | ✅ 해소 | 임베딩 모델 ID 는 그래프 모드만 기록(기존 동작), S2 의 P15 에서 보강 |
| **C3** | `_fetch_source_text` 첫 청크 fallback | P6 | `eval:314` `source_fetch_method` 컬럼, fallback 시 `judge_score=None`, summary 의 `source_fetch_method_counts`/`judge_skip_count` | ✅ 해소 | anchor 매칭 실패율이 높으면 fallback 비율로 운영 신호 가능 |
| **C4** | judge_score=-1 평균 오염 | P5 | `eval`에서 parse_error → None 분리, summary 의 `judge_score_parse_failures` 카운트 | ✅ 해소 | parse 실패율 자체를 줄이는 작업은 별도 |
| **C5** | answerable·generic 동일 프롬프트 | P4 | `synth:142` GENERIC 교체(`유일한 정답 출처`), `synth:656` `is_unique_source` 신규 함수, `filter_question` (d1) 게이트, reason `non_unique_source` | ✅ 해소 | 빌드 시간 +20~30% (LLM 호출 1건 추가) — `--concurrency` 로 상쇄 |

**Critical 잔여 위험: 없음.** 5건 모두 해소.

## High 위험 재평가

| # | 이전 위험 | 적용 패치 | 현 등급 | 잔여 |
|---|---|---|---|---|
| **H1** | 한글 식별자 누설 미검출 | P7 | ✅ 해소 (부분) | `_KOREAN_NOUN_RE` 가 4자 이상만 — 2~3자 한글 고유명사(예: "결제팀", "이커머스") 는 미검출 + `_KOREAN_COMMON_NOUNS` 화이트리스트가 좁아 도메인별 튜닝 필요 |
| **H2** | 재현성(temperature=0.7, seed 미전달) | 미패치(S2) | ⚠️ 유지 | 큰 회귀(Δ≥0.1) 검출은 가능, 미세 개선 측정은 여전히 곤란 — P13(S2) 로 분리 |
| **H3** | Judge source+retrieved 동시 노출 | 미패치 | ⚠️ 유지 | Judge 가 lexical overlap 채점에 회귀 가능 — reference-free judge 도입은 별도 PR |
| **H4** | baseline/treatment 동치성 미검증 | P8 | ✅ 해소 | `compare_runs.py` 가 gold_set_sha256 외 8개 핵심 키 동치성 자동 확인, mismatch 시 `--allow-config-mismatch` 없으면 종료 |
| **H5** | 그래프 τ=0.78 근거 부재 | 미패치(S2) | ⚠️ 유지 | P15 캘리브레이션 스크립트 작업으로 분리 |
| **H6** | 그래프 evidence DB fallback | P10 | ✅ 해소 | `build:906` 의 `or sg.get("entity_description")` 제거 → LLM 이 비우면 description 빈 문자열, T4 자동 skip |
| **H7** | `--no-filter` 출력 분리 없음 | P11 | ✅ 해소 | `_unfiltered_output_path` 헬퍼가 `.UNFILTERED` 접미사 강제, `_numbered_output_path` 와 호환 |
| **H8** | assemble dedup → retrieved < top_k | 미패치 | ⚠️ 유지 | `context_assembler.py` 의 tie-breaker 명시는 P14(S2) — 외부 모듈 영향 큼 |
| **H9** | 실패 질의 silent drop | P9 | ✅ 해소 | error row 에 `metric_failed=True` + 메트릭 키 None 명시, summary 에 `n_failed`/`failure_rate` 보고 |
| **H10** | `build_embed_fn` async silent skip | P12 | ✅ 해소 | `graph_match`에서 callable 에 `t4_disabled`/`skip_count` 속성, row 에 `graph_t4_disabled` 표기, summary 에 비율 |

**High 해소: 6건. 잔여(S2): 4건 (H2 재현성, H3 Judge 프롬프트 형태, H5 그래프 임계값 근거, H8 tie-breaker).**

## 신규 도구 평가 — `scripts/compare_runs.py`

P8로 신설된 `compare_runs.py` 를 처음 본 코드로 독립 평가:

| 항목 | 평가 |
|---|---|
| config 동치성 검증 | ✅ `EQUIVALENCE_KEYS` 8개(gold_set_sha256/embedding_model/llm_model/top_k/max_chunks/similarity_threshold/rerank_enabled/hyde_enabled) 자동 비교. mismatch 시 종료(`--allow-config-mismatch` 옵트인). 의미가 명확하고 사용자 안전. |
| paired Wilcoxon 직접 구현 | ✅ 절댓값 순위 + 동률 평균 처리(`avg_rank`) 정확. **단 N<10 일 때 정규근사 부정확** — 코드 주석에 "권장" 안내 부재. 사용자가 N=3 골드셋으로 비교하면 p-value 신뢰성 낮음. |
| bootstrap CI 결정성 | ✅ seed 고정(`seed=42`) 으로 재현 가능. `n_resample=1000` 기본, 사용자 조정 가능. |
| `paired_diff` 조인 | ✅ id inner join, `metric_failed` row 자동 제외. 메트릭 자동 추출(prefix 매칭). |
| scipy 미의존 | ✅ stdlib 만 — 운영 환경 가벼움. 단 정통 scipy 결과와 미세 차이 가능. |
| 시그니처 호환 | ✅ summary.json 스키마 그대로 사용, CSV 구조 그대로. |

**신규 도구 등급: B+ (작은 N 경고 부재만 추가하면 A−).**

## 차원별 등급 재산정

| 차원 | v1 | v2 | 핵심 변화 |
|---|---|---|---|
| 메트릭 함수 정확성 | A− | A− | 변경 없음 (이미 표준) |
| 결정론적 게이트 (영문/지시대명사) | A− | A− | 변경 없음 |
| 결정론적 게이트 (한글/그래프) | D | **B** | P7 + P10. 단 H1 잔여(4자 미만 한글) |
| LLM 게이트 (Judge 4단계) | C | **B** | P4 로 (a)·(d) 의미 분리, `is_unique_source` 추가 |
| 자기-평가 편향 차단 | **F** | **A−** | P1·P2·P3 강제 차단 + 메타 8키 + endpoint+model 동일성 검사 |
| 재현성 | D | D | 미패치 (S2의 P13) |
| 통계 처리 | C+ | **B+** | P8 의 `compare_runs.py` — Wilcoxon + bootstrap CI + 동치성 검증 |
| 감사 추적성 | C− | **A−** | C2(P1) + H4(P8) 의 fingerprint 기록 |
| 평가/시스템 결합 안정성 | C+ | **B** | P6·P12 로 silent fallback/skip 모두 표면화 |
| 운영 안전장치 | D+ | **B+** | P5·P6·P9·P12 — 모든 silent drop 명시 |
| **종합** | **C** | **B+** | 12건 패치 완료 |

## 새로 도입된 위험 (regression scan)

패치가 도입한 새 위험을 적극적으로 탐색한 결과:

1. **빌드 시간 증가** (Medium) — P4 의 `is_unique_source` LLM 호출 1건 추가로 청크당 호출 횟수 증가. `n_distractors=2` 기본 + 신규 (d1) 호출 = 청크당 LLM 호출 +1 (전체 ~25% 증가 예상). `--concurrency` 가 있어 상쇄 가능하지만 사용자 안내 필요.
2. **`--no-filter` 사용자 자동화 호환성** (Low) — `.UNFILTERED` 접미사 강제로 기존 CI/스크립트가 출력 경로 정확 매칭한다면 깨질 수 있음. PR 본문에 명시되었으나 운영 전 사전 공지 권장.
3. **`role_is_configured` 정책 변경 영향** (Low) — endpoint+model 동일성 검사 추가로 사용자가 "분리 의도였으나 우연히 같은 값 명시" 케이스에서 새로 차단됨. 의도된 안전장치지만 surprise 가능.
4. **compare_runs.py 의 N<10 경고 부재** (Low) — 사용자가 N=3 골드셋으로 비교 시 paired Wilcoxon p-value 신뢰성 부족 — 명시 권고 부재. S2 의 후속 작업으로 분리.

**새 Critical/High 위험 없음.** 모두 Medium/Low 범위.

## 사용 가능 / 사용 금지 매트릭스 (재평가)

| 의사결정 유형 | v1 | v2 | 변화 |
|---|---|---|---|
| 동일 환경 큰 회귀 검출 (Δ≥0.1) | YES | YES | 유지 |
| 동일 환경 A/B 미세 개선 (Δ<0.05) | NO | **CONDITIONAL** | N≥10 골드셋 + `compare_runs.py` 의 Wilcoxon p<0.05 + 95% CI 가 0 미포함 시 가능 |
| 외부 벤치마크 / 절대 점수 발표 | NO | NO | H2(재현성) + H3(Judge lexical overlap) 미해결 |
| 모델 교체 의사결정 | NO | **CONDITIONAL** | 메타데이터 기록 충분 — base 골드셋이 분리 설정으로 빌드된 경우 가능 (메타 확인 후) |
| 운영 출시 게이트 | NO | NO | H2·H3·H8 잔여로 미세 변동 신뢰성 부족 |
| 검색 알고리즘 연구 발표 | NO | NO | H2 재현성 미해결 |
| 내부 디버그·정성 검토 | YES | YES | 유지 + 신규: `source_fetch_method`/`graph_t4_disabled` 등 진단 풍부 |

## 잔여 S2 권고 (등급 A 회복용)

| 우선순위 | 항목 | 영향 등급 |
|---|---|---|
| 1 | **P13 — LLM endpoint seed 전달 + temperature=0.0 옵션** | 재현성 D → B+ |
| 2 | **P14 — assemble_context_with_sources tie-breaker `(similarity desc, document_id asc)` 명시** | 평가/시스템 결합 B → A− |
| 3 | **P15 — 그래프 τ=0.78 캘리브레이션 스크립트** | 결정론 게이트(그래프) B → A− |
| 4 | **compare_runs.py N<10 경고** | 통계 B+ → A− |
| 5 | **Judge 프롬프트 reference-free 모드** | LLM 게이트 B → A− |
| 6 | **한글 누설 게이트: 4자 미만 토큰 보강 + `_KOREAN_COMMON_NOUNS` 도메인 튜닝** | 결정론 게이트(한글) B → A− |

S2 6건 완료 시 종합 **A−**, 운영 출시 게이트 사용 조건부 가능 단계로 진입 예상.

## 진단 흐름 (현 패치 후 사용자가 확인할 항목)

운영 골드셋이 패치된 표준으로 빌드됐는지 빠르게 점검:

1. **메타데이터 점검**: `eval/gold_set.yaml` 의 `metadata` 블록에 다음 키가 있는지
   - `generator_model`, `judge_model`, `self_evaluation_warning`, `allow_self_eval`
   - 없으면 패치 전 빌드 — 재빌드 필요
2. **빌드 빌드 stderr 로그**: "Generator/Judge 모두 system LLM" 경고가 나면 P1 가 차단했어야 함. 만약 나오면 `--allow-self-eval` 사용 중인지 확인
3. **summary JSON 점검**: `judge_is_self`, `judge_score_parse_failures`, `n_failed`, `source_fetch_method_counts`, `graph_t4_disabled` 키가 모두 있는지
4. **baseline ↔ treatment 비교 시 항상 `compare_runs.py` 사용**: `python scripts/compare_runs.py --baseline ... --treatment ...` — config mismatch 자동 검출
5. **빌드/평가 stats 확인**: 새 fail 사유 `korean_leakage`, `non_unique_source` 카운트가 0이 아닌지 — 패치 효과 1차 시그널

## 부록 — 비교 보고서

- v1 (패치 전): `_workspace/findings_prev/SUMMARY.md` (종합 C)
- 패치 보고: `_workspace/patches/SUMMARY.md` (12건 적용 결과)
- 패치 검증: `_workspace/patches/C_verification.md` (회귀 테스트)
- 본 보고 v2 (재감사): `_workspace/findings/SUMMARY.md` (종합 B+)

세부 감사관 보고서: `01_gold_set_audit.md` (gold-set-auditor, 백그라운드 실행 중) + `02_eval_script_audit.md` (eval-script-auditor, 백그라운드 실행 중). 두 보고서 완료 후 본 SUMMARY 와 통합 권장.
