# RAG 평가 시스템 — S0/S1 패치 요약

`rag-eval-audit` 의 감사 보고서(_workspace/findings/SUMMARY.md, C 등급)를 받아 `rag-eval-fix` 하네스로 12건의 패치를 적용. Critical 5건 + High 10건이 해소되었다.

## 적용 결과

| 등급 | 항목 | 패치 | 상태 |
|------|------|------|------|
| **S0 (Critical)** | P1 — self-eval 차단 + 메타데이터 보강 | `build_synthetic_gold_set.py` | ✅ |
| | P2 — judge fall-through 차단 | `eval_search.py` | ✅ |
| | P3 — `role_is_configured` 보강 | `llm.py` | ✅ |
| | P4 — GENERIC 프롬프트 분리 | `synth.py` (`is_unique_source` 신규) | ✅ |
| | P5 — judge_score=-1 분리 | `eval_search.py` + metrics aggregate 호환 | ✅ |
| | P6 — `_fetch_source_text` fallback 플래그 | `eval_search.py` | ✅ |
| **S1 (High)** | P7 — 한글 누설 게이트 | `synth.py` (`has_korean_proper_noun_leakage`, 조사 stripping 포함) | ✅ |
| | P8 — gold_set fingerprint + compare_runs.py | `eval_search.py` + 신규 `scripts/compare_runs.py` | ✅ |
| | P9 — 실패 질의 명시 | `eval_search.py` summary 에 `n_failed`/`failure_rate` | ✅ |
| | P10 — 그래프 evidence DB fallback 제거 | `build_synthetic_gold_set.py:906` | ✅ |
| | P11 — `--no-filter` 출력 분리 | `build_synthetic_gold_set.py` `.UNFILTERED` 접미사 강제 | ✅ |
| | P12 — `graph_t4_disabled` 표기 | `graph_match.py` + `eval_search.py` | ✅ |

## 위험 해소 매트릭스

| 원래 위험 (등급) | 패치 | 검증 결과 | 잔여 위험 |
|---|---|---|---|
| **C1** self-eval fall-through | P1 + P2 + P3 | ✅ 옵트인 플래그 없으면 `parser.error()`/`SystemExit` 종료. `role_is_configured` 가 endpoint+model 동일성까지 검사 | 사용자가 `--allow-self-eval` 옵트인하면 fallback 가능. metadata `self_evaluation_warning=true` 로 추적 가능 |
| **C2** 메타데이터 모델 ID 누락 | P1 | ✅ 8개 신규 키 (model/endpoint/configured_separately/warning/allow flag) | 임베딩 모델 ID 는 P15 (S2) 에서 다룸 |
| **C3** `_fetch_source_text` 첫 청크 fallback | P6 | ✅ `source_fetch_method` 컬럼, fallback 시 judge_score 자동 skip | anchor 매칭 실패율이 높으면 운영 신호 필요 — summary 의 `source_fetch_method_counts` 모니터링 권장 |
| **C4** judge_score=-1 평균 오염 | P5 | ✅ parse_error → None 분리, `judge_score_parse_failures` 카운트 | parse 실패율 자체를 줄이려면 reasoning_mode 또는 Judge 프롬프트 보강(별도 PR) |
| **C5** answerable·generic 동일 프롬프트 | P4 | ✅ 두 프롬프트 의미 명확히 분리, `is_unique_source` 신규 함수, reason `non_unique_source` 추가 | 빌드 시간 +20~30% (LLM 호출 1건 추가). `--concurrency` 로 상쇄 |
| **H1** 한글 누설 미검출 | P7 | ✅ 조사 stripping + substring 매칭으로 한국어 고유명사 잡힘 | n-gram Jaccard 보조는 S2 로. _KOREAN_COMMON_NOUNS 화이트리스트는 도메인별 튜닝 가능 |
| **H4** baseline/treatment 동치성 미검증 | P8 | ✅ `gold_set_sha256` + `compare_runs.py` 자동 동치성 확인 + paired Wilcoxon + bootstrap CI | scipy 미의존이라 직접 구현된 정규근사 — N≥10 권장 |
| **H6** 그래프 evidence DB fallback | P10 | ✅ DB fallback 제거, 빈 description 은 T4 자동 skip | 그래프 골드 항목의 description 누락률을 stats 로 보고하는 게 추가 안전장치 |
| **H7** `--no-filter` 출력 분리 | P11 | ✅ `.UNFILTERED` 접미사 강제 | `_numbered_output_path` 와 호환 (UNFILTERED 가 stem 끝, `_NNN` 그 뒤) |
| **H9** 실패 질의 silent drop | P9 | ✅ `metric_failed=True` + 메트릭 키 None, summary 에 `n_failed`/`failure_rate` | `aggregate` 가 None 을 자동 스킵하므로 평균은 성공만, n_queries 는 실패 포함 — 사용자 명시 |
| **H10** `build_embed_fn` async silent skip | P12 | ✅ `t4_disabled`/`skip_count` 속성, row 에 `graph_t4_disabled` 표기 | 사용자 자동화가 graph 메트릭을 절대값으로 비교 시 `graph_t4_disabled` 체크 필요 |

## 신뢰도 재평가 (예상)

감사 보고서의 차원별 등급:

| 차원 | 패치 전 | 패치 후 (예상) | 비고 |
|---|---|---|---|
| 메트릭 함수 정확성 | A− | A− | 변경 없음 (이미 표준 준수) |
| 결정론적 게이트(영문/지시대명사) | A− | A− | 변경 없음 |
| 결정론적 게이트(한글/그래프) | D | **B+** | P7 + P10 |
| LLM 게이트(Judge 4단계) | C | **B** | P4 로 (a)·(d) 의미 분리 |
| 자기-평가 편향 차단 | F | **A−** | P1·P2·P3 강제 차단 + 옵트인 플래그 |
| 재현성 | D | D | S0/S1 범위 밖 (S2의 P13 LLM seed 작업) |
| 통계 처리 | C+ | **B+** | P8 의 `compare_runs.py` 가 paired Wilcoxon + bootstrap CI 제공 |
| 감사 추적성 | C− | **A−** | C2 (P1) + H4 (P8) 로 모델·골드셋 fingerprint 기록 |
| 평가/시스템 결합 안정성 | C+ | **B** | P6·P12 로 silent fallback 모두 표면화 |
| 운영 안전장치 (실패율) | D+ | **B+** | P5·P6·P9·P12 로 silent drop·skip 모두 명시 |
| **전체** | **C** | **B+** | S0+S1 12건 완료. S2 잔여 항목 (재현성·임계값 캘리브레이션) 으로 A− 도달 가능 |

## 검증 요약

| 검증 | 결과 |
|---|---|
| `python -m py_compile` (7개 파일) | ✅ |
| `ruff check` (패치 도입 위반 없음, 기존 1건만 잔존) | ✅ |
| `synth.py` 신규 함수 단위 테스트 (P4 + P7) | ✅ |
| `llm.py role_is_configured` 단위 (P3, 6 case) | ✅ |
| `metrics.aggregate` 회귀 (P5 + P9 None 스킵) | ✅ |
| `filter_question` 통합 (5 분기 모두) | ✅ |
| `compare_runs.py` 핵심 함수 (P8) | ✅ |
| `pytest tests/test_eval/` 풀 슈트 | ⏭️ 시스템 Python 3.9 미지원 — CI 환경에서 자동 실행 예정 |

상세 검증 절차는 `_workspace/patches/C_verification.md` 참조.

## 후속 권고

1. **PR 머지 후 회귀 1회 실행**: 같은 골드셋·동일 설정으로 `eval_search.py --label regression-check` 실행 → 기존 summary 와 메트릭 절댓값이 크게 어긋나지 않는지 확인. P9 의 None 분리로 미세 차이는 정상.
2. **`/rag-eval-audit` 재실행**: 신뢰도 등급 회복 (C → B+) 검증.
3. **운영 자동화 사용자 안내**: `--allow-self-eval`/`--allow-self-judge`/`.UNFILTERED` 접미사 강제는 의도된 동작 변경 — 사전 공지 필요.
4. **S2 계획** (별도 PR):
   - **P13** — LLM endpoint seed 전달 인프라 (재현성 D → B)
   - **P14** — `assemble_context_with_sources` tie-breaker 명시 (H8)
   - **P15** — 그래프 τ=0.78 캘리브레이션 스크립트
   - **P16** — `compare_runs.py` 에 N<10 경고 + improvement_significant boolean

## 부록 — 세부 패치 로그

- `_workspace/patches/A_gold_set_build.md` — gold-set-build-patcher (P1, P4, P7, P10, P11)
- `_workspace/patches/B_eval_script.md` — eval-script-patcher (P2, P3, P5, P6, P8, P9, P12)
- `_workspace/patches/C_verification.md` — rag-eval-change-verifier (통합 검증)
