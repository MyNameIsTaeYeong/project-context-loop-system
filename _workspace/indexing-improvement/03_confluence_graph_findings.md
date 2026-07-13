# 03 · Confluence Graph Vocabulary 검토 (검토 전용 · 코드 수정 없음)

작성: confluence-graph-analyst
대상: `src/context_loop/processor/graph_vocabulary.py` 의 엔티티/관계 타입 **화이트리스트 제한**
범위: confluence_mcp 소스 관점. 이번 라운드는 **검토만** (권고는 방향만 제시).

---

## 요약

- **상위집합 보장(#1)**: 런타임상 세 추출기가 방출하는 모든 type 은 vocab 에 선언되어 있다(현행 일치). 다만 이를 지키는 **테스트가 실질적 가드가 아님** — `llm_body` 가드는 구조상 항상 통과(순환)하고, `body`/`ast_code` 가드는 실제 방출값이 아니라 **손으로 미러링한 상수 집합**을 비교한다. 즉 "테스트가 누락을 잡는다"는 docstring 주장(graph_vocabulary.py:11)이 실제로는 절반만 성립. (MED)
- **subset 필터(#2)**: `_has_source(entry, "llm_body")` 방식은 현재 어휘에서는 **안전**하다("body" 는 "llm_body" 의 substring 이 아니므로 오검출 없음). 그러나 자유서술 `source` 문자열을 기계 판독 태그로 재사용하는 설계라 **역방향 확장 시 함정**이 있다 — 훗날 `body_*_vocab()` 을 keyword `"body"` 로 추가하면 `"llm_body"` entry 까지 매칭되어 잘못된 superset 이 된다. (MED)
- **어휘 폭(#3)**: confluence 인덱싱 LLM 이 실제로 보는 것은 15/18 전체가 아니라 **llm_body subset = entity 7 / relation 9**. 이 subset 은 `concept` 를 만능 sink 로 쓰게 만들어 노이즈를 유발하고, 아키텍처 문서에 흔한 **datastore/event·message/environment/config** 축이 없다(과소). 동시에 `documents` vs `documented_in`, `depends_on`/`uses`/`calls`, `module`/`system`, `has_part`/`contains` 는 **의미 중복·모호**(과다·혼동). (MED, 일부 MED-HIGH)
- **검색 측 정렬(#4)**: **가장 중요한 발견**. 현행 `graph_search.py` 는 순수 임베딩 시딩 방식으로, `graph_vocabulary` 를 **전혀 import 하지 않는다**. `INTENT_TO_RELATIONS`, `format_entity_types_for_prompt`, `all_entity_type_names/all_relation_type_names` 는 **프로덕션 어디에서도 소비되지 않고 테스트에서만 참조**된다. 모듈 docstring/주석이 전제하는 "검색 LLM 플래너가 union 어휘를 본다"(graph_vocabulary.py:7, 160-162)는 **이미 삭제된 LLM 플래너를 가리키는 스테일 서술**이다. 드리프트(중복 선언) 위험은 없지만, 그 이유가 "검색이 어휘를 아예 안 쓰기 때문"이라 정렬 목표 자체가 미실현. (드리프트 LOW / 데드코드·문서정합 MED)
- **INTENT 커버리지**: 어떤 intent 에도 안 걸리는 relation = `has_attachment`, `has_attribute`, `imports`, `contains` (4종). entity type 은 INTENT 매핑이 아예 없음. 단 INTENT 자체가 데드코드라 실효 영향은 LOW.
- **실데이터 대조(#5)**: 로컬에 graph_store DB/산출물(`*.duckdb`/`*.sqlite`/`*.parquet` 등) **없음** → 실제 분포 대조 생략.

---

## 발견사항

### F-CG-01 · 상위집합 가드 테스트가 실질 가드가 아님 (MED)

**근거**
- `graph_vocabulary.py:9-11` docstring: "추출기들의 어휘를 상위집합으로 재선언 … 테스트가 누락을 잡는다."
- `llm_body_extractor.py:52-53`: `_DEFAULT_ENTITY_TYPES = llm_body_entity_type_names()` — vocab 에서 **파생**된 상수.
- `tests/test_processor/test_graph_vocabulary.py:43-46` (`test_llm_body_vocab_subset_of_vocabulary`): `set(_DEFAULT_ENTITY_TYPES) <= all_entity_type_names()` 를 검사. `_DEFAULT_*` 가 vocab 의 subset 에서 파생되므로 이 단언은 **구조상 항상 참**(vocab 의 subset ⊆ vocab). 실제 방출 타입을 검증하지 못함.
- `test_graph_vocabulary.py:57-64` (ast_code), `:71-74` (body): 기대 집합을 **하드코딩**하고 vocab 포함만 확인. `ast_code_extractor.to_graph_data`/`body_extractor` 의 **실제 방출값을 introspect 하지 않는다**. 추출기가 새 타입을 방출하기 시작해도(예: ast 가 Rust `trait`/`enum` 을 emit) 하드코딩 기대집합을 같이 고쳐야만 실패 → 사실상 회귀 감지 불가.
- 유일하게 실효적인 가드는 link_graph (`test_graph_vocabulary.py:37-38`) — `_KIND_TO_ENTITY_TYPE.values()` 라는 **실제 소스**를 읽는다.

**영향**: vocab 이 실제 방출의 상위집합이라는 보장이 "코드 리뷰 시 사람이 동기화"에 의존. LLM 플래너가 부활하거나 검색이 type 필터를 쓰게 되면 사각지대가 곧바로 위험이 됨.

**권고 방향(수정 금지)**: (a) ast/body 가드를 하드코딩 미러가 아니라 **추출기 상수/실제 출력에서 파생**하도록 재설계, (b) `test_llm_body_vocab_subset` 은 순환이므로, `_DEFAULT_*` 의 원천을 vocab 이 아닌 "추출기가 실제 프롬프트로 강제하는 타입"으로 두거나 최소한 순환임을 주석화.

---

### F-CG-02 · `source` 문자열 substring 필터의 잠재적 오검출 함정 (MED)

**근거**
- `graph_vocabulary.py:165-166` `_has_source`: `return keyword in entry.source` — 자유서술 `source`(예: `"link_graph + llm_body"`, `"body + llm_body"`, `"llm_body + ast_code"`)에 대한 **부분 문자열** 매칭.
- `:169-179`: llm_body subset 은 keyword `"llm_body"` 로 필터. 현재 어휘에서 `"llm_body"` 를 포함하는 entry = entity {person, api, concept, system, module, policy, team}, relation {depends_on, implements, calls, owned_by, supersedes, has_part, uses, provides, documented_in}. **"body" 는 "llm_body" 의 substring 이 아니므로 `body`-only entry 는 걸리지 않아 현행은 안전**.
- 함정은 **역방향**이다: `"body" in "llm_body + ast_code"` → **True**. 지금은 `"body"` keyword 를 쓰는 subset 헬퍼가 없어서 문제 없지만, 주석(`:158-159`)이 "추출기마다 subset … link_graph_builder, ast_code 등"을 예고하므로, 누군가 `body_*_vocab()` 을 `_has_source(e, "body")` 로 추가하면 **모든 llm_body entry 가 오검출**된다. `"ast"`, `"link"` keyword 도 동일 부류의 취약성.

**영향**: 현행 무해. 확장 시 인덱싱 LLM 프롬프트에 "추출 안 하는 타입"이 노출 → 스키마 오염(모듈 docstring 이 명시적으로 막으려던 위험, `:159-161`)이 재발.

**권고 방향(수정 금지)**: `source` 를 자유서술 문자열이 아니라 **명시적 태그 집합**(예: `sources: frozenset[str]`)으로 모델링하고 필터는 집합 멤버십으로. 최소한 `_has_source` 를 정확 토큰 분리 매칭으로.

---

### F-CG-03 · confluence 실효 어휘 폭 = 7 entity / 9 relation, `concept` sink 편중 + 도메인 축 결손 (MED)

**근거**
- 파이프라인이 confluence 문서에 대해 LLM 추출을 돌릴 때(`pipeline.py:467-501`) config 는 기본값(`LLMBodyExtractionConfig`) → `allowed_entity_types = _DEFAULT_ENTITY_TYPES`(=llm_body subset). 프롬프트에도 subset 만 노출(`llm_body_extractor.py:489-496`, `:324-331`). 즉 confluence LLM 이 보는 어휘는 15/18 전체가 아니라:
  - entity(7): `person, api, concept, system, module, policy, team`
  - relation(9): `depends_on, implements, calls, owned_by, supersedes, has_part, uses, provides, documented_in`
- 이 subset 에는 아키텍처/운영 문서에 흔한 축이 없다: **datastore/database/table**, **event/message/topic**, **environment(prod/staging)**, **config/설정 키**. 프롬프트가 "목록 밖 타입 금지"(`llm_body_extractor.py:114-125`)이므로 이런 대상은 전부 `concept`(만능 sink) 또는 `system`/`module` 로 흘러 **타입 특이성 상실 + 노이즈 노드 양산**. `concept` 정의 자체가 "추상 개념/표준/용어"(graph_vocabulary.py:47)로 가장 넓어 sink 로 남용되기 쉬움.
- 반면 git_code/ast 전용 5종(`function, class, method, struct, interface`)과 `imports/contains` 는 confluence subset 에서 제외되어 있어 **confluence 프롬프트에는 과다 노출되지 않음**(subset 메커니즘이 이 부분은 잘 막고 있음).

**중복·모호 타입 평가**
- `documents`(body 전용, 문서→api) vs `documented_in`(llm, A→B): 이름이 거의 대칭 역관계라 **LLM 혼동 위험 MED-HIGH**. 다만 `documents` 는 llm subset 에 없어 LLM 이 직접 방출은 못 하므로 실제 충돌은 검색·해석 단계에 국한.
- `depends_on` / `uses` / `calls`: 의미 대량 중복. 동일한 "의존"을 세 이름 중 아무거나로 태깅 → 각 relation 의 recall 희석. INTENT 매핑이 셋을 한 그룹으로 묶어 완화하려 했으나(`graph_vocabulary.py:107-108`) **검색이 INTENT 를 안 씀**(F-CG-04)이라 완화 미작동. (MED)
- `module` vs `system` 경계: system="외부 가시 서비스", module="내부 컴포넌트 **또는** 코드 파일/패키지"(graph_vocabulary.py:50-56). module 이 confluence 개념과 git 코드 파일을 **동일 type 으로 이중 사용** → GraphStore 의 `(name, type)` 병합(link_graph_builder.py:139-141, graph_store 병합 규칙)에서 doc 의 "auth" module 과 code 의 "auth" 패키지가 **의도치 않게 병합**될 수 있음. (MED)
- `has_part`(llm) vs `contains`(ast): 둘 다 포함관계의 다른 이름. 소스가 달라 방출 충돌은 없으나 검색 union 관점에서 동의어 2개. (LOW)

**영향**: confluence 그래프가 `concept` 편중 + 의존류 3분산으로 **신호 대비 노이즈 악화**, 타입 기반 필터/집계의 유용성 저하.

**권고 방향(수정 금지)**: (a) 도메인 근거가 서면 `datastore`·`event` 정도만 신중히 추가(폭 증가는 재현율↑·노이즈↑ 트레이드오프), (b) `depends_on/uses/calls` 는 **하나(예: depends_on)로 수렴**하거나 방향/서브타입 규칙 명문화, (c) `documents`/`documented_in` 은 하나로 통일 또는 정의를 프롬프트에서 대비시켜 혼동 차단, (d) `module` 의 코드 파일 의미를 별도 type 로 분리해 doc↔code 오병합 방지 검토. 실제 데이터 분포 확인 전에는 적용 금지(F-CG-05).

---

### F-CG-04 · 검색 경로가 vocab 을 소비하지 않음 — 정렬 목표·헬퍼·INTENT 가 데드코드 (검색정렬 미실현: MED)

**근거**
- `graph_search.py:1-16` 주석: 이전 LLM 플래너(`plan_graph_search`/`execute_graph_search`)를 제거하고 **순수 임베딩 시딩**으로 대체. `search_graph`(:53-154)는 쿼리 임베딩 → `search_entities_by_embedding` → 1-hop 확장뿐이며 **type/relation 어휘를 쓰지 않는다**(type-agnostic).
- src 전역 grep 결과 `graph_vocabulary` import 는 **`llm_body_extractor.py` 단 하나**(인덱싱 측). `graph_search.py` 및 `tests/test_processor/test_graph_search.py` 는 vocab 을 import 하지 않음(확인됨, 빈 결과).
- 따라서 `INTENT_TO_RELATIONS`(:106-121), `format_entity_types_for_prompt`(:129), `format_relation_types_for_prompt`(:134), `format_intent_mapping_for_prompt`(:139), `all_entity_type_names/all_relation_type_names`(:147-152) 는 **프로덕션 미소비, 테스트 전용**.
- 모듈 docstring `:5-7` 및 subset 주석 `:158-162`("검색 LLM 은 모든 subset 의 union 을 본다")는 **존재하지 않는 검색 LLM 을 전제** → 스테일 문서.

**드리프트 관점**: 질문이 우려한 "검색이 자체 어휘를 중복 선언해 드리프트" 는 **발생하지 않음** — 그러나 그 이유가 정렬(검색이 union 을 본다)이 아니라 **검색이 어휘를 전혀 안 쓰기 때문**. 즉 vocab 의 절반(검색 정렬용 API)은 목적을 상실.

**INTENT 커버리지**: 어떤 intent 에도 매핑 안 된 relation = `has_attachment`, `has_attribute`, `imports`, `contains`. entity type 은 INTENT 에 아예 미포함(관계만 매핑). INTENT 가 데드코드라 실효 LOW, 부활 시 커버리지 갭으로 승격.

**영향**: 런타임 위험 낮음. 유지보수 위험: 독자가 "검색이 vocab 으로 정렬된다"고 오해 → 잘못된 근거로 vocab 확장/축소. 죽은 API 가 F-CG-01 의 순환 테스트를 떠받쳐 "잘 관리되는 것처럼" 보이게 함.

**권고 방향(수정 금지)**: 둘 중 택1을 **의사결정**으로 명시 — (a) 검색용 헬퍼/INTENT/docstring 을 데드코드로 인정하고 제거·격리, 또는 (b) 검색을 vocab 소비로 재배선(예: 임베딩 시딩 후 INTENT 기반 relation 필터/부스팅). 어느 쪽이든 이번 라운드는 코드 변경 없이 방향만.

---

### F-CG-05 · 실데이터 대조 불가 (정보 · 심각도 없음)

로컬에 graph_store 영속 산출물(`*.duckdb`/`*.db`/`*.sqlite`/`*.parquet`/`graph*.json`) 이 **존재하지 않음**(git root 및 하위 검색, `_workspace`/테스트 제외). 따라서 실제 저장된 `entity_type`/`relation_type` 분포, 사용률 0 타입, 정의 밖 타입 유입 여부를 **경험적으로 확인하지 못함**. F-CG-03 의 폭 조정 권고는 반드시 실 분포 확인 후 진행 권장.

---

## 미검토 영역 / 경계

- **eval 영역**: `graph_search.py:24` 가 `context_loop.eval.gold_set` 의 `GraphEntityRef/GraphRelationRef` 를 결과 타입으로 사용. 평가 측 tiered matching(T4 embedding) 이 type 명 변경에 얼마나 robust 한지는 **별도 하네스 영역**으로 변경 권고 대상 아님. 다만 F-CG-03 의 type 통폐합을 실행할 경우 골드셋의 type 표기와의 정합을 그 하네스에서 별도 확인 필요(정보 공유용).
- **git_code 경로**: `ast_code_extractor` 의 방출 타입은 confluence subset 밖이라 본 검토의 초점은 아님. F-CG-01 의 하드코딩 가드 취약성은 git_code 그래프 분석가와 **공유 이슈**(같은 test_graph_vocabulary.py).
- **GraphStore 병합 규칙 상세**: `(name, entity_type)` 병합이 F-CG-03 의 `module` 오병합에 관여하나, 병합 로직 자체(`storage/graph_store.py`)는 confluence-chunking/graph 경계 밖 저장소 레이어 — 본 라운드 미정독.

## confluence-chunking-analyst 와의 충돌점

- 공통 파일: `pipeline.py`, `confluence_extractor.py`. 본 산출물은 그래프 방출/어휘만 다루며 청킹(ExtractionUnit 분할) 로직은 건드리지 않음. `_gate_units`(llm_body_extractor.py:450-468)가 `unit.token_count`/`split_total`/`split_part` 에 의존하므로, 청킹 측이 unit 경계·토큰 카운트를 바꾸면 LLM 게이팅 대상 집합이 달라져 **간접적으로 방출 어휘의 밀도**에 영향(정보 공유).
