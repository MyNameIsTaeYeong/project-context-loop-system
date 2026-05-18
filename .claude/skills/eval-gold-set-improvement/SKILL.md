---
name: eval-gold-set-improvement
description: 골드셋 합성/평가 시스템(build_synthetic_gold_set.py · eval/* · eval_search.py)의 개선·확장·디버그 작업을 분석→설계→구현 3단계 에이전트 팀으로 수행한다. 골드셋 스키마 변경, 새 source_type 평가 추가, graph context 평가 도입, 청크 사이즈 강건성 개선, 메트릭 추가, 재처리·재실행·부분 수정 요청에 모두 트리거된다. "골드셋 개선", "평가 시스템 수정", "build_synthetic_gold_set 고쳐", "graph 평가 추가", "다시 실행", "보완"과 같은 표현에서 반드시 사용한다.
---

# Eval Gold-Set Improvement — 3단계 팀 오케스트레이터

## 트리거 키워드

- 골드셋(gold set, gold_set) + 개선/확장/수정/디버그/보완
- `build_synthetic_gold_set.py`, `eval_search.py`, `src/context_loop/eval/*` 파일 변경 요청
- 평가(eval) 시스템 + chunk/graph/source_type
- 후속: "다시 실행", "재실행", "이 부분만 수정", "방금 결과 보완"

단순 질문(예: "골드셋이 뭐야?")은 트리거하지 않는다. **변경 작업**일 때만 트리거.

## 실행 모드: 에이전트 팀 (파이프라인)

3명의 에이전트가 순차 협업, 파일 기반 산출물 전달 + 메시지로 질문/피드백 교환:

```
[analyst]     →  _workspace/01_analysis.md
   │
   ▼
[designer]    →  _workspace/02_design.md   (analyst 결과 읽음)
   │
   ▼
[implementer] →  실제 코드 + _workspace/03_implementation.md
```

팀원: `eval-system-analyst`, `eval-system-designer`, `eval-system-implementer` — 모두 `.claude/agents/`에 정의되어 있다. 모두 `model: "opus"` + `subagent_type: "general-purpose"`로 호출.

## Phase 0: 컨텍스트 확인 (실행 모드 판별)

워크플로우 시작 시 다음을 확인:

1. `_workspace/` 폴더 존재 여부와 내용
2. 사용자 의도가 "초기 실행" / "부분 재실행" / "새 입력으로 다시" 중 무엇인지

**모드 결정:**
- `_workspace/` 없음 → **초기 실행** (Phase 1부터)
- `_workspace/` 있음 + 사용자가 특정 단계만 수정 요청 → **부분 재실행** (해당 에이전트만 호출)
- `_workspace/` 있음 + 사용자가 새 요구사항 제시 → **새 실행**: 기존 `_workspace/`를 `_workspace_prev_{타임스탬프}/`로 백업 후 Phase 1부터

부분 재실행 매핑:
- "분석이 부족해" → analyst만 재호출 + designer/implementer에 영향 알림
- "설계를 바꾸자" → designer 재호출 + implementer 재호출
- "구현이 틀렸어" → implementer만 재호출

## Phase 1: 사용자 요구사항 정리

작업 시작 전 한 줄 요약 작성:
- 무엇을 바꾸고 싶은가
- 만족해야 할 조건 (R1, R2, R3…)
- 비기능 요구사항 (backward-compat 등)

이 요약을 `_workspace/00_requirements.md`에 저장. 이후 모든 산출물의 기준선.

## Phase 2: Analyst 가동

```
Agent({
  subagent_type: "general-purpose",
  model: "opus",
  description: "Analyze eval/gold-set system",
  prompt: """
    .claude/agents/eval-system-analyst.md 와
    .claude/skills/eval-domain-knowledge/SKILL.md 를 먼저 읽고 그 지침에 따라 작업하라.
    요구사항: _workspace/00_requirements.md 를 읽어라.
    산출물: _workspace/01_analysis.md
  """
})
```

analyst가 산출물을 완성하고 메시지로 보고하면 다음 Phase로.

## Phase 3: Designer 가동

```
Agent({
  subagent_type: "general-purpose",
  model: "opus",
  description: "Design gold-set improvements",
  prompt: """
    .claude/agents/eval-system-designer.md 와
    .claude/skills/eval-domain-knowledge/SKILL.md 를 먼저 읽고 그 지침에 따라 작업하라.
    입력: _workspace/00_requirements.md, _workspace/01_analysis.md
    산출물: _workspace/02_design.md
  """
})
```

designer가 02_design.md 의 "위험/미해결" 섹션에 사용자 결정이 필요한 항목을 넣었다면, 메인이 사용자에게 확인한 뒤 implementer로 진행.

## Phase 4: Implementer 가동

```
Agent({
  subagent_type: "general-purpose",
  model: "opus",
  description: "Implement gold-set changes",
  prompt: """
    .claude/agents/eval-system-implementer.md 와
    .claude/skills/eval-domain-knowledge/SKILL.md 를 먼저 읽고 그 지침에 따라 작업하라.
    입력: _workspace/02_design.md (필수), _workspace/01_analysis.md (배경)
    산출물: 실제 코드 + 테스트 + _workspace/03_implementation.md
    구현 후 반드시 pytest 와 ruff 로 검증할 것.
  """
})
```

## Phase 5: 결과 보고

implementer 완료 후 메인이:
1. `_workspace/03_implementation.md` 를 읽고 핵심 변경/검증 결과를 사용자에게 5~10줄로 요약
2. 사용자가 검토할 수 있도록 변경 파일 목록·테스트 결과·새 CLI 사용 예시를 제시
3. 추가 피드백을 받으면 적절한 Phase로 되돌아감 (부분 재실행)

## 데이터 전달 프로토콜

| 파일 | 작성자 | 소비자 | 용도 |
|------|------|------|------|
| `_workspace/00_requirements.md` | 메인 | analyst, designer, implementer | 요구사항 기준선 |
| `_workspace/01_analysis.md` | analyst | designer, implementer | 현황 분석 |
| `_workspace/02_design.md` | designer | implementer | 작업 지시서 |
| `_workspace/03_implementation.md` | implementer | 메인 | 변경 요약 |

메시지(`SendMessage`)는 짧은 질문·답변·진행 알림에만 사용. 본문은 항상 파일에.

## 에러 핸들링

- **에이전트 실패 1회 재시도**. 재시도 후도 실패면 해당 에이전트 결과 없이 사용자에게 보고 후 진행 여부 확인.
- **테스트 깨짐**: implementer가 자체적으로 1회 수정 시도. 2회 실패 시 사용자에게 보고.
- **설계와 구현 불일치**: implementer가 designer에게 SendMessage 로 질의. 답이 오면 진행.
- **상충 데이터**: 02_design.md 와 03_implementation.md 가 어긋나면 03 에 그 사실 기록 (지우지 않음).

## 테스트 시나리오

### 정상 흐름
1. 사용자: "골드셋이 청크 사이즈 변경에 강건하도록 + git_code/graph 도 평가 가능하게 개선해줘"
2. 메인 → 00_requirements.md 작성
3. analyst 가동 → 01_analysis.md (chunk_id 의존 12곳 식별, graph 미평가 확인)
4. designer 가동 → 02_design.md (GoldItem 새 스키마, 마이그레이션 전략)
5. implementer 가동 → 코드 + 테스트 + 03_implementation.md
6. 메인이 결과 보고, 사용자 OK

### 에러 흐름
1. implementer가 designer 설계와 충돌하는 부분 발견
2. implementer → designer 에 SendMessage 질의
3. designer가 02_design.md 갱신
4. implementer 재가동 (부분 재실행)

## 변경 이력

| 날짜 | 변경 내용 | 사유 |
|------|---------|------|
| 2026-05-18 | 초기 구성 — analyst/designer/implementer 3인 팀 | 골드셋 chunk-size 강건성 + git_code/graph 평가 도입 요청 |
