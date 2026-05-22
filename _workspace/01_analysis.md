# 01_analysis — 골드셋 생성·채점 cross-doc / answer-equivalence 분석

작성: 2026-05-22 (analyst, eval-gold-set-improvement)
대상 HEAD: PR#64 (그래프 도달 문서 본문 첨부) 병합 후
분석 범위: R1(저장 문서 기준 질의), R2(cross-document), R3(answer equivalence set)

> 모든 주장은 코드 인용(`파일:라인`) 근거. 추측 없음.

---

## 0. 용어 충돌 경고 (designer 필독)

코드베이스에는 이미 "R1/R2/R3" 와 "cross-doc/equivalence" 라는 단어가 **다른
의미로** 존재한다. 요구사항(`_workspace/00_requirements.md`)의 R1/R2/R3 와
혼동 금지:

| 코드/파일의 기존 표기 | 코드 내 의미 | 요구사항 R1/R2/R3 와의 관계 |
|---|---|---|
| `scripts/diagnose_r3_effect.py` | "R3 = 가상 질문 인덱싱(hypothetical-question indexing)" 효과 진단 | **무관**. 요구사항 R3(answer equivalence)와 이름만 같음 |
| `context_assembler.py:216,339` 주석 `# R3:` | "R3 = document_id 단위 dedup" | **무관** |
| `build_synthetic_gold_set.py` docstring `(R1 — chunk + graph 평가)` | "R1 = chunk+graph 동시 평가" | 요구사항 R1(저장 문서 기준 질의)과 다름 |
| `scripts/compare_runs.py:42` `EQUIVALENCE_KEYS` | run config 동치성 비교 키 | **무관**. answer equivalence 아님 |
| `tests/test_storage/test_graph_store.py:728+` `test_cross_doc_*` | 엔티티 **병합**(같은 엔티티가 여러 문서 소유) | 요구사항 R2(cross-document 질의)와 무관 |

→ 요구사항 의미의 cross-document 질의 / answer-equivalence-set 개념은 **현재
코드에 전혀 없다**. 신규 도입이 필요하다.

---

## 1. GoldItem 스키마의 정답 표현 방식

정의: `src/context_loop/eval/gold_set.py:158-251` (`GoldItem`).

현재 정답 필드 (`gold_set.py:169-183`):

| 필드 | 타입 | 라인 | 정답 표현 |
|---|---|---|---|
| `relevant_doc_ids` | `list[int]` | 171 | **단일 평탄 리스트**. "이 목록의 모든 문서가 정답" |
| `relevant_graph_entities` | `list[GraphEntityRef]` | 172 | (name,type) 정답 엔티티 목록 |
| `relevant_graph_relations` | `list[GraphRelationRef]` | 173 | 정답 관계 목록 (옵셔널, `--score-relations`) |

핵심 사실:

- **동치 집합(equivalence set) 개념은 없다.** `relevant_doc_ids` 는 평탄한
  `list[int]` 한 개뿐이다 (`gold_set.py:171`). "이 중 아무거나 하나면 OK"인
  그룹을 표현할 수 있는 중첩 구조(`list[list[int]]` 또는 그룹 dict)가 없다.
- 채점 시 이 리스트는 그대로 `set()` 으로 변환되어 **모든 원소가 독립 정답**으로
  취급된다 (`eval_search.py:386` → `relevant = set(item.relevant_doc_ids)`).
  즉 `relevant_doc_ids=[3,5]` 는 "3과 5 **둘 다** 정답(둘 다 찾아야 recall=1.0)"
  이지, 요구사항 R3 의 "3 **또는** 5 중 하나면 OK"가 **아니다**.
- cross-document 식별 플래그도 없다. `_classify_mode`(`eval_search.py:547-560`)
  의 분류는 chunk / graph / hybrid 3종뿐이며, 정답 doc 개수와 무관하다.

YAML 입출력: `to_dict`(`gold_set.py:185-215`) / `from_dict`(`217-251`).
- `to_dict` 는 빈 필드를 omit (`186-215`) → 신규 옵셔널 필드 추가 시 라운드트립
  무손실 패턴을 그대로 따르면 된다.
- `from_dict` 는 누락 키를 기본값 처리 (`232` `d.get("relevant_doc_ids", [])`
  등) → **새 옵셔널 필드 추가 시 기존 YAML 자동 폴백 가능**.

---

## 2. R1 — 저장 문서 기준 질의 생성 (충족도: 이미 충족)

### 생성이 실제 인덱싱 문서를 참조하는가 — 예.

chunk 모드:
- 후보 청크는 `metadata_store` 에서 직접 로드 — `load_candidate_chunks`
  (`build_synthetic_gold_set.py:124-175`). `store.list_documents()`(148) →
  `store.get_chunks_by_document(doc["id"])`(155) 로 **실제 인덱싱된 문서·청크만**
  후보가 된다.
- 정답 매핑: `relevant_doc_ids=[chunk["document_id"]]`
  (`build_synthetic_gold_set.py:601` no-filter 경로, `637` filter 통과 경로).
  `chunk["document_id"]` 는 `load_candidate_chunks` 가 `doc["id"]` 에서 채운 값
  (`build_synthetic_gold_set.py:162`) → 실제 DB document.id 와 1:1.
- 디버그 출처: `source_document_id=chunk["document_id"]` (`604,640`),
  `source_text_anchor=anchor` (`606,642`, anchor 는 `make_text_anchor`, synth.py:230).

graph 모드:
- 후보 subgraph 는 `load_candidate_subgraphs`(`build_synthetic_gold_set.py:183-299`)
  가 `meta_store.get_all_graph_nodes()`(211) + 노드 소유 문서
  `meta_store.get_node_document_ids()`(226) 로 로드 → 실제 그래프 노드만.
- 정답 매핑: `_make_graph_gold_item` 의 `relevant_doc_ids=list(sg["document_ids"])`
  (`build_synthetic_gold_set.py:997`). `sg["document_ids"]` 는 노드 소유 문서 전체
  (`282`, `doc_ids` from `226`).

### 결론 R1 = 이미 충족.
질의 정답 doc_id 는 실제 인덱싱된 document.id 와 매핑된다 (`build:162,601,637,997`).
보강 불필요. 단, **graph 모드의 다중 doc 처리는 R3 와 충돌**(아래 2.1).

### 2.1 graph 모드 다중-doc 의 의미적 함정 (R3 와 직결)
`_make_graph_gold_item` 이 `relevant_doc_ids=list(sg["document_ids"])`
(`build:997`) 로 **노드 소유 문서 전체**를 정답에 넣는다. 같은 엔티티가 3개
문서에 등장하면 `relevant_doc_ids=[A,B,C]`. 현재 채점(`eval_search.py:386` →
`recall_at_k`)은 이를 "A,B,C 모두 찾아야 recall=1.0"으로 해석한다
(`metrics.py:26-35`, `len(top_k & rel_set) / len(rel_set)`).

그러나 의미상 이건 "A·B·C **중 어디서든** 그 엔티티 정보를 찾으면 됨" =
**answer equivalence set 의 전형**이다. 현재 스키마로는 이 의도를 표현 못 해서
graph 항목의 recall 이 구조적으로 과소평가된다. → R3 도입의 1차 수혜처.

---

## 3. R2 — cross-document 케이스 (충족도: 미충족)

### 현재 cross-document 질의 생성 경로 = 없음.

- chunk 모드: 질문은 **단일 청크 본문 1개**만 보고 생성된다
  (`generate_questions(chunk["content"], ...)`, `build:569` / `synth.py:690`).
  정답도 그 청크의 단일 문서 (`build:601,637`). 두 문서를 함께 봐야 답이
  가능한 질의를 만드는 로직이 없다.
- graph 모드: 질문은 **단일 엔티티 + 1-hop 이웃 snippet**에서 생성
  (`generate_graph_questions(sg, ...)`, `build:757` / `synth.py:729`). snippet 은
  `build_subgraph_snippet`(`synth.py:242-279`) 으로 엔티티 1개 중심.
  `relevant_doc_ids` 가 다중일 수 있으나(§2.1), 이는 **같은 엔티티가 여러 문서에
  걸친 것**이지 "문서 A 엔티티와 문서 B 엔티티 간 관계"를 묻는 cross-doc 질의가
  아니다. 또한 그 다중 doc 의 의미는 R3(equivalence)이지 R2(conjunction)가 아니다.

### cross-document 식별 필드 = 없음.
- `GoldItem` 에 "cross-document 여부" 플래그 없음 (`gold_set.py:169-183`).
- `_classify_mode`(`eval_search.py:547-560`)는 chunk/graph/hybrid 만 분류,
  cross-doc 분리 집계 불가.
- 메트릭 분리 집계는 mode 별로만 split 됨 (`write_summary`,
  `eval_search.py:663-685`, `rows_by_mode` = chunk/graph/hybrid). cross-doc 별
  split 슬롯 없음.

### PR#64 와의 연결 (R2 가 측정하려는 대상).
그래프 도달 문서를 sources 에 첨부하는 PR#64 로직은
`context_assembler.py:574-599` 에 존재: `_search_graph_sourced_chunks`(574)로
그래프 도달 문서 본문을 가져오고, `592-599` 에서 `graph_result.document_ids` 의
문서를 `Source(document_id=doc_id, ...)` 로 sources 에 추가한다. 이 doc_id 들이
`eval_search.py:385` `retrieved_doc_ids = [s.document_id for s in sorted_sources]`
에 그대로 들어가 doc-level recall 에 반영된다. → **채점 인프라는 그래프 도달
문서를 이미 retrieved 로 카운트**한다. R2 의 갭은 "그것을 정답으로 요구하는
cross-doc 질의가 골드셋에 없다"는 **생성** 쪽이다.

### 결론 R2 = 미충족.
cross-doc 질의 생성 로직(없음) + 식별 필드(없음) + 분리 집계(없음) 모두 신규.

---

## 4. R3 — Answer equivalence set (충족도: 미충족) + 채점 영향 범위

### 4.1 스키마 — equivalence set 표현 불가 (§1 참조).

### 4.2 채점이 정답을 카운트하는 정확한 지점 (전수)

doc-level 채점은 `eval_search.py:evaluate_one`(318-544)에 집중.

**진입점 (정답 set 생성):**
- `eval_search.py:386` `relevant = set(item.relevant_doc_ids)` — 평탄 set.
  여기가 동치-집합 도입 시 **반드시 바뀌어야 할 단일 진입점**. 현재 모든 메트릭이
  이 `relevant` 를 공유한다.

**`relevant` 를 소비하는 메트릭 호출 (eval_search.py):**
| 라인 | 메트릭 | 함수(`metrics.py`) |
|---|---|---|
| 409 | `recall@k` | `recall_at_k` (`metrics.py:26-35`) |
| 410 | `precision@k` | `precision_at_k` (`metrics.py:38-50`) |
| 411 | `hit@k` | `hit_at_k` (`metrics.py:90-93`) |
| 412 | `ndcg@k` | `ndcg_at_k` (`metrics.py:65-87`) |
| 413 | `mrr` | `mrr` (`metrics.py:53-62`) |
| 407 | `relevant_doc_ids` (CSV 기록) | — (출력용) |

**메트릭 함수 내부의 카운트 키 (metrics.py) — 동치 집합이 깨뜨리는 가정:**
- `recall_at_k`(26-35): `len(top_k & rel_set) / len(rel_set)`. **분모 `len(rel_set)`**
  가 정답 "개수". 동치 집합 `{3,5}` 가 1개 정답 단위여야 하는데 현재는 2로 셈 →
  **recall 분모 왜곡**. R3 핵심 수정 지점.
- `precision_at_k`(38-50): `hits / k`. 동치 집합 내 2개가 동시에 top-k 에 들면
  hits 가 2로 셈 → precision 부풀려짐. 동치 의미면 1로 캡해야 함.
- `mrr`(53-62): 첫 매칭 위치 역수. 동치 집합은 "집합 중 첫 매칭"이면 되므로
  현 로직(`r in rel_set`)이 **거의 호환** — 단 여러 동치 집합이 있을 때 "집합당
  첫 매칭"의 평균이어야 정확.
- `ndcg_at_k`(65-87): `idcg` 가 `min(len(rel_set), k)` 기반(83). `len(rel_set)`
  이 동치 단위로 줄면 idcg 도 바뀜 → 영향 받음.
- `hit_at_k`(90-93): `any(r in rel_set)`. 동치 의미와 **자연 호환**(하나라도 있으면
  True). 수정 거의 불필요.

**집계 단계 (영향 간접):**
- `aggregate`(`metrics.py:96-126`) / `aggregate_with_variance`(129-199): row 의
  숫자 메트릭 평균. 위 메트릭 값이 동치 의미로 바뀌면 자동 반영. **함수 자체는
  수정 불필요**(키 추가만 하면 됨).
- `write_summary`(`eval_search.py:644-742`): mode별 split(663-685). cross-doc
  분리 집계(R2) 추가 시 여기 `rows_by_mode` 확장 필요.

**graph-level 채점 (별도, R3 와 동일 패턴 위험):**
- `eval_search.py:416-439` `graph_recall@k` 등도 `entity_report.all_relevant_keys`
  를 `recall_at_k` 분모로 씀. 그래프 엔티티에도 "동치 엔티티(여러 표현 중 하나)"
  개념이 4-tier 매칭(`graph_match.py`)으로 부분 존재하나, equivalence-set
  단위 카운트는 아님. R3 를 graph 에도 적용할지는 designer 결정(§8).

### 4.3 동치 집합 도입 시 영향받는 함수 전부

| 파일:라인 | 함수 | 영향 | 수정 필요도 |
|---|---|---|---|
| `eval_search.py:386` | `evaluate_one` 진입 `relevant=set(...)` | 정답 표현 변환점 | **필수** |
| `eval_search.py:409-413` | 메트릭 호출 5개 | 새 동치-aware 메트릭으로 교체 | **필수** |
| `metrics.py:26-35` | `recall_at_k` | 분모=집합 수 | **필수**(또는 동치 wrapper) |
| `metrics.py:38-50` | `precision_at_k` | 집합 내 중복 캡 | **필수** |
| `metrics.py:65-87` | `ndcg_at_k` | idcg 분모 | **필수** |
| `metrics.py:53-62` | `mrr` | 집합당 첫 매칭 | 권장 |
| `metrics.py:90-93` | `hit_at_k` | 자연 호환 | 검토만 |
| `gold_set.py:171,185-251` | `GoldItem` 스키마 + to/from_dict | 새 필드 | **필수** |
| `build_synthetic_gold_set.py:601,637,997` | 정답 emit | 동치 집합 채우기(특히 graph §2.1) | R3 생성 시 |
| `eval_search.py:547-560` | `_classify_mode` | cross-doc 분류(R2) | R2 시 |
| `eval_search.py:663-685` | `write_summary` split | cross-doc 분리 집계(R2) | R2 시 |
| `eval_search.py:96-114` | `diagnose_r3_effect._parse_ids`/set 비교 | (외부 진단 스크립트) `relevant_doc_ids` 평탄 가정 | 호환 검토 |

> 설계 선택지: (a) `metrics.py` 함수를 동치-aware 로 직접 수정 vs
> (b) `eval_search.py` 에서 동치 집합을 "대표 1개로 축약한 retrieved/relevant"로
> 전처리 후 기존 메트릭 재사용. (b)가 backward-compat·테스트 영향 최소.

---

## 5. 하위 호환 제약 / 마이그레이션

### 5.1 기존 골드셋 포맷 (실측)
포맷은 **YAML** (요구사항 본문은 "JSON"이라 했으나 코드 실측은 YAML —
`gold_set.py:278-294` `yaml.safe_dump`/`safe_load`, 파일 확장자 `.yaml`).
designer 는 이 불일치를 인지할 것. `load_gold_set`(290-294)은 YAML 만 읽는다.

기존 항목 필드 (omit-if-empty 라 실제 YAML 에 존재하는 키):
`id`, `query`, `relevant_doc_ids`(항상), 선택적으로
`relevant_graph_entities`/`relevant_graph_relations`/`source_type`/
`source_document_id`/`source_text_anchor`/`source_section_path`/`difficulty`/
`synthesized`/`notes`/`source_chunk_id`(deprecated).
(`to_dict` `gold_set.py:185-215`)

### 5.2 폴백 메커니즘 (이미 견고)
- `from_dict`(`gold_set.py:217-251`)는 모든 필드를 `d.get(..., 기본값)` 으로 읽음.
  → 새 옵셔널 필드(`relevant_doc_groups` / `cross_document` 등) 누락 시 기본값으로
  로드. 기존 YAML 무수정 로드 가능.
- 요구사항 NFR "없으면 기존 단일-정답 채점으로 폴백" → `evaluate_one:386` 에서
  새 필드 부재 시 `relevant_doc_ids` 기반 기존 경로 유지하도록 분기하면 충족.

### 5.3 마이그레이션 고려사항
- **GraphRelationRef/GraphEntityRef 전례**가 좋은 마이그레이션 패턴: 옵셔널 필드를
  `to_dict` omit-if-empty + `from_dict` 기본값으로 추가한 선례(`gold_set.py:77-103`,
  `129-155`)를 그대로 따르면 무손실·하위호환.
- 메타데이터: `GoldSet.metadata`(`gold_set.py:260`)는 자유 dict → cross-doc/equiv
  생성 정책(LLM 사용 여부, 결정론 여부)을 metadata 에 기록하면 추적 가능
  (build 의 metadata 기록 패턴 `build:491-518`).
- **diagnose_r3_effect.py 호환**(`diagnose_r3_effect.py:96` `_parse_ids(b.get(
  "relevant_doc_ids"))`): CSV 의 `relevant_doc_ids` 컬럼을 평탄 int 리스트로 파싱.
  동치 집합 도입 후에도 CSV `relevant_doc_ids` 컬럼이 평탄 리스트로 유지되면
  이 스크립트는 깨지지 않음 — CSV 직렬화(`eval_search.py:407`) 형태 보존 권장.

---

## 6. R1/R2/R3 충족도 요약

| 요구사항 | 상태 | 근거 |
|---|---|---|
| **R1** 저장 문서 기준 질의 생성 + doc_id 매핑 | **이미 충족** | chunk: `build:148,155,162,601,637`; graph: `build:211,226,997`. 정답 doc_id = 실제 DB document.id |
| **R2** cross-document 케이스 생성 + 식별 필드 | **미충족** | 생성: 단일 청크/단일 엔티티만 입력(`build:569,757`); 식별 필드 없음(`gold_set.py:169-183`); 분리 집계 없음(`eval_search.py:547-560,663-685`) |
| **R3** answer equivalence set 스키마 + 채점 | **미충족** | 스키마: 평탄 `list[int]` 만(`gold_set.py:171`); 채점: `set(relevant_doc_ids)` 전원 정답(`eval_search.py:386`, `metrics.py:26-35`). graph 다중-doc 은 equivalence 의도이나 conjunction 으로 오채점(§2.1) |

부분 충족 사항:
- R3 의 채점 인프라(메트릭 함수, 집계, CSV)는 **존재**하나 의미가 conjunction.
- R2 의 측정 대상(그래프 도달 문서의 retrieved 카운트)은 PR#64 로 **이미 작동**
  (`context_assembler.py:574-599` → `eval_search.py:385`). 갭은 생성/식별/집계뿐.

---

## 7. 영향 범위 매트릭스

| 변경 영역 | 영향 파일 | 영향 테스트 | 마이그레이션 |
|---|---|---|---|
| GoldItem 동치/cross-doc 필드 | `eval/gold_set.py:169-251` | `tests/test_eval/test_gold_set.py`(roundtrip 17,34,48,60) | 옵셔널 추가 → 기존 YAML 무수정 |
| 동치-aware 메트릭 | `eval/metrics.py:26-87` | `tests/test_eval/test_metrics.py:20-114` | 기존 시그니처 보존 시 무영향 |
| 채점 진입 `relevant` 변환 | `scripts/eval_search.py:386,407-413` | (직접 테스트 없음 — 추가 필요) | CSV `relevant_doc_ids` 평탄 유지 권장 |
| cross-doc 분류·집계 | `eval_search.py:547-560,663-685` | (추가 필요) | metadata 정책 기록 |
| 생성: equiv/cross-doc | `build_synthetic_gold_set.py:601,637,997` + 신규 경로 | `tests/test_eval/test_build_synthetic_gold_set.py`, `test_synth.py` | metadata 기록 |
| 외부 진단 호환 | `scripts/diagnose_r3_effect.py:96-113` | (없음) | CSV 포맷 보존 |

---

## 8. 미해결 질문 (designer 에게)

1. **스키마 형태**: 동치 집합을 `relevant_doc_groups: list[list[int]]` (그룹별 OR,
   그룹 간 AND)로 할지, 아니면 그룹 객체(`{any_of: [...]}`)로 할지. 후자가 cross-doc
   AND 와 혼합 표현이 명확. R2(AND)와 R3(OR)를 한 스키마에서 어떻게 합성?
2. **graph 다중-doc 재해석**(§2.1): `_make_graph_gold_item:997` 의
   `list(sg["document_ids"])` 를 R3 동치 집합으로 자동 변환할지(권장) — 기존
   graph 골드셋 recall 의미가 바뀜. 마이그레이션/재생성 필요 여부.
3. **메트릭 구현 위치**: `metrics.py` 함수 직접 수정 vs `eval_search.py` 전처리
   (대표 1개 축약). 후자가 test_metrics 23개 테스트 무영향. 결정 필요.
4. **precision 동치 의미**: 동치 집합 내 2개가 top-k 에 동시 출현 시 hits=1 로
   캡할지(중복 정답은 1개 가치) vs 그대로 둘지. recall 과 일관성 위해 캡 권장.
5. **R2 생성의 LLM 의존**(NFR): 두 문서를 잇는 cross-doc 질의를 LLM 으로 생성할지
   (비용·재현성), 그래프 엣지(`graph_store.get_edges_between`, `build:250`)에서
   서로 다른 문서를 잇는 엣지를 **결정론적으로** 추출해 질의 시드를 만들지.
6. **요구사항 "JSON" vs 코드 "YAML"** 불일치(§5.1) — 어느 쪽 기준으로 진행?
7. **cross-doc 식별 필드 vs mode 확장**: `_classify_mode`(`eval_search.py:547`)에
   `cross_doc` mode 를 추가할지, 별도 boolean 플래그를 둘지.

---

요약: R1 은 코드상 이미 충족(`build:162,601,637,997`). R2(cross-doc 생성/식별/
집계)와 R3(equivalence set 스키마/채점)는 **전부 미충족** — 코드에 해당 개념 없음.
채점 변경의 단일 진입점은 `eval_search.py:386`, 메트릭 분모 왜곡 핵심은
`metrics.py:26-35,65-87`. 스키마 확장은 기존 `from_dict` 기본값 폴백
(`gold_set.py:217-251`)으로 하위호환 안전. graph 모드 다중-doc(`build:997`)은
이미 equivalence 의도이나 conjunction 으로 오채점되어 R3 의 1차 수혜처.
미해결 질문 7개, 영향 파일 6개.
