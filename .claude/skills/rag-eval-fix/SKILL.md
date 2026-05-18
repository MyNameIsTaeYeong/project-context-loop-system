---
name: rag-eval-fix
description: 검색·RAG 평가 시스템(scripts/build_synthetic_gold_set.py, scripts/eval_search.py 및 의존 모듈)의 감사 결과(_workspace/findings/SUMMARY.md 의 S0/S1 권고)에 따라 코드 패치를 적용하고 변경을 검증한다. "S0 패치 적용", "S1 패치 적용", "감사 결과 기반 개선", "rag-eval-audit 결과 패치", "골드셋 평가 신뢰성 패치", "build_synthetic_gold_set 개선", "eval_search 개선", "특정 위험만 패치(P1만, C3만 등)", "감사 위험 해소 검증" 같은 요청이나, 이전 감사·패치 이후 새 위험이 발견되어 추가 개선이 필요할 때 반드시 이 스킬을 사용한다. 패치 부분 재실행·검증만 재실행도 지원.
---

# RAG Eval Fix Orchestrator

`rag-eval-audit` 가 감사 보고서를 만들면, 이 스킬이 보고서의 S0/S1 권고를 받아 코드에 적용하고 변경을 검증한다. 감사와 패치가 분리되어 있어 사용자가 보고서 검토 후 별도로 패치 결정할 수 있다.

## 적용 범위

- `_workspace/findings/SUMMARY.md` 의 S0 6건(P1~P6, Critical 해소 우선) + S1 6건(P7~P12, High 해소)
- 골드셋 생성 측 패치(`build_synthetic_gold_set.py`, `synth.py`) + 평가 측 패치(`eval_search.py`, `llm.py`, `graph_match.py`) + 신규 도구(`compare_runs.py`)
- 변경 검증: syntax/lint/test/위험 해소 spot check

S2 이상(아키텍처 변경, 새 LLM seed 인프라 등)은 별도 스킬·PR로 분리한다.

## 실행 모드

**하이브리드** — Phase A 서브 병렬, Phase B 서브 단독, Phase C 메인 종합.

| Phase | 모드 | 에이전트 | 산출물 |
|---|---|---|---|
| A. 패치 적용 | 서브 병렬 (`run_in_background: true`) | gold-set-build-patcher, eval-script-patcher | `_workspace/patches/A_gold_set_build.md`, `B_eval_script.md` + 실제 코드 변경 |
| B. 변경 검증 | 서브 단독 | rag-eval-change-verifier | `_workspace/patches/C_verification.md` |
| C. 종합 | 메인 | (메인 직접 작성) | `_workspace/patches/SUMMARY.md` (패치 요약) |

## Phase 0: 컨텍스트 확인

먼저 `_workspace/findings/SUMMARY.md` 가 있는지 확인. 없으면 **사용자에게 감사 선행 안내** — `/rag-eval-audit` 를 먼저 실행해야 한다.

다음으로 `_workspace/patches/` 확인:
- 미존재 → **초기 실행** (Phase A부터 전체)
- 존재 + 사용자가 "특정 P만 다시" 요청 → **부분 재실행** (해당 P 항목만 처리, 다른 결과 유지)
- 존재 + 사용자가 "검증만 다시" 요청 → **검증 재실행** (Phase B만, A 결과 유지)
- 존재 + 새 감사 결과(SUMMARY.md 변경 감지)로 새 P 추가 → 기존 `patches/`를 `patches_prev/`로 이동 후 새 실행

## Phase 1: 패치 매핑 확인

`_workspace/findings/SUMMARY.md` 의 S0/S1 권고를 읽고 각 P 항목을 두 패처 중 하나에 할당:

| Group | 담당 에이전트 | 패치 항목 | 파일 |
|---|---|---|---|
| A | gold-set-build-patcher | P1, P4, P7, P10, P11 | `build_synthetic_gold_set.py`, `synth.py` |
| B | eval-script-patcher | P2, P3, P5, P6, P8, P9, P12 | `eval_search.py`, `llm.py`, `graph_match.py`, 신규 `compare_runs.py` |

사용자가 "S0만" 또는 "P1, P5만" 같이 범위를 지정하면 해당 항목만 매핑.

## Phase 2: Phase A — 패치 적용 (병렬)

```
Agent(gold-set-build-patcher, model="opus", run_in_background=true,
      prompt="_workspace/findings/SUMMARY.md 와 01_gold_set_audit.md 를 읽고
              할당된 P 항목({list})을 patch. 변경된 줄과 회귀 위험을
              _workspace/patches/A_gold_set_build.md 에 기록.")

Agent(eval-script-patcher, model="opus", run_in_background=true,
      prompt="_workspace/findings/SUMMARY.md 와 02_eval_script_audit.md 를 읽고
              할당된 P 항목({list})을 patch + scripts/compare_runs.py 신설.
              변경 요약을 _workspace/patches/B_eval_script.md 에 기록.")
```

두 패처는 통신하지 않고 파일로만 산출물 교환. 단 가능한 의존(P1→P8, P3→P1)은 각자 패치 로그에 명시.

## Phase 3: Phase B — 변경 검증

```
Agent(rag-eval-change-verifier, model="opus",
      prompt="_workspace/patches/A_gold_set_build.md 와 B_eval_script.md 를 읽고
              실제 코드를 검사. 5단계 검증(구조 무결성/정적 검사/테스트 회귀/
              위험 해소 spot check/후방 호환)을 수행하여
              _workspace/patches/C_verification.md 작성.")
```

검증 PARTIAL/FAIL이면 메인이 사용자에게 보고하고, 사용자가 추가 패치 요청 시 해당 P만 부분 재실행.

## Phase 4: Phase C — 메인 종합

`_workspace/patches/SUMMARY.md` 작성:

```markdown
# RAG 평가 시스템 — S0/S1 패치 요약

## 적용 결과
- S0: P1, P2, P3, P4, P5, P6 (6건) — ✅/⚠️/❌
- S1: P7, P8, P9, P10, P11, P12 (6건) — ✅/⚠️/❌

## 위험 해소 매트릭스
| 원래 위험 | 패치 | 검증 결과 | 잔여 위험 |
|---|---|---|---|
| C1 | P1+P2+P3 | ✅ | (옵트인 플래그로만 fallback 가능) |
| ... | ... | ... | ... |

## 신뢰도 재평가
- 패치 전: C (감사 보고)
- 패치 후 (이 PR 머지 시): {예상 등급}

## 후속 권고
- S2 이상 (LLM seed 인프라, graph τ 캘리브레이션 등)은 별도 PR로
- 변경 코드 반영 후 `/rag-eval-audit` 재실행 권장
```

## 데이터 전달 / 에러 핸들링

- 산출물: `_workspace/patches/{A,B,C}_*.md` + 실제 코드 변경
- 에이전트 실패 시 1회 재시도. 재실패 시 해당 자리에 `AGENT_FAILED` + 종합에 누락 명시
- 두 패처가 같은 파일을 우연히 수정하는 경우는 분담 명확(A=build/synth, B=eval/llm/graph_match)으로 사전 방지. 발생 시 verifier가 검출

## 후속 작업 지원

- "P5만 다시" → eval-script-patcher만 호출, B 로그의 P5 섹션 갱신, C 재실행
- "검증만 다시" → verifier만 재호출
- "S2 추가" → 별도 스킬·PR로 분리하라고 안내 (이 스킬 범위 밖)

## 테스트 시나리오

**정상 흐름:** 사용자가 "S0+S1 패치" 요청 → 감사 보고서 존재 확인 → 패치 매핑 → 두 패처 병렬 → 검증 PASS → 메인이 종합 SUMMARY 작성 → 사용자에게 위험 해소 매트릭스 보고.

**에러 흐름 1:** 감사 보고서 미존재 → 사용자에게 `/rag-eval-audit` 선행 안내, 종료.

**에러 흐름 2:** eval-script-patcher가 P8(compare_runs.py 신설)에서 실패. 1회 재시도 후 재실패 → B 로그에 `P8: AGENT_FAILED` 기록, 다른 P 항목은 적용. verifier가 P8 미적용을 명시. 사용자에게 P8만 수동 또는 부분 재실행 요청.

**에러 흐름 3:** verifier가 ruff 회귀를 발견. 메인이 어떤 패치 줄이 lint 위반인지 사용자에게 보여주고 수정 옵션 제공.

## 출력 위치 규칙

- 패치 로그: `_workspace/patches/A_gold_set_build.md`, `B_eval_script.md`, `C_verification.md`, `SUMMARY.md`
- 코드 변경: 작업 디렉터리 원래 경로 (`scripts/`, `src/context_loop/eval/`)
- 이전 실행 보존: `_workspace/patches_prev/`
- 사용자 지정 경로 없음 (코드 변경은 PR 단위로 관리)
