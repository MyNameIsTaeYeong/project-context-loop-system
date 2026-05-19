# RAG 평가 시스템 — S3 9건 패치 요약

v3 재감사(`_workspace/findings/SUMMARY.md`, 종합 **A−**)의 잔여 권고 및 서브
감사관 발견(HIGH 2 + Medium 3)을 `rag-eval-fix` 하네스로 적용. 신뢰도
**A− → A 회복 목표**.

## 적용 결과 (9건 전부 ✅)

| ID | 영역 | 변경 위치 | 상태 |
|---|---|---|---|
| **N-H1** | Judge 함수 3개 seed 전파 | `synth.py` `is_answerable`/`is_unique_source` + `eval_search.py` `judge_answer` + `filter_question` 호출 체인 (chunk·graph 양쪽) | ✅ |
| **N-H2** | calibrate 양성/음성 쌍 보강 | `calibrate_graph_match.py` — `trivial-normalize` + `alias-only` (substring/prefix), `unrelated` + `type-drift` 종류 추적 | ✅ |
| **Judge 분산** | n_samples median + std | `eval_search.py` `judge_answer(n_samples=)`, `_judge_answer_single` 헬퍼, row 에 `judge_score_std`/min/max/mean | ✅ |
| **한글 자동 학습** | 코퍼스 빈도 stopword | `synth.py` `build_korean_stopwords_from_corpus`, build 체인 전파 (chunk + graph) | ✅ |
| **paired bootstrap** | 개선 확률 + Cohen's d | `compare_runs.py` `paired_bootstrap` 신규 — mean/CI/p_improve/p_min_effect/cohen_d_paired. 기존 `bootstrap_ci` 호환 시그니처 유지 | ✅ |
| **P15 auto-tune τ** | 자동 반영 옵션 | `calibrate_graph_match.py` `--apply` 플래그 + `_apply_threshold_to_module` 정규식 헬퍼 | ✅ |
| **N-M1** | EQUIVALENCE_KEYS judge_mode | `compare_runs.py:42-52` `judge_mode` 추가 | ✅ |
| **N-M2** | reference-free fallback 우회 | `eval_search.py` `needs_source = judge_mode in ("overlap","entailment")` 만 source skip | ✅ |
| **N-M3** | Judge 프롬프트 강화 | `JUDGE_PROMPT_REFERENCE_FREE` 에 "self-knowledge 차단 — 학습 지식 사용 금지" 명시 | ✅ |

## v3 잔여 위험 해소 매트릭스

| v3 잔여 | S3 패치 | 코드 인용 | v4 등급 (예상) |
|---|---|---|---|
| **N-H1** Judge seed 미전달 | N-H1 | `synth.py:is_answerable/is_unique_source` seed 인자 + filter_question/build 체인 + `judge_answer` seed | **해소** |
| **N-H2** calibrate 양성 쌍 자명 | N-H2 | `_build_pair_buckets` 가 4 종류 (trivial/alias/unrelated/type-drift) 반환 | **해소** |
| N-M1 EQUIVALENCE_KEYS | N-M1 | `compare_runs.py` EQUIVALENCE_KEYS 에 `judge_mode` | **해소** |
| N-M2 source fallback 불필요 | N-M2 | mode 가 reference-free 면 fallback 무시 | **해소** |
| N-M3 self-knowledge | N-M3 | 프롬프트에 명시 차단 | **해소 (완화)** |
| Judge 모드별 분산 측정 | Judge 분산 | `n_samples >= 2` 시 std/min/max/median 기록 | **해소** |
| 한글 게이트 false positive | 자동 학습 | 코퍼스 빈도 ≥ 5 stem 자동 제외 | **해소** |
| paired bootstrap 부족 | paired_bootstrap | p_improve/cohen_d/p_min_effect | **해소** |
| P15 수동 반영 | auto-tune τ | `--apply` 로 graph_match.py:33 자동 갱신 | **해소** |

## 차원별 등급 영향 (예상)

| 차원 | v3 | S3 | 변화 |
|---|---|---|---|
| LLM 게이트 (Judge) | A− | **A** | seed 전파 + 분산 측정 + self-knowledge 차단 |
| 결정론적 게이트 (한글) | A− | **A** | 자동 학습으로 false positive 통제 |
| 결정론적 게이트 (그래프) | B+ | **A−** | calibrate 보강 + auto-tune |
| 통계 처리 | A− | **A** | paired_bootstrap + improvement probability |
| 재현성 | B+ | **A−** | Judge seed 전파로 게이트 결정성 완성 |
| **종합** | **A−** | **A** | 9건 완료 |

## 검증

| 항목 | 결과 |
|---|---|
| `python -m py_compile` (9 파일) | ✅ |
| `ruff check` (S3 도입 위반 0, main 기존 1건만 잔존) | ✅ |
| `paired_bootstrap` 단위 (mean/CI/p_improve/cohen_d/min_effect_size/결정성) | ✅ |
| `bootstrap_ci` 호환 시그니처 회귀 | ✅ |
| `EQUIVALENCE_KEYS` 에 `judge_mode` 포함 | ✅ |
| `build_korean_stopwords_from_corpus` 빈도 학습 + `extra_stopwords` 적용 효과 | ✅ |
| `is_answerable`/`is_unique_source` seed 인자 + 전파 | ✅ |
| `calibrate_graph_match` 코드 패턴 (4 종류 쌍 + auto-tune + --apply) | ✅ |
| `eval_search.py` 코드 패턴 (N-H1/N-M2/N-M3 + n_samples CLI) | ✅ |
| `pytest tests/test_eval/` 풀 슈트 | ⏭️ 워크트리 Python 3.9 미지원, CI 자동 실행 |

## 의도된 동작 변경 / 호환성

- `paired_bootstrap` 가 신규 함수, 기존 `bootstrap_ci` 시그니처 유지 → 후방 호환 ✅
- `judge_answer` 반환 타입이 `(int, str)` → `(int, str, dict)` 로 3-tuple 변경 — 내부 함수라 외부 호출자 없음 (eval_search.py 내에서만 사용)
- `EQUIVALENCE_KEYS` 에 `judge_mode` 추가 → 기존 baseline/treatment 비교에서 `judge_mode` 가 다르면 mismatch 알림. `--allow-config-mismatch` 로 강행 가능
- `--judge-n-samples`, `--judge-seed-base`, `--min-effect-size`, `--apply` 모두 옵션 (기본값 보유)

## 후속 작업 (등급 A+ → A 안정화)

S3 완료로 A 도달 예상. A+ 수준 추가 작업은 다음:
1. 시스템 RAG `context_assembler` 의 tie-breaker 동기화 (운영 결정성)
2. compare_runs 의 BCa (bias-corrected accelerated) bootstrap 도입
3. P15 결과를 metadata 에 자동 기록 (현 default 와 권장 τ 의 거리)
4. AnthropicClient 의 seed 우회 — N회 호출 후 median

## 사용자 5% 통과율 진단 (별도 후속)

S3 의 **한글 자동 학습** 이 `fail_korean_leakage` 비중 감소에 직접 기여. 단
주된 원인이 `fail_non_unique_source` 라면 별도 작업 필요 — 그래프 모드에서
unique_source 게이트 완화 옵션. PR 머지 후 운영 stats 확인 권장.

## 부록

- v1 (C): `_workspace/findings_prev/SUMMARY.md`
- v2 (B+): `_workspace/findings_v2/SUMMARY.md`
- v3 (A−): `_workspace/findings/SUMMARY.md`
- S0/S1 patches: `_workspace/patches/SUMMARY.md`
- S2 patches: `_workspace/patches/S2_SUMMARY.md`
- 본 S3 patches: `_workspace/patches/S3_SUMMARY.md`
