# 종합 검토 결론 — 그래프 엔티티/관계 타입 정의 적합성 (2026-07-13)

> 입력: `03_confluence_graph_findings.md` (F-CG-01~09), `04_git_code_graph_findings.md` (F-GG-12~23)
> 이 문서는 두 분석 보고서의 종합 판정 + 개선 우선순위 제언. 코드 변경 없음.

## 종합 판정

**"정의 자체의 내부 정합성은 견고하나, 어휘가 (1) 검색에서 소비되지 않고
(2) 실데이터가 요구하는 타입을 담지 못하며 (3) 경계 모호로 노드 병합이 깨진다"**
— 즉 형식적으로는 적합하지만 실효 관점에서는 **부분 부적합**.

### 잘 되어 있는 것

- `graph_vocabulary.py` 를 SSOT로 두고 추출기 방출 타입 ⊆ vocab 을 테스트로
  강제하는 구조 (F-GG-12: ast_code 축 정확 일치, F-CG 정합성 대조표: llm_body
  subset `==` 강제).
- llm_body strict 필터 (어휘 외 타입 드롭)로 그래프 오염 방지.

### 부적합 축 3가지

#### 축 1 — 검색 측 미소비 (F-CG-01 + F-GG-21, HIGH×2)

`INTENT_TO_RELATIONS` / `format_*_for_prompt` / `all_*_names` 는 제거된
`graph_search_planner` 의 유산으로 **프로덕션 참조 0건** (테스트만 참조).
현 검색(`graph_search.py`)은 순수 임베딩 시딩 + 1-hop 확장이라 entity_type /
relation_type 을 랭킹·필터에 전혀 쓰지 않는다. docstring의 "검색 union" 서술은
실체가 없다. → 어휘 description 을 아무리 다듬어도 검색 품질에 효과 0.
부수: 시드 임베딩이 엔티티 이름(FQN)만 벡터화(F-GG-22)라 어휘 개선 이득이
검색에 전달되지 않는 병목이 별도로 존재.

#### 축 2 — 커버리지 공백 (F-CG-02 + F-GG-14/15/16/17, HIGH×3)

- **confluence**: 이벤트/토픽 관계(`publishes_to`/`consumes_from`) 부재 —
  실측 DB에서 LLM이 자연 방출했으나 현재 어휘로는 전량 드롭. 데이터스토어,
  결정사항, 마일스톤, 환경/배포, 외부 URL(F-CG-05)도 미표현.
- **git_code**: `calls` 가 llm_body 전용 태깅 + AST가 호출을 추출 안 함 →
  **코드 call-graph 구조적 0건**. 상속/구현 엣지 부재(brace 언어 extends /
  implements 는 예약어로 폐기). `defined_in` 부재로 top-level 심볼이 고립 섬.
  enum/type alias/const/decorator 사각지대.
- git_code relation 은 imports/contains 2종에 100% 집중 예측 —
  `calls`/`implements` 등 상당수 어휘가 code source 기준 dead.

#### 축 3 — 경계 모호·병합 실패 (F-CG-03 + F-GG-13/19, HIGH×2)

- `system` vs `module` 판정 기준("외부에서 보이는" vs "내부")이 문서 관점
  의존 → 실측에서 LLM이 동의어 `service`(6)/`component`(5)를 선택. 현재
  strict 어휘에선 이들이 전량 드롭되고, 통과하더라도 병합 키
  `(정규화이름, entity_type)` 특성상 타입이 갈리면 노드 분열.
- `module` 과부하: AST 파일/import/docstring/constants/fallback 5종 +
  LLM 추상 컴포넌트가 한 타입 공유. AST 이름(`user_service.py`) vs LLM
  자연어(`Token Validator`)라 의도한 코드↔문서 수렴이 실제로는 안 일어남.

### 구조적 원인 — 관측·검증 인프라 부재

- 드롭 통계가 개수만 기록, 타입명 미기록 (F-CG-08) → 어휘 공백을 데이터로
  발견 불가. system↔service 동의어 선택도 관측 불가.
- 드리프트 테스트가 "subset ⊆ vocab" 단방향 + 하드코딩 기대값 (F-CG-07) →
  죽은 vocab 항목 누적, 역방향 표류 미탐.
- 동의어/오타 정규화 부재 (F-CG-09) → 완전일치 실패는 전부 드롭.

## 개선 우선순위 제언 (구현은 사용자 승인 후)

| 순위 | 항목 | 근거 발견 | 성격 |
|---|---|---|---|
| P1 | 드롭 타입명 계측(`Counter`) + 동의어 정규화(`service→system` 등 aliases) | F-CG-08/09, F-CG-02/03 | 저비용, 드롭 회수 + 확장 근거 확보 |
| P2 | 어휘 확장: `publishes_to`/`consumes_from`, `datastore`; `calls`/`implements` ast 승격 + 상속(`inherits`) 신설 + `contains`(파일→심볼) 연결 | F-CG-02, F-GG-14/15/16 | 커버리지 — call-graph/이벤트 그래프 신설 |
| P3 | `system`/`module` 판정 기준 명문화 + few-shot; 코드 파일 전용 타입(`code_file`) 또는 property 분리 | F-CG-03, F-GG-13/19 | 경계 정예화, 병합 파손 방지 |
| P4 | dead vocab 정직화: `INTENT_TO_RELATIONS`/`format_*` 제거 vs 검색 재연결 **결정 필요** (+ 시드 임베딩을 name+sig+docstring 으로 확장은 검색측 별도 작업) | F-CG-01, F-GG-21/22 | 설계 결정 — 사용자 선택 필요 |
| P5 | 드리프트 테스트 양방향화(역방향: vocab 모든 항목이 최소 1개 추출기에서 방출) | F-CG-07 | 검증 인프라 |

- P4는 방향 결정(제거 vs 재연결)이 필요한 설계 분기 — 나머지는 독립 진행 가능.
- 착수 전 1회성으로 실제 인덱스에서 타입 분포 집계(F-GG-23 의 방법) 권장.
