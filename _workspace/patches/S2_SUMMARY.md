# RAG 평가 시스템 — S2 잔여 6건 패치 요약

`rag-eval-audit` v2 재감사 보고서(`_workspace/findings/SUMMARY.md`, 종합 B+)의 S2 잔여 권고 6건을 `rag-eval-fix` 하네스로 적용. 신뢰도 B+ → A− 회복 목표.

## 적용 결과

| ID | 영역 | 변경 위치 | 상태 |
|---|---|---|---|
| **P13** | LLM seed/temperature 인프라 | `llm_client.py` (Endpoint/OpenAI 구현체) + `synth.py` (generate_questions·generate_graph_questions) + `build_synthetic_gold_set.py` (체인 전파 + CLI `--generator-temperature` / `--generator-seed-base`) | ✅ |
| **P14** | tie-breaker 명시 | `eval_search.py:209` 직후 — `sorted(assembled.sources, key=(−similarity, document_id))` 명시 stable sort | ✅ |
| **P15** | 그래프 τ 캘리브레이션 | **신규** `scripts/calibrate_graph_match.py` — 양성/음성 쌍 cosine 분포에서 F1 최대 τ 산출 | ✅ |
| **CR** | compare_runs N<10 경고 | `scripts/compare_runs.py` — `_MIN_SAMPLE_RECOMMENDED=10`, stdout 경고 + 메트릭별 `*` 마킹 + summary JSON `low_sample_warning` | ✅ |
| **JM** | reference-free Judge | `eval_search.py` — `JUDGE_PROMPT_OVERLAP` / `REFERENCE_FREE` / `ENTAILMENT` 3 템플릿, CLI `--judge-mode` (기본 reference-free) | ✅ |
| **KG** | 한글 게이트 보강 | `synth.py` — `_KOREAN_NOUN_RE` 3자로 완화 + `_KOREAN_COMMON_NOUNS` 화이트리스트 30+ 항목 확장 + josa stripping min_stem_len=2 | ✅ |

## 차원별 등급 영향 (예상)

| 차원 | 패치 전 (v2) | 패치 후 (S2) | 핵심 변화 |
|---|---|---|---|
| 메트릭 함수 정확성 | A− | A− | 변경 없음 |
| 결정론적 게이트 (영문) | A− | A− | 변경 없음 |
| **결정론적 게이트 (한글)** | B | **A−** | KG — 3자 고유명사 검출 + 화이트리스트 |
| 결정론적 게이트 (그래프) | B | B+ | (P15 캘리브레이션 도구로 보조, 직접 적용은 사용자 결정) |
| LLM 게이트 (Judge 4단계) | B | B | 변경 없음 |
| 자기-평가 편향 차단 | A− | A− | 유지 |
| **재현성** | **D** | **B+** | P13 — endpoint seed + temperature CLI |
| **통계 처리** | B+ | **A−** | CR — N<10 경고 + 메트릭별 마킹 |
| 감사 추적성 | A− | A− | 유지 |
| **평가/시스템 결합 안정성** | B | **A−** | P14 — tie-breaker 명시 |
| 운영 안전장치 | B+ | **A** | JM(reference-free 기본) + CR(통계 경고) |
| **종합** | **B+** | **A−** | S2 6건 완료 |

## 위험 해소 매트릭스

| 원래 위험 (v2 잔여) | 패치 | 해소 정도 |
|---|---|---|
| H2 재현성 부족 | P13 | endpoint 가 OpenAI/vLLM seed 지원하면 결정성 보장. anthropic 은 무시 (kwargs picked up). temperature 기본 0.0 으로 비결정성 차단 |
| H3 Judge lexical overlap | JM | `--judge-mode reference-free` (기본) 로 source chunk 노출 차단. overlap·entailment 옵션 보존 |
| H5 그래프 τ 근거 부재 | P15 | 캘리브레이션 스크립트로 사용자가 본인 데이터에서 F1 최적 τ 산출 가능. 현재 default 0.78 ±0.03 권장 |
| H8 시스템 tie-breaker | P14 | eval 측에서 명시 stable sort `(−similarity, document_id)` 적용. context_assembler 자체는 안 건드림 (외부 의존 회피) |
| Wilcoxon N<10 경고 부재 | CR | stdout · summary JSON 양쪽에 명시 경고. 메트릭별 표본 부족 마킹 |
| 한글 4자 미만 미검출 | KG | 3자 컷오프 + 화이트리스트로 일반어 false positive 통제 |

## 호환성

- **CLI 호환**: 신규 옵션 모두 기본값 보유. 옵트인 / 기본 동작 변경.
  - `--judge-mode` 기본값 `reference-free` (이전 `overlap` 동작에서 변경) — Judge 활성화 사용자에게 사전 공지 필요. legacy 동작은 `--judge-mode overlap`.
  - `--generator-temperature` 기본 0.0 (이전 0.7 하드코딩) — Generator 다양성 감소, 재현성 우선. 다양성 필요 시 명시 0.7.
  - `--generator-seed-base` None 기본 (기존 미전달 동작 유지).
- **YAML/JSON 스키마**: 새 키만 추가. metadata 에 `generator_temperature`, `generator_seed_base` 추가. summary 에 `judge_mode`, `low_sample_warning` 추가.
- **`build()` / `evaluate_one` 시그니처**: 새 keyword 인자 기본값 보유 → 외부 호출 후방 호환.

## 검증

| 항목 | 결과 |
|---|---|
| `python -m py_compile` (9 파일) | ✅ |
| `ruff check` (S2 패치 도입 위반 0건, main 기존 1건만 잔존) | ✅ |
| `synth.py` 한글 게이트 단위 (3자 고유명사 검출 / 일반어 false negative / 4자 회귀) | ✅ |
| `synth.generate_questions` seed/temperature 전파 통합 (kwargs picked up) | ✅ |
| `compare_runs` 통계 함수 회귀 + N<10 상수 + bootstrap CI 결정성 | ✅ |
| Judge 3 템플릿 구조 검증 (reference-free 가 source_chunk 미노출) | ✅ |
| P14 tie-breaker 코드 패턴 (sorted with negative similarity) | ✅ |
| P15 calibrate_graph_match.py syntax + 정규식·F1 산출 로직 | ✅ |
| `pytest tests/test_eval/` 풀 슈트 | ⏭️ 워크트리 Python 3.9 미지원, CI 환경 자동 실행 |

## 의도된 동작 변경 (사용자 안내)

1. **`--judge-mode` 기본값이 reference-free** — Judge 활성화 시 source chunk 가 더 이상 Judge 에 노출되지 않음. 기존 점수와 직접 비교 불가 (다른 측정 지표). legacy 호환: `--judge-mode overlap`
2. **Generator temperature 기본 0.0** — 다양성 감소, 재현성 향상. 같은 청크에서 매번 비슷한 질문 생성. 다양성 필요 시 `--generator-temperature 0.7`
3. **한글 게이트가 3자 명사도 검출** — 빌드 시 `fail_korean_leakage` 카운트가 증가할 수 있음. 화이트리스트 부족이면 false positive 발생 가능 — `_KOREAN_COMMON_NOUNS` 확장으로 도메인 튜닝 권장

## 사후 권고

1. **PR 머지 후 캘리브레이션 1회**: `python scripts/calibrate_graph_match.py --output eval/graph_threshold_calibration.json` 으로 본 환경의 권장 τ 산출 → `graph_match.py:33` 갱신 검토
2. **운영 자동화 사용자 안내**: `--judge-mode reference-free` 가 기본이 되면서 절대 점수가 이전과 다를 수 있음을 사전 공지
3. **`/rag-eval-audit` 재실행**: B+ → A− 회복 검증

## 부록

- v1 감사: `_workspace/findings_prev/SUMMARY.md` (C)
- v2 재감사: `_workspace/findings/SUMMARY.md` (B+)
- S0/S1 패치: `_workspace/patches/SUMMARY.md`
- 본 S2 패치: `_workspace/patches/S2_SUMMARY.md` (A− 회복)
