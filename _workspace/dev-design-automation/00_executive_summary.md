# 00. 개발·설계 업무 자동화 검토 — 종합 요약

> 목적: project-context-loop-system을 활용한 **실제 개발/설계 업무 자동화** 가능성 검토.
> 기준: `origin/main` (7e7bbe0, 로컬 브랜치보다 14커밋 앞섬 → 워크트리로 정확 분석).
> 범위: **검토/분석 전용**(구현 없음). 작성일 2026-06-01.

## 한눈에 보기

이 시스템은 이미 **자동화 플랫폼의 핵심 building block을 대부분 갖춘** RAG+지식그래프 시스템이다.
- ✅ **MCP 서버**(stdio/SSE, 4 tool)가 완성 → 사내 코드·문서 질의 통로가 즉시 사용 가능
- ✅ **git_code AST 인덱싱**이 LLM 없이 코드 심볼·import·contains 그래프를 **결정론적·고신뢰**로 추출(D-036)
- ✅ **정교한 검색 조립기**(멀티뷰 임베딩 + LLM 그래프 플래너 + 리랭커 + HyDE + 원본 소스 첨부)
- ✅ **웹 대시보드/REST API + /graph 시각화 + 평가 하네스**까지 운영 도구 완비
- ⚠ **CLI 미구현**(엔트리포인트 깨짐) → 자동화 트리거(CI/cron) 표준 공백
- ⚠ **호출(call) 그래프 부재** → import/contains만 추출, 정밀 영향분석의 한 단계 부족

**결론:** 자동화를 막는 것은 코드 역량이 아니라 ① 코퍼스 데이터, ② 트리거 표준 CLI, ③ 호출 그래프 한 단계다. 이 셋을 깔면 다수 시나리오가 수일~수주 내 PoC 가능하다.

## 5가지 검토 항목 → 문서 맵

| # | 항목 | 문서 |
|---|------|------|
| 1 | 기능/API 분석 (MCP·웹·CLI·파이프라인) | [`01_capability_analysis.md`](01_capability_analysis.md) |
| 2 | 개발 업무 자동화 시나리오 (10개) | [`02_dev_automation_scenarios.md`](02_dev_automation_scenarios.md) |
| 3 | 설계 업무 자동화 시나리오 (10개) | [`03_design_automation_scenarios.md`](03_design_automation_scenarios.md) |
| 4 | 시나리오별 타당성 평가 | [`04_feasibility_assessment.md`](04_feasibility_assessment.md) |
| 5 | 우선순위 매트릭스 (영향력×난이도) | [`05_priority_matrix.md`](05_priority_matrix.md) |

> 병렬 분석 에이전트 3인의 원본 산출물(교차검증용): `A_capability_inventory.md`, `B_dev_scenarios.md`, `C_design_scenarios.md`. 본 00~05 문서는 이를 종합·정렬한 결정판이다.

## 최우선 권고 (Top 5)

1. **E1 CLI 복원** — 모든 자동화의 배치/CI 트리거 전제. 작업량 작음(P0)
2. **E2 코퍼스 인덱싱** — 대상 레포·문서를 실제 인덱싱. 데이터 없으면 전부 무의미(P0)
3. **D-1 사내지식 Q&A / Claude Code MCP 연동** — MCP 완성, 즉시 가치, 모든 시나리오의 공통 인터페이스(P0)
4. **S-1 의존성/레이어 위반 분석** — 결정론 그래프 기반 고신뢰, 빠른 가치(P1)
5. **E3 호출(call) 그래프 추가** — D-2(코드리뷰)·S-2(영향도)·D-4/S-10(부채/안티패턴) 정밀도를 동시에 올리는 단일 고레버리지(P1)

## 단계적 로드맵 (요약)

- **Phase A (기반, ~1주):** E1 + E2 + D-1 → Claude Code가 사내 자산을 질의하는 환경
- **Phase B (결정론 분석, ~1~2주):** S-1, D-9, S-7 + E3 착수 → 품질 리스크 없는 구조 분석
- **Phase C (고효과 자동화, ~2~4주):** D-2, S-2, D-4/S-10, D-5 → PR 파이프라인 연동 자동화
- **Phase D (보류 재평가):** S-4, S-5 등은 그래프 diff 인프라·그래프 품질 성숙 후

## 현실성 경고 (솔직한 한계)

- **문서 LLM 의미 그래프**에 의존하는 시나리오(S-5 설계 drift, S-6 용어사전 일부)는 현 그래프 성숙도에서 **자동 게이트로 부적합** — graph-search-diagnosis·merge-quality 하네스가 다뤄온 품질 이슈 때문에 **검수 보조 도구**로만 신뢰.
- 반면 **git_code AST 그래프**(`imports`/`contains`/FQN) 기반 시나리오는 결정론적이라 **지금 바로 신뢰 가능**.
- 따라서 초기 자동화는 **코드 구조 기반 시나리오에 집중**하고, 문서 의미 기반은 품질 개선과 병행해 점진 확대하는 것이 안전하다.
