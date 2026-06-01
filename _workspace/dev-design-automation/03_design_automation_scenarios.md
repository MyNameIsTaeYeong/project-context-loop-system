# 03. 설계(아키텍처) 업무 자동화 시나리오

> 기준: `origin/main`. 구조: ① 대상 ② building block ③ gap ④ 워크플로우 ⑤ 산출물 ⑥ 난이도 + **그래프품질 의존도**(낮음/중간/높음) + 기대효과.
> 핵심 현실성: **git_code AST 그래프(`imports`/`contains`/FQN)는 결정론적·고신뢰**(D-036)이므로 의존도 "낮음" 시나리오는 즉시 신뢰 가능. 반면 **문서 LLM 의미 그래프 + 엔티티 alignment에 의존하는 시나리오**는 현 성숙도에서 검수 보조로만 신뢰(graph-search-diagnosis·merge-quality 하네스가 다뤄온 품질 이슈).

---

## S-1. 의존성 분석 & 레이어 위반 탐지 [즉시 PoC / 의존도 낮음]
- **대상:** 모듈/패키지 의존 맵, 순환 의존, 레이어 위반(상위→하위 역방향 import), 외부 의존 인벤토리
- **building block:** `ast_code_extractor` `imports`/`import_symbols` → `graph_store.graph`(NetworkX DiGraph) → `nx.simple_cycles`, 도달성/degree 분석
- **gap:** 레이어 규칙 정의(설정), 리포트 생성기. 결정론 그래프 위 분석만 얹으면 됨
- **워크플로우:** 그래프 로드 → 의존 매트릭스/사이클/위반 산출 → 리포트
- **산출물:** 의존 매트릭스 + 순환/위반 목록 + 외부 의존 인벤토리
- **기대효과:** 아키텍처 건강도 객관화. **가장 빠른 가치, 품질 리스크 거의 없음**

## S-2. 영향도 분석(Impact Analysis) [중간 / 의존도 낮음~중간]
- **대상:** "이 함수/모듈을 바꾸면 무엇이 영향받나" — 변경 blast radius
- **building block:** `get_graph_context(entity, depth)`, `graph.predecessors()` 역방향 import 탐색, `get_neighbors_from_node_id`. MCP로 Claude Code가 직접 질의 가능
- **gap:** **import 도달성 버전은 즉시 PoC**. 정밀 함수 단위 blast radius는 **call 그래프 부재가 한계**(현재 import/contains만 → 호출 관계 미추적). `ast.Call` 분석 추가 시 정밀도 급상승
- **워크플로우:** 변경 심볼/모듈 → 역방향 그래프 탐색 → 영향 파일·심볼 집합 → 영향도 리포트
- **산출물:** 영향 반경 목록(파일/심볼) + 그래프 경로
- **기대효과:** 변경 리스크 사전 가시화, 리뷰/QA 범위 산정

## S-3. 아키텍처 다이어그램 자동 생성 [중간 / 의존도 낮음]
- **대상:** 컴포넌트 다이어그램, 의존 그래프, 엔티티 관계도
- **building block:** `web/api/graph.py`(`/api/graph/full`·`explore`) + vis.js(`/graph` 페이지 이미 구현). 노드/엣지 payload 존재
- **gap:** **mermaid/PlantUML export 엔드포인트**(문서 임베드용), 모듈 수준 집계 뷰(현재 심볼 단위가 많아 추상화 레벨 조정 필요)
- **워크플로우:** 그래프 → 추상화 레벨 선택(모듈/심볼) → vis.js 렌더 or mermaid 텍스트 export
- **산출물:** 인터랙티브 그래프(/graph) + 문서 임베드용 mermaid
- **기대효과:** 설계 문서 다이어그램 수작업 제거, 항상 최신

## S-4. 아키텍처 문서 자동 업데이트 [대형 / 의존도 중간]
- **대상:** 코드 변경 시 영향받은 모듈 설계 문서 섹션만 자동 갱신
- **building block:** `reprocessor.py`(content_hash 변경감지), git_sync 재인덱싱, code_doc↔git_code `document_sources` 연결
- **gap:** **그래프 스냅샷/diff 인프라**(이전 버전 그래프 보존·비교)가 없음. 변경 섹션 매핑 + 문서 패치 로직 필요
- **워크플로우:** git_sync → 재인덱싱 → 그래프 diff → 영향 문서 섹션 식별 → 해당 섹션만 재생성 → 검수 큐
- **산출물:** 갱신된 설계 문서 + 변경 요약(diff)
- **기대효과:** 문서-코드 drift 최소화. 다만 스냅샷 인프라 신규로 비용 큼

## S-5. 설계 일관성/표류(drift) 검증 [대형 / 의존도 높음 ⚠]
- **대상:** 설계 문서(confluence)의 의도된 구조 vs 실제 코드 그래프 구조 대조 → 불일치 리포트
- **building block:** 문서 LLM 그래프(`graph_extractor`/`link_graph_builder`) ↔ 코드 AST 그래프 교차, `entity_normalizer`(엔티티 alignment)
- **gap:** **문서 그래프 품질 + 엔티티 정렬 정확도에 강하게 의존** — 현 성숙도에선 false positive 위험. 두 그래프의 엔티티 매핑 신뢰도가 관건
- **워크플로우:** 문서 그래프 추출 → 코드 그래프와 엔티티 정렬 → 누락/추가/모순 관계 식별 → 리포트
- **산출물:** "설계 vs 구현 불일치" 후보 목록(검수 필요)
- **기대효과:** 설계 표류 조기 발견. **단, 현재는 검수 보조 도구로만 신뢰** — 자동 게이트 부적합

## S-6. 도메인 모델/용어 사전 자동 구축 [중간 / 의존도 중간]
- **대상:** 사내 엔티티/용어 통합 사전(정의 + 출처 + 동의어)
- **building block:** `entity_normalizer`, `graph_vocabulary`, `graph_merge_log`(병합 이력), `graph_node_documents`(엔티티 출처)
- **gap:** 사전 뷰/정의 생성. 병합 품질에 의존(merge-quality 하네스 영역)
- **워크플로우:** 엔티티 수집 → 정규화/병합 → 출처·정의 LLM 요약 → 용어집
- **산출물:** 도메인 용어 사전(엔티티 | 정의 | 출처문서 | 병합변형)
- **기대효과:** 용어 통일, 신규자/협업 커뮤니케이션 비용↓

## S-7. 신규 기능 설계 컨텍스트 자동 조립 [즉시 PoC / 의존도 낮음~중간]
- **대상:** 새 기능 기획 시 관련 기존 모듈·문서·결정사항을 자동 수집해 설계 시작점 제공
- **building block:** `search_context`(기능 설명→관련 코드/문서) + `get_graph_context`(인접 모듈)
- **gap:** 거의 없음(MCP 질의 + 템플릿). 기획 입력 → 컨텍스트 팩 생성
- **워크플로우:** 기능 설명 입력 → MCP 다중 질의 → 관련 모듈/문서/결정 수집 → "설계 시작 팩"
- **산출물:** 설계 컨텍스트 팩(관련 코드·문서·과거 결정·영향 모듈)
- **기대효과:** 설계 리서치 시간 단축, 기존 자산 재활용

## S-8. ADR(아키텍처 결정 기록) 보조 [중간 / 의존도 낮음]
- **대상:** `.context/decisions/DECISIONS.md`(D-번호) 패턴 연계 — 변경 시 영향받는 결정 추적, 신규 ADR 초안
- **building block:** decisions 문서 인덱싱(manual/upload source_type), `search_context`로 관련 결정 인출
- **gap:** **`.context/decisions` ADR 인덱싱 파이프라인**(현재 자동 인덱싱 대상 아님), 변경↔결정 매핑
- **워크플로우:** 변경 PR → 관련 결정 검색 → 영향/모순 결정 표시 → ADR 초안 제안
- **산출물:** 영향받는 ADR 목록 + 신규 ADR 초안
- **기대효과:** 결정 추적성, 결정-구현 정합

## S-9. 엔티티 통합 품질 모니터링 [즉시 PoC / 부분 구현됨]
- **대상:** 중복 엔티티·잘못된 병합 탐지, 통합 품질 지표
- **building block:** `/api/graph/merges`, `graph_merge_log`, `get_merged_node_groups` — **이미 graph-overview-merge-quality 하네스로 구축됨**
- **gap:** 정기 모니터링 잡 + 임계치 알림(대시보드엔 이미 노출)
- **산출물:** 통합 품질 리포트/대시보드 패널
- **기대효과:** 그래프 신뢰성 지속 관리 → 다른 설계 시나리오의 토대 강화

## S-10. 설계 안티패턴/리스크 스캐너 [중간 / 의존도 낮음]
- **대상:** god-module, 과결합(높은 fan-in/out), 고아 모듈, 불안정 의존(Martin 안정성 지표)
- **building block:** 순수 구조 지표 — `imports`/`contains` 카운트, degree 분포, `stats()`. 의미 해석 불필요 → AST 그래프만으로 신뢰
- **gap:** 지표 계산기 + 스코어링 룰
- **워크플로우:** 그래프 → 구조 지표 산출 → 임계치 위반 스코어링 → 리스크 리포트
- **산출물:** 모듈별 리스크 점수표 + 근거
- **기대효과:** 리팩터링/설계 개선 우선순위 객관화. D-4(기술부채)와 시너지

---

## 시나리오 요약 표

| # | 시나리오 | building block | 난이도 | 그래프품질 의존도 | 기대효과 |
|---|----------|---------------|--------|------------------|----------|
| S-1 | 의존성 분석/레이어 위반 | imports 그래프+NetworkX | 즉시 PoC | 낮음 | ★★★ |
| S-2 | 영향도 분석 | get_graph_context 역탐색 | 중간 | 낮음~중간 | ★★★ |
| S-3 | 아키텍처 다이어그램 | /api/graph + vis.js | 중간 | 낮음 | ★★ |
| S-4 | 아키텍처 문서 자동 업데이트 | reprocessor+그래프 diff | 대형 | 중간 | ★★ |
| S-5 | 설계 drift 검증 | 문서그래프↔코드그래프 | 대형 | **높음 ⚠** | ★★ (검수보조) |
| S-6 | 도메인 용어 사전 | entity_normalizer+vocab | 중간 | 중간 | ★★ |
| S-7 | 신규기능 설계 컨텍스트 | search+graph_context | 즉시 PoC | 낮음~중간 | ★★ |
| S-8 | ADR 보조 | decisions 인덱싱+search | 중간 | 낮음 | ★ |
| S-9 | 엔티티 통합 품질 모니터 | /api/graph/merges | 즉시 PoC(부분구현) | — | ★ (기반강화) |
| S-10 | 설계 안티패턴 스캐너 | 구조 지표 | 중간 | 낮음 | ★★ |

**가장 신뢰 가능한 top3(현 그래프 성숙도 기준):** S-1(의존성 분석) → S-2(영향도, import 버전) → S-10(안티패턴 스캐너). 모두 **결정론 AST 그래프**만 사용해 품질 리스크가 낮다.

> **공통 핵심 enabler:** `ast_code_extractor`에 `ast.Call` 분석을 추가한 **정밀 호출 그래프**. S-2·S-10과 개발 시나리오 D-2·D-7의 정밀도를 동시에 끌어올리는 단일 고레버리지 작업.
