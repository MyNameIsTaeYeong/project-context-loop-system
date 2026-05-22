---
name: indexing-analysis
description: 인덱싱 파이프라인이 "현재 어떻게 동작하는가"를 소스타입(confluence_mcp / git_code)별로 6단계(수집→전처리→청킹→임베딩→그래프추출→저장) 서술형 분석한다. "인덱싱 과정 분석", "인덱싱 단계별 분석", "인덱싱이 어떻게 이루어지는지", "파이프라인 동작 파악", "수집/청킹/임베딩/그래프/저장 단계 분석", "다시/추가/특정 소스만/특정 단계만 분석" 요청 시 사용. 개선·수정·구현이 아니라 동작 이해가 목적일 때 이 스킬을 쓴다(개선은 indexing-improvement 스킬).
---

# Indexing Analysis Orchestrator

## 목표

인덱싱 파이프라인의 **현재 동작**을 두 소스(confluence_mcp / git_code)별로 6단계(수집·전처리·청킹·임베딩·그래프추출·저장)로 분해하여 서술형으로 분석한다. **구현·개선이 아닌 동작 이해가 목적.** 개선·구현이 필요하면 `indexing-improvement` 스킬을 안내한다.

## 실행 모드

**서브 에이전트 병렬 (팬아웃)**
- 2개 분석가를 `run_in_background: true`로 동시 launch
- 데이터 전달: 파일 기반 (`_workspace/indexing-analysis/`)

## Phase 0: 컨텍스트 확인

1. **`_workspace/indexing-analysis/` 존재 여부**:
   - 없으면 → **초기 실행** (Phase A부터)
   - 있고 "다시/추가/보완" → **부분 재실행** (지정 소스/단계만)
   - 있고 새 분석 요청 → 기존을 `_workspace/indexing-analysis_prev_{날짜}/`로 이동 후 새 실행
2. **요청 파싱**: "전체" / "confluence만" / "git_code만" / "특정 단계만" 식별. 불명확하면 사용자 확인.
3. **원격 기준**: 사용자가 `origin/main` 기준 요청 시 `git status`로 머지 여부 확인, 필요 시 사용자 확인 후 `git fetch origin && git merge origin/main`.

## Phase A: 병렬 분석 (2 에이전트)

**실행 모드:** 서브 에이전트 병렬 호출 (단일 메시지에서 동시 launch)

1. `confluence-indexing-analyst` → `01_confluence_mcp_indexing.md`
2. `git-code-indexing-analyst` → `02_git_code_indexing.md`

Agent 호출 필수 파라미터:
- `model: "opus"`
- `subagent_type: "general-purpose"` (커스텀 정의는 `.claude/agents/{name}.md`에 존재하나 SDK 기본 타입 사용)
- `description`: 한 줄 (예: "Confluence 인덱싱 6단계 분석")
- `prompt`: 다음 형식
  ```
  당신은 .claude/agents/{agent_name}.md 에 정의된 에이전트입니다.
  먼저 그 파일을 읽고 역할/6단계/출력 구조를 숙지한 뒤 작업하세요.

  작업: {source_type} 인덱싱 파이프라인을 수집→전처리→청킹→임베딩→그래프추출→저장
  6단계로 분해하여 _workspace/indexing-analysis/{출력파일}에 서술형 분석을 작성.
  각 단계마다 호출 함수(파일:라인), 입력/산출 데이터 형태, 주요 파라미터를 포함할 것.
  개선점이 아니라 "현재 어떻게 동작하는가"를 기술.

  기존 산출물이 있으면 읽고 보완하세요.
  완료 시 한 줄 요약 + 파일 경로를 반환하세요.
  ```

2명 모두 완료될 때까지 대기. 누락 보고서가 있으면 해당 분석가만 1회 재실행.

## Phase B: 통합 개요

**실행 모드:** 메인(오케스트레이터) 직접 수행

2개 분석 보고서를 읽고 `_workspace/indexing-analysis/00_overview.md` 작성:
- 두 소스타입의 6단계를 나란히 비교한 표 (공유 모듈 / 분기 지점 명시)
- 공통 진입점(`process_document`)과 source_type 분기 요약
- 두 소스가 공유하는 단계(임베딩·저장)와 갈라지는 단계(수집·전처리·청킹·그래프) 구분

## 데이터 전달 프로토콜

| 단계 간 | 매체 | 파일 |
|---------|------|------|
| A → B | 파일 | `01_*.md`, `02_*.md` |
| B → 사용자 | 파일 + 메시지 | `00_overview.md` 요약 |

작업 디렉토리: `_workspace/indexing-analysis/` (보존)

## 에러 핸들링

| 상황 | 대응 |
|------|------|
| 분석가 보고서 누락 | 해당 분석가만 1회 재실행. 재실패 시 누락 표시하고 Phase B 진행 |
| 코드 경로가 문서와 불일치 | 분석가가 실제 코드를 우선. 발견 시 보고서에 "CLAUDE.md 설계와 실제 코드 차이" 명시 |

## 범위 가드

- 코드 수정 금지 (분석 전용 하네스).
- 평가 시스템(`scripts/eval_search.py`, `src/context_loop/eval/*`)은 인덱싱 범위 밖.
- 개선·구현 요청은 `indexing-improvement` 스킬로 안내.

## 산출물 체크리스트

- [ ] `_workspace/indexing-analysis/01_confluence_mcp_indexing.md`
- [ ] `_workspace/indexing-analysis/02_git_code_indexing.md`
- [ ] `_workspace/indexing-analysis/00_overview.md`

## 테스트 시나리오

**정상 흐름:**
1. 사용자: "두 소스타입 인덱싱 과정 6단계로 분석해줘"
2. Phase 0: `_workspace/indexing-analysis/` 없음 → 초기 실행
3. Phase A: 2 분석가 병렬 → 2 보고서
4. Phase B: 통합 개요 작성 → 사용자 보고

**부분 재실행 흐름:**
- 사용자: "git_code 임베딩 단계만 더 자세히"
- Phase 0: 기존 산출물 확인 → git-code-indexing-analyst만 재호출 (해당 단계 보강)
