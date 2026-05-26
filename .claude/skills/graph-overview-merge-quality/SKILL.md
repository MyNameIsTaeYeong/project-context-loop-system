---
name: graph-overview-merge-quality
description: 웹 대시보드의 전역 그래프 페이지(/graph)와 엔티티 통합 품질 진단을 통합적으로 구축·확장·디버그한다. "전역 그래프 탭", "/graph 페이지", "엔티티 통합 품질", "의미론적 머지/병합 진단", "그래프 엔터티가 어느 문서에서 추출되었는지 확인", "merge quality", "duplicate entity detection", "그래프 가시화", "전역 그래프 보기" 같은 요청 시 반드시 사용. 후속 작업("다시 실행", "재실행", "/graph 페이지 수정", "merge-quality만 다시", "노드 상세에 X 추가", "보완", "업데이트", "이전 결과 기반")도 본 스킬로 처리. 그래프 검색 평가가 낮은 funnel 진단(`graph-search-diagnosis`)이나 인덱싱 추출 로직 자체 개선(`indexing-improvement`)과는 영역이 다르다 — 본 스킬은 가시화 + 통합 품질 측정 + 운영자가 보는 페이지 구축에 집중.
---

# Graph Overview & Merge Quality — Orchestrator

## 목표

웹 대시보드에 **전역 그래프 페이지(/graph)**를 구축하여 (1) 모든 그래프 노드의 관계를 한 화면에서 보고, (2) 각 엔티티가 어떤 문서에서 추출됐는지 확인하고, (3) 의미론적 통합 품질이 어느 수준인지 진단까지 노출한다. 자동 병합은 본 스킬 범위가 아니다 — **진단 + 메트릭 도구화**.

## 실행 모드

**하이브리드** — Phase별로 다른 모드:

| Phase | 모드 | 사유 |
|-------|------|------|
| A. 분석 | 서브 에이전트 (단일) | analyst가 보고서를 파일로 떨어뜨리면 충분, 팀 통신 불필요 |
| B. 구현 | 서브 에이전트 (병렬, background) | backend와 frontend가 독립 진행 — analyst 보고서 + 서로의 산출물 파일을 통해 조율 |
| C. 검증 | 서브 에이전트 (단일) | QA가 incremental하게 builder들에게 SendMessage로 피드백, 최종 합격 판정만 메인에 반환 |

## Phase 0: 컨텍스트 확인

워크플로우 시작 시 다음을 판별한다:

- `_workspace/01_merge_diagnosis.md`, `_workspace/02_backend.md`, `_workspace/03_frontend.md`, `_workspace/04_qa.md` 존재 여부 확인
- **초기 실행**: 산출물 없음 → 전체 Phase 실행
- **부분 재실행**: 사용자가 특정 영역(예: "frontend만", "merge-quality 메트릭만")을 지시 → 해당 에이전트만 재호출, 다른 산출물은 보존
- **새 라운드**: 산출물은 있지만 사용자가 "처음부터 다시"를 명시 → 기존 `_workspace/`를 `_workspace_prev_{timestamp}/`로 이동 후 새로 시작

`_workspace/00_context.md`를 만들어 다음을 기록:
- 사용자 요청 원문
- 어떤 Phase를 실행할지
- 이전 결과 사용 여부

## Phase A: 진단 (analyst)

```python
Agent(
    description="엔티티 통합 품질 진단",
    subagent_type="graph-merge-analyst",
    model="opus",
    prompt="""
    `_workspace/00_context.md`를 먼저 읽고 사용자 요청을 파악하라.

    `~/.context-loop/data/metadata.db` SQLite를 직접 조회하여 GraphStore의
    현재 엔티티 통합 정책이 의미론적으로 같은 엔티티를 충분히 통합하고 있는지
    진단하라. 정해진 카테고리(공백/구분자, 다국어, 단/복수, 타입 충돌, FQN)
    별로 잠재 중복 그룹을 찾고, 메트릭을 정의하라.

    출력: `_workspace/01_merge_diagnosis.md` 단일 파일.
    builder들이 4절(메트릭 정의)과 5절(UI 권고)을 사양으로 쓴다.
    """
)
```

산출물: `_workspace/01_merge_diagnosis.md`

## Phase B: 구현 (backend + frontend 병렬)

analyst 보고서가 떨어진 직후, backend와 frontend를 **병렬 background**로 실행. 둘 다 `_workspace/01_merge_diagnosis.md`를 입력으로 사용.

```python
# 동일 메시지 안에서 두 Agent 호출 (병렬)
Agent(
    description="전역 그래프 API 구현",
    subagent_type="graph-overview-api-builder",
    model="opus",
    run_in_background=True,
    prompt="""
    `_workspace/00_context.md`와 `_workspace/01_merge_diagnosis.md`를 읽고
    백엔드 라우터를 구현하라.

    구현 대상:
    - GET /graph (페이지 셸)
    - GET /api/graph/clusters
    - GET /api/graph/cluster/{entity_type}/nodes
    - GET /api/graph/node/{node_id}
    - GET /api/graph/merge-quality

    src/context_loop/web/api/graph_overview.py 신규, app.py에 라우터 등록.
    완료 후 _workspace/02_backend.md에 변경 요약과 응답 shape 예시 작성.
    """
)
Agent(
    description="전역 그래프 UI 구현",
    subagent_type="graph-overview-ui-builder",
    model="opus",
    run_in_background=True,
    prompt="""
    `_workspace/00_context.md`와 `_workspace/01_merge_diagnosis.md`를 읽고
    프론트엔드를 구현하라.

    구현 대상:
    - base.html nav에 'Graph' 메뉴 추가
    - templates/graph_overview.html 신규
    - static/js/graph_overview.js 신규
    - 타입별 클러스터 카드 → 펼침 → 노드 상세(출처 문서, 이웃) → 통합 품질 패널

    backend가 `_workspace/02_backend.md`에 응답 shape을 기록할 때까지 기다리지
    말고 graph-overview-api-builder.md의 명세를 사양으로 사용해 시작하라.
    완성 직전 backend의 실제 shape을 확인하고 불일치 시 SendMessage로 통보.
    완료 후 _workspace/03_frontend.md에 변경 요약 작성.
    """
)
```

두 에이전트 완료를 기다린 뒤 다음 Phase 진입.

**충돌 시:** 두 builder가 같은 파일을 동시 편집하지 않도록 분담:
- backend: `src/context_loop/web/api/*`, `src/context_loop/web/app.py`
- frontend: `src/context_loop/web/templates/*`, `src/context_loop/web/static/*`
- 페이지 셸 `graph_overview.html`은 backend가 더미만, frontend가 본격 구현 (작성 시점이 다르면 frontend가 final)

## Phase C: 검증 (QA)

```python
Agent(
    description="전역 그래프 페이지 통합 검증",
    subagent_type="graph-overview-qa",
    model="opus",
    prompt="""
    `_workspace/00_context.md`, `01_merge_diagnosis.md`, `02_backend.md`,
    `03_frontend.md`를 모두 읽고, 백엔드 API와 프론트엔드의 경계면 통합이
    올바른지 검증하라.

    검증 시나리오(S1~S6)를 모두 수행하고, 발견 사항을 _workspace/04_qa.md에
    카테고리별로 정리. 마지막에 PASS / PASS_WITH_NOTES / FAIL 중 하나를 명시.
    FAIL이면 어느 builder가 어떤 부분을 수정해야 하는지 재작업 지시 초안 포함.
    """
)
```

산출물: `_workspace/04_qa.md`

## Phase D: 결과 보고 (오케스트레이터 책임)

`_workspace/01_merge_diagnosis.md`의 핵심 결론(메트릭 수치, 상위 중복 그룹, 권고)을 사용자에게 1회 보고한다. 사용자가 "의미론적 통합이 되고 있는지" 명시적으로 물은 본 작업의 핵심 답변.

QA 결과가 PASS면:
- 페이지 경로와 진입 방법(`uvicorn ...` + 브라우저 `/graph`)을 안내
- 사용자에게 피드백 요청

PASS_WITH_NOTES면:
- 합격이지만 어떤 사소한 항목이 보류됐는지 알리고, 추가 라운드 의향 확인

FAIL이면:
- 책임 builder를 재호출하여 1회 재시도. 그래도 실패면 사용자에게 상황 보고 + 다음 라운드 권유 (재시도 무한 루프 금지)

## 데이터 전달 프로토콜

- **파일 기반**: `_workspace/` 하위. 명명 컨벤션 `{phase번호 또는 영역}_{이름}.{ext}` — `00_context.md`, `01_merge_diagnosis.md`, `02_backend.md`, `03_frontend.md`, `04_qa.md`
- **반환값**: 각 서브 에이전트는 짧은 요약(완료 여부 + 산출물 경로 + 핵심 발견) 1단락만 반환
- **메시지**: builder ↔ QA 간 SendMessage는 본 모드에서는 사용 안 함 (서브 에이전트는 별도 메시지 채널 없음). 대신 builder가 본인 산출물 파일에 "QA 주의 사항" 절을 두어 미리 알릴 수 있다.

## 에러 핸들링

- **analyst 실패**: 1회 재시도. 재실패 시 사용자 보고 후 중단. (builder/QA가 의존하므로 진행 불가)
- **backend 실패**: frontend는 그대로 진행하되 결과 통합 부분은 placeholder로. QA가 backend 실패를 별도 보고하면 다음 라운드에서 backend만 재호출.
- **frontend 실패**: backend는 정상 처리. 사용자가 API만 사용 가능 상태로 임시 사용 + 다음 라운드에서 frontend 재시도.
- **QA가 FAIL**: 책임 builder 1회 재호출. 2회 연속 FAIL이면 사용자 보고 + 다음 라운드 결정 위임.
- 상충 데이터(예: analyst가 정의한 메트릭과 backend 구현이 다름)는 삭제 금지. 출처(파일 + 절) 명기하여 양쪽 보고서에 보존.

## 테스트 시나리오

### 정상 흐름
1. 사용자가 "그래프 탭 만들어줘"라고 요청 → Phase A → Phase B (병렬) → Phase C → Phase D 보고
2. 결과: `/graph` 진입 가능, 클러스터/노드 상세/출처 문서/메트릭 모두 동작, QA PASS

### 부분 재실행 흐름 (예: "merge-quality 메트릭만 다시")
1. Phase 0에서 기존 `_workspace/01_merge_diagnosis.md` 존재 확인
2. analyst만 재호출하여 4절(메트릭 정의)만 갱신
3. backend 재호출하여 `/api/graph/merge-quality`만 수정
4. QA가 S2, S6만 재검증
5. Phase D에서 변경분만 보고

### 에러 흐름 (analyst 실패)
1. Phase A에서 analyst가 SQLite에 접근하지 못함 (DB 파일 없음)
2. 1회 재시도 → 실패
3. 사용자에게 "데이터 디렉토리에 graph 데이터가 없습니다. 문서를 먼저 처리해주세요" 보고 + 중단

## 합격 체크리스트 (Phase D 전)

- [ ] `_workspace/` 5개 파일 모두 존재
- [ ] `_workspace/04_qa.md`의 합격 여부 PASS 또는 PASS_WITH_NOTES
- [ ] `/graph`에 진입 가능 (라우터 등록 + 템플릿 존재)
- [ ] base.html nav에 Graph 메뉴 존재
- [ ] 노드 상세 → 출처 문서 → `/documents/{id}` 링크 동작
- [ ] merge-quality 응답의 메트릭이 4종 이상 포함
- [ ] CLAUDE.md 변경 이력에 본 하네스 실행 라인 추가
