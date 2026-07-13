# 06 · 검색 경로 ↔ graph_vocabulary 정렬 개선 방안 옵션 (설계 검토 전용 · 코드 수정 없음)

- 라운드: 2026-07-13 · graph-search-improvement-designer
- 해소 대상 발견: **"검색 경로가 graph_vocabulary 를 소비하지 않음"**
  (F-CG-04 / F-GG-06, 근거 `03_confluence_graph_findings.md` · `04_git_code_graph_findings.md`, 종합 `05_vocab_review_summary.md`)
- 입력 추가 확인: `graph_search.py`, `graph_store.py`(API·비용), `graph-search-diagnosis{,_r1,_r2}`(플래너 제거 이력), 소비처(`context_assembler.py` → `tools.py`)
- **본 문서는 옵션 설계·비교만. 코드·평가 하네스 변경 없음.**

---

## 0. 전제 사실 확정 (설계 근거)

옵션 평가에 직접 물리는 사실을 먼저 코드로 확정한다.

### 0.1 소비처 추적 — 그래프 결과 text 가 흘러가는 곳

```
search_graph()  (graph_search.py:53)
  → GraphSearchResult.text                     (_format_text, :157)
  → context_assembler.assemble_context :151     sections.append(graph_result.text)
  → "\n\n---\n\n".join(sections) = context_text (:184)
  → mcp/tools.py:48  search_context() 이 그대로 return (str)
  → MCP 클라이언트(외부 답변 LLM = Claude)가 tool 결과로 수신
```

**핵심**: 이 코드베이스에는 그래프 text 를 받아 합성하는 **내부 답변 LLM 이 없다.**
`search_context` 는 조립된 컨텍스트 문자열을 MCP tool 결과로 **그대로 반환**하고,
해석·답변은 외부 LLM 이 한다. 따라서 "옵션 D(프롬프트 측 정렬)"에서 우리가
제어할 수 있는 표면은 **`graph_result.text` 문자열 자체**(= `_format_text` 출력)뿐이며,
답변 LLM 시스템 프롬프트를 직접 고치는 경로는 이 리포지토리 범위에 없다.

또한 `graph_result.entities` / `.relations` 는 `context_assembler.py:730-731` 에서
`retrieved_entities`/`retrieved_relations` 로 뽑혀 **평가(eval) 채점에만** 쓰인다
(운영 답변 텍스트에는 안 들어감). 즉 vocab 정렬이 실제 **운영 답변 품질**에
닿는 유일한 통로는 `text` 이고, **eval 메트릭**에 닿는 통로는 `entities/relations`
이다 — 두 소비 경로를 옵션별로 구분해 평가해야 한다.

### 0.2 graph_store API — 필터 지원 여부와 추가 비용

모두 **in-memory NetworkX DiGraph**. DB 왕복·인덱스 재빌드 비용 없음.

| API | relation/type 필터 | 현재 비용 | 필터/부스팅 추가 비용 |
|-----|-------------------|-----------|----------------------|
| `search_entities_by_embedding` (:1027) | **없음**. 단 결과에 `entity_type` 포함(:1051) | 이미 전 노드 O(N) cosine 선형 스캔 | entity_type 화이트리스트 in-loop 비교 = 무시 가능 |
| `get_neighbors_from_node_id` (:618) → `_bidirectional_bfs` (:399) | **없음**. successors/predecessors 만, edge data 미참조 | O(방문 노드+엣지) | relation 필터하려면 BFS 를 edge-data 순회로 교체 필요(소규모) |
| `get_edges_between` (:642) | **없음**, 그러나 `relation_type` 을 이미 반환(:653) | O(전체 엣지) 1회 | 결과 조립 단계 relation 가중치 = 무시 가능(이미 순회 중) |

결론: **relation 부스팅/정렬(옵션 B의 결과-정렬 부분)은 거의 공짜** —
`get_edges_between` 이 이미 `relation_type` 를 손에 쥔 채 순회한다.
**relation 필터링(BFS 확장 단계에서 엣지 타입으로 이웃 가지치기)** 만
`_bidirectional_bfs` 를 edge-aware 로 바꾸는 소규모 작업이 든다.

### 0.3 플래너 제거는 왜 일어났나 (옵션 C 의 정합성 판단 근거)

커밋 `ff731ac "Simplify graph search: replace LLM planner with embedding-seeded search"`.
진단 이력을 시간순으로 읽으면:

- **R1**: 플래너(`plan_graph_search`)가 있고, `get_neighbors` 표면매칭 실패 시
  임베딩 fallback 추가. → 메트릭 여전히 < 10%.
- **R2**: 진범은 방향성(sink 시드에서 outgoing 0, 88.9% miss). 해결로
  (a) 양방향 BFS, (b) **query_embedding 기반 always-on 시드 보강**(플래너
  결과가 있어도 항상 임베딩 top-k union). → 이 시점에 플래너의 "시드 선택"
  기여가 임베딩 시딩에 **흡수**됨.
- **R3(계획)**: 플래너 출력 schema 를 인덱싱과 정렬(target_entities/relations
  + priority ordering)해 MRR 회복 시도.
- **이후 제거**: `graph_search.py:9-13` docstring 이 명시 — "3층 패치(시드 이름
  임베딩 fallback / always-on 보강 / 최후 폴백)가 **전부 임베딩 검색으로 수렴**했고,
  그 수렴점 하나만 남겼다."

**결정적 관찰 2가지**:
1. 제거 사유는 "플래너가 **오작동**"이 아니라 **중복·수렴 + 결정성/비용 단순화**다.
   R2 의 always-on 임베딩 보강이 플래너의 유효 기여를 이미 삼켜서, 플래너는
   LLM 호출·비결정성만 남기고 유효 seed 집합을 바꾸지 못하는 상태가 됐다.
2. 진단 리포트의 개선 효과(graph_recall 30~50%, MRR 0.3~0.5)는 **전부 "예상"**
   이며 **eval 로 실측된 바 없다**(각 06_verification 은 "별도 평가 실행 필요"로
   종료). 즉 플래너의 relation/intent 가이드가 검색 품질을 **올렸다는 실증 근거는
   존재하지 않는다.** → 옵션 C(플래너 부분 부활)는 "제거로 잃은 것이 없음 +
   부활로 얻을 것이 미실증"이라는 이중 불리를 안고 출발한다.

### 0.4 인덱싱 측 한계 — relation 필터의 실효 신호 지도

옵션 B/C 는 "질의 intent → relation 필터/부스팅"이 골자다. 그런데 **필터할 엣지
타입이 실제 그래프에 있어야** 효과가 난다. 실제 방출 엣지(03/04 실측):

| 그래프 | 실제 방출 relation | INTENT_TO_RELATIONS 커버 | 실효 |
|--------|-------------------|-------------------------|------|
| **git_code (AST)** | `imports`, `contains` **2종뿐** (F-GG-05) | **0종** (F-GG-06: INTENT 에 imports/contains 없음) | **relation 정렬 사실상 무의미** |
| **confluence llm_body** | depends_on/implements/calls/owned_by/supersedes/has_part/uses/provides/documented_in | 대부분 커버 | **여기서만 유효 신호** |
| confluence 결정론(link/body) | references/mentions_user/mentions_ticket/has_attachment/mentions/documents/has_attribute | 부분 커버 | 티켓·참조 질의에 약간 |

**두 개의 상한 못(caps)**:
- **F-GG-05 (코드 그래프)**: "이 함수 누가 호출?", "이 인터페이스 누가 구현?",
  "이 클래스 base?" — 이런 코드 질의는 `calls`/`implements`/`extends` **엣지 자체가
  존재하지 않는다.** relation 필터를 아무리 정교하게 만들어도 필터 대상이
  imports/contains 뿐 → **코드 질의에 대한 relation 정렬의 기대 효과 ≈ 0.**
  옵션 B/C 의 이득은 구조적으로 **confluence 지식 그래프 질의에 국한**된다.
- **F-CG-03 (confluence)**: depends_on/uses/calls 3분산 + concept sink 편중.
  relation 부스팅이 3분산을 **묶어주면**(INTENT 가 이미 셋을 한 그룹으로 매핑,
  graph_vocabulary.py:107-108) 3분산으로 희석된 recall 을 부분 회복하는
  **딱 한 곳의 실이득**이 있다. 반대로 concept sink 는 relation 이 아니라 entity
  타입 문제라 relation 정렬로는 못 고친다.

---

## 옵션 A — 정리 (데드코드 격리 + docstring 현실화)

**내용**: `INTENT_TO_RELATIONS`, `format_entity_types_for_prompt`,
`format_relation_types_for_prompt`, `format_intent_mapping_for_prompt`,
`all_entity_type_names`, `all_relation_type_names`, `format_vocab_entries_for_prompt`
(검색 정렬용, 소비처 0)를 **데드코드로 명시**(제거 또는 `# 미사용/향후용` 격리)하고,
`graph_vocabulary.py:4-7, 158-162` 와 `graph_search.py` 의 "검색 LLM 플래너가
union 어휘를 본다"는 스테일 서술을 **"검색은 type-agnostic 임베딩 시딩. vocab 은
인덱싱(llm_body subset)에서만 소비"**로 갱신. **실사용 중인 llm_body subset
메커니즘(`llm_body_*_vocab/_names`, `_has_source`)은 그대로 유지.**

| 항목 | 평가 |
|------|------|
| 변경 파일·규모 | `graph_vocabulary.py`(docstring + 데드 API 격리/삭제), `graph_search.py`(docstring 1문단). 테스트: `test_graph_vocabulary.py` 의 포매터/INTENT/all_* 참조 테스트 정리. **XS~S**, 로직 무변경 |
| 결정론/지연/비용 | **전부 불변** (프로덕션 경로 미변경) |
| 검색 품질 기대 효과 | **0** (품질 개선 아님). 이득은 유지보수: "검색이 vocab 으로 정렬된다"는 오해 제거, F-CG-01 순환 테스트를 떠받치던 죽은 API 제거로 착시 해소 |
| 위험 / 과거 이력 정합 | **가장 낮음.** 플래너 제거(ff731ac)와 완전 정합 — 그 결정을 문서에 확정하는 행위. 리스크: 삭제 시 미래에 옵션 B/C 를 택하면 일부 헬퍼를 되살려야 함 → **삭제보다 "격리+주석"** 을 권장(되돌리기 쉬움) |
| eval 하네스 영향 | **없음.** eval 은 `graph_search.entities/relations` 만 소비, vocab 포매터 미참조. (고지만: 변경 권고 아님) |
| 인덱싱 한계(F-GG-05/F-CG-03) 감쇄 | 무관(품질 목표가 없음). 한계와 충돌하지 않음 |

**정리**: 저위험·저비용 "빚 청산". 단독으로는 검색 품질을 못 올리지만, **어떤
품질 옵션을 택하든 먼저 깔려야 하는 바닥**(스테일 문서가 남으면 B/C/D 설계 근거가
오염됨).

---

## 옵션 B — 경량 결정론 정렬 (LLM 無, relation 부스팅/필터)

**내용**: 임베딩 시딩·1-hop 확장은 유지. 질의 **키워드 → INTENT_TO_RELATIONS
매칭**(문자열/사전 기반, LLM 無)으로 "주목 relation 집합"을 얻어,
(B1) 결과 정렬 단계에서 해당 relation 엣지에 연결된 노드를 **부스팅**(rank 상향),
또는 (B2) 1-hop 확장에서 해당 relation 엣지를 **우선/가지치기**. 전부 결정론.

| 항목 | 평가 |
|------|------|
| 변경 파일·규모 | B1(부스팅): `graph_search.py`(`_format_text`/`_build_entity_refs` 전 rank 재정렬 + 키워드→relation 매처) + vocab 에서 `INTENT_TO_RELATIONS` 재소비. **S**. B2(확장 필터): 추가로 `graph_store._bidirectional_bfs` 를 edge-aware 로 → **S~M**. 신규 테스트 다수 |
| 결정론/지연/비용 | **결정론 유지**, LLM 호출 0. 지연: `get_edges_between` 이 이미 relation_type 보유(0.2절) → 부스팅 비용 무시 가능. 필터(B2)도 in-memory, 무시 가능 |
| 검색 품질 기대 효과 | **confluence 의존/소유/표준 질의에서만** 유효. 예: "X 가 의존하는 것" → depends_on/uses/calls 그룹 부스팅 → **F-CG-03 의 3분산 희석을 묶어 recall/rank 부분 회복**(0.4절의 유일 실이득 지점). 소유("담당 팀") → owned_by/mentions_user 상향 |
| 위험 / 과거 이력 정합 | 플래너 제거와 **정합**(LLM 안 되살림, 결정성 유지). 진짜 위험은 **키워드→intent 매칭의 취약성**: INTENT 키는 한국어 산문("의존 관계 / depends / 무엇이 필요한가")이라 질의 표면과 어긋나기 쉬움 → 오매칭 시 엉뚱한 relation 부스팅으로 **rank 악화(precision↓)**. always-on 임베딩 시딩 위에 얹는 "rank 재배열"이라 recall 은 안 깎지만, rank 지표(MRR/NDCG)는 양날 |
| eval 하네스 영향 | rank 변경 → **MRR/NDCG/graph_hit@k 분포가 움직임**. `retrieved_entities` 순서가 곧 rank(graph_search.py:97-99). 개선/악화 방향은 **실측 전 불확실** → 반드시 eval 재측정 필요(별도 하네스, 변경 권고 아님·영향 고지) |
| 인덱싱 한계(F-GG-05/F-CG-03) 감쇄 | **강하게 감쇄됨.** F-GG-05: 코드 질의는 필터할 relation 이 imports/contains 뿐 → **코드 질의 이득 ≈ 0**. 실이득은 confluence llm_body 서브그래프에 국한. F-CG-03: 3분산 묶기는 INTENT 가 이미 그룹화해 여기선 오히려 **한계가 이득의 원천**. 다만 concept sink 노이즈는 relation 정렬로 못 잡음 |

**정리**: "LLM 없이 vocab 을 진짜로 소비하는" 최소 안. 이득이 실재하나 **좁다**
(confluence 의존·소유류). 코드 질의엔 무의미. rank 양날성 때문에 **eval 게이팅
필수** — 실측으로 이득 확인 못 하면 옵션 A 로 후퇴해야 하는 조건부 안.

---

## 옵션 C — LLM 플래너 부분 부활 (intent 분류만 LLM)

**내용**: 질의 **intent 분류만** 소형/일반 LLM 1콜로 수행 → relation 필터 집합
획득. 시드는 임베딩 유지. (옵션 B 의 "키워드 매처"를 LLM 분류기로 교체한 형태.)

| 항목 | 평가 |
|------|------|
| 변경 파일·규모 | `graph_search.py`(LLM 클라이언트 주입 + intent 분류 프롬프트 + relation 적용), `context_assembler`/`tools`(client 배선), vocab(INTENT·포매터 재소비). **M**. 프롬프트·모킹 테스트 다수 |
| 결정론/지연/비용 | **결정론 상실**(LLM 샘플링), **지연 +1 LLM 라운드트립**, **토큰 비용 발생**. `graph_search.py` docstring 이 자랑하는 "LLM 0회, 결정적"을 정면으로 되돌림 |
| 검색 품질 기대 효과 | 상한은 옵션 B 와 **동일**(둘 다 "relation 필터 획득"이 목표, 획득 수단만 키워드 vs LLM). LLM 이 한국어 질의 intent 를 키워드 매칭보다 **정확히** 잡을 순 있으나, 그 이득도 **0.4절의 상한 못에 똑같이 걸린다** |
| 위험 / 과거 이력 정합 | **가장 높음. 과거 제거 결정과 정면 충돌.** 0.3절: 플래너는 (a) always-on 임베딩 시딩에 유효 기여가 흡수돼 제거됐고, (b) 그 relation 가이드가 품질을 올렸다는 **실측 근거가 애초에 없다.** 부활은 "미실증 이득을 위해 결정성·비용·지연을 재도입"이며, 되살린 intent→relation 조차 **코드 질의엔 무효(F-GG-05)**. 부활을 정당화하려면 "옵션 B 로는 못 잡고 LLM 분류로만 잡히는 질의 유형 + 그 이득의 eval 실측"이라는 높은 근거가 선행돼야 함 |
| eval 하네스 영향 | 옵션 B 와 동일(rank 이동) + **비결정성으로 eval 재현성 저하**(같은 골드셋 재실행 시 분산). eval 은 결정성 가정이 강함 → 하네스 신뢰성에 부담(고지만) |
| 인덱싱 한계 감쇄 | 옵션 B 와 동일하게 **강한 감쇄**(F-GG-05 코드 이득 0, F-CG-03 confluence 국한). LLM 이라 해서 없는 엣지를 만들지 못함 |

**정리**: 이득 상한은 B 와 같은데 비용(결정성·지연·토큰)은 B 보다 크고, **과거
제거 결정을 정당화 없이 되돌린다.** 채택하려면 "B 로 부족함 + C 로만 얻는 이득의
실측"이 반드시 앞서야 한다 → **현 단계 비권장.**

---

## 옵션 D — 프롬프트 측 정렬 (그래프 text 에 type 의미 주입)

**내용**: 검색 로직·rank 불변. `_format_text`(graph_search.py:157) 가 만드는
그래프 컨텍스트 text 에, **결과에 등장한 entity_type/relation_type 의 vocab
description 을 용어집(glossary)으로 첨부**(`format_vocab_entries_for_prompt` 재사용).
0.1절대로 이 text 가 곧 답변 LLM 이 보는 유일한 표면이므로, 외부 LLM 이
`depends_on`/`concept`/`module` 같은 타입을 **정확히 해석**하도록 "타입 의미 해석"만 정렬.

| 항목 | 평가 |
|------|------|
| 변경 파일·규모 | `graph_search.py`(`_format_text` 에 등장 타입 수집 + glossary 블록 append) + vocab `format_vocab_entries_for_prompt` 재소비. **XS~S**. 순수 additive |
| 결정론/지연/비용 | **결정론 유지**, LLM 0, 지연 무시. 유일 비용: 그래프 섹션 텍스트 길이 소폭↑(등장 타입 수만큼, 보통 5~10줄) → MCP context 토큰 예산 내 안전 |
| 검색 품질 기대 효과 | **검색(retrieval) 지표는 불변** — 무엇을 찾는지 안 바꿈. **답변(generation) 품질**에서: 외부 LLM 의 타입 오해석 감소. 특히 F-CG-03 의 concept sink, module 이중의미(F-GG-03), Java interface→class(F-GG-04) 처럼 **타입 라벨이 오해를 부르는 지점**을, description 을 붙여 완충. 코드/문서 질의 **양쪽에 고루** 작동(타입 해석은 relation 유무와 무관) |
| 위험 / 과거 이력 정합 | 플래너 제거와 **정합**(검색 로직·시딩 불변). 위험: (1) glossary 가 vocab description 의 **부정확성을 그대로 노출** — F-GG-04(interface 설명이 Java 포함) 같은 스테일 설명을 답변 LLM 에 주입하면 **오해를 오히려 강화**. → 옵션 A 의 description 정확화(interface 에서 Java 제외 등)가 **선행 조건**. (2) 노이즈 노드에도 description 이 붙어 장황해질 수 있음(등장 타입만 1회씩 요약해 완화) |
| eval 하네스 영향 | `entities/relations`(eval 소비) 불변, `text` 만 변경 → **현 graph 메트릭(entity/relation 매칭)에 영향 없음.** 답변 품질은 현 하네스가 측정 안 함(고지만) |
| 인덱싱 한계(F-GG-05/F-CG-03) 감쇄 | **감쇄를 우회한다.** F-GG-05(없는 엣지)와 무관 — 검색을 안 바꾸므로. F-CG-03(concept sink/3분산)은 "제거"가 아니라 "해석 보조"로 **완화**. 단 description 이 부정확하면(F-GG-04) 역효과 → A 의존 |

**정리**: 유일하게 **코드/문서 질의 양쪽에, 결정성·비용 부담 없이** 작동하는 안.
retrieval 은 못 올리지만 답변 해석을 정렬한다. F-GG-05 상한 못을 **우회**하는 점이
B/C 대비 구조적 강점. 단 vocab description 정확화(A)가 없으면 오해를 되레 심을 수 있음.

---

## 옵션 비교 요약

| 축 | A 정리 | B 경량 결정론 | C LLM 부분부활 | D 프롬프트 |
|----|--------|--------------|---------------|-----------|
| 규모 | XS~S | S(B1)~M(B2) | M | XS~S |
| 결정론 | 유지 | 유지 | **상실** | 유지 |
| 지연/비용 | 0 | ~0 | **+LLM콜** | ~0 |
| retrieval 품질 | 0 | confluence만 ↑(양날) | confluence만 ↑(양날) | 0 |
| 답변 해석 품질 | 0 | 0 | 0 | **↑ (양 소스)** |
| 과거 이력 정합 | **완전** | 정합 | **충돌** | 정합 |
| eval 재현성 | 무영향 | rank 이동 | rank 이동+**비결정** | 무영향 |
| F-GG-05(코드) 감쇄 | n/a | **이득≈0** | **이득≈0** | **우회(무관)** |
| F-CG-03(confluence) | n/a | 3분산 묶어 부분회복 | 동left | 해석 완화 |

---

## 우선 권고

### 권고: A(선행) → D(주력) → B는 조건부, C는 보류

**1단계 — 옵션 A (즉시, 무조건).**
검색 정렬용 데드 API 를 격리(삭제보다 주석 격리 — B/D 가 일부 재소비하므로)하고,
`graph_search.py`/`graph_vocabulary.py` 의 "검색 LLM 플래너" 스테일 서술을 현실
(type-agnostic 임베딩 시딩)로 갱신. **동시에** F-GG-04/F-GG-03 등 **vocab
description 부정확 지점을 교정**(interface 설명에서 Java 제외 명시, module 포괄
범위 명시). 이유: (a) 어떤 품질 옵션도 스테일 문서 위에선 오설계되고, (b) D 의
glossary 가 이 description 을 그대로 답변 LLM 에 노출하므로 **D 의 안전 전제**다.

**2단계 — 옵션 D (주력 품질 개선).**
`_format_text` 에 등장 타입 glossary 를 주입해 "타입 의미 해석" 정렬. 근거:
- 유일하게 **코드·문서 질의 양쪽**에 작동(0.4절 F-GG-05 상한 못을 **우회**).
- 결정성·비용·지연 부담 0 → 플래너 제거 철학과 완전 정합.
- retrieval 지표를 안 건드리므로 **현 eval 메트릭에 회귀 위험 0**, 순수 additive.
- 실질 정렬 목표(vocab 이 검색측 소비자에게 실제로 쓰인다)를 **가장 낮은 위험으로
  실현**. (전제: 1단계의 description 교정.)

**3단계 — 옵션 B: 조건부(confluence 한정, eval 게이팅).**
D 이후에도 "의존/소유류 confluence 질의의 rank 가 낮다"가 **eval 로 확인되면**,
depends_on/uses/calls·owned_by 그룹 **부스팅(B1만, 저위험)**을 얹는다. 조건:
- **git_code 질의엔 적용 안 함**(F-GG-05 로 이득 0, 오매칭 위험만).
- 키워드→INTENT 매처의 오매칭이 rank 를 악화시키지 않는지 **eval 재측정으로 게이팅**.
  이득 미확인 시 B 포기(A+D 로 충분).
- B2(확장 필터)는 상한 못 대비 공수(BFS edge-aware 개조)가 커 후순위.

**보류 — 옵션 C.**
과거 제거 결정(0.3절: 수렴 흡수 + 미실증 이득)과 정면 충돌. 이득 상한이 B 와
동일한데 결정성·비용·재현성을 희생한다. **"B 로는 못 잡고 C 로만 잡히는 질의
유형과 그 이득의 eval 실측"이 선행 입증되기 전까지 착수 금지.** 인덱싱 측
`calls`/`extends`/`implements` 엣지가 실제로 생기기 전(별도 R2 후보 F-GG-05)에는
코드 질의 이득이 원천적으로 없어, C 의 근거가 성립할 여지도 좁다.

### 단계적 경로 한 줄
> **A(문서·description 정직화) 로 바닥을 깔고 → D(무비용 해석 정렬) 를 주력으로
> 얹은 뒤 → B(confluence 부스팅) 는 eval 이 이득을 실증할 때만 → C 는 보류.**
> 근거: F-GG-05 로 relation 정렬의 코드측 이득이 원천 봉쇄돼 B/C 의 유효 범위가
> confluence 로 좁고, 플래너 제거는 "실증된 이득 없이 되돌릴 결정이 아니다."

### eval 하네스 영향 (고지 · 변경 권고 아님)
- A/D: `entities/relations` 불변 → **현 graph 메트릭 회귀 위험 0**. D 의 답변
  해석 품질 이득은 **현 하네스가 측정하지 않는 축**(별도 판단 필요).
- B/C: `retrieved_entities` **rank 이동** → MRR/NDCG/graph_hit@k 재측정 필수.
  C 는 추가로 비결정성으로 재현성 저하. 어느 쪽도 본 라운드에서 하네스를 고치지
  않는다 — `eval/*`·`eval_search.py` 는 별도 영역.
