# R4 Round Scope — Confluence 추출 품질 회복

## 배경

사내 테스트(노드 6000 / 엣지 8500) 에서 그래프 데이터 가치가 체감되지 않는다는 보고. 진단 결과 그래프 인프라(저장·검색·평가)는 R2/R3 작업으로 정비 완료, 반면 **추출 측 critical 개선 4개가 설계만 완료·미적용 상태**로 누적되어 데이터 자체가 부족·왜곡됨.

사용자 합의된 우선순위 (AskUserQuestion 답변):
- 트랙: **추출 품질 회복 (Track 1)**
- 소스: **Confluence 우선** (Git Code 는 별도 트랙)
- 브랜치: **`claude/indexing-improvement-r4` (origin/main 603ad9d 기준 신규)**

상위 plan: `/root/.claude/plans/pr66-snazzy-kitten.md`

## R4 스코프 — 미적용 4개 패치

이전 라운드(R1/R2/R3) 의 `_workspace/indexing-improvement{,-r3}/` 분석에서 식별된 미적용 발견 중, **Confluence 그래프 추출 측 4개** 만 본 라운드에서 적용.

| ID | 발견 | 영향 | 우선순위 |
|---|---|---|---|
| **F-CG-04** | `llm_body_extractor.py` 의 unit-scoped `valid_entity_names` 검증이 cross-section 관계 30~60% 드롭 추정 | Critical | 🔴 즉시 |
| **F-CG2-08** | LLM 표기 변형(`depending_on`/`part_of` 등) 으로 알맞은 관계가 vocab 검증에서 탈락 | Medium | 🟡 |
| **F-CG2-02/04** | 거대 문서(>16K 입력 토큰)에서 JSON 출력 잘림으로 문서 전체 그래프 손실 | High | 🟠 |
| **F-CG2-06** | 같은 엔티티 표기 변형(`AuthService`/`Auth Service`/`auth-service`) dedup 부족 | Medium | 🟡 |

## 코드 앵커

- `src/context_loop/processor/llm_body_extractor.py:216` (legacy unit 경로 — `unit_valid_entity_names`)
- `src/context_loop/processor/llm_body_extractor.py:232, 246-247` (검증 지점)
- `src/context_loop/processor/llm_body_extractor.py:285-421` (`extract_graph_data_from_document` — 문서 단위 경로)
- `src/context_loop/processor/llm_body_extractor.py:522` (`_canonical_name`)
- `src/context_loop/processor/graph_vocabulary.py:38-99` (ENTITY/RELATION TYPES 정의)

## 스코프 *밖* (이번 라운드 비변경)

- Git Code (AST 상속·호출 추출) — 별도 트랙
- cross-doc 씨앗 의미 재정의 — 별도 결정 필요
- RAG 통합 강화 (graph-direct answer / entity rerank weight)
- 검색측 (양방향 BFS, R3 priority ordering) — 이미 적용 완료
- 청크 사이즈 정책 (R3 옵션 B) — 별개 트랙
- `scripts/eval_search.py`, `scripts/build_synthetic_gold_set.py`, `src/context_loop/eval/*` — 별도 하네스 영역

## 산출물 (5-파일 R4 구조)

본 라운드는 스코프가 좁아 표준 7-파일 (`01..07_*.md`) 대신 5-파일 압축 구조 사용:

- [x] `00_round_scope.md` — 본 문서
- [ ] `01_analysis.md` — 4개 패치의 현재 코드 영향 범위 + 회귀 위험 (confluence-graph-analyst)
- [ ] `02_design.md` — 각 패치의 구체적 코드 변경 설계 (indexing-improvement-designer)
- [ ] `03_implementation.md` — 변경 요약 (indexing-improvement-implementer)
- [ ] `04_verification.md` — pytest 통과 + 그래프 통계 diff (indexing-change-verifier)

## 검증 요구사항

1. F-CG-04 회복 단위 테스트 — unit A 의 엔티티가 unit B 관계의 source/target 으로 등장하는 fixture 로 보존 검증
2. alias 정규화 단위 테스트 — `depending_on`→`depends_on` 등 명백한 변형만 보수적으로
3. canonical name dedup 단위 테스트 — `AuthService`/`Auth Service` 단일 노드 수렴
4. 큰 문서 폴백 라우팅 단위 테스트 — 16K 임계값 경계 확인
5. `pytest src/context_loop/processor/` 전체 회귀 통과

## 운영 가드

- **커밋·푸시는 verifier 단계 PASS 후 사용자 확인 받고**. 자동 푸시 금지.
- alias 매핑 표는 보수적으로 — 의도 외 정규화 방지. 명백한 표기 변형만 등록.
- F-CG2-02/04 폴백 임계값은 기본 16K, configurable 하게 (`LLMBodyExtractionConfig` 신규 필드).
- 기존 테스트 (R3 까지 작성된) 회귀 없음 보장.
