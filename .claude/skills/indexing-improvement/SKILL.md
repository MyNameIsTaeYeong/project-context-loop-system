---
name: indexing-improvement
description: 인덱싱 파이프라인(청킹·그래프 추출) 검토와 개선을 수행한다. confluence_mcp / git_code 소스의 청킹·그래프 로직 분석, 개선점 도출, 구현, 검증까지 한 흐름으로 처리. "인덱싱 로직 검토/개선", "청킹 개선", "그래프 추출 개선", "chunker/extraction_unit/ast_code_extractor/body_extractor/llm_body_extractor/link_graph_builder 검토", "인덱싱 회귀 확인", "재실행/업데이트/추가 라운드/이전 결과 기반 개선" 같은 요청 시 사용. 평가 시스템(eval_search.py, build_synthetic_gold_set.py)이 아니라 인덱싱 자체를 다룬다.
---

# Indexing Improvement Orchestrator

## 목표

인덱싱 파이프라인의 **청킹 + 그래프 추출** 로직을 두 소스(confluence_mcp / git_code)별로 체계적으로 검토하고, 우선순위 기반으로 개선까지 구현하는 4-페이즈 워크플로우.

## 실행 모드

**하이브리드** (서브 에이전트 기반)
- Phase A: 4개 분석가 서브 에이전트 **병렬** (`run_in_background: true`)
- Phase B/C/D: 단일 서브 에이전트 순차 (designer → implementer → verifier)
- 데이터 전달: 파일 기반 (`_workspace/indexing-improvement/`)

## Phase 0: 컨텍스트 확인

스킬 시작 시 다음을 확인한다:

1. **`_workspace/indexing-improvement/` 존재 여부**:
   - 없으면 → **초기 실행** (Phase A부터 시작)
   - 있고 사용자가 "다시", "추가", "보완" 요청 → **부분 재실행** (사용자가 지정한 단계부터)
   - 있고 사용자가 새로운 검토 요청 → 기존을 `_workspace/indexing-improvement_prev_{날짜}/` 로 이동 후 새 실행

2. **사용자 요청 파싱**:
   - "전체" / "특정 소스만" / "특정 측면(청킹/그래프)만" / "이전 결과 기반 R2 진행" 등을 식별
   - 명확하지 않으면 사용자에게 확인

3. **원격 기준 확인**:
   - 사용자가 `origin/main` 기준을 요청했다면 `git status`로 현재 HEAD 확인 (이미 merge 되었는지)
   - 머지가 필요하면 사용자에게 확인 후 `git fetch origin && git merge origin/main`

## Phase A: 병렬 분석 (4 에이전트)

**실행 모드:** 서브 에이전트 병렬 호출

4명을 `run_in_background: true`로 **단일 메시지에서 동시 launch**:

1. `confluence-chunking-analyst` → `01_confluence_chunking_findings.md`
2. `git-code-chunking-analyst` → `02_git_code_chunking_findings.md`
3. `confluence-graph-analyst` → `03_confluence_graph_findings.md`
4. `git-code-graph-analyst` → `04_git_code_graph_findings.md`

Agent 호출 시 필수 파라미터:
- `model: "opus"`
- `subagent_type: "general-purpose"` (커스텀 에이전트 정의는 `.claude/agents/{name}.md` 로 존재하지만 SDK 기본 타입 사용)
- `description`: 짧은 한 줄 (예: "Confluence 청킹 분석")
- `prompt`: 다음 형식
  ```
  당신은 .claude/agents/{agent_name}.md 에 정의된 에이전트입니다.
  먼저 그 파일을 읽고 역할/원칙/체크리스트를 숙지한 뒤 작업하세요.

  작업: {scope, 예: "confluence_mcp 청킹 경로 전체 검토 및 _workspace/indexing-improvement/01_confluence_chunking_findings.md 작성"}

  기존 산출물이 있으면 읽고 보완하세요.
  완료 시 한 줄 요약 + 파일 경로를 반환하세요.
  ```

4명이 모두 완료될 때까지 대기 (모든 백그라운드 완료 알림 수신).

오케스트레이터는 4개 보고서가 모두 존재하는지 확인. 누락된 보고서가 있으면 해당 분석가만 재실행.

## Phase B: 설계 통합

**실행 모드:** 단일 서브 에이전트

`indexing-improvement-designer`를 호출:
- `subagent_type: "general-purpose"`, `model: "opus"`
- 4개 보고서를 모두 읽고 `05_improvement_plan.md` 생성

생성 후 사용자에게 계획서 요약을 보고:
- 라운드 1 항목 N건, 라운드 2 항목 M건
- 변경 예정 파일 목록
- 사용자에게 "R1을 그대로 진행할까요?" 확인

사용자 피드백이 오면 designer 재호출하여 plan을 update.

## Phase C: 구현

**실행 모드:** 단일 서브 에이전트

`indexing-improvement-implementer`를 호출:
- prompt에 "라운드 1만 구현" 명시 (사용자가 다르게 지시했으면 그것)
- `subagent_type: "general-purpose"`, `model: "opus"`
- 작업 완료 후 `06_implementation_report.md` 와 실제 파일 변경

구현 후 변경 파일 목록과 추가 테스트 명령을 verifier 단계로 전달.

## Phase D: 검증

**실행 모드:** 단일 서브 에이전트

`indexing-change-verifier`를 호출:
- 계획서 + 구현 보고서 + `git diff origin/main` 비교
- 테스트 직접 실행
- `07_verification_report.md` 작성

결과에 따라:
- **PASS**: 사용자에게 최종 요약 보고 (변경 파일, 통과 테스트, 다음 라운드 권고)
- **PASS-WITH-NOTES**: 주의사항을 사용자에게 명시
- **FAIL**: implementer로 회신 (구체적 실패 항목 전달) → Phase C 재실행

## 데이터 전달 프로토콜

| 단계 간 | 전달 매체 | 파일 |
|---------|----------|------|
| A → B | 파일 | `01..04_*.md` |
| B → C | 파일 | `05_improvement_plan.md` |
| C → D | 파일 + 메시지 | `06_implementation_report.md` + 실제 diff |
| 최종 → 사용자 | 메시지 | `07_verification_report.md` 요약 |

작업 디렉토리: `_workspace/indexing-improvement/` (보존 — 후속 라운드 컨텍스트)

## 에러 핸들링

| 상황 | 대응 |
|------|------|
| 분석가 보고서 누락 (Phase A) | 해당 분석가만 재실행 1회. 재실패 시 누락 표시하고 Phase B 진행 |
| 계획서가 비어있음 (Phase B) | 분석가 보고서 재검토, 필요 시 분석가 재호출 |
| 구현 중 계획 가정 오류 (Phase C) | implementer가 즉시 중단·보고 → designer 재호출하여 plan 갱신 |
| 검증 실패 (Phase D FAIL) | 구체적 실패 목록을 implementer로 회신 → Phase C 재실행 1회. 재실패 시 사용자에게 보고 |
| 테스트 자체가 깨져있음 (사전 존재 fail) | verifier가 기존 fail vs 신규 fail 구분, 신규 fail만 책임 |

## 범위 가드 (절대 침범하지 않음)

다음은 별도 하네스 영역 — 이 워크플로우에서 다루지 않는다:
- `scripts/eval_search.py` / `scripts/build_synthetic_gold_set.py` 변경
- `src/context_loop/eval/*` 변경
- `.claude/agents/` 의 eval/audit/patcher 계열 에이전트는 호출하지 않음

이 영역에 변경이 필요하다고 발견되면, 발견 보고서에 "별도 하네스 영역 — 추가 작업 권고" 로만 기록.

## 산출물 체크리스트

각 라운드 종료 시:
- [ ] `_workspace/indexing-improvement/01..04_*.md` 4개 (분석)
- [ ] `_workspace/indexing-improvement/05_improvement_plan.md` (설계)
- [ ] `_workspace/indexing-improvement/06_implementation_report.md` (구현)
- [ ] `_workspace/indexing-improvement/07_verification_report.md` (검증)
- [ ] `src/...`, `tests/...` 실제 코드 변경
- [ ] 변경 영역 테스트 통과

## 테스트 시나리오

**정상 흐름:**
1. 사용자: "인덱싱 로직 검토하고 개선해줘"
2. Phase 0: `_workspace/indexing-improvement/` 없음 → 초기 실행
3. Phase A: 4 분석가 병렬 → 4 보고서 생성
4. Phase B: designer → R1=5건, R2=3건 계획서 생성
5. 사용자 승인
6. Phase C: implementer → 5개 파일 변경, 테스트 추가
7. Phase D: verifier → PASS
8. 사용자에게 요약 보고

**에러 흐름 1 (테스트 실패):**
- Phase D에서 신규 테스트 1개 실패 → implementer 재호출 → 1회 수정 → PASS

**에러 흐름 2 (계획 오류):**
- Phase C 도중 implementer가 "F-03의 가정이 틀림 (실제 코드가 이미 그렇게 동작)" 보고
- designer 재호출하여 plan 수정 → 새 계획으로 Phase C 재진행

**부분 재실행 흐름:**
- 사용자: "R2를 진행해줘"
- Phase 0: 기존 산출물 확인 → Phase B(plan 확인) → Phase C(R2 구현) → Phase D

## 사용자 보고 형식

최종 보고 시 다음을 포함:
- 검토된 영역 4개 (각 발견 건수)
- R1에서 구현된 개선 N건 (간단 목록)
- 통과 테스트 (변경 영역)
- 회귀 위험 / 후속 권고 (있으면)
- 다음 라운드 진행 여부 안내
