---
name: rag-eval-audit
description: 검색·RAG 평가용 합성 골드셋과 평가 스크립트의 신뢰성을 체계적으로 감사한다. 골드셋 편향(샘플링/정답 누설/Judge 게이트), 평가 메트릭 정확성(Recall/Precision/MRR/nDCG/tie-breaker), 그리고 evaluator-generator 의존성으로 인한 self-evaluation bias를 3-에이전트 팀으로 분업하여 분석. "골드셋 신뢰성", "평가 신뢰성", "RAG 평가 감사", "검색 평가 검토", "골드셋 편향", "self-evaluation bias", "build_synthetic_gold_set", "eval_search" 같은 요청이나, 평가 결과를 운영 의사결정에 쓰기 전 사전 검증이 필요할 때 반드시 이 스킬을 사용한다. 이전 감사 결과를 갱신하거나 부분 재실행도 지원.
---

# RAG Evaluation Audit Orchestrator

검색·RAG 평가용 **합성 골드셋 생성기**와 **평가 스크립트**의 신뢰성을 3-에이전트로 감사하고, 운영 사용 가능 여부를 판정한다.

## 적용 범위

- 합성 골드셋 생성 스크립트(역방향 생성 + Judge 게이트 + 다중 모델 분리)의 편향·재현성·강건성 점검
- 평가 스크립트의 메트릭 구현 정확성(Recall/Precision/MRR/nDCG/Hit), tie-breaker 결정성, Judge 채점 메타-편향
- 생성기-평가기-시스템 본체 간 LLM/임베딩/데이터 의존성으로 인한 self-evaluation bias

## 실행 모드

**하이브리드** — Phase A 서브 에이전트 병렬, Phase B 서브 에이전트 순차, Phase C 메인 종합.

| Phase | 모드 | 에이전트 | 산출물 |
|---|---|---|---|
| A. 독립 감사 | 서브 병렬 (`run_in_background: true`) | gold-set-auditor, eval-script-auditor | `_workspace/findings/01_*.md`, `02_*.md` |
| B. 교차 분석 | 서브 단독 | rag-bias-cross-analyst | `_workspace/findings/03_*.md` |
| C. 종합 | 메인 | (메인 직접 작성) | `_workspace/findings/SUMMARY.md` |

## Phase 0: 컨텍스트 확인 (초기/후속/부분 재실행 판별)

먼저 `_workspace/findings/`의 기존 산출물을 확인한다.

- 산출물 미존재 → **초기 실행** (Phase 1-3 전체)
- 산출물 존재 + 사용자가 "다시", "보완", "특정 차원만" 요청 → **부분 재실행**: 해당 에이전트만 호출, 다른 파일은 유지
- 산출물 존재 + 사용자가 코드 변경 후 재평가 요청 → **새 실행**: 기존 `_workspace/findings/`를 `_workspace/findings_prev/`로 이동 후 Phase 1부터

## Phase 1: 분석 대상 스테이징

대상 파일을 `_workspace/source/`에 추출. 기본 대상: `origin/main`의 다음 파일.

| 파일 | 역할 |
|---|---|
| `scripts/build_synthetic_gold_set.py` | 골드셋 생성 진입점 |
| `scripts/eval_search.py` | 평가 진입점 |
| `src/context_loop/eval/synth.py` | 생성/필터 로직 |
| `src/context_loop/eval/metrics.py` | 메트릭 구현 |
| `src/context_loop/eval/llm.py` | role별 LLM client builder |
| `src/context_loop/eval/graph_match.py` | 그래프 채점/임베딩 |

스테이징 명령 예시:

```bash
mkdir -p _workspace/source _workspace/findings
for f in scripts/build_synthetic_gold_set.py scripts/eval_search.py \
         src/context_loop/eval/synth.py src/context_loop/eval/metrics.py \
         src/context_loop/eval/llm.py src/context_loop/eval/graph_match.py; do
  git show "origin/main:$f" > "_workspace/source/$(basename $f)"
done
```

사용자가 다른 기준(브랜치, PR diff, 로컬 작업본)을 지정하면 스테이징 명령을 조정한다.

## Phase 2: Phase A — 독립 감사 (병렬)

두 감사관을 서브 에이전트로 **병렬 실행**한다.

```
Agent(gold-set-auditor, model="opus", run_in_background=true,
      prompt="_workspace/source/ 의 build_synthetic_gold_set.py + synth.py + llm.py + graph_match.py 를
              감사하여 _workspace/findings/01_gold_set_audit.md 를 작성하라.
              에이전트 정의의 7개 차원 모두 다루고 위험 등급화하라.")

Agent(eval-script-auditor, model="opus", run_in_background=true,
      prompt="_workspace/source/ 의 eval_search.py + metrics.py + llm.py + graph_match.py 를
              감사하여 _workspace/findings/02_eval_script_audit.md 를 작성하라.
              에이전트 정의의 7개 차원 모두 다루고 위험 등급화하라.")
```

두 에이전트는 통신하지 않는다. 산출물 파일이 유일한 인터페이스.

## Phase 3: Phase B — 교차 분석 (순차)

Phase A 두 산출물이 완성된 뒤 호출한다.

```
Agent(rag-bias-cross-analyst, model="opus",
      prompt="_workspace/findings/01_gold_set_audit.md 와 02_eval_script_audit.md 를 읽고,
              _workspace/source/ 의 모든 파일을 다시 검토하여,
              5개 의존 채널(A-E)과 4개 시나리오를 분석한 후
              _workspace/findings/03_cross_bias_analysis.md 를 작성하라.
              단일 감사관이 놓친 경계면 위험을 발굴하는 게 핵심.")
```

## Phase 4: Phase C — 메인 종합

세 산출물을 통합하여 `_workspace/findings/SUMMARY.md`를 메인이 직접 작성한다. 구조:

```markdown
# RAG 평가 신뢰성 감사 — 종합 보고

## TL;DR 판정
{한 문장: 이 골드셋·평가 스크립트로 측정한 메트릭을 어떤 수준의 의사결정에 쓸 수 있는가}

## Top 5 위험 (등급순)
1. [CRITICAL/HIGH] ... — 증거 file:line — 영향 — 권고
2. ...

## 사용 가능 / 사용 금지 매트릭스
| 의사결정 유형 | 사용 가능? | 단서 |
| --- | --- | --- |
| 동일 시스템 내 코드 변경 전후 비교 (A/B) | … | … |
| 다른 RAG 시스템과의 외부 벤치마크 | … | … |
| 모델/임베딩 교체 의사결정 | … | … |
| 운영 출시 게이트 | … | … |

## 우선순위 개선 권고 (코드 패치 단위)
1. {파일:줄} — {구체 변경} — {기대 효과}
...

## 부록
- 세부 감사 보고서 링크 (01, 02, 03)
- 다음 재감사 권장 트리거 (모델 교체, 골드셋 갱신 등)
```

## 데이터 전달 / 에러 핸들링

- 모든 산출물은 `_workspace/findings/`에 파일로 저장. 중간 산출물 보존(감사 추적용).
- 에이전트 실패 시 1회 재시도. 재실패 시 해당 보고서 자리에 "AGENT_FAILED" 표시 + 종합 보고서에 누락 명시.
- 두 감사관의 결론이 상충하면 종합 보고서에 양쪽 의견을 출처와 함께 병기. 임의로 한쪽을 채택하지 않는다.

## 후속 작업 지원

- "01만 다시" → gold-set-auditor만 재호출, 기존 02/03은 유지하되 SUMMARY는 재작성
- "코드 X 패치 후 재평가" → 새 실행 모드 (`findings_prev/`로 이동 후 Phase 1부터)
- "특정 차원만 깊이 봐달라" → 해당 에이전트에 차원 한정 프롬프트 전달, 산출물은 별도 파일 `_workspace/findings/0X_focus_{차원}.md`

## 테스트 시나리오

**정상 흐름:** 사용자가 "골드셋·평가 신뢰성 검토" 요청 → Phase 1 스테이징 → Phase 2 병렬 감사 → Phase 3 교차 분석 → Phase 4 SUMMARY 생성 → 사용자에게 TL;DR + 매트릭스 보고.

**에러 흐름:** Phase 2의 eval-script-auditor가 실패. 1회 재시도 후 재실패 → 02 자리에 "AGENT_FAILED: {원인}" 작성, Phase 3는 01만으로 진행하되 02 누락 명시, SUMMARY에 "평가 스크립트 감사 누락 — 보고 신뢰도 절반" 경고 포함.

## 출력 위치 규칙

- 최종 보고서: `_workspace/findings/SUMMARY.md` (사용자에게 경로 안내)
- 세부 보고서: `_workspace/findings/01_*.md`, `02_*.md`, `03_*.md`
- 소스 스테이징: `_workspace/source/`
- 이전 실행 보존: `_workspace/findings_prev/` (재실행 시)

사용자 지정 경로(예: `eval/audit_report.md`)가 있으면 SUMMARY를 그쪽으로도 복사.
