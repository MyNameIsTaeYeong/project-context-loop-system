# PR #79 계획서 리뷰 — 통합 원천(source-grounded) 골드셋 생성

> 대상: `_workspace/source-grounded-goldset/00_plan.md` (PR #79, Draft, 코드 변경 없음).
> 리뷰 기준 코드: `origin/main` (= 이 리뷰 브랜치 베이스). 모든 주장은 실제 파일·라인·테스트
> 실행으로 검증했다.
> 결론(TL;DR): **계획서의 진단·설계 방향은 타당하고 코드 참조도 정확하다. 승인 가능하되,
> 구현 착수 전 아래 6개 보정점(C1–C6)을 §11 결정과 함께 반영할 것을 권고한다.**

---

## 1. 검증 요약 — 계획서 주장 ↔ 실제 코드

| 계획서 주장 | 검증 결과 | 증거 |
|---|---|---|
| `load_candidate_subgraphs`가 graph_store 노드(entity_name)를 읽음 | ✅ 정확 | `build_synthetic_gold_set.py:212`, `:246`, `:301` |
| `_make_graph_gold_item`이 `sg["entity_name"]`를 복붙(self-fitting) | ✅ 정확 | `build_synthetic_gold_set.py:1473`, `:1516`, `:1536` |
| `_make_cross_doc_gold_item` / `_run_chunk_mode` 존재 | ✅ 정확 | `:1571`, `:914` |
| GoldItem에 제안 신규 필드(reference_answer 등) 부재 | ✅ 정확 (추가만 하면 됨) | `gold_set.py:163-200` |
| `detect_role_collisions`가 **3-way**(gen/judge/system) | ✅ 정확 — "4중 확장"은 미구현 신규 작업 | `llm.py:267-313` |
| `sanitize_graph_aliases`/`sanitize_graph_evidence` 재사용 가능 | ✅ 존재 | `synth.py:648`, `:677` |
| `generate_graph_questions`/`filter_question` 재사용 가능 | ✅ 존재 | `synth.py:893`, `:1065` |
| R3 self-fitting이 CRITICAL로 확정됨 | ✅ 정확 | `_workspace/findings/SUMMARY.md` R3 |
| §8 "원문/섹션 조회 API" 활용 가능 | ✅ 실재 | `metadata_store.py`: `get_document:298`, `list_documents:304`, `original_content` 컬럼 `:24` |

**판정**: 계획서는 환각 없이 실제 코드/감사 결과에 정확히 정초해 있다. 핵심 발상(인덱스→골드
방향을 원문→골드로 뒤집어 evidence_span에 진실 고정, 한 사실이 doc/answer/graph 3단위 동시
서빙)은 R3·청크 음의공간 부재 문제에 대한 **올바른 처방**이다.

## 2. 강점

- **토대 재사용이 명확**: PR #78의 surface tier·그래프 CI·`detect_role_collisions`·`sanitize_*`를
  대체하지 않고 그 위에 얹는다고 명시 — 회귀 위험 최소화.
- **플래그(`--source-grounded`) 뒤 격리**: 기존 상대 A/B 경로를 깨지 않는 증분 전략. P0–P6 단계화도
  저위험.
- **두/세 축 분리(§6.4 divergence 리포트)**: "맞지만 근거 없음(파라메트릭)" vs "대체 유효 출처"를
  구분해 한 숫자로 뭉개지 않는 설계는 RAG 평가의 흔한 함정을 정확히 피한다.
- **잠정→절대 승격 + 인간 앵커(§7)**: 합성 골드의 절대값을 곧장 신뢰하지 않고 라벨링·CI·생성기
  정밀도와 함께 보고하는 규율이 적절하다.

## 3. 보정점 (구현 착수 전 반영 권고)

### C1 — "기존 실패 테스트" 베이스라인이 부정확 (회귀 게이트에 직접 영향)
계획서 §10은 *"origin/main에 기존 실패 5건(`test_fetch_source_text_*`, `test_filter_question_*`)"*
이라며 "신규 실패 0만 확인"을 회귀 기준으로 삼는다. 실제 실행(`pytest tests/test_eval/`) 결과
**정확히 5건 실패**가 맞으나, 집합이 나열 패턴과 다르다:

```
FAILED test_build_synthetic_gold_set.py::test_fetch_source_text_anchor_match
FAILED test_build_synthetic_gold_set.py::test_fetch_source_text_legacy_chunk_id_fallback
FAILED test_build_synthetic_gold_set.py::test_make_graph_gold_item_falls_back_to_node_description   # ← 패턴에 없음
FAILED test_synth.py::test_filter_question_passes_clean
FAILED test_synth.py::test_filter_question_fails_generic
```
`test_filter_question_*`은 8개 중 2개만 실패하고, 패턴에 없는 `test_make_graph_gold_item_...`이
포함된다. 패턴 기반으로 베이스라인을 잡으면 신규 회귀를 기존 실패로 오인하거나 그 반대가 될 수
있다. → **권고: 정확한 노드 ID 5개를 known-fail 목록으로 고정**(또는 `xfail` 표식)하고 그 목록
대비 delta=0을 게이트로 삼을 것.

### C2 — §4 스키마 의사코드의 dataclass mutable-default 버그
계획서 §4는 다음처럼 적었다:
```python
supporting_facts: list[SupportingFact] = []   # ← 런타임 에러
provenance: dict = {}                          # ← 런타임 에러
acceptable_surface_forms: list[str] = []       # ← 런타임 에러
```
`@dataclass`에서 가변 기본값은 `ValueError: mutable default ... use default_factory`로 막힌다.
기존 `GoldItem`은 전부 `field(default_factory=list)`를 쓰고 있다(`gold_set.py:181-190`). →
**권고: 신규 필드도 `field(default_factory=list/dict)`로 작성**. 사소하지만 P0에서 그대로 복붙하면
즉시 깨진다.

### C3 — evidence_span substring 검증은 "관계 환각"을 못 막는다
§5[4](c)·§10은 evidence_span이 원문에 substring으로 존재하는지만 검증한다. 그러나
`SupportingFact{entity, relation, target}`에서 **entity·target이 둘 다 원문에 있어도 relation은
추출 LLM이 지어낼 수 있다**(예: 원문은 "A와 B를 함께 언급"인데 추출기가 `A --depends_on--> B`로
단정). substring 검증은 이를 통과시킨다. → **권고: Judge 게이트에 "relation이 evidence_span에서
함의(entailment)되는가"를 NLI/분리 judge로 추가**(§11-8과 연결). 그래프 단위 절대성의 핵심.

### C4 — `detect_role_collisions` 4중 확장 시 "임베딩"은 현재 비교 차원에 아예 없음
계획서는 "추출LLM·임베딩까지 4중 확장"이라 했으나, 현재 `detect_role_collisions`는 (endpoint,
model) 페어만 비교하고 **임베딩 모델은 식별 대상이 아니다**(S1-4는 임베딩 ID를 *기록*만 하고
assert 안 함, SUMMARY R4). → **권고: (a) 추출 LLM을 4번째 역할로 페어 비교에 추가, (b) 임베딩은
별도 차원으로 "골드 evidence 임베딩 = 검색 인덱스 임베딩 = T4 채점 임베딩" 동일성 경고를
명시적 신규 체크로 분리**. "4중"이라는 한 단어가 두 종류의 작업(LLM 페어 + 임베딩 순환)을 가리는
것을 풀어둘 것.

### C5 — "원문 섹션" 샘플링이 평가 대상 인덱스에 재결합될 위험
§5[1]은 `documents.original_content`를 직접 샘플링한다(좋음, 인덱스 독립). 하지만 §8은 "섹션"
샘플링도 언급하는데, **섹션 경계(`section_path`/`section_index`)는 documents가 아니라 chunks
테이블에 산다**(`metadata_store.py:43-46`, `:391-415`). 섹션 단위로 자르면 *평가 대상 청킹 산출물*에
다시 의존하게 되어 음의공간 부재가 부분 재발한다. → **권고: 사실 추출은 document.original_content
원문 위에서 수행하고, 섹션은 (필요 시) 청킹과 독립한 자체 분할기로 자를 것. evidence_span의 출처
문서 id만 정답키로 쓰고 chunk_id는 절대 정답키로 승격 금지**(이미 `source_chunk_id`가
DEPRECATED·채점 금지로 표시돼 있음 — `gold_set.py:198-200` — 이 규율을 신규 경로에도 유지).

### C6 — answer judge가 4중 분리에 포함돼야 함
§6.2 answer correctness의 개방형 채점은 judge LLM을 재도입한다. 이 judge가 생성 시
`filter_question`의 judge나 시스템 LLM과 같으면 새로운 self-eval 채널이 생긴다. → **권고: answer
judge도 C4의 충돌 검사 대상에 포함**하고, provenance에 별도 ID로 기록.

## 4. §11 미결정 사항 — 리뷰어 권고 (구현 세션이 사용자와 확정)

| # | 결정 | 권고 | 근거 |
|---|---|---|---|
| 1 | 단위 우선순위 | **doc → graph → answer** 순 증분 | doc은 기존 chunk recall·`relevant_doc_groups`(OR-group) 재사용으로 최소 신규 코드. answer는 judge 의존이라 가장 위험 → 마지막 |
| 2 | 모델 분리 | 추출·generator·judge·answer-judge 4역할 + 임베딩 모두 상이하게. C4 충돌 검사로 강제 | R3/R4/R9 재발 차단 |
| 3 | 인간 검증 규모 | 우선 20–50건 소규모를 **P4 채점 신뢰 전에** 확보(생성기 정밀도 캘리브레이션) | §7 "절대"는 인간 앵커 없이는 성립 불가. P5로 미루면 P4 숫자가 근거 없이 인용될 위험 |
| 4 | 소스 범위 | **confluence_mcp 1종부터**(산문 풍부 → 사실/evidence_span 추출 용이) → git_code 확장 | 위험 점진. 단 브리지 측정(S0-3)엔 결국 양쪽 필요 |
| 5 | 매칭 방식 | 기존 `graph_match` tier + chunk recall **재사용**, 사실 전용 매처 신설 보류 | PR #78 토대 최대 활용, 표면 변동 최소화 |
| 6 | 답변 채점 | 팩토이드 정규화 일치부터, 개방형 judge는 플래그 뒤 | 결정론 우선, judge 편향 격리 |
| 7 | 불가능 골드 | `answerable=False`를 **별도 "인덱싱 표적" 버킷**으로(무성 분모 제외 금지) | 인덱싱 누락 가시화가 본 작업의 목적 |
| 8 | faithfulness(축 C) | 분리 judge/NLI로 측정하되, divergence는 **우선 경고**로 노출(메트릭 합산 보류) | §6.4 설계 유지, 조기 과적합 방지 |
| 9 | 문서 OR-group | 초기엔 evidence_span 출처 단일 id + 명시적 한계 라벨. 자동 등가 확장은 보류 | §6.1이 이미 "하한(proxy)"으로 정직하게 표기 — 그대로 |

## 5. 범위·프로세스 코멘트

- 본 PR은 **계획서만**(239줄, 1파일) 담은 Draft로, 코드 변경이 없다. 머지 가능 상태(`mergeable_state: clean`)이며
  리뷰 시점에 리뷰/코멘트 0건이었다.
- 구현은 계획서 명시대로 **별도 세션 + `claude/source-grounded-goldset` 브랜치**에서 P0부터 증분
  진행이 적절하다. P0–P6 전체는 6개 파일에 걸친 다중 세션 규모이므로, **C1–C6를 P0 착수 조건으로**
  반영하면 첫 단계부터 깨지지 않는다.
- 플래그 격리 덕에 기존 상대 A/B 경로는 무영향 — 이 점은 머지 리스크를 크게 낮춘다.

## 6. 최종 권고

**계획서 자체는 ACCEPT**(방향·진단·코드 정초 모두 견고). 단, 구현 세션은 착수 전:
1. C1 정확한 known-fail 5개 노드 ID 고정,
2. C2 dataclass `default_factory` 수정,
3. C3 relation entailment 검증 추가,
4. C4 4중 분리에서 LLM 페어 vs 임베딩 순환을 분리 명세,
5. C5 섹션 샘플링의 인덱스 재결합 차단(문서 원문 기준 유지),
6. C6 answer judge를 충돌 검사에 포함

을 §11 결정(특히 #1 단위 우선순위, #2 모델 분리, #3 인간 검증 규모)과 함께 확정할 것.
