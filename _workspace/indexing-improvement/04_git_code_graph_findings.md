# 04. git_code 그래프 어휘(graph_vocabulary) 검토 — git-code-graph-analyst

- 라운드: **검토 전용 (코드 수정 금지)**
- 대상: `src/context_loop/processor/graph_vocabulary.py` 의 어휘 화이트리스트가
  git_code(AST) 관점에서 적절한가.
- 방법: `graph_vocabulary.py` ↔ `ast_code_extractor.py:to_graph_data` ↔
  `tests/test_processor/test_graph_vocabulary.py` ↔ `graph_search.py` ↔
  `graph_store.py` 정합 대조 + 실제 emit 타입 실행 확인(Python/Java/TS 샘플).
- 근거 표기: `파일:라인`. 심각도: HIGH / MED / LOW. **권고는 방향만 (수정 금지).**

실행 대조(실측):
- Python 샘플 → emit entity_type = {module, function, class, method}, relation = {imports, contains}
- Java 샘플 → `interface Foo` 및 `enum Color` 모두 **`class`** 로 emit, `run` → method
- TS 샘플 → `interface I` → `interface`, `enum E` → **미추출**, `m` → method

---

## 요약 (심각도순)

| ID | 심각도 | 한 줄 |
|----|--------|-------|
| F-GG-05 | HIGH | AST 관계는 `imports`/`contains` 2종뿐 — 상속/구현/호출 엣지 전무. `calls`/`implements`/`depends_on` 은 llm_body 전용이라 코드 그래프에선 방출되지 않음 |
| F-GG-06 | HIGH | 코드 intent용 `INTENT_TO_RELATIONS` 항목 없음 + 매핑/전체 포매터가 **소비처 0** (graph_search 는 순수 임베딩 시딩, 플래너 부재) — 어휘의 intent 가이드가 사문화 |
| F-GG-02 | MED | `test_ast_code_vocab_subset_of_vocabulary` 가 하드코딩 상수 대조라 실제 `to_graph_data` 출력 드리프트(새 타입 emit)를 못 잡음 |
| F-GG-03 | MED | `module` 타입이 파일/import모듈/모듈docstring-pseudo/상수-pseudo 4종을 겸함 — 어휘 설명 대비 과부하, `constant`/`variable`/`type_alias` 어휘 없음 |
| F-GG-04 | MED | Java `interface`/`enum` 이 `class` 로 emit → 어휘의 "interface(Java)" 설명 부정확, `enum` 어휘 부재, TS `enum` 미추출 |
| F-GG-07 | MED | `module` 이중 소속(llm_body+ast_code)의 실효 병합 거의 안 일어남(명명 규칙 상이) — 표기상 union 일 뿐, 드문 오병합 위험만 잔존 |
| F-GG-01 | LOW | (확인) coarse 레벨에선 선언 6종 ↔ emit 6종이 정확히 일치 — 미선언 emit / 미emit 선언 없음 |
| F-GG-08 | LOW | 크로스파일 Go struct/interface 가 메서드 parent 로 등록될 때 `class` 로 강제 — 타입 소실 엣지케이스 |

---

## F-GG-01 (LOW) — coarse 레벨 정합은 성립: 미선언 emit / 미emit 선언 없음

**근거**
- 선언(ast_code): `graph_vocabulary.py:60-66` = function/class/method/struct/interface
  + `module`(llm_body+ast_code, 51-56). relation `imports`(97)/`contains`(98).
- 실제 emit(`to_graph_data`): entity_type = `sym.symbol_type`
  (`ast_code_extractor.py:257`) + 파일/import `module`(228,241) + parent `class`(271).
  symbol_type 발생원: python module/function/class/method
  (`ast_code_extractor.py:359,393,410,437,448`), brace go
  function/struct/interface(`562-566`)·method(`682`), java class/function(`567-576`),
  ts function/class/interface(`577-581`), js function/class(`582-585`), fallback
  module(`949`).
- 실측: Python/Java/TS 3샘플 모두 emit 집합이 선언 6종의 부분집합.

**판정**: 화이트리스트 자체는 "방출되는데 미선언" 또는 "선언됐는데 미방출"
타입이 **coarse 레벨에서는 없음**. 아래 F-GG-02~04 는 그 아래 층(테스트 강제력,
타입 의미 과부하, 언어별 실제 라벨)의 문제.

**권고 방향**: 유지. 단 F-GG-02~04 를 함께 볼 것.

---

## F-GG-05 (HIGH) — relation 폭 부족: 상속/구현/호출 엣지 전무

**근거**
- `to_graph_data` 가 만드는 relation 은 `imports`(`ast_code_extractor.py:298-303`)
  와 `contains`(`308-312`) **두 종뿐**. 실측 확인.
- 상속/구현: `_python_class_sig`(`495-518`) 가 base/keyword 를 **시그니처 문자열**
  로만 보존(`class UserService(BaseService, ...)`). 엣지로는 승격 안 됨 →
  "무엇이 X 를 상속/구현하나" 질의는 그래프 탐색으로 불가.
- 호출: AST 추출기는 함수 호출 파싱 자체가 없음. `calls` 는
  `graph_vocabulary.py:89` 에서 **llm_body 전용**으로 선언 → git_code 문서는
  llm_body 를 타지 않으므로(`pipeline.py:162-238` 의 git_code 분기는 AST만 사용)
  코드 그래프엔 `calls` 엣지가 존재할 수 없음.
- `implements`(`graph_vocabulary.py:88`)·`depends_on`(87) 도 llm_body 전용.

**영향**: 그래프 질의 "이 함수 어디서 호출돼?", "이 인터페이스 누가 구현?",
"이 클래스가 상속하는 base?" 는 git_code 그래프만으로는 답 불가. 코드 그래프는
사실상 파일↔import(모듈 의존)와 클래스↔메서드(소속) 2관계로 축소됨.

**권고 방향**: (수정 금지) 상속(`extends`/`inherits`)·구현(`implements` 를
ast_code source 로 확장) 엣지를 base/interface 이름에서 도출하는 것과, 호출
엣지(정적 call 추출)의 비용/정확도 트레이드오프를 설계 단계에서 판단.
최소한 어휘 `source` 필드에 "코드 상속/구현/호출은 현재 미추출"을 명시해
소비자(플래너·평가)가 부재를 전제하도록.

---

## F-GG-06 (HIGH) — 코드 intent 가이드 부재 + intent 매핑 자체가 사문화

**근거 (a) 코드 intent 누락**
- `INTENT_TO_RELATIONS`(`graph_vocabulary.py:106-121`) 7개 intent 중
  `imports`/`contains` 를 참조하는 항목이 **하나도 없음**. "이 함수 어디서 import
  되나", "이 클래스가 포함하는 메서드" 같은 코드 질의에 대응 relation 가이드가 없음.
- "이 함수 어디서 호출돼?" 는 `calls` 로 매핑(107행 "의존 관계")되지만 F-GG-05
  대로 git_code 그래프엔 `calls` 엣지가 없어 매핑이 공회전.

**근거 (b) 그 매핑을 쓰는 소비자가 없음 (더 근본)**
- `graph_vocabulary` 를 import 하는 프로덕션 코드는 `llm_body_extractor.py:29-34`
  **한 곳뿐**이고, 거기서도 `llm_body_*` subset 헬퍼만 사용.
- `format_intent_mapping_for_prompt`, `INTENT_TO_RELATIONS`,
  `format_relation_types_for_prompt`, `format_entity_types_for_prompt`,
  `all_entity_type_names`, `all_relation_type_names` 는 `src/`·`scripts/` 전체에서
  **호출처 0** (테스트 전용). grep 확인.
- `graph_search.py` 는 "LLM 호출 0회, fallback 0층"의 **순수 임베딩 시딩**
  구조이며(`graph_search.py:1-16, 87-89`), 과거 LLM 플래너
  (`plan_graph_search`)는 제거됨(`graph_search.py:9-13` 주석). 즉 task 가 가정한
  "graph_search.py 플래너"는 현재 존재하지 않는다.

**영향**: intent→relation 가이드는 현재 어떤 검색 경로에도 주입되지 않는다.
코드 intent 항목을 추가해도 지금 구조에선 검색 품질에 영향이 없음. 반대로 이는
"어휘가 검색을 안내한다"는 모듈 docstring(`graph_vocabulary.py:4-7`)의 전제가
llm_body 인덱싱 프롬프트에만 국한됨을 뜻함.

**권고 방향**: (수정 금지) 둘 중 하나로 정합화 —
(1) intent 매핑/전체 포매터를 실제 검색 경로에서 소비하도록 재배선하고 그때
코드 intent(`imports`/`contains` 및 향후 상속/호출)를 추가하거나,
(2) 소비처 없는 API 를 "미사용/향후용"으로 명시해 유지보수 기대치를 낮춤.
어느 쪽이든 "코드 질의는 임베딩 시딩만으로 처리"라는 현 사실을 문서화.

---

## F-GG-02 (MED) — ast_code 어휘 테스트가 실제 방출을 강제하지 못함

**근거**
- `test_ast_code_vocab_subset_of_vocabulary`
  (`tests/test_processor/test_graph_vocabulary.py:49-64`) 는
  `expected_etypes = {"module","function","class","method","struct","interface"}`
  라는 **손으로 쓴 상수**가 `all_entity_type_names()` 의 부분집합인지만 검사.
  `to_graph_data`/`extract_code_symbols` 를 **호출하지 않음**.
- 따라서 추출기가 새 `symbol_type`(예: `enum`, `constant`)을 emit 하기 시작해도
  이 테스트는 실패하지 않음 — "추출기 확장 시 vocab 갱신을 잊으면 잡는다"는
  모듈 docstring(`graph_vocabulary.py:10-11`)·테스트 docstring(50-55)의 의도를
  실제로는 보장하지 못함. link_graph/llm_body 테스트(33-46, 112-134)는 추출기
  상수를 직접 import 해 대조하지만 ast_code 만 상수 대조 부재(추출기 쪽에 노출된
  "emit 타입 상수"가 없기 때문).

**커버 못 하는 항목**: Java `enum`/`interface`→`class` 붕괴(F-GG-04),
`module` 과부하(F-GG-03), struct/interface 가 실제로 emit 되는지 여부.

**권고 방향**: (수정 금지) 추출기가 emit 하는 symbol_type 집합을 소형 픽스처로
`to_graph_data` 를 돌려 산출→vocab 부분집합 검증하는 형태(실제 출력 기반)로
바꾸면 드리프트를 잡는다. 또는 추출기 측에 `EMITTED_ENTITY_TYPES` 상수를 노출해
link_graph/llm_body 와 동일한 "상수 직접 대조" 패턴으로 통일.

---

## F-GG-03 (MED) — `module` 타입 과부하 + constant/variable/type_alias 어휘 부재

**근거 (실측 entity 목록, Python 샘플)**
```
('user_service.py', 'module')                 # 파일 엔티티
('logging', 'module'), ('a.b', 'module')      # import 모듈
('user_service.py::__module__', 'module')     # 모듈 docstring pseudo-symbol
('user_service.py::__constants__', 'module')  # 모듈 레벨 상수 묶음 pseudo
```
- 파일 엔티티(`ast_code_extractor.py:225-229`), import 모듈(`239-243`),
  그리고 python `__module__` docstring(`356-364`, symbol_type="module")·
  `__constants__`(`392-400`, symbol_type="module") 이 **모두 `module`** 로 병합됨.
- 어휘 정의는 `module` = "시스템 내부 컴포넌트 또는 코드 파일/패키지"
  (`graph_vocabulary.py:52-56`) — 상수/모듈docstring 은 이 정의에 안 맞음.
- 상수(`MAX_RETRIES` 등)는 검색 대상으로 명시되었으나(`ast_code_extractor.py:334-340`)
  전용 어휘(`constant`/`variable`)가 없어 `module` 로 흡수 → entity_type 기준
  필터/가이드가 상수와 파일을 구분 못함. 타입 alias 도 상수 묶음에 섞임(`366`).

**영향**: entity_type 별 질의/가이드(예: "module 노드만")가 파일·외부의존·상수·
docstring 을 뭉뚱그림. 검색 정밀도·플래너 가이드 해상도 저하. F-GG-07 의 오병합
표면적도 넓힘.

**권고 방향**: (수정 금지) `constant`(또는 `variable`) 어휘 신설 + 상수/모듈
docstring 심볼의 entity_type 분리 검토. 최소한 `module` 설명에 "파일·외부 import·
모듈 상수·모듈 docstring 을 포괄"함을 명시해 소비자 오해 방지.

---

## F-GG-04 (MED) — Java interface/enum 이 `class` 로 emit, `enum` 어휘 부재, TS enum 미추출

**근거 (실측)**
- Java: `interface Foo`, `enum Color` 모두 `class` 로 emit
  (`ast_code_extractor.py:567-571` 의 java 패턴이 `class|interface|enum` 을 한
  덩어리로 잡아 sym_type 을 일괄 `"class"` 로 지정).
- TS: `interface I`→`interface`(정상, `580`), 그러나 `enum E` 는 패턴 부재로
  **미추출**(TS 패턴 목록 `577-581` 에 enum 없음).
- 어휘: `interface` 설명이 "Go/TypeScript/**Java** 인터페이스"
  (`graph_vocabulary.py:65-66`) 지만 실제로 Java 는 `interface` 를 **한 번도
  emit 하지 않음**(전부 class). `enum` 은 어휘에 아예 없음.

**영향**:
- 어휘 설명이 실제 emit 과 불일치(Java interface). 소비자가 "interface 노드"로
  Java 인터페이스를 찾으면 0건.
- Java enum·Java interface 가 class 노드에 섞여 타입 구분 소실. TS enum 은 그래프·
  청크에서 완전 누락(그래프 노드화 자체 안 됨).

**권고 방향**: (수정 금지) 두 방향 중 택1을 설계에서 —
(a) 추출기를 Java interface→`interface`, enum→신설 `enum` 으로 세분화하고 어휘에
`enum` 추가, 또는 (b) 세분화가 과하면 어휘 `interface` 설명에서 Java 를 빼고
"Java 는 class 로 표기됨"을 명시. TS enum 미추출은 청킹 커버리지 이슈로
git-code-chunking-analyst 와 공유(패턴 확장 필요).

---

## F-GG-07 (MED) — `module` 이중 소속(llm_body+ast_code)의 실효 병합은 거의 없음

**근거**
- graph_store 병합 키 = `normalize_entity_name(name)` + `entity_type`
  (`graph_store.py:197-204`). 정규화는 공백/`-`/`_` 제거 + casefold 이며 **`.` 는
  보존**(`entity_normalizer.py:35,78-82`).
- ast_code `module` 명명: 파일 basename(`git_repository.py:426` `title=Path(...).name`
  → `user_service.py`), import 모듈은 dotted path(`a.b`, `logging`),
  pseudo 는 `file::__module__`. 즉 **`.py` 접미/점표기/`::` scope** 를 가진 기계적 이름.
- llm_body `module` 명명: LLM 이 본문(Confluence)에서 뽑은 자연어 컴포넌트명
  (어휘 예시 "Token Validator", `graph_vocabulary.py:53-54`).
- 정규화 후 비교: `user_service.py`→`userservice.py`, LLM "User Service"→`userservice`
  (`.py` 없음) → **불일치**. `.` 미제거로 파일명과 산문명은 구조적으로 안 합쳐짐.
  실효 병합은 LLM 이 정확히 `user_service.py` 라고 파일명을 쓸 때만 성립(희귀).

**영향/위험**
- 이중 소속 선언은 표기상 union 일 뿐, 실제 (name,type) 병합은 드묾 → "두 추출기
  결과가 한 노드로 수렴"한다는 모듈 docstring(`graph_vocabulary.py:4-6`) 전제가
  module 에 대해선 약함.
- 낮지만 존재하는 오병합: LLM 이 컴포넌트를 `logging`/`system`(소문자) 등
  import 모듈명과 우연히 동일하게 명명하면 import 노드와 병합되어 크로스소스
  document_ids 가 섞임(`graph_store.py:217-220`). `system` 은 별도 type 이라 안전,
  하지만 `module` 동명은 가능.

**권고 방향**: (수정 금지) module 병합을 실제로 노리려면 ast_code 파일 엔티티를
정규화 정합 가능한 논리명으로 맞추거나(예: 확장자 제거), 반대로 코드 파일
module 과 산문 컴포넌트 module 을 **다른 type 으로 분리**해 오병합 여지를 없앨지
설계에서 결정. 현 상태 유지 시 "module 이중소속은 명목상"임을 어휘 주석에 명시.

---

## F-GG-08 (LOW) — 크로스파일 struct/interface parent 가 `class` 로 강제

**근거**
- parent 엔티티 등록 루프(`ast_code_extractor.py:262-273`)는 parent 가 아직
  top-level 심볼로 등록 안 됐으면 **무조건 `entity_type="class"`** 로 추가(271).
- 같은 파일에 `type X struct{}` 가 있으면 struct 가 먼저 struct 타입으로 등록되어
  중복 회피(주석 245-248 의도대로). 그러나 Go 메서드만 있고 struct 정의가 **다른
  파일**에 있으면 parent_fqn 미등록 → `class` 로 등록되어 struct/interface 타입 소실.

**영향**: 크로스파일 리시버 메서드에서 parent 타입이 struct→class 로 왜곡. 발생
빈도 낮고 두 타입 모두 어휘 내라 어휘 위반은 아님 — 타입 정밀도만 손실.

**권고 방향**: (수정 금지) parent_signature(`type X struct`)로 struct/interface 를
추론해 타입을 보존하는 개선 여지. 우선순위 낮음.

---

## 점검 항목별 결론 매핑

1. **ast_code 어휘 정합**: coarse 일치(F-GG-01). 단 테스트 강제력 약함(F-GG-02),
   Java interface/enum→class 라 어휘 설명과 세부 불일치(F-GG-04).
2. **언어 커버리지**: `enum` 어휘 부재·Java enum/interface 붕괴·TS enum 미추출
   (F-GG-04), 상수/변수/type_alias 어휘 부재로 `module` 과부하(F-GG-03).
3. **relation 폭**: imports/contains 2종뿐, 상속·구현·호출 엣지 전무(F-GG-05).
4. **module 이중 소속**: 명명 규칙 상이로 실효 병합 희귀, 명목상 union(F-GG-07).
5. **검색 측 정렬**: 코드 intent 항목 없음 + intent 매핑/포매터가 소비처 0,
   플래너 부재(F-GG-06).
6. **실데이터 대조**: 로컬 graph_store 산출물(`*.db`/graph artifact) **미발견** →
   entity_type/relation_type 실분포 대조 **생략**. 대신 추출기 실행으로 emit 타입을
   실측 대조함(위 각 항목).

## 범위 밖 (별도 하네스 영역)
- 평가/골드셋(`eval/*`, `build_synthetic_gold_set.py`, `eval_search.py`)의 graph
  채점·매칭은 **별도 하네스 영역**으로 본 보고서 범위에서 제외.

## git-code-chunking-analyst 와의 충돌/공유점
- 같은 `ast_code_extractor.py` 를 봄. **공유 이슈**: TS enum 미추출·Java enum/
  interface 붕괴(F-GG-04)는 청킹 커버리지와 그래프 노드화 양쪽에 영향 —
  청킹 패턴 확장이 이뤄지면 그래프 어휘(`enum` 등)도 동반 갱신 필요.
- `module` pseudo-symbol(`__module__`/`__constants__`)은 청킹 측 결정(상수/docstring
  을 심볼로 승격)이 그래프 어휘 과부하(F-GG-03)로 파급된 사례 — 청킹 설계 변경 시
  그래프 어휘와 협의 필요.
