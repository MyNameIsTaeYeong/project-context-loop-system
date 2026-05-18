# RAG Eval Change Verifier — 검증 보고

## 한 줄 판정

**PASS** — S0(P1~P6) + S1(P7~P12) 12건 모두 코드에 반영. syntax/ruff/통합 회귀 PASS. 시스템 Python 3.9 환경 제약으로 프로젝트 pytest 풀 슈트는 본 워크트리에서 미실행(CI 환경에서 검증 권장).

## 1. 구조 무결성

- `python -m py_compile` — 패치된 7개 파일(`scripts/build_synthetic_gold_set.py`, `scripts/eval_search.py`, `scripts/compare_runs.py`, `src/context_loop/eval/{synth,llm,metrics,graph_match}.py`) 모두 통과
- 신규 파일: `scripts/compare_runs.py` (생성 완료, ~310줄)
- CLI help (코드 grep 으로 옵션 등록 확인):
  - `--allow-self-eval` → build:1083 부근에 등록
  - `--allow-self-judge` → eval:1100 부근에 등록
  - `compare_runs.py --help` 정상 출력 (인자 9개 확인)

## 2. 정적 검사 (ruff)

- 패치 전후 ruff 비교: 기존 main 에 이미 있던 `E501` 1건(`scripts/build_synthetic_gold_set.py:1081`) 외에는 **패치가 새로 도입한 위반 없음**
- 패치 도중 발견된 신규 E501 2건(`synth.py:365` docstring, `compare_runs.py:305` header line) — 모두 수정 완료

## 3. 테스트 회귀

| 검증 | 결과 |
|---|---|
| `synth.py` 단위 (P4 GENERIC 프롬프트 분리, P7 한글 누설, 기존 leakage/demonstrative 회귀) | PASS |
| `llm.py role_is_configured` 단위 (P3 — 6 case: fall-through·CLI override·동일값 명시·eval.role·우선순위) | PASS |
| `metrics.aggregate` 회귀 (P5 None 자동 스킵, P9 실패 row 메트릭 None 제외) | PASS |
| `filter_question` 통합 (정상 통과·non_unique_source·korean_leakage·leakage·demonstrative 전 분기) | PASS |
| `compare_runs.py` 핵심 함수 (`bootstrap_ci` 결정성, `wilcoxon_p_value`, `paired_diff`) | PASS |
| 프로젝트 `pytest tests/test_eval/` 풀 슈트 | **SKIPPED** — 시스템 Python 3.9, 프로젝트 요구 3.11+ (PEP 604 union 미지원). CI 환경에서 정상 실행 예상 |

## 4. 위험 해소 spot check

| 원래 위험 | 패치 | 코드 인용 | 판정 |
|---|---|---|---|
| **C1** self-eval fall-through | P1 + P2 + P3 | (a) build:1194-1205 `parser.error` (b) eval:843-869 `--allow-self-judge` 분기 (c) llm:235-259 endpoint+model 동일성 체크 | ✅ |
| **C2** 메타데이터 모델 ID 누락 | P1 | build:480-487 — `generator_model`, `judge_model`, `generator_endpoint`, `judge_endpoint`, `generator_configured_separately`, `judge_configured_separately`, `self_evaluation_warning`, `allow_self_eval` 8 키 추가 | ✅ |
| **C3** `_fetch_source_text` 첫 청크 fallback | P6 | eval:`source_fetch_method` 컬럼 (eval:314), fallback 시 judge_score skip, summary 에 `source_fetch_method_counts`/`judge_skip_count` | ✅ |
| **C4** judge_score=-1 평균 오염 | P5 | eval:`judge_score < 0 → None` 분리, summary 에 `judge_score_parse_failures` | ✅ |
| **C5** answerable·generic 동일 프롬프트 | P4 | synth:142 `GENERIC_PROMPT_TEMPLATE` 교체("유일한 정답 출처"), synth:656 `is_unique_source` 신규 함수, `filter_question` 의 (d1) 게이트 추가, reason `non_unique_source` 신규 | ✅ |
| **H1** 한글 식별자 누설 미검출 | P7 | synth:364 `_KOREAN_NOUN_RE`, synth:401 `has_korean_proper_noun_leakage` (조사 stripping 포함), `filter_question` 의 (b2) 게이트, reason `korean_leakage` 신규 | ✅ |
| **H4** baseline/treatment 동치성 미검증 | P8 | eval:`enriched_config["gold_set_sha256"]` 등 5개 키 기록 + 신규 `scripts/compare_runs.py` (config 동치성 검증 + paired Wilcoxon + bootstrap CI) | ✅ |
| **H6** 그래프 evidence DB fallback | P10 | build:906 `description = gq.evidence_description` (`or sg.get("entity_description")` 폴백 제거) | ✅ |
| **H7** `--no-filter` 출력 분리 없음 | P11 | build:992 `_unfiltered_output_path` 헬퍼, args.no_filter 시 `.UNFILTERED` 접미사 강제 | ✅ |
| **H9** 실패 질의 silent drop | P9 | eval:741-773 `metric_failed=True` + 표준 메트릭 키 None, summary 에 `n_failed`/`n_successful`/`failure_rate` 보고 | ✅ |
| **H10** `build_embed_fn` async silent skip | P12 | graph_match:`build_embed_fn` 반환 callable 에 `t4_disabled`/`skip_count` 속성, `evaluate_one` 이 row 에 `graph_t4_disabled` 기록, summary 에 비율 보고 | ✅ |

## 5. 후방 호환 확인

- **CLI 호환**: 기존 옵션 모두 유지. 새 옵션은 옵트인(`--allow-self-eval`, `--allow-self-judge`, `--allow-config-mismatch`) — 정상 분리 설정 사용자는 영향 없음
- **goldset YAML 스키마**: 새 metadata 키 8개 추가, 기존 키 삭제·이름 변경 없음
- **summary JSON 스키마**: 새 키만 추가 (`gold_set_sha256`, `judge_is_self`, `judge_score_parse_failures`, `n_failed`, `failure_rate`, `source_fetch_method_counts`, `graph_t4_disabled`, `graph_t4_skip_count`)
- **`build()` 함수 시그니처**: keyword 인자 8개 추가, 모두 기본값 보유 → 외부 호출자 후방 호환
- **`_fetch_source_text` 시그니처 변경**: 반환 타입이 `str` → `tuple[str, str]` 로 변경. 호출부(`evaluate_one`) 만 수정됐고 외부 노출 함수는 아님 (모듈 내부 함수)

## 6. 의도된 동작 변경 (사용자 안내 필요)

| 변경 | 사용자 영향 |
|---|---|
| 옵션 미지정 + Generator/Judge 가 system LLM 으로 fall-through 시 build 종료 | 기존: 경고 후 진행. 신규: `--allow-self-eval` 없으면 `parser.error()` 종료. 메시지에 해결 방법 안내. |
| `--judge` 만 켜고 분리 endpoint 없으면 eval 종료 | 기존: 경고 후 자기 채점. 신규: `--allow-self-judge` 없으면 `SystemExit`. |
| `--no-filter` 사용 시 출력 파일명에 `.UNFILTERED` 강제 | 같은 경로 덮어쓰기 불가. CI/자동화에서 출력 경로 매칭이 정확해야 함. |
| `filter_question` LLM 호출 +1 (`is_unique_source` 추가) | 빌드 시간 20~30% 증가 가능. `--concurrency` 로 일부 상쇄. |

## 사후 권고

- **PR 머지 후 운영 환경에서 회귀 1회 실행**: 같은 골드셋·동일 설정으로 `eval_search.py --label baseline` 새 코드로 실행 → 기존 summary 와 메트릭 절댓값이 크게 어긋나지 않는지 확인 (P9 의 None 분리로 미세 차이는 가능)
- **CI 환경 (Python 3.11+) 에서 `pytest tests/test_eval/` 풀 슈트 실행**: 본 워크트리에서는 환경 제약으로 미실행
- **`/rag-eval-audit` 재실행**: S0/S1 패치 적용 후 신뢰도 등급이 C → B+ 로 회복되었는지 감사 보고서 갱신. S2 (LLM seed 인프라, graph τ 캘리브레이션 등) 는 별도 PR
- **사용자 안내**: 운영 자동화 사용자에게 `--allow-self-eval`/`--allow-self-judge` 변경 사항 사전 공지 (Slack/PR 본문 등)

## 부록 — 세부 패치 로그

- `_workspace/patches/A_gold_set_build.md` (gold-set-build-patcher: P1, P4, P7, P10, P11)
- `_workspace/patches/B_eval_script.md` (eval-script-patcher: P2, P3, P5, P6, P8, P9, P12)
- 본 보고서가 두 로그 + 메인 직접 검증을 종합
