# Confluence Graph — 엔티티/관계 타입 정의 적합성 검토

> 라운드 스코프: `graph_vocabulary.py` 를 단일 출처로 하는 entity_type / relation_type **정의의 적합성**만 검토.
> 대상 경로: `link_graph_builder` / `body_extractor` / `llm_body_extractor` (confluence_mcp 그래프 추출) + 검색 소비자 `graph_search.py`.
> 코드 변경 없음(분석만). F-CG-NN 형식.

---

## 요약

- 총 발견 **9건** (HIGH 4, MED 4, LOW 1).
- 핵심 결론 5가지:
  1. **정의-구현 정합성은 대체로 OK** — `_KIND_TO_*`, `_DEFAULT_*`(=llm_body subset), body_extractor 4종은 모두 vocab 의 진짜 subset이고 테스트가 이를 강제한다. 다만 vocab 의 `source` 태그가 **런타임 실제 방출과 어긋난다** (body의 concept/mentions/has_attribute는 기본 OFF, F-CG-04).
  2. **검색 측 정렬은 사실상 사문(死文)** — `INTENT_TO_RELATIONS` / `format_*_for_prompt` / `all_*_names` 는 **프로덕션에서 아무도 호출하지 않는다**. 실제 검색(`graph_search.py`)은 순수 임베딩 시딩이라 relation_type/vocab을 전혀 소비하지 않는다 (F-CG-01, HIGH).
  3. **커버리지 공백이 실데이터로 확인됨** — 이전 라운드 실측 DB에서 LLM이 자연 방출한 `publishes_to`/`consumes_from`(Kafka 이벤트), `service`, `component` 는 **현재 어휘에 전혀 없다**. 지금 인덱싱하면 strict 필터가 전부 드롭한다 (F-CG-02, HIGH; F-CG-05, MED).
  4. **타입 경계 모호성이 실데이터로 발현** — LLM이 vocab의 `system`/`module` 대신 동의어 `service`/`component` 를 골랐다. 병합 키가 `(정규화이름, entity_type)` 이라 **같은 실세계 엔티티가 추출기·라운드에 따라 다른 타입으로 방출되면 노드가 쪼개진다** (F-CG-03, HIGH).
  5. **드리프트 방지 테스트는 "subset ⊆ vocab" 만 검증** — 방향이 한쪽뿐이라 vocab에 과잉 항목이 쌓여도, 추출기가 어휘 밖 타입을 방출해도 못 잡는다 (F-CG-07, MED).

- 가장 시급한 3건: **F-CG-01**(검색 정렬 사문화), **F-CG-02**(이벤트/토픽 커버리지 공백 — 실데이터), **F-CG-03**(system↔service 경계로 노드 분열 — 실데이터).

### 실데이터 근거 확보 상태

- 현 리포지토리에는 **질의 가능한 그래프 DB가 없다**. `GraphStore` 는 `networkx` **인메모리** 구조(`graph_store.py:18,94`)로, 파이프라인 실행 시 런타임 구축되며 `.db/.duckdb/.parquet` 로 영속화된 그래프 스냅샷 파일이 없다 (`find` 결과 0건). 따라서 이번 세션에서 라이브 `entity_type/relation_type` 분포 집계는 불가.
- 대신 **이전 라운드가 운영 DB(`~/.context-loop/data/metadata.db`)를 실측한 스냅샷**을 근거로 사용한다: `_workspace/graph-search-diagnosis_r2/01_index_diagnosis.md:9-24`. 이 스냅샷은 현재 vocab 리팩터 **이전**에 인덱싱된 것이라(아래 검증) 현재 어휘와 직접 비교 시 드리프트를 드러낸다 — 이 자체가 유효한 근거다.

**실측 분포 (document_id=5, 노드 21 / 엣지 16):**

| entity_type | count | 현재 vocab 존재? |
|---|---|---|
| service | 6 (Auth/Order/Product/Payment/Notification/Search Service) | ❌ (vocab엔 `system`) |
| team | 6 | ✅ |
| component | 5 (PostgreSQL/Redis/MySQL/결제DB/Elasticsearch) | ❌ (vocab엔 `module`) |
| system | 4 (API Gateway, Kafka, SMTP, FCM) | ✅ |

| relation_type | count | 현재 vocab 존재? |
|---|---|---|
| uses | 7 | ✅ |
| depends_on | 6 | ✅ |
| publishes_to | 2 | ❌ |
| consumes_from | 1 | ❌ |

- 검증: `service` / `component` / `publishes_to` / `consumes_from` 문자열은 **현재 `src/` 전체 어디에도 없다**(grep 0건). 즉 이 DB는 옛 어휘로 인덱싱됐고, 그 옛 어휘가 지금은 사라졌다 → vocab이 시간에 따라 표류했음을 실증. 동시에, **동일한 Confluence 아키텍처 문서를 LLM에 넣으면 자연히 `service`/`component`/`publishes_to`/`consumes_from` 를 뽑는다**는 관찰 근거이기도 하다.

---

## 발견 사항

### F-CG-01 (HIGH): 검색 측 어휘 정렬(`INTENT_TO_RELATIONS`·프롬프트 포매터·`all_*_names`)이 프로덕션에서 미소비 — 사문(dead)

- **위치**: `graph_vocabulary.py:106-152`(정의), `graph_search.py` 전체(소비자), `mcp/context_assembler.py:20,534`(검색 진입점).
- **정의가 주장하는 설계**: 모듈 docstring(`graph_vocabulary.py:155-162`)은 "검색 LLM 은 모든 subset 의 union 을 본다", "그래프 탐색 플래너 같은 소비자가 일관된 가이드를 LLM 에 제공"이라고 명시. `INTENT_TO_RELATIONS` 는 "LLM 플래너 가이드용".
- **실제**: grep 결과 `INTENT_TO_RELATIONS`, `format_entity_types_for_prompt`, `format_relation_types_for_prompt`, `format_intent_mapping_for_prompt`, `all_entity_type_names`, `all_relation_type_names` 의 **비-테스트 참조가 0건**. 유일한 소비자는 `tests/test_graph_vocabulary.py`.
- **왜 그런가**: `graph_search.py:1-16` docstring — "LLM 호출 0회, fallback 0층". 과거 LLM 플래너(`plan_graph_search`/`execute_graph_search`)가 전부 "임베딩 검색으로 수렴"해서 제거됐고, 남은 것은 `search_entities_by_embedding` → 1-hop 확장뿐. **relation_type / entity_type 어휘를 검색이 전혀 참조하지 않는다.**
- **영향**:
  - 검토 관점 4("INTENT_TO_RELATIONS 매핑이 실제 relation 분포·질의 유형에 적합한가")는 **무의미** — 매핑이 검색에 연결돼 있지 않으므로 적합성 여부와 무관하게 효과 0.
  - "인덱싱 subset vs 검색 union 노출 설계"의 union 쪽(검색)이 실체가 없다. 설계 의도와 구현이 어긋남.
  - vocab의 `description` 문구를 아무리 다듬어도 검색 랭킹/리콜에 영향이 없다 — relation_type은 인덱싱 필터로만 작동.
- **실데이터 정합**: 실측 relation 분포(uses 7, depends_on 6, publishes_to 2, consumes_from 1)에서 `INTENT_TO_RELATIONS` 가 강조하는 `implements`/`provides`/`supersedes`/`has_part`/`calls`/`owned_by`/`mentions_ticket` 은 **0건**. 매핑이 실분포와 크게 어긋나지만, 소비자가 없어 실해는 없음(그래서 severity를 CRITICAL 아닌 HIGH로).
- **개선 방향**: 둘 중 하나로 정직화. (a) 검색을 hybrid로 되돌려 relation_type 필터/부스팅을 실제 소비하게 하고 `INTENT_TO_RELATIONS` 를 그 입력으로 연결, 또는 (b) 검색이 순수 임베딩으로 확정됐다면 `INTENT_TO_RELATIONS` + `format_*` + docstring의 "검색 union" 서술을 **제거/축소**하고 vocab의 역할을 "인덱싱 필터 단일 출처"로 재정의. 현 상태는 "검증되지만 쓰이지 않는" 코드가 유지비만 발생.

---

### F-CG-02 (HIGH): 이벤트/토픽(Kafka pub/sub) 관계를 표현할 어휘가 없음 — 실데이터로 확인된 드롭

- **위치**: `graph_vocabulary.py:74-99`(RELATION_TYPES 전체), `llm_body_extractor.py:247-254,411-418`(strict 드롭).
- **문제**: 현재 relation 어휘에 **메시지/이벤트 관계가 전무**. `publishes_to`, `consumes_from`, `subscribes_to`, `produces`, `emits` 류가 없다. 가장 가까운 `calls`("동기/비동기 호출")로 억지 매핑 가능하나, pub/sub의 비대칭(생산자/소비자)과 토픽 매개를 잃는다.
- **실데이터 근거**: 실측 DB(`graph-search-diagnosis_r2/01_index_diagnosis.md:23-24`)에서 Kafka 노드에 대해 LLM이 `publishes_to`(2), `consumes_from`(1)을 자연 방출. 이 관계는 **현재 어휘에 없다** → 지금 재인덱싱하면 `llm_body_extractor.py:249`(`rtype not in allowed_rtypes`)에서 **전량 드롭**, `stats.dropped_relations` 로만 계상.
- **엔티티 측 동반 공백**: Kafka 자체는 실측에서 `system` 으로 잡혔으나, **토픽/이벤트 엔티티**(`order.created` 같은 토픽명)를 담을 entity_type이 없다. 검토 관점 2가 예시로 든 "이벤트/토픽(Kafka 등)"은 엔티티·관계 양쪽 모두 미표현.
- **기타 커버리지 공백 (관점 2 체크리스트 대조)**:
  - 회의록 **결정사항(decision)**: 엔티티/관계 없음. `supersedes`(폐기)는 있으나 "결정"이라는 1급 노드 개념 없음.
  - **날짜/마일스톤(milestone/date)**: 없음. `ticket`(Jira)만 시점을 간접 표현.
  - **데이터베이스/테이블**: 실측에서 PostgreSQL/MySQL/Redis가 `component` 로 잡혔으나 현재 vocab엔 `component` 도, `database`/`table` 도 없음 → 지금은 `module`("코드 파일/패키지")로 흡수해야 하는데 DB 인프라를 "코드 모듈"로 부르는 건 의미 왜곡.
  - **환경/배포(environment/deployment)**: 없음. `deployed_to`, `runs_in` 류 관계 부재.
  - **지표/알림(metric/alert)**: 없음.
- **LLM의 어휘 외 처리 방식(검토 관점 2 후단)**: **strict drop**. 프롬프트(`llm_body_extractor.py:125` "위 어휘에 없는 type은 절대 사용하지 마세요")로 억제 + 후처리에서 `allowed_*` 미포함 시 드롭(엔티티는 `226/392`, 관계는 `247-252/411-416`). **fallback 타입도, 동의어 정규화도 없다.** 어휘 밖 신호는 조용히 소실되고 통계 카운터로만 남는다.
- **개선 방향**: confluence 문서 유형(아키텍처/회의록/운영 런북)에 맞춰 어휘 확장 검토 — 최소한 이벤트 관계(`publishes_to`/`consumes_from` 또는 통합 `messaging`)와 데이터스토어 엔티티(`datastore`/`database`)를 1급 추가. 또는 strict drop 대신 **어휘 외 타입을 드롭 전에 로깅·집계하는 관측 훅**을 두어 "무엇을 놓치는지" 데이터로 수집한 뒤 어휘 확장 우선순위를 정할 것(현재 `dropped_*` 는 개수만 세고 타입명을 안 남김 — F-CG-08 참조).

---

### F-CG-03 (HIGH): `system` vs `module` 경계 모호 → LLM이 동의어(`service`/`component`) 선택 시 노드 분열

- **위치**: `graph_vocabulary.py:50-56`(system/module 설명), `graph_store.py:101,174,197-235`(병합 키 `normalize(name)+entity_type`).
- **경계 모호성**:
  - `system` = "외부에서 보이는 서비스 (예: Auth Service)" / `module` = "시스템 내부 컴포넌트 또는 코드 파일/패키지 (예: Token Validator, user_service.py)". "외부에서 보이는" vs "내부"는 문서 관점 의존이라 LLM이 일관 판정하기 어렵다. "Auth Service"가 어떤 문서에선 외부 시스템, 다른 문서에선 내부 컴포넌트로 서술될 수 있다.
  - `module` 이 **코드 파일**(user_service.py, ast_code 출처)과 **추상 내부 컴포넌트**(Token Validator, llm_body 출처)를 한 타입에 섞음 — source 태그도 "llm_body + ast_code"로 이중. AST가 뽑는 `user_service.py` 와 LLM이 뽑는 서비스명이 같은 타입 공간을 공유해 검색 시 이질적 노드가 뒤섞인다.
- **실데이터 근거 (경계 모호가 실제로 발현)**: 실측 DB에서 LLM은 vocab의 `system` 대신 **`service`** 를 6개(Auth/Order/Product/Payment/Notification/Search Service), `module` 대신 **`component`** 를 5개 방출. 즉 프롬프트로 `system`/`module` 을 강제해도, 문서에 "~ Service", "~ 컴포넌트/DB" 표기가 흔하면 LLM이 표면 표기에 이끌려 동의어를 고른다. (그 DB는 옛 어휘라 통과했지만) **현재 strict 어휘에선 6+5개 엔티티가 전량 드롭된다.**
- **노드 병합 파손 시나리오**: 병합 키는 `(normalize_entity_name(name), entity_type)` (`graph_store.py:197-202`, normalizer는 공백/`-`/`_` 제거·소문자화 `entity_normalizer.py:78-82`). 이름 정규화 덕에 "AuthService"↔"Auth Service"↔"auth_service"는 병합되지만 **entity_type이 키에 포함**되므로:
  - `body_extractor` 가 "Payment Service"를 굵게 강조로 `concept` 로 뽑고(옵션 ON 시), `llm_body_extractor` 가 같은 "Payment Service"를 `system` 으로 뽑으면 → `(paymentservice, concept)` vs `(paymentservice, system)` **두 노드로 분열**. 검색 시딩·1-hop 확장이 갈라져 리콜 손실.
  - 라운드 간 어휘 변경(`service`→`system`)만으로도 재인덱싱 시 기존 `service` 노드와 신규 `system` 노드가 공존/분열(재처리 소유권은 document_id 기반이라 옛 노드가 고아로 남을 수 있음 — 소유권 모델은 본 스코프 밖이나 경계 모호가 이를 악화).
- **개선 방향**:
  - system/module 설명에 **판정 기준을 명문화**(예: "배포 단위·네트워크 경계를 가지면 system; 그 안의 논리 컴포넌트/파일이면 module") + few-shot 반례 1–2개. 현재 한 줄 설명은 구분에 불충분.
  - 코드 파일(ast_code)과 추상 컴포넌트(llm_body)를 `module` 한 타입에 합친 것을 재고 — 별도 타입(`code_file` vs `component`)으로 분리하거나, 최소한 검색이 이를 구분할 수 있게.
  - 병합 취약성 완화: 동일 name의 서로 다른 entity_type을 인덱싱 후 리포트하는 관측 훅(같은 정규이름이 2+ 타입으로 존재하면 경고).

---

### F-CG-04 (MED): vocab `source` 태그가 런타임 실제 방출과 불일치 — body의 concept/mentions/has_attribute는 기본 OFF

- **위치**: `graph_vocabulary.py:47,83,85`(source 태그), `body_extractor.py:87-88`(기본 OFF), `pipeline.py:440`(기본 config로 호출).
- **문제**: vocab이 `concept` source="body + llm_body", `mentions` source="body", `has_attribute` source="body"로 태깅. 그러나 `body_extractor` 에서 이들을 만드는 `extract_bold_terms`/`extract_table_headers` 는 **기본 False**(`body_extractor.py:87-88`), 그리고 `pipeline.py:440` 은 `extract_body_graph(units, doc_title=title)` 를 **config 없이(=기본값)** 호출.
- **결과**: 프로덕션 기본 경로에서 `body_extractor` 가 실제 방출하는 건 `api`(documents)와 `ticket`(mentions_ticket)뿐. `concept`/`mentions`/`has_attribute` 는 **body에서 0건** — `concept` 은 사실상 `llm_body` 단독 출처, `has_attribute`/`mentions` 는 (기본 설정에선) **아무도 방출하지 않는 사문 관계**.
- **정합성 판정(관점 1)**: 어휘가 방출 가능한 타입의 상위집합이라는 주장 자체는 참(config로 켜면 방출됨). 하지만 `source` 태그가 "기본 활성 여부"를 구분하지 않아, 태그만 보고 "concept은 body에서도 나온다"고 오독하게 만든다. subset 필터(`_has_source`, `graph_vocabulary.py:165-179`)가 이 태그 문자열에 의존하므로, 태그 부정확은 향후 자동 분류 로직에 리스크.
- **개선 방향**: source 태그에 기본 ON/OFF 표기 추가(예: `"body(opt) + llm_body"`) 또는 default-off 신호를 별도 필드로. 최소한 `has_attribute`/`mentions` 가 기본 경로에서 미방출임을 문서화.

---

### F-CG-05 (MED): 외부 URL·이메일 링크가 그래프에서 소실 — link 경로 커버리지 공백

- **위치**: `link_graph_builder.py:37-49`(`_KIND_TO_*` 매핑), `118-120`(`_should_include`).
- **문제**: `_KIND_TO_ENTITY_TYPE` 는 page/user/jira/attachment 4종만 가지고, `url`(외부 링크)은 매핑이 없어 `_should_include` 가 False → **외부 시스템/문서 참조가 그래프에 전혀 안 들어간다.** 사내 위키에서 외부 표준 문서(RFC, k8s docs), 외부 대시보드, 이메일 담당자 링크는 중요한 신호인데 결정론 경로에서 버려진다.
- **어휘 관점**: vocab에 `url`/`external_resource` entity_type과 대응 relation이 없어, 설령 link_graph가 url을 넣고 싶어도 담을 어휘가 없다. LLM 경로가 본문 내 URL을 `system`/`concept` 로 우연히 주울 순 있으나 비결정적.
- **개선 방향**: 외부 리소스용 entity_type(`external_resource` 등) + relation(`links_to`/`references`) 추가 여부를 커버리지 관점에서 결정. 노이즈(광고/트래킹 URL) 우려가 있으면 도메인 화이트리스트/블랙리스트 병행.

---

### F-CG-06 (MED): `mentions` / `references` / `documented_in` 3개 "언급/참조" 관계의 경계 불명확

- **위치**: `graph_vocabulary.py:76,83,95`.
- **문제**: `references`("문서가 다른 페이지를 참조 — Confluence link", link_graph), `mentions`("문서가 개념/엔티티를 본문에서 언급", body), `documented_in`("A 가 B 에 문서화", llm_body) — 세 관계의 의미가 겹친다. 특히 `mentions` vs `documented_in` 은 방향과 주체만 다를 뿐 "무엇이 어디에 서술됨"이라는 동일 개념. `mentions`(body) vs `mentions_user`/`mentions_ticket`(link) 접두 혼용도 명명 비일관.
- **LLM 혼동 위험**: llm_body 프롬프트에는 `documented_in`, `mentions`(subset 아님 — mentions는 body 태그이므로 llm subset에서 제외됨), `references`(link 태그, llm subset 제외)가 각각 다르게 노출되나, "A가 B에 문서화"는 `depends_on` 만큼 명확한 방향 신호가 없어 LLM이 남발하거나 회피하기 쉽다.
- **정합성 확인**: `documented_in` 은 `INTENT_TO_RELATIONS` 의 "문서 간 참조" 의도에 `references`, `mentions` 와 함께 묶여 있음(`graph_vocabulary.py:119-120`) — 세 관계가 사실상 한 버킷으로 취급된다는 방증. 3개를 유지할 실익이 검색(F-CG-01로 미소비) 측면에서 없다.
- **개선 방향**: `documented_in` 을 `references`/`mentions` 로 통합 검토, 또는 각 description에 배타적 판정 기준 명시. 명명 규칙(`mentions_*` prefix)을 일관화.

---

### F-CG-07 (MED): 드리프트 방지 테스트가 "subset ⊆ vocab" 단방향만 검증 — 과잉·역방향 누락 미탐

- **위치**: `tests/test_graph_vocabulary.py:33-74`, `graph_vocabulary.py:9-11`(docstring "테스트가 누락을 잡는다").
- **테스트가 실제로 잡는 것**: 추출기 상수(`_KIND_TO_*`, `_DEFAULT_*`, body 4종, ast 6종)가 vocab의 **subset인지**(`<=`). `test_llm_body_subset_helpers_are_consistent` 만 `==` 로 양방향 강제(단 llm_body에 한정).
- **못 잡는 것**:
  1. **vocab 과잉 항목**: vocab에 있으나 어떤 추출기도 방출 안 하는 타입(예: F-CG-04로 사실상 미방출인 `has_attribute`, 혹은 향후 죽은 항목)을 탐지 못함. `all_names ⊇ subset` 은 항상 참이므로 죽은 vocab 항목이 무한 누적 가능.
  2. **하드코딩 기대값의 화석화**: `test_body_extractor_vocab_subset` 은 `expected_etypes={"concept","api","ticket","document"}` 를 **테스트에 손으로 박아뒀다**(`test_graph_vocabulary.py:71-72`). body_extractor가 새 타입을 방출해도 이 상수를 같이 안 고치면 테스트는 통과 → 실제 추출기 상수와의 동기화를 **직접 introspection하지 않는다**. ast_code도 동일(`57-59` 하드코딩).
  3. **어휘 밖 방출 런타임 미검증**: LLM이 어휘 밖 타입을 방출→드롭하는 경로는 `test_llm_body_extractor.py:193-233` 이 단위로 검증하나, "실제 confluence 문서에서 무엇이 드롭되는가"는 테스트 없음.
- **정합성 판정(관점 5)**: "테스트가 누락을 잡는다"는 주장은 **부분적으로만 참** — 추출기가 vocab에 없는 타입을 추가하면(그리고 하드코딩 기대값을 같이 수정하면) 잡지만, vocab이 표류하거나 죽은 항목이 쌓이는 반대 방향은 못 잡는다. 실측 DB의 `service`/`publishes_to` 드리프트가 테스트를 통과했을 것(그 어휘가 코드 상수에 없었으므로 subset 검증 대상이 아님).
- **개선 방향**: (a) 추출기 상수를 test가 하드코딩 대신 **직접 import해 비교**(body_extractor의 방출 타입을 상수화하고 그것과 vocab을 대조). (b) "vocab의 모든 항목은 최소 한 추출기의 방출 집합에 속한다"는 **역방향 테스트** 추가로 죽은 항목 탐지.

---

### F-CG-08 (MED): 드롭 통계가 개수만 세고 "무엇을/어떤 타입을" 드롭했는지 안 남김 — 어휘 공백 관측 불가

- **위치**: `llm_body_extractor.py:104-108`(`dropped_entities`/`dropped_relations` int 카운터), `226,249,392,411`(드롭 지점).
- **문제**: 어휘 밖 타입은 조용히 드롭되고 **개수만** 집계된다. 어떤 type 문자열이 몇 번 드롭됐는지(예: `service` 6회, `publishes_to` 2회) 기록이 없어, **커버리지 공백을 데이터로 발견할 수단이 없다.** 그래서 F-CG-02/03 같은 공백이 실측 DB(우연히 옛 어휘로 통과된)를 통해서만 드러났다.
- **영향**: 어휘 확장 우선순위를 근거 기반으로 정할 수 없음. LLM이 반복적으로 어떤 동의어를 고르는지(system↔service) 관측 불가 → 프롬프트/설명 개선 루프가 닫히지 않음.
- **개선 방향**: 드롭 시 `Counter[type_name]` 를 stats에 추가(예: `dropped_entity_types: dict[str,int]`). 인덱싱 배치 후 상위 드롭 타입을 집계하면 어휘 확장/동의어 매핑의 정량 근거가 된다. (코드 변경 제안 — 본 라운드는 분석만.)

---

### F-CG-09 (LOW): 동의어/오타 정규화 부재 — strict 매칭이 `depending_on`·`part_of` 등 근접 표기를 전량 드롭

- **위치**: `llm_body_extractor.py:247-252`(`rtype not in allowed_rtypes` → drop), 동의어 매핑 없음.
- **문제**: allowed 집합에 대한 완전일치만 통과. LLM이 `depends_on` 대신 `depending_on`, `has_part` 대신 `part_of`/`contains`, `system` 대신 `service` 를 쓰면 drop. 온도 0이라 재현적이나, 긴 입력·다국어에서 표기 흔들림이 커진다(이전 라운드 F-CG2-08에서도 지적).
- **관점 3 연계**: `depends_on` vs `uses` vs `calls` 세 의존 관계는 실측에서 `uses`(7)/`depends_on`(6)로 활발히 쓰였고 `calls` 0건 — 경계가 있긴 하나 LLM이 uses/depends_on을 사실상 혼용하는 정황. description이 "런타임/빌드 의존성" vs "도구/라이브러리 사용"으로 갈라 놓았으나 실무 문서에서 둘은 종종 같은 문장에서 등장.
- **개선 방향**: `graph_vocabulary` 에 `aliases` 필드 또는 `normalize_relation_type()`/`normalize_entity_type()` 정규화 함수 도입(예: `service→system`, `component→module`, `part_of→has_part`). 이는 F-CG-02/03의 드롭을 상당 부분 회수하는 저비용 방어선.

---

## 정합성 대조표 (관점 1)

| 추출기 | 방출 entity_type | 방출 relation_type | vocab source 태그 정합? |
|---|---|---|---|
| `link_graph_builder` (`link_graph_builder.py:37-49`) | document, person, ticket, attachment | references, mentions_user, mentions_ticket, has_attachment | ✅ 일치 (url만 미표현 — F-CG-05) |
| `body_extractor` 기본 | document, api, ticket | documents, mentions_ticket | ⚠️ concept/mentions/has_attribute는 기본 OFF인데 태그는 "body"로 표기 (F-CG-04) |
| `body_extractor` 옵션 ON | +concept | +mentions, +has_attribute | ✅ (옵션 시) |
| `llm_body_extractor` (`_DEFAULT_*`=subset) | system, module, policy, team, person, concept, api | depends_on, implements, calls, owned_by, supersedes, has_part, uses, provides, documented_in | ✅ subset==vocab llm_body 태그 (테스트 `==` 강제) |
| **실측 LLM (옛 어휘)** | **service, component**, system, team | uses, depends_on, **publishes_to, consumes_from** | ❌ service/component/publishes_to/consumes_from 미표현 (F-CG-02/03) |

- subset 필터(`llm_body_*_names`)와 llm_body_extractor `_DEFAULT_*` 는 **완전 일치**하고 `test_llm_body_subset_helpers_are_consistent` 가 `==` 로 강제 → 이 축은 견고.
- 과잉 항목 후보: `has_attribute`(기본 미방출), `documented_in`(F-CG-06 통합 후보), 검색 미소비 관계 다수(F-CG-01).

---

## 검토하지 않은 영역

- **라이브 그래프 분포 집계**: 인메모리 GraphStore라 질의 가능한 DB가 없어 이번 세션 실측 불가. 근거는 이전 라운드 스냅샷(옛 어휘) 재사용 — 현재 어휘로 재인덱싱한 실분포는 미확보. **후속 권고**: 현재 vocab으로 confluence 샘플 문서를 인덱싱한 뒤 `dropped_entity_types`/`dropped_relation_types`(F-CG-08) 를 집계해 커버리지 공백을 정량화할 것.
- **git_code(ast_code) 경로의 module/enum 등**: `module`/`function`/`class`/`method`/`struct`/`interface` 는 git-code-graph-analyst 스코프. 단 `module` 이 confluence(llm_body)와 git_code(ast)에 **공유**되어 병합 공간이 겹치는 점만 F-CG-03에서 지적.
- **재처리 시 고아 엣지/소유권 모델**: `graph_store` document_id 소유권은 본 라운드(어휘 적합성) 스코프 밖. F-CG-03의 노드 분열이 이를 악화시킬 수 있다는 연결만 언급.
- **confluence-chunking-analyst 와의 충돌점**: 둘 다 `pipeline.py` 그래프 추출 호출부(414-517)를 본다. 본 findings는 어휘/타입만, chunking analyst는 unit 분할을 다루므로 직접 충돌은 없으나, F-CG-04의 body_extractor 기본 OFF 정책은 chunking(ExtractionUnit) 산출물의 활용도와 연결됨 — 공유 유의.

---

## 우선순위 제언 (어휘 적합성 한정, 코드 변경 없음 — 분석 결론)

1. **F-CG-01 정직화**: 검색이 vocab을 소비하지 않는 현실과 docstring/`INTENT_TO_RELATIONS` 의 "검색 union" 서술 간 괴리를 해소(제거 또는 검색 재연결 결정). 가장 큰 개념적 부채.
2. **F-CG-02/03 커버리지·경계 결정**: 이벤트(pub/sub)·데이터스토어 어휘 추가 여부, system/module description 정예화 — 실데이터가 공백을 실증한 최우선 실무 이슈.
3. **F-CG-08 관측 훅 + F-CG-07 역방향 테스트**: 어휘 확장을 데이터 기반으로 돌릴 수 있는 계측·검증 인프라. 이게 없으면 2번의 우선순위를 계속 추측으로 정하게 됨.
4. **F-CG-09 동의어 정규화**: 저비용으로 2/3의 드롭을 회수하는 방어선.
