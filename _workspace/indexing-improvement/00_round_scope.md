# 라운드 범위 — 그래프 엔티티/관계 타입 정의 적합성 검토 (2026-07-13)

## 사용자 요청

> 현재 그래프 컨텍스트 추출 과정에서 제한하고 있는 엔티티와 관계에 대한 정의가
> 적합한지 검토해주세요

## 스코프

- **검토 대상**: `src/context_loop/processor/graph_vocabulary.py` (어휘 SSOT)
  및 이를 사용/제한하는 추출기·검색 경로
  - confluence_mcp: `link_graph_builder.py`, `body_extractor.py`,
    `llm_body_extractor.py`
  - git_code: `ast_code_extractor.py`
  - 검색 측 소비: `graph_search.py` (플래너, INTENT_TO_RELATIONS)
- **이번 라운드는 검토(분석)만** — 구현은 사용자 승인 후 별도 진행.
- 청킹 측면은 스코프 외 → 분석가 01/02(청킹) 미실행. 그래프 분석가 2명만 실행.

## 검토 관점

1. 정의-구현 정합성 (vocab source 태그 vs 추출기 실제 방출 타입)
2. 커버리지 (어휘로 표현 못 하는 중요 엔티티/관계)
3. 타입 경계 모호성 (system/module/concept, mentions/references,
   depends_on/uses/calls, 노드 병합 키 충돌)
4. 검색 측 정렬 (INTENT_TO_RELATIONS 적합성, subset/union 노출 설계)
5. 실데이터 분포 (dead vocab / 과밀 타입)

## 이전 라운드

- `_workspace/indexing-improvement_prev_2026-07-13_ctx128k/`: 128K 컨텍스트
  모델 대응 (max_input_tokens config 노출 등) — 본 라운드와 독립.
