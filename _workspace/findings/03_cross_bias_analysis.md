# Cross-Bias Analysis — 의존성 교차 분석 (그래프 측정 경계면 한정)

## 한줄 판정
**HIGH→CRITICAL. 그래프 메트릭의 self-evaluation 분리는 "코드로 보장되지 않는다".**
generator≠judge 분리는 코드로 강제되지만(`build_synthetic_gold_set.py:1828-1835`),
그래프 측정의 진짜 누설 축 3개 — **(인덱싱 추출 LLM)↔(정답)↔(검색 인덱스)** 의 self-fitting,
**(인덱싱 임베딩)=(검색 시드 임베딩)=(T4 채점 임베딩)** 의 3중 동일 모델 순환,
**(generator)↔(시스템 planner/HyDE LLM)** 의 질의표현 동조 — 는 **전부 분리 강제·검사·기록 대상 밖**이다.
두 단일 감사관(01 C1/C2, 02 F6)이 각각 절반씩 본 것을 연결하면, 이 셋이 **하나의 닫힌 회로**를
이루어 "자기가 만든 답을 자기 임베딩으로 회수하고 자기 임베딩으로 채점"하는 구조가 드러난다.
결론(4 시나리오 표)으로 직행하면: **인덱스 고정 A/B만 조건부 YES, 나머지는 NO/조건부.**

---

## 채널별 통합 분석 (그래프 맥락)

### A. self-fitting 순환의 전체 경로 — 인덱싱 추출 → 골드 entity → T1 자명 통과
**두 감사관 연결: 01 C1 (골드 name/type 이 인덱스 노드 직접 유래) + 01 C2 (추출 LLM↔generator 분리 미기록).**

전체 call path를 추적하면 닫힌 회로가 된다:

1. 인덱싱 시점: 추출 LLM이 graph_node 의 `entity_name`/`entity_type` 을 만들어 meta_store 에 저장.
2. 골드 생성: `load_candidate_subgraphs` 가 `node.get("entity_name")`(build:237) / `entity_type`(:240)
   를 그대로 읽고, `_make_graph_gold_item` 이 `sg["entity_name"]`/`sg["entity_type"]`
   을 **변형 없이 정답에 박는다**(build:1426-1427).
3. 평가 시점: retrieved 엔티티도 **같은 인덱스**에서 나온다 —
   `assemble_context_with_sources(...)` 의 `assembled.retrieved_graph_entities`(eval:431),
   그 임베딩은 `graph_store.build_entity_embeddings`(eval:1038)로 같은 노드에서 빌드.
4. 채점: T1 exact 는 `(name.lower, type)` 동등 비교(graph_match.py:276-280).

→ gold name/type 과 retrieved name/type 이 **동일 인덱스의 동일 문자열**이므로,
planner/retriever 가 그 노드를 결과 리스트에 포함만 시키면 T1 이 trivially 1.0.
측정되는 것은 "표기 변형·동의어 강건성"이 아니라 "그 노드를 회수했는가"뿐이다.

**경계면 위험(단일 감사관이 못 본 부분):** 01 C2 는 "추출 LLM ID 미기록"을 지적하지만,
그 영향이 **A 채널(self-fitting)을 통해 T1 자명 통과로 귀결**된다는 인과를 명시하지 않았다.
즉 추출 LLM == generator 든 아니든 **T1 자명 통과는 동일하게 성립한다** — name/type 은
generator 가 아니라 인덱스에서 직접 복사되므로(build:1426). 분리를 100% 지켜도 A 는 남는다.
generator↔추출 LLM 동일 여부는 A 가 아니라 **D 채널(alias/description 누설)** 에서만 영향을 준다.
이 구분이 두 감사관 모두에서 흐릿하다.

### B. 임베딩 3중 순환 — "자기 답을 자기 임베딩으로 회수하고 자기 임베딩으로 채점"
**두 감사관 연결: 02 F6 (T4 임베딩 = 검색 인덱싱 `build_entity_embeddings` 동일 client)
+ 01 (골드 description_embedding 도 같은 임베딩으로 박힘).**

세 지점이 모두 **같은 `_build_embedding_client(config)` + `processor.embedding_model`** 이다:

| 지점 | 코드 | 임베딩 출처 |
|---|---|---|
| (1) 골드 생성 evidence 임베딩 | build:1889-1891 `_build_embedding_client(config)`, `processor.embedding_model`; `_embed_graph_item_descriptions`→`aembed_with_client`(graph_match.py:530) | gold description_embedding |
| (2) 검색 인덱스 엔티티 임베딩 | eval:1038 `graph_store.build_entity_embeddings(embedding_client)`, embedding_client=eval:964 `_build_embedding_client(config)` | retrieved 엔티티 회수 시드 |
| (3) T4 채점 임베딩 | eval:1031 `build_embed_fn(embedding_client, ...)`, graph_match.py:316/327 | T4 cosine 매칭 |

세 곳이 **동일 모델**이면 3중 순환이 닫힌다:
- (1)=(3): 골드 description 을 임베딩한 벡터와 retrieved 를 임베딩한 벡터가 같은 공간 →
  같은 텍스트면 cosine=1.0. generator 가 인덱스 description 을 보고 evidence 를 쓰면(01 H2)
  자기 임베딩 거리가 인위적으로 가까워진다.
- (2)=(3): 검색이 노드를 회수할 때 쓴 시드 임베딩과 T4 채점 임베딩이 같은 모델 →
  "회수에 성공했다"는 사실 자체가 "임베딩 공간에서 가깝다"를 함의하고, 같은 공간으로
  채점하므로 **T4 가 검색 결정을 재확인**할 뿐 독립 검증이 아니다.

**여기에 02 F1 (T4 type-무시 + τ=0.65) 이 곱해진다:** type 게이트 없이(graph_match.py:305,330)
느슨한 0.65(:33)로, 같은 임베딩 공간에서 회수한 후보를 채점하면 false-positive 흡수율이
구조적으로 상향. 즉 **B(임베딩 동일)와 02 F1(느슨한 T4)은 독립 결함이 아니라 상승작용**한다 —
단일 감사관은 F6 을 "중(Medium)"으로 봤으나, F1(Critical)과 결합하면 등급이 올라간다.

**추적성 사각(02 §5 와 01 §7 연결):** eval summary 는 `embedding_model`(eval:1265)을 기록하고,
골드 metadata 도 `embedding_model`(build:676)을 기록한다. **그러나 둘이 같은지 자동 대조하는
코드가 없다.** 02 F7 이 라벨 간 임베딩 대조 부재를 지적했지만, **골드↔평가 임베딩 대조**(이것이
B 순환의 트리거)는 두 감사관 어디서도 검사 장치로 다뤄지지 않는다.

### C. generator LLM ↔ 시스템 planner/HyDE LLM 공유 — 질의 표현 편향
**단일 감사관 양쪽에서 누락된 채널.**

- 평가 검색은 `assemble_context_with_sources(..., llm_client=llm_client, ...)`(eval:388-394)
  를 호출. 이 `llm_client` 는 `_build_llm_client(config)`(eval:963) = **시스템 LLM(`llm.*`)**.
  내부에서 HyDE 생성·graph planner 추론에 이 시스템 LLM을 쓴다(eval:383 graph_planner_seed,
  :397 include_graph).
- generator 는 `build_eval_llm_client(config,"generator",...)`(build:1803). 미지정 시
  `eval.generator.*` → 없으면 `llm.*` 로 fall-through(llm.py:166-168, 222-232).

→ generator 옵션 미지정이면 **generator == 시스템 planner/HyDE LLM == 같은 family**.
generator 가 만든 질문 Q 의 어휘·표현이 시스템 HyDE 가 생성하는 가설 문서의 어휘와 동조하면,
HyDE 검색이 Q→노드 회수에 유리해진다(질의표현 편향). 이는 A(self-fitting)의 **질의 측 부스터**다.

**왜 두 감사관이 놓쳤나:** 01 은 generator↔judge / generator↔추출 LLM 만 봤고(C2),
02 는 채점부만 봤다. **검색 본체(assemble)의 LLM이 generator 와 같은 family인지**는
경계면이라 단일 파일 감사로는 안 보인다. `role_is_configured`(llm.py:235)는 role↔system
비교만 하지, generator↔judge↔system planner **3자 동일성**을 보지 않는다.

### D. alias/description 누설 → T2/T4 통과 + query-only 누설 게이트 사각
**01 H2 (generator 작성 alias/description 누설) + 누설 게이트가 query 만 검사함을 연결.**

- alias 는 generator 가 작성(synth.py:198-199 "동의어 떠올리기")해 골드에 그대로 박힘
  (`_make_graph_gold_item` aliases=list(gq.entity_aliases), build:1428). T2 는 retrieved name
  이 alias 집합에 들면 hit(graph_match.py:286-294).
- evidence_description 도 generator 작성(build:1423,1429) → T4 소스(graph_match.py:310).
- **누설 게이트는 `gq.query` 만 본다**(`filter_question(gq.query, sg["subgraph_snippet"], ...)`,
  build:1054; 게이트 내부 `has_identifier_leakage(question, source_chunk)` synth.py:988,994).
  → aliases / evidence_description 은 **누설 검사 없이** 골드에 직행한다.

**경계면 위험:** generator↔추출 LLM 이 같은 family면(01 C2, 미검사) generator 가 적는 alias 가
**인덱스의 실제 표기와 어휘 동조** → retrieval 이 그 표기로 노드를 반환할 때 T2 자명 통과.
즉 C2(추출↔generator 분리 미기록)의 실질 피해는 **A(T1)가 아니라 D(T2/T4)** 에서 발생한다.
이 인과 연결이 단일 감사관 보고서엔 명시적으로 끊겨 있다(01 은 C2 와 H2 를 별 항목으로 분리).
**완화 요소(01 인용):** description 의 graph_store 원본 폴백 금지(build:1415-1423)는 T4 자명
1.0 을 일부 막는 양심적 설계. 그러나 generator 가 evidence 를 채우면 D 누설은 남는다.

### E. 코드↔지식 브리지가 self-fitting 대칭성을 깨는가 (비대칭 편향 위험)
**입력 지시의 E 항 — 두 감사관 모두 다루지 않은 신규 경계면.**

01 의 핵심 운영 권고는 "**양 arm 이 동일 인덱스/동일 추출 LLM** 위에서 평가되면 self-fitting
편향이 양쪽에 동일하게 작용하므로 *상대 차이*는 유효"(01:262-264)이다. 이 **대칭성 가정**이
깨지는 경우를 짚는다:

- 브리지(cross-source 연결: confluence 이름→코드 FQN 등)가 **retrieved 엔티티 집합을 바꾸면**,
  한 arm 은 브리지로 추가 노드를 회수하고 다른 arm 은 아니다. 그런데 골드 정답은 **양 arm
  공통의 인덱스 노드**(A 채널)다. 따라서:
  - 브리지가 회수한 노드가 골드 정답과 **같은 인덱스 문자열**이면 → 그 arm 만 T1 자명 통과
    혜택을 더 받음. 편향이 **비대칭**이 되어 "상대 차이 = 시스템 개선" 해석이 깨진다.
  - 표준 graph 모드는 단일 노드 정답(01 H1, build:1406-1407)이라 브리지를 자극 못함(01 H3).
    cross-doc 모드(build:1461,1479 `relevant_doc_groups=[[src],[tgt]]`, AND 채점)만 브리지를
    자극하나, cross-doc 엔티티는 description/embedding 부재(01 M1, build:1481-1487)라
    **T1/T2 표면 일치에 더 의존** → A(self-fitting)가 cross-doc 에 전이되고, 브리지가 한 arm
    에서만 발동하면 그 self-fitting 혜택이 비대칭 분배된다.

**결론(E):** "동일 인덱스면 self-fitting 이 대칭"이라는 01 의 가정은 **retrieved 집합을 바꾸는
변경(브리지 on/off, graph weight, planner 교체)에는 성립하지 않을 수 있다.** A 채널의 자명
통과 혜택이 회수 집합에 종속되기 때문이다. 이건 단일 감사관이 못 본 2차 효과다.

---

## A. LLM Fall-through 다이어그램

```
                         CLI override 미지정 시 fall-through
  ┌─────────────┐
  │ config.llm.*│ (시스템 RAG 답변/HyDE/graph-planner LLM)
  └──────┬──────┘
         │  ← eval:963 _build_llm_client(config) → assemble(llm_client=...) eval:394
         │
   ┌─────┴────────────────────────┬───────────────────────────┐
   │ eval.generator.* 비면         │ eval.judge.* 비면          │ (시스템은 항상 llm.*)
   ▼ (llm.py:166-168,222-232)     ▼                            ▼
 generator ───fall──> llm.*    judge ───fall──> llm.*    system planner/HyDE = llm.*
```

| role | 미지정 시 떨어지는 곳 | 분리 강제? | 그래프 영향 |
|---|---|---|---|
| generator | `eval.generator.*`→`llm.*` (llm.py:166,222) | gen∨judge 중 하나라도 분리 안 되면 `parser.error`(build:1828-1835) | C 채널: generator==planner/HyDE면 질의표현 동조 |
| judge | `eval.judge.*`→`llm.*` (동일) | 위와 동일(OR 조건) | 그래프 게이트(filter_question)의 self-bias |
| **system planner/HyDE** | **항상 `llm.*`** (eval:963→assemble:394) | **검사 없음** | C 채널 핵심 — generator/judge 와 3자 동일 가능 |
| **graph 추출 LLM** (인덱싱) | 별도 파이프라인, 기록 없음 | **검사·기록 전무**(01 C2) | A/D 채널 — 정답·alias 어휘 동조 |

주의: `role_is_configured`(llm.py:235-259)는 role↔system **2자** 비교만 한다.
generator/judge 를 둘 다 같은 모델로 지정해도(OR 조건 build:1828) 통과 가능하고, **system
planner/HyDE 가 generator 와 같은 family인 3자 동일**은 어떤 게이트도 막지 못한다.

---

## B. 임베딩 공유 위험 (요약표)
| 질문 | 답 | 출처 |
|---|---|---|
| 골드 생성 임베딩 = 검색 임베딩? | **같음** (둘 다 `_build_embedding_client(config)`/`processor.embedding_model`) | build:1889-1891, eval:958-964 |
| 검색 인덱스 임베딩 = T4 채점 임베딩? | **같음** (같은 embedding_client) | eval:1031, eval:1038 |
| 임베딩 모델 ID 기록? | 양쪽 기록되나 **자동 대조 없음** | build:676, eval:1265 |
| type-무시+τ=0.65로 증폭? | 예 (B×F1 상승작용) | graph_match.py:33,305,330 |

---

## C. 청크/엔티티 ID 정답 매칭의 한계 (그래프)
- 골드 정답 = 인덱스 노드 `(name,type)`. 검색도 같은 노드 공간에서 결과 산출(A 채널).
- generator 는 노드 X 를 보고 질문 Q 를 만든다(synth.py GRAPH_GENERATE) → Q 에 X 의 답이
  있을 수밖에 없음(역방향 생성). 어휘 중첩이 크면 검색이 X 를 회수하기 쉽다.
- **신선도 검증 장치 점검:** paraphrase 강제 = 없음(프롬프트가 entity-name 그대로 허용,
  01 L2 synth.py:183). 다른 노드로도 답 가능한 질문 배제 = `is_unique_source`+distractor
  게이트 존재(synth.py:1008,1018)나 **query 기준**이라 entity-name 누설은 통과.
  type-변형 hold-out = 없음(type 을 인덱스 그대로 복사, build:1427 → T4 type-agnostic
  설계가 실측 미발동, 01 C1 권고2).
- 다중 골드셋이 같은 노드 집합에서 paraphrase 로 생성되면 std 는 검색 변동성이 아니라
  **paraphrase 변동성**을 잰다(02 F8). temperature 0.0 기본(synth.py:780)이면 그조차 작다.

---

## D. 평가-시스템 결합도
- eval 은 시스템 함수 `assemble_context_with_sources`(eval:388)를 직접 호출 — **운영과 동일
  코드 경로**(평가 전용 분기 없음). 단 tie-breaker 만 평가 레이어에서 재정렬(eval:413-416,
  운영 동작 무영향 주석). → "평가 결과가 운영서 재현"은 양호.
- 시스템 설정(top_k/rerank/hyde/graph_match_threshold/embedding_model)은 config_summary 에
  기록(eval:1260-1287) + index fingerprint(`graph_store_sha256` :1282). 추적성 양호.
- **결합 사각:** graph 메트릭은 bootstrap CI 에서 제외(02 F2, eval:778) → 결합도는 높은데
  불확실성 정량치가 없어 "재현은 되나 노이즈 구분 불가".

---

## E. 문서화된 의도 vs 코드 강제력
| 의도(docstring/주석) | 코드 강제? | 위치 |
|---|---|---|
| generator≠judge 권장 | **강제**(parser.error) | build:1828-1835 |
| generator≠추출 LLM 권장 | **없음**(기록조차 안 함) | 01 C2 |
| generator≠system planner | **없음** | C 채널 |
| 골드 임베딩=평가 임베딩 경고 | **없음**(기록만, 대조 안 함) | B 채널 |
| τ=0.65 "별도 validation 권장" | **없음**(자인만, 미검증 적용) | graph_match.py:36-39 |
| description graph_store 폴백 금지 | **강제**(빈 evidence→T4 skip) | build:1415-1423 |

→ 강제되는 것은 generator↔judge↔system **답변 LLM** 분리와 description 폴백 금지 둘뿐.
그래프 누설의 핵심(A 추출↔정답, B 임베딩 순환, C planner 동조, D alias 누설)은 **전부 무강제**.

---

## 시나리오 분석 (그래프 메트릭 신뢰성)

### 시나리오 1: retriever/planner만 변경, 인덱스 고정 (A/B)
- A(self-fitting): 양 arm 동일 인덱스 → T1 자명 통과 혜택 **대칭**(01:262).
- B(임베딩): 동일 모델이지만 양 arm 동일 → 채점 편향 대칭.
- E(브리지): planner 변경이 **retrieved 집합을 바꾸면** A 혜택이 비대칭화될 수 있음(단서).
- **판정: 조건부 YES.** 단서 — (a) tier 분해(T1~T4)로 surface(T1-T3) 변화 우선 판단(02 권고1),
  (b) graph N≥150 + cross-doc 동반(01 H1/H3), (c) per-query bootstrap CI 추가(02 F2),
  (d) 브리지/회수집합 변화가 큰 변경은 A 비대칭 점검. **절대값 인용 금지, 상대 방향만.**

### 시나리오 2: 인덱싱(추출 LLM/임베딩) 변경 A/B
- A: 정답 표기가 인덱스 유래(build:1426)인데 인덱스가 바뀌면 **골드 자체가 한 arm 에 더
  적합**. self-fitting 편향 **비대칭** → 차이 = 시스템 개선인지 self-fit 차인지 분리 불가.
- B: 임베딩 모델 교체 시 골드 description_embedding(생성 시 모델)과 평가 임베딩이 **불일치**
  가능 → cosine 의미 붕괴(02 F7 변형). 골드↔평가 임베딩 대조 코드 없음(B 추적 사각).
- **판정: NO.** 인덱싱/추출/임베딩을 바꾸는 A/B 의 graph entity 메트릭 비교는 **사용 금지**
  (01:264,275). 재생성한 골드셋으로 양 arm 을 각각 self-fit 시키는 것도 정답 분포가 달라져 부적합.

### 시나리오 3: 임베딩 모델 교체 (채점/검색 임베딩 변경)
- (1)골드 description_embedding 은 **생성 시점 모델로 이미 박힘**(build:1528). 평가 임베딩만
  바꾸면 g_emb(구모델) vs r_emb(신모델) cosine = **비교 불가 공간**(graph_match.py:314-330).
  단, g_emb 이 None 이면 lazy 재임베딩(graph_match.py:315-316)되어 신모델로 통일 — 즉
  **골드에 임베딩이 박혔는지 여부에 따라 동작이 갈린다**(재현성 함정, 02 §6).
- **판정: NO (조건부도 어려움).** 임베딩 교체 시 골드를 **재생성**해 description_embedding 을
  새 모델로 다시 박아야 의미. 그렇게 하면 시나리오 2(인덱싱 변경)와 같아져 비대칭 NO.

### 시나리오 4: 외부 벤치마크 (인간 라벨, self-fitting 없음)
- A/B/C/D 순환 모두 끊김(정답이 인덱스 유래가 아님). T1-T4 가 진짜 강건성을 측정.
- **판정: YES.** 단 — graph_match τ 와 임베딩 모델을 **고정·기록**(graph_match.py:33, eval:1265),
  T4 type-무시(02 F1)는 false-positive 흡수가 여전하므로 surface/fuzzy 분리 리포팅 권장.
  본 합성 골드셋은 외부 벤치마크 자격 없음 — A 채널이 살아 있기 때문.

| 시나리오 | 그래프 메트릭 신뢰? | 핵심 단서 |
|---|---|---|
| 1. retriever/planner만(인덱스 고정) | 조건부 YES | tier 분해+CI+N≥150+cross-doc, 회수집합 큰 변경 시 A 비대칭 점검 |
| 2. 인덱싱/추출 변경 | NO | self-fitting 비대칭, 골드 정답이 인덱스 유래 |
| 3. 임베딩 교체 | NO | g_emb/r_emb 공간 불일치, 골드 재생성 필요 → 2와 동일화 |
| 4. 외부 벤치마크 | YES | 합성 골드셋은 자격 없음; τ·임베딩 고정, surface/fuzzy 분리 |

---

## 단일 감사관이 놓친 경계면 위험 (통합으로 새로 드러남)

1. **C 채널 전체(generator↔system planner/HyDE LLM 동조):** 01 은 추출 LLM 만, 02 는 채점부만
   봤다. 검색 본체 `assemble`(eval:388-394)의 LLM이 generator 와 같은 family면 질의표현이
   동조해 A(self-fitting)를 질의 측에서 부스트한다. `role_is_configured`(llm.py:235)는 2자만
   비교, **3자 동일을 못 막는다.**

2. **B 순환의 트리거 = 골드↔평가 임베딩 대조 부재:** 02 F6 은 "검색=채점 임베딩 동일"을, 02 F7
   은 "라벨 간 임베딩 대조 부재"를 봤으나, **골드 생성 임베딩 vs 평가 임베딩 일치 검사**(B 순환을
   닫는 핵심)는 어디서도 검사 장치로 다뤄지지 않는다. 둘 다 기록은 되나(build:676, eval:1265)
   assert 가 없다.

3. **C2(추출↔generator)의 실제 피해 지점 재배치:** 01 은 C2 를 A(T1) 옆 별 항목으로 뒀으나,
   추출 LLM 동일성은 **T1(A)에 영향 없고**(name 은 인덱스서 복사) **T2/T4(D, alias/description
   누설)에 작용**한다. 인과를 바로잡으면 C2 의 우선 패치 대상은 누설 게이트(D)다.

4. **B×F1 상승작용 등급 상향:** 02 가 F6 을 Medium 으로 분류했으나, F1(type-무시 τ=0.65,
   Critical)과 같은 임베딩 공간에서 결합하면 false-positive 흡수가 구조적으로 커진다 →
   **F6 의 실질 등급은 High로 재평가**.

5. **E 채널(브리지가 self-fitting 대칭성 파괴):** 01 의 "동일 인덱스면 self-fit 대칭" 가정이
   retrieved 집합을 바꾸는 변경(브리지/graph weight/planner)에는 성립하지 않을 수 있다.
   시나리오 1 의 조건부 YES 에 "회수집합 큰 변경 시 A 비대칭 점검" 단서가 필요한 이유.

---

## 종합 판정

### 합성 골드셋 그래프 메트릭의 의사결정 사용 가능 범위
- **동일 인덱스/동일 추출 LLM/동일 임베딩** 위에서 retriever·planner·rerank 파라미터의
  **상대 A/B 방향성**(시나리오 1). 단 tier 분해 + per-query CI + N≥150 + cross-doc 동반 충족 시.
- cross-doc 모드의 **doc-level AND recall**(A 영향 상대적으로 적음, doc_id 기반).

### 절대 사용하면 안 되는 결론 유형
- graph entity recall **절대값**을 품질/SLA/외부 보고로 인용 (A self-fitting + B 순환 + F1 부풀림).
- **인덱싱/추출/임베딩을 바꾸는 A/B**의 graph entity 메트릭 비교 (시나리오 2/3, 편향 비대칭).
- 합성 골드셋을 **외부 벤치마크처럼** 강건성 증거로 사용 (A 채널이 살아 있음).
- N<100 graph 항목에서 5%p 미만 차이 (01 H1 0/1 채점 + 02 F2 CI 부재).
- T1 변화만으로 "의미 매칭 개선" 주장 (surface 회수율 변화일 뿐).

### 우선순위 개선 권고 (코드 패치 단위)
1. **[A/B 추적, CRITICAL]** 골드 metadata 에 graph 추출 LLM ID + `graph_gold_from_index:true`
   플래그 + 노드 fingerprint 기록(build:673-679 블록). eval 단계에서 **골드 `embedding_model`
   vs config `processor.embedding_model` 불일치/일치 양쪽을 경고** — 일치 시 "B 순환(self-similarity)
   위험", 불일치 시 "cosine 공간 불일치" 경고(eval:1265 직후 assert 추가).
2. **[C 채널, HIGH]** `role_is_configured` 를 **3자**(generator/judge/system) 동일성까지 검사로
   확장하고, generator==system(planner/HyDE) 시 metadata 에 `generator_is_system_planner:true`
   경고(llm.py:235-259 + build:1818-1828).
3. **[D 채널, HIGH]** `filter_question` 의 결정론 누설 게이트(synth.py:988,994)를
   **aliases / evidence_description 텍스트에도** 적용(build:1053 직전). 현재 query 만 검사.
4. **[B×F1, HIGH]** T4 surface(T1-T3)/fuzzy(T4) **분리 리포팅 강제**(eval:457 부근
   `graph_recall_surface@k` 별도 산출), τ 0.65→0.78 복원 또는 type-게이트 플래그(graph_match.py:330).
5. **[통계, CRITICAL]** graph_* bootstrap CI 제외 해제(eval:778), per-query CI 산출 후
   **델타가 두 라벨 CI 비중첩일 때만 "개선" 판정**(02 F2).
6. **[E 채널, MEDIUM]** graph A/B 시 `--enable-cross-doc` 필수화 + retrieved 집합 변화량을
   row 에 기록해 A 비대칭 위험을 사후 진단 가능케(브리지 on/off 영향 분리).
7. **[추적성, MEDIUM]** per-pair 매칭 증거(golden_key, retrieved_index, tier, cosine)를 row 에
   평탄화(02 F5) — T4 false-positive 와 B 순환을 spot-check 가능하게.

### 두 감사관과 상충 시 병기
- **F6 등급:** eval-script-auditor=Medium(02:27). 본 분석=High (B×F1 상승작용 + 3중 순환).
  양측 모두 "임베딩 분리 미강제"라는 사실엔 일치, 등급만 상이.
- **C2 피해 지점:** gold-set-auditor 는 C2 를 self-fitting(C1/A) 인접 항목으로 배치(01:56-68).
  본 분석은 C2 의 실질 피해를 T1(A)이 아닌 T2/T4(D)로 재배치. 사실관계(name=인덱스 복사,
  build:1426) 자체엔 합의, 인과 귀속만 상이.
