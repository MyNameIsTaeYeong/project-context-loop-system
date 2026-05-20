---
name: graph-search-diagnosis
description: 그래프 검색 품질 진단·개선 워크플로우. graph_hit/precision/recall/MRR/NDCG 같은 그래프 측 메트릭이 비정상적으로 낮을 때(예: < 10%) 인덱스/검색 파이프라인/메트릭 산출의 어느 단계에서 손실이 나는지 funnel 진단하고 우선순위 기반으로 개선까지 구현한다. "그래프 검색 평가가 낮다", "graph_recall이 0%", "그래프 매칭 실패", "그래프 검색 개선/진단/디버그", "검색 funnel 분석", "엔티티 매칭 실패", "재실행/업데이트/추가 라운드" 같은 요청 시 사용. 인덱싱 추출 로직 자체 개선은 indexing-improvement 영역, 평가 신뢰성 감사는 rag-eval-audit 영역 — 본 스킬은 검색측 품질 funnel 진단이 핵심.
---

# Graph Search Diagnosis Orchestrator

## 목표

그래프 검색 메트릭이 비정상적으로 낮을 때 (인덱스 / 검색 / 메트릭 산출) funnel을 진단하고, 우선순위 기반으로 개선까지 구현하는 4-Phase 워크플로우.

## 실행 모드

**하이브리드 (서브 에이전트 기반)**
- Phase A: 진단가 3명 병렬 (인덱스 / 검색 / 메트릭)
- Phase B/C/D: 단일 서브 에이전트 순차 (designer → implementer → verifier)
- 데이터 전달: 파일 기반 `_workspace/graph-search-diagnosis/`

## Phase 0: 컨텍스트 확인

1. `_workspace/graph-search-diagnosis/` 존재 여부
   - 없으면 → 초기 실행 (Phase A부터)
   - 있고 사용자가 "다시/추가/보완" → 부분 재실행
   - 있고 새 평가 결과 제공 → 기존 백업 후 새 실행
2. 사용자 요청 파싱 ("전체 진단" / "특정 단계만")
3. 가능하면 사용자에게 최근 eval 실행 결과 파일 경로 요청 (per-query row 데이터가 있으면 진단 정밀도↑)

## Phase A: 3-funnel 병렬 진단

`Agent` + `run_in_background=true`로 3명 동시 launch:

1. `graph-index-diagnostic-analyst` → `01_index_diagnosis.md`
2. `graph-search-pipeline-analyst` → `02_search_pipeline_diagnosis.md`
3. `graph-eval-metric-analyst` → `03_eval_metric_diagnosis.md`

Agent 호출 필수 파라미터:
- `model: "opus"`
- `subagent_type: "general-purpose"`
- prompt 형식:
  ```
  당신은 .claude/agents/{agent_name}.md 에 정의된 에이전트입니다.
  먼저 그 파일을 정독하여 역할/원칙/체크리스트를 숙지하세요.
  작업: {scope}
  완료 시 한 줄 요약 + 파일 경로를 반환하세요.
  ```

3개 보고서 완료 후 메인이 funnel 분포를 한 눈에 정리.

## Phase B: 설계 통합

`graph-search-improvement-designer` 호출 → `04_improvement_plan.md` 생성.

사용자에게 R1 항목 요약 보고 + 승인 받음.

## Phase C: 구현

`graph-search-improvement-implementer` 호출. 라운드 1만 구현. `05_implementation_report.md` + 실제 코드 변경.

## Phase D: 검증

`graph-search-change-verifier` 호출. PASS면 사용자에게 최종 보고. FAIL이면 implementer 재호출 (1회 한정).

## 데이터 전달 프로토콜

| 단계 간 | 매체 | 파일 |
|---------|------|------|
| A → B | 파일 | `01..03_*.md` |
| B → C | 파일 | `04_improvement_plan.md` |
| C → D | 파일 + diff | `05_implementation_report.md` + `git diff` |
| 최종 → 사용자 | 메시지 | `06_verification_report.md` 요약 |

## 에러 핸들링

| 상황 | 대응 |
|------|------|
| 진단가 보고서 누락 | 1회 재실행, 재실패 시 누락 표시하고 Phase B 진행 |
| 사용자가 eval 결과 데이터 제공 불가 | 코드 정적 분석으로만 진단 (정밀도 ↓, 보고서에 명시) |
| 구현 중 계획 가정 오류 | 즉시 중단·보고 → designer 재호출 |
| 검증 실패 | implementer 재호출 1회 한정 |

## 범위 가드

다음은 별도 하네스 영역 — 본 워크플로우에서 다루지 않음:
- `scripts/eval_search.py`, `scripts/build_synthetic_gold_set.py` 신뢰성·편향 수정 → `rag-eval-audit`/`rag-eval-fix`
- 그래프 추출 알고리즘 변경 (body_extractor, llm_body_extractor, link_graph_builder 등 추출 로직) → `indexing-improvement`

본 하네스는 **검색 funnel의 손실 진단·완화**가 핵심. 변경이 추출 로직에 닿으면 발견에 명시하고 그 하네스로 위임.

## 사용자 보고 형식

최종 보고:
- 진단된 funnel (단계별 손실 분포)
- R1 구현 N건 (간단 목록)
- 통과 테스트 / 회귀 위험
- 평가 메트릭 재측정 권고 (별도 하네스 호출)
- 다음 라운드 안내

## 테스트 시나리오

**정상 흐름**:
1. 사용자: "graph_recall이 5%인데 진단해줘"
2. Phase A: 3 진단가 병렬 → 인덱스 비어있지 않음, 검색 시드 매칭 실패율 90%, 메트릭은 정상
3. Phase B: R1 = 시드 매칭 정규화 보강 + 임계값 조정
4. Phase C: 구현
5. Phase D: PASS → 사용자에게 보고

**에러 흐름**:
- 사용자가 평가 row 데이터 제공 불가 → 정적 분석만으로 진행 (보고서에 한계 명시)
