# Git Code Graph — 엔티티/관계 타입 정의 적합성 검토 (이번 라운드)

> **스코프**: 이번 라운드는 코드 변경 없이 **어휘(entity_type / relation_type) 정의의 적합성**만 검토한다.
> 어휘 단일 출처는 `src/context_loop/processor/graph_vocabulary.py`, AST 방출측은
> `src/context_loop/processor/ast_code_extractor.py::to_graph_data`, 검색측은
> `src/context_loop/processor/graph_search.py` + `src/context_loop/mcp/context_assembler.py` 이다.
> 이전 산출물(`indexing-improvement_prev_R1/04`, `_prev_2026-07-13/04`)의 구조·미해결 항목을
> 이어받되, 이번 스코프(어휘 적합성)에 맞춰 재정리·보완한다. 넘버링은 R1 의 F-GG-01~11 과
> 충돌하지 않도록 **F-GG-12 부터** 이어간다.

## 한 줄 요약

AST 가 실제 방출하는 타입(module/function/class/method/struct/interface + imports/contains)은
vocab 의 `ast_code` 태그와 **정확히 일치**한다(누락/과잉 없음). 그러나 (1) **코드 그래프의 핵심인
`calls`/상속/`defined_in` 관계가 아예 방출되지 않고**(어휘엔 있어도 git_code 경로가 채우지 않음),
(2) **module 타입이 파일·import·docstring·상수·LLM 컴포넌트에 과다 공유**되어 병합 경계가 모호하며,
(3) **검색측이 어휘를 전혀 소비하지 않는다**(INTENT_TO_RELATIONS·format_* 는 사라진
`graph_search_planner` 의 유산으로 dead, 현 검색은 엔티티 **이름 임베딩** 시딩만 사용) — 세 축에서
"정의는 맞지만 활용/커버리지가 비어 있다"가 핵심 결론이다.

---

## 검토 관점 1 — 정의-구현 정합성

### F-GG-12 (LOW / INFO) — AST 방출 타입은 vocab 의 ast_code 태그와 정확히 일치

- **근거**:
  - 방출 entity_type: `to_graph_data` 는 파일 노드 `"module"`(ast_code_extractor.py:227),
    import 모듈 `"module"`(241), 심볼 `sym.symbol_type`(256), 부모 클래스 `"class"`(270)만 생성.
    `sym.symbol_type` 의 값 도메인은 `function`(410)/`method`(438,674,829)/`class`(448,731 등)/
    `struct`(Go, `_DEFINITION_PATTERNS["go"]` 562-566)/`interface`(565,580)/`module`(docstring 359,
    constants 396, fallback 950).
  - 방출 relation_type: `"imports"`(302)와 `"contains"`(311) 두 종뿐.
  - vocab ast_code 태그: entity = module/function/class/method/struct/interface
    (graph_vocabulary.py:52-67), relation = imports/contains (97-98).
  - 테스트가 이 정합을 강제(tests/test_processor/test_graph_vocabulary.py:57-64).
- **판정**: **누락/과잉 없음.** 정의-구현 정합성은 양호하다.
- **개선 방향**: 없음(유지). 단, 아래 F-GG-13~16 의 "어휘엔 있으나 방출 안 됨"과 구분할 것 —
  이 항목은 "방출하는 것"에 한정한 일치 판정이다.

### F-GG-13 (MED) — `module` 타입 과부하: 파일·import·docstring·constants·fallback 이 모두 같은 타입

- **근거**: 하나의 `entity_type="module"` 로 방출되는 서로 다른 5종:
  1. 파일 자체 노드 — 이름 `file_title`(예 `user_service.py`), ast_code_extractor.py:227
  2. import 된 외부/내부 모듈 — 단순 이름(예 `logging`), 241
  3. 모듈 docstring 심볼 `__module__` — FQN `user_service.py::__module__`, 356-364 → 256
  4. 모듈 상수 묶음 심볼 `__constants__` — FQN `user_service.py::__constants__`, 392-400 → 256
  5. fallback 파일 전체 심볼(stem 이름) — 943-955 → 256
- **영향**: 검색·평가 결과에서 `(name, module)` 페어가 "파일인지 import 인지 상수 묶음인지"를
  타입만으로 구분 불가. 특히 (3)(4)는 심볼이지 "모듈"이 아님에도 module 로 태깅되어 의미 왜곡.
- **개선 방향(어휘 관점 제안, 구현 아님)**: docstring/constants 심볼에 별도 타입
  (`module_doc`, `constant`/`constant_group`) 부여 검토. 최소한 import 모듈과 파일 노드의
  구분자(예 파일 노드 property `is_file=true`)를 두어 병합 경계를 명확히.

---

## 검토 관점 2 — 타입 제한의 적합성(커버리지)

### F-GG-14 (HIGH) — `calls` 관계는 llm_body 전용 태깅인데 git_code 경로는 호출 관계를 전혀 추출하지 않음 → 코드 call-graph 부재

- **근거**:
  - vocab 에서 `calls` 는 `source="llm_body"` (graph_vocabulary.py:89). ast_code 태그 아님.
  - `to_graph_data` 는 `ast.Call` 을 순회하지 않는다 — relation 은 imports/contains 뿐
    (ast_code_extractor.py:290-312). 함수 호출 엣지 생성 코드가 존재하지 않음.
  - git_code 경로는 LLM 추출기를 호출하지 않는다: 파이프라인이 source_type 으로 분기되어
    git_code 는 `extract_code_symbols`/`to_chunks`/`to_graph_data` 만 타고(이전 R2 산출물
    §1.1 근거: pipeline.py:131-208), `llm_body_extractor`(= `calls` 를 만드는 유일한 추출기)는
    Confluence/upload 브랜치 전용.
  - 즉 **git_code 문서에는 `calls` 엣지가 구조적으로 0건.** `calls` 어휘는 Confluence LLM 추출에서만
    채워지므로, 코드 심볼 사이의 호출은 그래프에 없다.
- **영향**: "이 함수 어디서 호출돼?", "핸들러가 어떤 검증 로직을 부르나?" 류 질의를 코드 그래프로 답 불가.
  이는 R1 F-GG-04 의 재확인이며, 이번 어휘 스코프에서는 **"어휘에 calls 는 있으나 code source 로는
  방출 불가능하게 태깅되어 있다"** 는 정의-커버리지 불일치로 격상된다.
- **개선 방향**: `calls` 에 ast_code source 를 추가할지(= AST 로 같은-파일 호출 추출 도입)를
  결정해야 한다. 어휘만 놓고 보면 `calls` 를 llm_body 전용으로 못박은 것은 코드 그래프의 최대 결손.
  최소안: 같은-파일 정의 심볼 집합 대비 `ast.Call` 매칭(결정론)으로 `calls` 를 code source 로 승격.

### F-GG-15 (HIGH) — 상속/구현(`extends`/`inherits`/code-level `implements`) 엔티티가 아닌 관계로 없음

- **근거**:
  - Python `class Foo(Base)` 의 base 는 **시그니처 문자열**에만 존재(`_python_class_sig` 495-518)
    하고 그래프 엣지로는 방출 안 됨. brace 언어의 `extends`/`implements` 는 오히려
    `_RESERVED_SYMBOL_NAMES`(614-619)로 **버려진다**(심볼로도, 관계로도 안 잡음).
  - vocab 에 `inherits`/`extends` 자체가 없음. `implements` 는 있으나 llm_body 전용(88) →
    F-GG-14 와 같은 이유로 code source 로는 절대 방출되지 않음.
- **영향**: "이 클래스 상속 구조", "인터페이스 X 구현체" 질의를 그래프로 못 답함. 클래스 노드는
  contains(메서드)만 가진 섬이 되어 상속 계층 traversal 불가.
- **개선 방향**: `inherits`/`extends`(code) 관계 신설 + `implements` 에 ast_code source 추가.
  AST 로 결정론 추출 가능(Python `node.bases`, brace 정규식) — LLM 불필요.

### F-GG-16 (MED) — `defined_in`(심볼→파일) 부재로 파일 노드와 top-level 심볼이 단절

- **근거**: relation 은 imports(파일→모듈)와 contains(클래스→메서드)뿐(290-312).
  **top-level 함수/클래스와 파일 module 노드를 잇는 엣지가 없다.** 파일 노드의 1-hop 이웃은
  import 모듈들뿐이고, top-level 함수 노드는 이웃이 자기 자신뿐인 **고립 섬**이 된다.
- **영향(정량)**: `search_graph` 는 시드 노드 자신을 항상 포함(`_bidirectional_bfs` visited=set(sources),
  graph_store.py:415)하므로 top-level 함수가 시드로 뽑히면 **자기 자신은 반환된다**(= 완전 유실은 아님).
  그러나 (a) 파일 노드에서 그 파일의 함수로 내려갈 수 없고, (b) 함수에서 파일/형제 심볼로 확장 불가 →
  "이 파일/모듈에 뭐가 있나", 다중 홉 코드 질의의 연결성이 끊긴다. 서브그래프가 파편화되어
  graph context 텍스트가 빈약해진다.
- **개선 방향**: `contains`(파일 module → 소속 심볼) 또는 `defined_in`(심볼 → 파일) 엣지 신설.
  `contains` 를 재사용하면 새 어휘 없이 연결성 확보 가능(어휘 변경 최소).

### F-GG-17 (MED) — 언어별 커버리지 격차: enum / type alias / const·var / decorator 미포함

- **근거 (지원 언어: python/go/java/typescript/javascript, `_LANG_MAP` 86-94)**:
  - **enum**: Java 는 `enum` 이 class 패턴에 흡수되어 `"class"` 로 태깅(568-575, sym_type="class").
    TypeScript `enum X {}` 은 `_DEFINITION_PATTERNS["typescript"]`(577-581)에 패턴이 없어 **미추출**.
    Go `type X int` 상수형 enum 도 미추출. vocab 에 `enum` 타입 없음.
  - **type alias**: Python `X = int`/`TypeAlias` 는 `__constants__` 로 뭉뚱그려짐(module 타입).
    Go `type X = Y`, TS `type X = ...` 미추출. 전용 타입 없음.
  - **constant/variable**: Python 만 `__constants__` 로 수집(367-400)하되 타입은 `module`(F-GG-13).
    brace 언어의 top-level const/var 는 전부 미추출. vocab 에 `constant`/`variable` 없음.
  - **decorator**: `@app.route(...)` 는 body prefix 로만 포함(`_body_start_line` 479-492),
    엔티티/관계로는 없음. "이 데코레이터가 붙은 엔드포인트" 질의를 그래프로 못 답함.
- **영향**: Python 이 가장 풍부(docstring/constants/정확한 상속 시그니처), brace 언어는 함수/클래스/
  메서드/import 로 축소. TS enum/type, 모든 언어 decorator/const 는 그래프 사각지대.
- **개선 방향**: 우선순위 — TS `enum`/`type` 패턴 추가(현재 완전 누락), Java enum 을 `class` 와 분리,
  const 를 module 에서 분리. 어휘로는 `enum`, `constant`, (선택)`type_alias` 신설 검토.

### F-GG-18 (LOW) — `tests`(테스트→대상) 관계 부재

- **근거**: 테스트 파일도 일반 코드 파일로 파싱될 뿐, `test_foo → foo` 같은 관계 개념이 없다.
  파일명/함수명 규칙(`test_*`) 으로 결정론 추론 여지는 있으나 미구현. vocab 에 `tests` 없음.
- **영향**: "X 를 테스트하는 코드는?" 질의 미지원. 다만 코드베이스 사용 패턴상 우선순위는 낮음.
- **개선 방향**: 후순위. 도입 시 파일명/import 기반 휴리스틱 + `tests` 관계 신설.

---

## 검토 관점 3 — 타입 경계 모호성

### F-GG-19 (HIGH) — 공유 `module` 타입의 병합 경계 실패: AST 파일 노드 vs LLM 컴포넌트 노드가 수렴하지 않음

- **근거**:
  - 노드 병합 키는 `(entity_name 소문자, entity_type)` 완전 일치(graph_store.py:101,174,200-202).
  - AST 파일 노드 이름 = `file_title = Path(source_id).name`(ingestion/git_repository.py:426) →
    **확장자 포함 파일명**(예 `user_service.py`).
  - Confluence LLM module 노드 이름 = LLM 이 본문에서 뽑은 **자연어**(예 `Token Validator`,
    `user service`). vocab 설명은 `user_service.py` 를 예시로 들지만(graph_vocabulary.py:52-56),
    LLM 이 확장자 포함 파일명을 그대로 출력할 확률은 낮다.
  - 결과: 두 module 노드는 이름 규칙 차이(`user_service.py` vs `user service`/`User Service`)로
    **병합되지 않고 분리**된다. vocab 이 의도한 "같은 (name, type) 은 같은 노드로 수렴"이
    module 타입에서는 실현되지 않는다.
- **영향**: "코드 파일"과 "문서가 말하는 컴포넌트"가 그래프에서 다른 노드로 남아, 코드↔문서
  크로스링크(예 Confluence 설계문서 ↔ 실제 구현 파일)가 끊긴다. 병합이 우연히 성사되면 오히려
  이질적 두 개념이 한 노드로 붕괴하는 반대 위험도 존재(타입만으로 구분 불가, F-GG-13).
- **개선 방향**: (a) 코드 파일 노드에 전용 타입(`code_file`) 또는 property flag 를 부여해 LLM
  `module` 과 의도적으로 분리하고, (b) 코드↔문서 연결은 병합이 아닌 명시적 `documented_in`/
  `implements` 엣지로 잇는 설계를 검토. 최소한 "module 은 두 세계에서 뜻이 다르다"를 문서화.

### F-GG-20 (LOW) — method vs function 구분이 검색에 무익

- **근거**: 검색측 시딩(`search_entities_by_embedding`, graph_store.py:1027-1055)은 **entity_type 을
  필터/가중에 전혀 쓰지 않는다** — cosine 유사도만으로 top_k. 결과 조립(graph_search.py)에서도
  type 은 표시용 텍스트(`_format_text` 172)와 평가 페어 키에만 등장. 즉 method/function/struct/
  interface/class 세분화는 **retrieval 랭킹에 0 영향**, 순전히 표시/평가용.
- **영향**: 세분화 자체는 무해하나, 검색 이득은 없음. method↔function 을 나눈 정합성(F-GG-13 의
  parent 처리 로직)만 유지 비용으로 남는다.
- **개선 방향**: 유지해도 무방(표시·평가 가치 있음). 단 "검색 정확도 개선"을 노린다면 type 세분화가
  아니라 **type 기반 필터링/부스팅을 검색측에 도입**하는 쪽이 실효(관점 4 참조).

---

## 검토 관점 4 — 검색 측 정렬

### F-GG-21 (HIGH) — 검색측이 어휘를 소비하지 않음: INTENT_TO_RELATIONS·format_* 는 dead, 코드 의도 매핑도 없음

- **근거**:
  - 현 검색 경로는 `context_assembler._rerank_and_search_graph → search_graph`
    (mcp/context_assembler.py:20,499-534)로, **쿼리 임베딩 시딩 + 1-hop 확장**만 한다.
    LLM 플래너 없음(graph_search.py:1-16 도크스트링이 "LLM 호출 0회, plan/execute 폐기"를 명시).
  - `INTENT_TO_RELATIONS` / `format_intent_mapping_for_prompt` / `format_entity_types_for_prompt` /
    `format_relation_types_for_prompt` / `all_entity_type_names` / `all_relation_type_names` 는
    **src/scripts 어디에서도 호출되지 않는다**(테스트만 참조). 유일 소비자였던 `graph_search_planner`
    는 코드베이스에서 제거됨(파일 부재; tests/test_processor/test_graph_vocabulary.py:52 주석이
    아직 "graph_search_planner 의 LLM 가이드"를 근거로 든다 = 스테일).
  - 따라서 vocab 의 "검색 LLM 은 모든 subset 의 union 을 본다"(graph_vocabulary.py:159-162)와
    INTENT_TO_RELATIONS 전체가 **검색 관점에서 dead vocab**.
  - 게다가 INTENT_TO_RELATIONS(106-121)에는 **코드 의도 행이 하나도 없다** — "함수 호출/import
    관계/클래스 구조"가 없고, `imports`/`contains`/`calls` 를 가리키는 intent 항목이 전무. 코드
    질의를 relation 으로 매핑할 근거 자체가 비어 있다("API/엔드포인트" 행은 calls/provides/documents
    를 가리키지만 코드 그래프엔 이 엣지가 없음, F-GG-14).
- **영향**: 코드 질의("X 함수 어디서 호출돼?", "이 클래스 구조")는 (1) 관계 어휘 가이드의 도움을
  전혀 못 받고, (2) 애초에 calls/inherits 엣지가 없어(F-GG-14/15) traversal 로 답할 대상이 없다.
  결과적으로 코드 질의는 **엔티티 이름 임베딩 시딩의 우연한 매칭**에만 의존.
- **개선 방향**: 두 갈래. (a) INTENT_TO_RELATIONS 를 살릴 거면 검색측에 재연결하고 코드 의도 행
  (`함수 호출 → calls`, `import/의존 → imports, depends_on`, `클래스/구조 → contains, inherits`)을
  추가. (b) 재연결 안 할 거면 dead 임을 명시(문서화)하고 검색측 type/relation 기반 부스팅을 별도로 설계.
  어느 쪽이든 현재는 "어휘는 정의됐으나 검색이 안 씀"이 최대 미스얼라인.

### F-GG-22 (HIGH) — 엔티티 시드 임베딩이 **이름(FQN)만** 임베딩 → 코드 자연어 질의 매칭 취약

- **근거**: `build_entity_embeddings` 는 "모든 엔티티 **이름**의 임베딩"을 캐시(graph_store.py:913-,
  도크스트링 명시)하고, `_entity_embeddings[node_id] = (name, emb)` 로 이름만 벡터화.
  코드 심볼의 이름은 FQN 문자열(예 `user_service.py::UserService.__init__`, ast_code_extractor.py:195-207).
  signature/docstring(노드 property description)은 **시딩 임베딩에 들어가지 않는다**.
- **영향**: "결제 실패 재시도 정책 함수" 같은 자연어 코드 질의가 FQN 토큰 문자열과 저유사도 →
  코드 노드가 시드 threshold(0.5, graph_search.py:31)를 넘기 어렵다. entity_type/relation 세분화가
  아무리 정교해도 이 단계에서 코드 노드가 시드되지 못하면 그래프 섹션이 통째로 생략된다
  (search_graph 는 시드 0 이면 None 반환, 87-95).
- **개선 방향**: 어휘 스코프 밖이지만 강조 — 코드 노드는 이름 대신 `name + signature + docstring`
  (= `to_chunks` 의 embed_text, ast_code_extractor.py:184-190 와 동형)을 시딩 임베딩에 쓰는 것이
  코드 그래프 검색의 실효 개선. 이것이 없으면 어휘 개선 이득이 검색에 전달되지 않는다.

---

## 검토 관점 5 — 실데이터 근거(어휘 활용도)

### F-GG-23 (INFO) — 인덱싱된 그래프 저장소 부재로 실분포 집계 불가

- **근거**: 리포 트리에 `*.db`/`*.sqlite`/`*.duckdb` 등 인덱싱 산출 저장소가 없다(전체 스캔 결과 0건).
  `GraphStore` 는 런타임 in-memory NetworkX(`self._graph`)이며 영속 데이터가 커밋돼 있지 않다.
- **영향**: entity_type/relation_type 별 분포(dead vocab / 과밀 타입)를 **이번 라운드에서 실측 불가**.
- **집계 방법(인덱싱된 환경에서 실행 권장)**:
  - `GraphStore.stats()` 및 노드 순회로 type counter 를 만드는 경로가 이미 있음
    (graph_store.py:722-756, `summary["entity_types"]` = `type_counter.most_common()`; 837-860 은 문서별).
    이를 호출하거나, metadata_store 가 SQLite 로 영속화된 환경에서
    `SELECT entity_type, COUNT(*) FROM graph_nodes GROUP BY entity_type` /
    `SELECT relation_type, COUNT(*) FROM graph_edges GROUP BY relation_type` 로 직접 집계.
  - **정성 예측**(코드 근거 기반): git_code 그래프는 relation 이 imports/contains **2종에 100% 집중**
    (F-GG-14/15/16 로 calls/inherits/defined_in 이 0건)될 것이 확실. entity 는 function/method 가
    다수, module 이 파일·import·docstring·constants 로 과밀(F-GG-13), struct/interface 는 Go/TS
    비중에 따라 희소. `calls`/`implements`/`inherits`/`enum`/`constant`/`tests` 는 code source 기준
    **dead**(0건)로 예측된다.
- **개선 방향**: 개선 착수 전 실제 인덱스에서 위 쿼리로 예측을 검증할 것(1회성). 특히 module 과밀도와
  relation 2종 집중이 수치로 확인되면 F-GG-13/14/16 의 우선순위 근거가 된다.

---

## 우선순위 요약

| ID | 심각도 | 한줄 | 핵심 근거 |
|----|--------|------|-----------|
| F-GG-14 | HIGH | `calls` 가 llm_body 전용 → 코드 call-graph 0건 | vocab:89, ast:290-312, source 분기 |
| F-GG-15 | HIGH | 상속/구현 엣지 부재 (`inherits`/`extends`/code `implements`) | ast:495-518,614-619; vocab:88 |
| F-GG-19 | HIGH | `module` 병합 경계 실패 (AST 파일명 vs LLM 자연어) | graph_store:200-202, git_repository:426 |
| F-GG-21 | HIGH | 검색측이 어휘 미소비 (INTENT/format_* dead, 코드 intent 없음) | graph_search.py:1-16, 소비자 부재 |
| F-GG-22 | HIGH | 시드 임베딩이 이름(FQN)만 → 코드 자연어 질의 취약 | graph_store:913-; graph_search:31,87-95 |
| F-GG-13 | MED | `module` 타입 과부하(파일/import/docstring/constants/fallback) | ast:227,241,256,356,392,950 |
| F-GG-16 | MED | `defined_in`(심볼→파일) 부재 → top-level 심볼 고립 | ast:290-312; graph_store:415 |
| F-GG-17 | MED | 언어별 커버리지 격차(enum/type/const/decorator) | ast:568-581,367-400,479-492 |
| F-GG-12 | LOW | 방출 타입은 vocab 과 정확 일치(양호) | ast 전반; test:57-64 |
| F-GG-18 | LOW | `tests` 관계 부재 | — |
| F-GG-20 | LOW | method vs function 구분이 검색에 무익 | graph_store:1027-1055 |
| F-GG-23 | INFO | 인덱스 부재로 실분포 미집계(방법·예측 제시) | 전체 스캔 0건 |

## chunking-analyst 와의 공유/충돌점

- 같은 `ast_code_extractor.py` 를 보되 영역 분리: chunking = `to_chunks`(143-192)/embed_text,
  graph = `to_graph_data`(210-314). **F-GG-22 는 교차점** — 코드 노드 시딩 임베딩을 `to_chunks`
  의 embed_text(name+sig+docstring) 와 동형으로 맞추자는 제안이므로, chunking 측이 정의한 embed_text
  포맷을 그래프 시딩이 재사용하면 두 영역이 정렬된다. 충돌 없음(상호 보완).
