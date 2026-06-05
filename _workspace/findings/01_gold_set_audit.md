# Gold-Set Auditor — 합성 골드셋 생성 신뢰성 감사 (그래프 측정 한정)

## 한줄 판정
**HIGH 위험 (그래프 entity 측정 경로는 CRITICAL 요소 포함).** 이 골드셋으로 산출한
그래프 메트릭(특히 entity recall@k)은 **검색 품질이 아니라 "검색기가 인덱스 노드를
그대로 회수했는가"를 측정**한다. 절대값(예: "graph recall 0.82")을 운영 품질 지표나
SLA 로 보고하면 안 된다. A/B 개선 판단은 **조건부로만** 가능하다(아래 결론 참조).

## 검토 범위
- `_workspace/source/build_synthetic_gold_set.py` (1953줄) — 그래프 진입점:
  `load_candidate_subgraphs` (203), `load_cross_doc_seeds` (325), `build` (408),
  `_process_subgraph_item` (~977), `_run_graph_mode` (1084),
  `_process_cross_doc_item` (~1220), `_run_cross_doc_mode` (1310),
  `_make_graph_gold_item` (1398), `_make_cross_doc_gold_item` (1461),
  `_embed_graph_item_descriptions` (1496), `main` (1615).
- `_workspace/source/synth.py` (1169줄) — `GRAPH_GENERATE_PROMPT_TEMPLATE` (163),
  `CROSS_DOC_GENERATE_PROMPT_TEMPLATE` (222), `generate_graph_questions` (773),
  `generate_cross_doc_questions` (829), `filter_question` (945),
  `has_identifier_leakage` (412), `has_korean_proper_noun_leakage` (518),
  `stratified_sample` (1126).
- `_workspace/source/llm.py` (259줄) — `build_eval_llm_client` (123),
  `role_is_configured` (235), `_effective_role_target` (209).
- `_workspace/source/graph_match.py` (580줄) — `match_entity_tiered` (251),
  `run_entity_matching` (341), `DEFAULT_GRAPH_MATCH_THRESHOLD` (33).
- `_workspace/source/gold_set.py` (328줄) — `GraphEntityRef` (54), `GoldItem` (162).
- 교차참조: `_workspace/source/eval_search.py` `run_entity_matching` 호출부 (429).

## 핵심 발견 (위험 등급순)

### [CRITICAL] C1. 그래프 entity 정답이 평가 대상 인덱스 노드에서 직접 유래 → self-fitting
- 증거:
  - 골드 엔티티 name/type 출처: `_make_graph_gold_item` 가 `sg["entity_name"]` /
    `sg["entity_type"]` 를 그대로 정답으로 박는다 — build_synthetic_gold_set.py:1426-1427.
  - 그 `sg` 의 name/type 은 인덱싱된 graph_node 에서 직접 읽는다 —
    `load_candidate_subgraphs` build_synthetic_gold_set.py:237 (`node.get("entity_name")`),
    :240 (`entity_type`).
  - 평가 시 retrieved entity 도 **같은 인덱스**에서 나온다 —
    eval_search.py:431 `list(assembled.retrieved_graph_entities)`.
  - T1 exact 매칭은 `(name.lower, type)` 동등 비교 — graph_match.py:276-280.
- 영향: gold name/type 과 retrieved name/type 이 **동일 인덱스의 동일 문자열**이므로,
  검색기가 해당 노드를 회수만 하면 T1 이 trivially 1.0 으로 hit 한다. 측정되는 것은
  "표기 변형·동의어·의미 매칭에 대한 그래프 시스템의 강건성"이 아니라 "planner/retriever
  가 그 노드를 결과에 포함시켰는가"이다. 인덱싱 파이프라인이 동일하면 표면 일치는 항상
  자명하게 성립한다 — 골드셋 생성 방식 자체를 측정하는 순환.
- 권고:
  1. metadata 와 평가 요약에 **tier 분해 강제 노출**(T1/T2/T3/T4 hit 비율). T1 비중이
     높으면 "표면 일치 측정"임을 명시. (현재 graph_match.py:385 에서 tier_counts 는
     집계되나, 골드셋 신뢰도 경고로 연결되지 않음.)
  2. graph 모드 골드셋에 **type-변형 hold-out** 을 의도적으로 주입(예: gold type 을
     인덱스와 다르게 일반화하여 T4 embedding 경로 강제). 현재 `_make_graph_gold_item`
     은 type 을 인덱스 그대로 복사하므로 T4 의 type-agnostic 설계(graph_match.py:8,305)가
     실측에서 발동되지 않는다.
  3. 최소한 self-fitting 위험을 metadata 에 `graph_gold_from_index: true` 같은 플래그로
     기록하고 eval 단계에서 "graph entity recall 은 self-fitting 위험 있음" 경고를 띄울 것.

### [CRITICAL] C2. generator≠judge 분리는 강제되지만, generator≠"평가 대상 인덱싱 LLM" 분리는 전무
- 증거:
  - generator/judge 가 system LLM 과 같으면 빌드 차단 — main() build_synthetic_gold_set.py:1828-1835
    (`parser.error(...)`), 판정은 `role_is_configured` llm.py:235-259.
  - 그러나 그래프 **노드/엣지 추출 LLM**(인덱싱 시점)이 generator 와 다른지에 대한 검사·
    기록은 어디에도 없다. 골드 name/type 은 인덱싱 LLM 산출물(C1)이고, 질문/aliases/
    description 은 generator 산출물이다. 둘이 같은 모델/패밀리면 어휘 선택이 상관되어
    aliases·evidence_description 이 인덱싱 표현과 동조한다.
  - metadata 에 인덱싱(추출) LLM ID 가 기록되지 않는다 — build_synthetic_gold_set.py:655-684
    는 generator/judge/embedding 모델만 기록.
- 영향: generator/judge 분리 게이트는 "질문 생성과 품질 판정의 자기평가 편향"만 막는다.
  그래프 측정의 진짜 누설 축(인덱싱 표현 == 정답 표현)은 분리 강제 대상 밖이다.
  분리를 다 지켜도 self-fitting(C1)은 그대로 남는다.
- 권고:
  1. 골드셋 metadata 에 graph 추출 LLM(model/endpoint) 을 기록하고, generator 와 동일
     family 면 경고를 띄울 것(build_synthetic_gold_set.py:673-679 블록에 추가).
  2. README/docstring 이 아니라 코드가 "graph 모드에서 추출 LLM ID 미기록 시 graph 메트릭
     신뢰도 저하" 를 stats/metadata 에 남기도록 할 것.

### [HIGH] H1. 단일 엔티티 gold(W-3)로 per-item recall 이 0/1 — 채점 해상도·소표본 민감도
- 증거:
  - `_make_graph_gold_item` 가 핵심 노드 **1개만** `relevant_graph_entities` 로 기록 —
    build_synthetic_gold_set.py:1406-1407 주석("핵심 노드 1개만 기록 ... 이웃 포함 시
    채점 후해짐"), :1425-1431, :1451.
  - 따라서 per-item entity recall 분모는 항상 1 — graph_match.py:370-373
    (`all_relevant_keys` 에 골든 키 1개), 결과적으로 item recall ∈ {0, 1}.
- 영향: 항목당 신호가 1비트라 채점 해상도가 낮다. 집계 recall 은 곧 hit한 항목 비율이며,
  소표본에서 분산이 크다. 기본 그래프 노드 수는 `--n-graph-nodes or --n-chunks`
  (build_synthetic_gold_set.py:1864)로 작게 설정되기 쉽다. N=20~30 수준이면 표준오차가
  ±0.09~0.11 수준이라 A/B 간 5%p 개선을 통계적으로 분리하기 어렵다.
- 권고:
  1. graph 항목 N **≥ 150~200** 권고(0/1 채점에서 ±0.05 SE 목표 시 binomial 기준 약 100,
     계층화 손실 감안 150+). metadata 의 `stats.graph_passed` 를 표본수로 노출하고, 임계
     미만이면 eval 단계에서 "graph recall 신뢰구간 넓음" 경고.
  2. relation 채점(`--score-relations`, build_synthetic_gold_set.py:1434-1441)을 기본
     활성화하거나, 1-hop 이웃 일부를 보조 정답으로 추가해 항목당 신호를 늘리는 옵션 제공.

### [HIGH] H2. generator 작성 aliases/description 의 정답 누설 → T2/T4 trivial 통과
- 증거:
  - aliases 는 generator LLM 이 작성하고 골드에 그대로 박힌다 —
    `_make_graph_gold_item` build_synthetic_gold_set.py:1428 (`aliases=list(gq.entity_aliases)`),
    파싱 synth.py:683-690.
  - 프롬프트가 aliases 를 "위 엔티티 정보에서 자연스럽게 떠오르는 동의어"로 요구 —
    synth.py:198-199. 즉 generator 가 인덱스 표기를 보고 그 변형을 적는다.
  - T2 alias 매칭은 retrieved name 이 alias 집합에 들어가면 hit(score 1.0) — graph_match.py:286-294.
  - description(T4 소스)도 generator 가 작성 — synth.py:196-197, 골드 박기 :1423,1429.
- 영향: generator 가 "인덱스에 존재할 법한 표기"를 alias 로 적으면, retrieval 이 그
  표기로 노드를 반환했을 때 T2 가 자명 통과한다. evidence_description 역시 인덱스
  description/snippet 을 보고 쓰므로 T4 cosine 이 부풀려질 수 있다. 즉 강건성 tier
  (T2/T4)가 "생성기가 정답 변형을 미리 적어둔 덕"에 통과 → 강건성 과대평가.
- 완화 요소(부분): description 을 graph_store 원본으로 폴백하지 **않음** —
  build_synthetic_gold_set.py:1415-1423(빈 evidence 면 T4 skip). 이는 T4 자명 1.0 을
  일부 막는 양심적 설계. 그러나 generator 가 evidence 를 채우면 누설 가능성은 남는다.
- 권고:
  1. `filter_question` 의 결정론 누설 게이트(synth.py:988,994)를 **aliases/evidence 텍스트에도**
     적용. 현재는 `gq.query` 만 검사(build_synthetic_gold_set.py:1053-1061) — aliases/description
     은 누설 검사 없이 골드에 박힌다.
  2. T2 alias 채점을 별도 tier 로 분리 보고하고, alias hit 비율이 높으면 "generator-supplied
     alias 의존" 경고. graph_match.py:124 tier_counts 에 이미 alias 카운트 존재 — 노출만 하면 됨.

### [HIGH] H3. 표준 graph 모드는 단일 노드 질문이라 cross-source 브리지를 자극 못함
- 증거:
  - `GRAPH_GENERATE_PROMPT_TEMPLATE` 는 "이 엔티티 또는 관계에서 답을 찾을 수 있는" 질문을
    요구 — synth.py:175-176. 정답도 단일 엔티티(H1). 한 노드만 회수하면 만점.
  - 프롬프트가 오히려 "관계의 다른 엔티티 이름까지 줄줄 나열하지 말 것" 으로 다중 엔티티
    브리지를 **억제** — synth.py:183-184.
- 영향: confluence 이름→코드 FQN 같은 cross-source 브리지(서로 다른 source_type 노드를
  잇는 추론)를 측정하지 못한다. graph 시스템의 핵심 가치(여러 출처 연결)에 대한 측정
  민감도가 표준 graph 모드에는 부재.
- 완화: cross-doc 모드(`_make_cross_doc_gold_item` build_synthetic_gold_set.py:1461,
  `relevant_doc_groups=[[src],[tgt]]` AND :1479, `cross_document=True` :1480)가 이 공백을
  의도적으로 메운다. 두 문서를 모두 봐야 답 가능한 AND 채점이라 브리지를 자극한다.
- 권고:
  1. graph 시스템 A/B 판정 시 **cross-doc 모드를 필수 동반**(`--enable-cross-doc`).
     표준 graph 단독 메트릭은 브리지 능력에 무감하므로 단독 사용 금지.
  2. cross-doc seeds 가 5개 미만이면 generic gate skip 경고(build_synthetic_gold_set.py:1348-1352)
     가 뜨는데, 이 경우 표본 자체가 빈약하므로 cross-doc 메트릭도 보고에서 제외할 것.

### [MEDIUM] M1. cross-doc gold 엔티티는 description/embedding 부재 → T1/T2 만으로 채점
- 증거: `_make_cross_doc_gold_item` 의 GraphEntityRef 두 개 모두 description/embedding 없음 —
  build_synthetic_gold_set.py:1481-1487 (source 엔티티만 aliases, target 은 name/type 뿐).
  T4 는 description 없으면 name fallback(graph_match.py:310)이나 type-agnostic 의미 매칭
  강건성은 사실상 측정 안 됨.
- 영향: cross-doc 채점은 인덱스 표기 정확 일치(T1)에 더 강하게 의존 — self-fitting(C1)이
  cross-doc 에도 전이. 브리지 발동(H3 완화)은 doc_group AND 채점이 담당하나 entity tier
  강건성은 약함.
- 권고: cross-doc 에서도 generator evidence_description 을 두 엔티티에 채워 T4 를 활성화하거나,
  cross-doc 의 entity-tier 메트릭은 보고에서 doc-level AND 메트릭과 분리할 것.

### [MEDIUM] M2. 그래프 매칭 임계값 변경(0.78→0.65)이 경험적 검증 없이 적용
- 증거: `DEFAULT_GRAPH_MATCH_THRESHOLD = 0.65`, docstring 이 "이전 0.78 은 너무 보수적",
  "골드셋 신뢰성에 영향이 큰 변경이므로 별도 validation 권장" 이라고 스스로 미검증을 인정 —
  graph_match.py:33-40. metadata 에 기록은 됨(build_synthetic_gold_set.py:677).
- 영향: T4 cosine 하한이 낮아지면 의미적으로 느슨한 매칭이 hit 처리되어 recall 이 구조적으로
  상향. 0.65 의 근거가 추측에 가까워 절대값 신뢰도 저하. 단 metadata 기록으로 재현성은 확보.
- 권고: 라벨링된 hold-out 으로 τ-sweep(0.6~0.85) precision/recall 곡선 산출 후 임계값 고정.
  그 전까지 graph 메트릭 절대값은 τ 가정에 종속됨을 보고서에 명시.

### [MEDIUM] M3. 그래프 노드 샘플링은 source_type 만 균등화 — degree/representativeness 편향
- 증거:
  - `stratified_sample(..., key="source_type")` — build_synthetic_gold_set.py:1128-1130.
    토픽/노드 차수(degree)/길이/난이도 균등화 없음.
  - 후보 필터는 1-hop 이웃 ≥ min_neighbors 만 요구 — load_candidate_subgraphs:264-267,
    그 외 degree 분포 보정 없음. min_neighbors 기본 1(build_synthetic_gold_set.py:428).
- 영향: hub 노드(고차수)가 distractor·정답 모두에서 과대표집될 수 있고, leaf/희소 노드의
  검색 난이도는 측정에서 누락. source_type 균등화만으로는 그래프 구조 편향을 못 잡음.
- 완화: 샘플링은 rng 셔플 후 round-robin 으로 결정론적·재현 가능(synth.py:1150-1168,
  seed 전파 build_synthetic_gold_set.py:1901). 편향은 있으나 재현은 됨.
- 권고: degree 분위수(예: 저/중/고차수)를 2차 stratify 키로 추가하거나, 최소한 metadata 에
  샘플 degree 분포를 기록해 대표성 진단을 가능케 할 것.

### [LOW] L1. 결정성: 샘플링은 결정적이나 LLM 결정성은 best-effort
- 증거: `--seed` 는 샘플링/distractor 셔플만 결정(rng, build_synthetic_gold_set.py:1901,
  synth.py:1150). LLM seed 는 별도 `--generator-seed-base`(기본 None → seed 미전달,
  build_synthetic_gold_set.py:1782-1789)이고 subgraph 별 `seed_base + idx`
  (build_synthetic_gold_set.py:1005), judge 는 `sg_seed + 10000 + j`(:1050-1051),
  distractor 는 `seed + 100 + i`(synth.py:1020) 로 충돌 회피. temperature 기본 0.0
  (synth.py:780). docstring 이 OpenAI/vLLM 만 실효, Anthropic 무시라고 명시(:1787-1788).
- 영향: `--generator-seed-base` 미지정이 기본이라 동일 seed 라도 LLM 응답이 비결정적일
  수 있다 → 골드셋 재생성 시 항목이 달라질 수 있음. 다중 골드셋은 `base_seed+i-1`
  (build_synthetic_gold_set.py:1901)로 충분히 분리.
- 권고: graph A/B 비교용 골드셋은 `--generator-seed-base` 명시 + OpenAI 호환 endpoint
  사용을 운영 절차로 강제. metadata 에 seed_base 기록은 이미 됨(:664).

### [LOW] L2. Judge 게이트는 그래프 질문에 적용되나 snippet 기준이라 누설 검출 범위 제한
- 증거: graph 질문도 `filter_question` 4단계 게이트 통과(build_synthetic_gold_set.py:1053-1061),
  입력 source_chunk = `sg["subgraph_snippet"]`(:1054). 게이트는 (a)답가능성
  (b1)ASCII누설 (b2)한국어누설 (c)지시대명사 (d1)유일성 (d2)distractor — synth.py:957-1028.
  단, 프롬프트가 "엔티티 이름은 그대로 써도 된다"(synth.py:183)고 허용하므로 entity-name
  자체의 어휘 중첩은 게이트가 의도적으로 통과시킴.
- 영향: 게이트는 reasoning 없이 yes/no 만 받는다(synth.py:139-140,156-159, parse_yes_no
  :715) — 판정 근거 미기록. entity-name 중첩 허용은 self-fitting(C1)을 게이트가 막지
  못하는 구조적 이유. 통과율(stats)은 metadata 에 기록되나(:654) 그래프 게이트 효과성
  (통과율 분포)이 별도 노출되지 않음.
- 권고: graph_passed/graph_generated 비율을 metadata 에 명시(이미 _stats 에 있음 —
  build_synthetic_gold_set.py:1017,1046)하고, 95%+ 또는 50% 미만이면 게이트 형식성/과엄격
  경고. judge 에 reasoning 요구는 비용 대비 효익 낮으므로 선택.

## 차원별 상세 점검 (그래프 맥락)

### 1. 샘플링 편향
`stratified_sample` 은 source_type 만 균등화(build_synthetic_gold_set.py:1129,
synth.py:1126-1168). degree/토픽/길이/난이도 미고려 → hub 편향 가능(M3). 한 노드 N개
질문의 다양성 강제 메커니즘은 없음 — temperature 0.0 기본이라 오히려 N개가 유사해질 수
있음(synth.py:780, L1). cross-doc seeds 는 disjoint-doc 엣지에서 결정론적 추출
(build_synthetic_gold_set.py:344-358, 정렬 :390-396)로 재현성은 좋음. **판정: MEDIUM (M3).**

### 2. 역방향 생성의 정답 누설 (그래프 self-fitting)
가장 심각. 골드 name/type 이 평가 인덱스 노드에서 직접 유래(C1, build_synthetic_gold_set.py:1426-1427
← load:237,240) → T1 자명 통과(graph_match.py:276-280). generator aliases/description 도
인덱스 표기 동조(H2). 누설 방지 장치: query 에 대해서만 결정론 누설 게이트
(synth.py:988,994), description 의 graph_store 폴백 금지(build_synthetic_gold_set.py:1415-1423).
그러나 aliases/evidence 텍스트에는 누설 게이트 미적용(H2). **판정: CRITICAL (C1+H2).**

### 3. Judge 게이트 효과성
4단계: 답가능성(LLM)/누설(결정론 ASCII+한국어)/지시대명사(결정론)/유일성+distractor(LLM)
— synth.py:957-1028. graph 질문에 적용됨(build_synthetic_gold_set.py:1053). 게이트는
self-fitting 을 검출하지 못함 — entity-name 중첩을 프롬프트가 허용(synth.py:183)하고
게이트 입력이 snippet 이라 인덱스-유래 정답 표기를 누설로 보지 않음(L2). 통과율 가시화 부족.
**판정: MEDIUM (L2) — 게이트 자체는 동작하나 그래프 누설 축을 못 막음.**

### 4. Generator/Judge 분리 강제성
강제됨: 둘 다 system LLM 과 같으면 `parser.error` 차단(build_synthetic_gold_set.py:1828-1835),
`role_is_configured` 가 effective (endpoint,model) 비교로 실질 동일까지 탐지(llm.py:242-259).
`--allow-self-eval` 시 metadata 기록(:661-662) + 경고(:1836-1841). 그러나 그래프 측정의
핵심 누설원인 **인덱싱 LLM ↔ generator 분리**는 강제·기록 대상 밖(C2). **판정: CRITICAL (C2)
— 일반 분리는 우수하나 그래프 맥락의 진짜 축이 누락.**

### 5. 결정성 / 재현성
샘플링/셔플 결정적(seed, build_synthetic_gold_set.py:1901). LLM 은 seed_base 옵션이며
기본 미전달 → best-effort(L1, synth.py:756-758 docstring). seed 충돌 회피 오프셋 체계 존재
(generator idx, judge +10000, distractor +100). 다중 골드셋 seed 분리 양호(:1901).
temperature/seed call_kwargs 통일(synth.py:761-768,817-824,898-906). **판정: LOW (L1).**

### 6. 그래프 질문 / 매칭
τ=0.65 미검증 변경(M2, graph_match.py:33-40), metadata 기록은 됨(:677). T2 alias 채점이
generator 작성 alias 에 의존(H2). T4 type-agnostic 설계(graph_match.py:8,305)는 골드 type 이
인덱스와 동일해 실측 미발동(C1 권고2). 추출 LLM == generator 여부 미기록(C2).
**판정: HIGH (H2) + MEDIUM (M2).**

### 7. 메타데이터 / 추적성
양호한 부분: generator/judge model·endpoint, seed, seed_base, temperature, embedding_model,
graph_match_threshold_default, score_relations, self_evaluation_warning 기록
(build_synthetic_gold_set.py:645-684). `--no-filter` 는 `.UNFILTERED` 경로 강제(:1547-1562).
부족한 부분: (a) graph 추출 LLM ID 미기록(C2), (b) graph gold 가 인덱스 유래라는
self-fitting 플래그 부재(C1), (c) chunk fingerprint/노드 fingerprint 미기록 — 동일 인덱스에서
생성됐는지 검증 불가. eval 단계 self-evaluation 경고 노출은 self_evaluation_warning 플래그로
가능하나 graph self-fitting 경고는 없음. **판정: MEDIUM.**

## 종합 위험 매트릭스
| 차원 | 위험 등급 | 핵심 이슈 |
| --- | --- | --- |
| 1. 샘플링 | MEDIUM | source_type만 균등화, degree/hub 편향(M3); 질문 다양성 강제 없음 |
| 2. 정답 누설(self-fitting) | CRITICAL | gold name/type 이 인덱스 노드 직접 유래 → T1 자명(C1); alias/evidence 누설(H2) |
| 3. Judge 게이트 | MEDIUM | 4단계 적용되나 entity-name 중첩 허용·snippet 기준이라 누설 미검출(L2) |
| 4. Gen/Judge 분리 | CRITICAL | 일반 분리는 강제·기록 우수하나 인덱싱 LLM↔generator 분리 누락(C2) |
| 5. 결정성/재현성 | LOW | 샘플링 결정적, LLM seed best-effort·기본 미전달(L1) |
| 6. 그래프 매칭 | HIGH | τ=0.65 미검증(M2); T2/T4 가 generator 작성물 의존(H2) |
| 7. 메타데이터/추적성 | MEDIUM | 추출 LLM·self-fitting·fingerprint 미기록(C2/C1) |
| (위협3) 단일엔티티 0/1 | HIGH | per-item recall 해상도 1비트, 소표본 민감(H1) |
| (위협5) 브리지 미자극 | HIGH | 표준 graph 단일노드라 cross-source 무감, cross-doc 동반 필요(H3) |

## 운영 권고

### 이 골드셋으로 만든 그래프 메트릭을 A/B 개선 판단에 쓸 수 있는가?
**조건부 YES — 단, 다음을 모두 지킬 때에 한해 "상대 비교"로만.**
- 양 arm 이 **동일 인덱스/동일 추출 LLM** 위에서 평가될 것. 그러면 self-fitting(C1) 편향이
  양쪽에 동일하게 작용하므로 *상대 차이*는 retriever/planner 변경 효과로 해석 가능.
  (인덱싱 자체를 바꾸는 A/B 는 self-fitting 편향이 비대칭이 되어 **사용 금지**.)
- graph entity 메트릭은 **tier 분해(T1/T2/T3/T4)와 함께** 봐야 한다. T1 변화만으로 개선을
  주장하면 표면 회수율 변화일 뿐 의미 매칭 개선이 아니다.
- graph 항목 N ≥ 150 (H1) + cross-doc 모드 동반(H3) + τ 고정·기록(M2).

### 사용 가능 범위
- 동일 인덱스 위 retriever/planner/rerank 파라미터의 **상대 A/B** (방향성 판단).
- cross-doc 모드의 doc-level AND recall — 브리지 능력의 상대 비교(C1 영향 적음, doc_id 기반).

### 사용 금지 범위
- graph entity recall **절대값**을 품질 지표·SLA·외부 보고로 사용 (self-fitting C1로 부풀려짐).
- 인덱싱/추출 파이프라인을 바꾸는 A/B 의 graph entity 메트릭 비교 (편향 비대칭).
- N<100 graph 항목에서의 5%p 미만 차이 판정 (0/1 채점 분산, H1).
- 표준 graph 모드 단독으로 cross-source 브리지 능력 주장 (H3).

### 보완 작업 (우선순위 순)
1. **(C1/C2)** metadata 에 graph 추출 LLM ID + `graph_gold_from_index` 플래그 + 노드
   fingerprint 기록, eval 요약에 self-fitting 경고 노출. (build_synthetic_gold_set.py:673-679)
2. **(C1)** tier 분해(T1~T4 hit 비율)를 eval 기본 출력으로 승격, T1 편중 경고.
   (graph_match.py:385 tier_counts 활용)
3. **(H2)** `filter_question` 의 결정론 누설 게이트를 aliases/evidence_description 텍스트에도
   적용. (build_synthetic_gold_set.py:1053-1061 직전에 누설 검사 추가)
4. **(H1)** graph 항목 N≥150 운영 가이드 + 표본수 임계 미만 시 신뢰구간 경고.
5. **(H3)** graph A/B 시 `--enable-cross-doc` 필수화, cross-doc seeds<5 시 보고 제외.
6. **(M2)** τ-sweep validation 후 임계값 고정·근거 문서화. (graph_match.py:33)
7. **(M3)** degree 분위수 2차 stratify + 샘플 degree 분포 metadata 기록.
