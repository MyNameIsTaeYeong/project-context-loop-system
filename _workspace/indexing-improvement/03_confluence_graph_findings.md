# Confluence Graph Extraction — Findings

## 요약

- 총 발견 **13건** (Critical 1, High 7, Medium 4, Low 1)
- 가장 시급한 3건:
  - **F-CG-04** llm_body_extractor가 unit별로 entity 검증을 격리 → 한 문서 안에서 unit 간 entity 참조 끊김 (검색에서 핵심 관계 누락)
  - **F-CG-05** link_graph_builder가 외부 URL 링크를 모두 제외 → 외부 시스템 참조 시그널 손실
  - **F-CG-07** graph_vocabulary가 단일 출처가 아니라 추출기들이 자체 어휘 사용 → 어휘 drift 발생 시 발견 어려움

## 발견 사항

### F-CG-01 (HIGH): `_strip_code_for_prose`가 들여쓰기 코드 블록 미인식

- **위치**: `src/context_loop/processor/body_extractor.py:44-45, 297-304`
- **현재 동작**:
  ```python
  _CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
  _INLINE_CODE_RE = re.compile(r"`[^`\n]+`")
  ```
  - 펜스(`)와 인라인 코드(`)만 제거
  - 4-space 들여쓰기 코드 블록 (`    code line`)은 prose로 간주
- **문제**: Confluence의 일부 매크로 변환 결과가 들여쓰기 코드를 생성하면, 그 안의 `PROJ-123`이나 `**Bold**` 같은 패턴이 noise로 잘못 추출됨
- **개선 방향**:
  - `^    ` 들여쓰기 블록 제거 정규식 추가
  - 또는 `~~~` 펜스도 처리
  - 또는 html_to_markdown 결과의 코드 포맷을 점검하여 ``` 한 가지로 통일 보장 (별도 작업)
- **영향 범위**: html_to_markdown이 들여쓰기 코드를 만드는 경우의 false positive
- **심각도**: High | **공수**: S

---

### F-CG-02 (MEDIUM): API 엔드포인트 정규식이 query string 미포함

- **위치**: `src/context_loop/processor/body_extractor.py:46-49`
- **현재 동작**:
  ```python
  _API_RE = re.compile(
      r"\b(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+"
      r"(/[A-Za-z0-9_\-/{}:.~]+)"
  )
  ```
  - `?`, `&`, `=` 등 query string 문자 부재 → `GET /users?type=admin` 에서 `/users` 만 추출
- **문제**: query parameter가 의미를 가진 API 엔드포인트가 일반화되어 같은 엔드포인트로 dedup → 의도된 동작일 수도 있고 손실일 수도 있음
- **개선 방향**:
  - path만 보존(현재 동작) 유지 + query 포함 옵션 (config flag)
  - 또는 path까지만 보존하되 `description`에 원본 query 보존
- **영향 범위**: REST API 문서의 엔드포인트 노드 정밀도
- **심각도**: Medium | **공수**: S

---

### F-CG-03 (HIGH): llm_body_extractor가 split된 unit의 part[1..N]을 모두 스킵 → 정보 누락

- **위치**: `src/context_loop/processor/llm_body_extractor.py:286-292`
- **현재 동작**:
  ```python
  if cfg.skip_split_overlap_parts and unit.split_total > 1 and unit.split_part > 0:
      stats.units_skipped_overlap += 1
      continue
  ```
- **문제**:
  - 거대 섹션이 5개 part로 분할되면 LLM은 part[0]만 봄
  - overlap_tokens(200) 만으로는 part[1..4]에만 있는 엔티티/관계가 LLM에 전달 안 됨
  - 결과: 거대 섹션의 후반부 엔티티 누락
- **재현/근거**: split_total=5인 unit 처리 시 stats에 units_skipped_overlap=4 누적
- **개선 방향**:
  - (1) split된 unit들을 LLM 호출 전 다시 합쳐서 한 번에 호출 (max_tokens 한계 주의)
  - (2) 또는 모든 part에 LLM 호출 (비용 4배)
  - (3) 또는 split_part>0에도 호출하되 stats에 split_part 표시
  - 권장: (3) — 비용 동일하지 않지만 (현재 part[0] 한 번 → split_total 번), 옵션화하여 사용자 선택
- **영향 범위**: 거대 섹션을 가진 confluence 문서의 그래프 추출 완전성
- **심각도**: High | **공수**: S (옵션 토글) ~ M (스마트 병합)

---

### F-CG-04 (CRITICAL): llm_body_extractor의 entity 검증이 unit별로 격리됨 → 문서 단위 일관성 깨짐

- **위치**: `src/context_loop/processor/llm_body_extractor.py:210-258`
- **현재 동작**:
  ```python
  for unit, payload in results:
      ...
      unit_valid_entity_names: set[str] = set()  # ← unit마다 새로 시작
      for ent in raw_entities:
          ...
          unit_valid_entity_names.add(name.lower())

      for rel in raw_relations:
          if (... or src.lower() not in unit_valid_entity_names
                  or tgt.lower() not in unit_valid_entity_names): ...
              stats.dropped_relations += 1
              continue
  ```
- **문제**:
  - 한 문서의 unit A에서 "Auth Service"가 entity로 정의됨
  - unit B의 LLM 응답에서 "Auth Service → Token Validator depends_on" 관계가 있지만, unit B의 entities에 "Auth Service"가 없으면 (B에서는 언급만 있고 entity 정의를 안 만들었을 수 있음) 관계 드롭
  - 결과: 문서 단위 추론에서 핵심 cross-unit 관계 누락
- **재현/근거**: 같은 문서의 다른 unit에서 같은 entity가 등장하면, LLM이 entity 정의는 한 번 만들고 다른 unit에서는 관계만 만들 가능성 — 그 관계가 모두 드롭
- **개선 방향**:
  - (1) 2-pass: 첫 pass에서 모든 unit의 entities 누적 → 두 번째 pass에서 relations 검증
  - (2) 또는 한 pass에서 entities를 누적하면서, relations은 가장 마지막에 일괄 검증
  - (3) 또는 unit_valid_entity_names 대신 cumulative_entity_names 사용 + 같은 unit에서 정의된 것만 인정하는 strict 옵션
- **영향 범위**: 모든 LLM body 추출의 cross-unit 관계 — 그래프 추론의 핵심
- **심각도**: Critical | **공수**: M

---

### F-CG-05 (HIGH): link_graph_builder가 외부 URL을 모두 제외 → 외부 시스템 참조 손실

- **위치**: `src/context_loop/processor/link_graph_builder.py:37-50, 118-120`
- **현재 동작**:
  ```python
  _KIND_TO_ENTITY_TYPE = {
      "page": "document", "user": "person", "jira": "ticket",
      "attachment": "attachment",
  }
  # "url" 누락 — _should_include에서 제외됨
  ```
- **문제**:
  - 외부 시스템 문서(예: `https://kubernetes.io/docs/...`)나 도구 (`https://grafana.internal/d/api-latency`) 참조가 그래프에 없음
  - "이 페이지는 어떤 외부 시스템을 참조하는가" 같은 의존성 분석 불가
- **개선 방향**:
  - `"url": "external_resource"` 추가, `"references_external"` 관계
  - target_name은 도메인+path (예: `kubernetes.io/docs/concepts/storage`)
  - anchor_text가 있으면 description으로 저장 (entity 이름은 URL 정규화)
  - 필터 옵션: 화이트리스트 도메인 또는 길이 제한 (너무 짧은 URL 제외)
- **영향 범위**: 외부 시스템 의존성/참조 추적
- **심각도**: High | **공수**: M

---

### F-CG-06 (MEDIUM): link_graph_builder의 page entity가 target_title 우선 → 같은 page가 표시명 다르면 별개 노드

- **위치**: `src/context_loop/processor/link_graph_builder.py:123-136`
- **현재 동작**:
  ```python
  if link.kind == "page":
      if link.target_title:
          return link.target_title
      if link.target_id:
          return f"page:{link.target_id}"
  ```
  - target_title을 entity name으로 사용 → 같은 page가 두 곳에서 다른 표시명(별칭/오타)이면 두 entity로 분리
- **문제**: 그래프 노드 dedup이 불완전 → 검색에서 같은 페이지가 여러 결과로 나옴
- **개선 방향**:
  - target_id가 있으면 id 기반 키 사용 (entity name은 id 또는 정규화된 title)
  - 또는 다른 표시명을 description에 누적
- **영향 범위**: 같은 페이지를 별칭으로 참조하는 문서들
- **심각도**: Medium | **공수**: M

---

### F-CG-07 (HIGH): graph_vocabulary가 단일 출처가 아님 → 어휘 drift 발견 어려움

- **위치**: `src/context_loop/processor/graph_vocabulary.py:9-11` (모듈 docstring) + `body_extractor.py:39-58`, `llm_body_extractor.py:33-53`, `link_graph_builder.py:37-49`
- **현재 동작**: 각 추출기가 자체 어휘 상수를 유지. graph_vocabulary는 별도 정의 — 추출기와 분리됨. 모듈 docstring: "추출기 측 코드는 자체 어휘 상수를 그대로 유지하지만, 이 모듈이 그것들을 상위집합으로 재선언"
- **문제**:
  - 새 entity_type을 추출기에 추가하고 graph_vocabulary에 추가 누락하면 graph_search_planner의 가이드가 stale
  - 반대로 graph_vocabulary만 추가하고 추출기 누락 시 가이드는 있지만 데이터 없음
  - 현재 ast_code_extractor의 "method", "struct", "interface"는 vocab에 정의 안 됨
- **재현/근거**:
  - vocab `ENTITY_TYPES`에 "function", "class"만 있음
  - ast_code_extractor가 "method", "struct", "interface" 타입의 entity 생성 (line 247의 `entity_type=sym.symbol_type`)
- **개선 방향**:
  - (1) 추출기들이 graph_vocabulary에서 어휘를 import (단일 출처)
  - (2) graph_vocabulary에 "method", "struct", "interface" 추가
  - 권장: (2) 우선 (즉시), (1)은 장기 리팩토링
- **영향 범위**: graph_search_planner LLM 가이드의 완전성. 현재는 LLM이 모르는 타입을 추출했어도 vocab 가이드가 누락되어 검색 플래너가 활용 못 함
- **심각도**: High | **공수**: S (vocab 보강) ~ L (단일 출처화)

---

### F-CG-08 (MEDIUM): body_extractor의 _normalize_term이 trailing 문자만 제거, 영어 leading 기호 누락

- **위치**: `src/context_loop/processor/body_extractor.py:307-309`
- **현재 동작**: `term.strip().strip("·•:,.;\"'`")` — 양쪽 모두 strip하지만 한정된 문자 집합
- **문제**:
  - 한국어 문맥의 `「용어」`, `〈용어〉`, `［용어］` 같은 괄호 미제거
  - 영어 `&Term`, `#Term`, `@Term` leading 미제거 (`@`/`#`는 의도일 수 있음)
- **개선 방향**: trim 문자 집합 확장, 한국어 양각 인용부호 추가
- **영향 범위**: Bold term 추출의 정규화
- **심각도**: Medium | **공수**: S

---

### F-CG-09 (MEDIUM): body_extractor의 API 엔드포인트 false positive — placeholder/예제 패턴

- **위치**: `src/context_loop/processor/body_extractor.py:225-239`
- **현재 동작**: 펜스 코드블록 안의 `POST /your-endpoint/{id}` 같은 placeholder도 entity로 추출
- **문제**: 예제 코드의 placeholder가 실제 엔드포인트와 동급으로 검색에서 hit
- **개선 방향**:
  - placeholder 패턴 (`{xxx}` 비율이 path의 50% 이상, `your-`/`example-` prefix 등) 필터
  - 또는 코드블록 내 API는 별도 entity_type으로 (`api_example` vs `api`)
- **영향 범위**: API 문서의 그래프 노드 정밀도
- **심각도**: Medium | **공수**: S

---

### F-CG-10 (HIGH): body_extractor에서 self_entity가 항상 처음에 등록되지만, 같은 doc_title을 다른 문서에서 사용 시 충돌

- **위치**: `src/context_loop/processor/body_extractor.py:127-130, 174-178`
- **현재 동작**: `Entity(name=doc_title, entity_type="document")` 가 self-entity로 등록. GraphStore의 (name, entity_type) 병합에 의존
- **문제**: 다른 문서가 같은 제목을 가지면 (예: 여러 페이지가 "API Spec" 제목) → 같은 노드로 병합되어 모든 outgoing 관계가 한 노드에 모임 → 검색에서 "어느 문서인가" 구분 불가
- **개선 방향**:
  - self-entity 이름에 document_id suffix 또는 source_id 포함 (예: `API Spec [doc#42]`) — 표시 추악
  - 또는 별도 `entity_type="document"` 노드의 키를 (title, document_id)로 — 그러나 graph_store는 (name, entity_type)만 봄 → graph_store 변경 필요
  - 또는 confluence page_id 기반 정규화 (`title (page:12345)`)
- **영향 범위**: 동일 제목 페이지가 있는 워크스페이스
- **심각도**: High | **공수**: M (graph_store 영향 검토 필요)

---

### F-CG-11 (HIGH): llm_body_extractor의 stats가 부정확 — units_called가 dropped 항목도 카운트

- **위치**: `src/context_loop/processor/llm_body_extractor.py:199-203`
- **현재 동작**:
  ```python
  for unit, payload in results:
      if payload is None:
          stats.units_failed += 1
          continue
      stats.units_called += 1  # ← payload가 있으면 무조건 +1
  ```
- **문제**: units_called는 "LLM 호출이 성공하고 응답을 받은 unit 수"인데, 응답이 빈 객체이거나 어휘 외 항목만 있어도 카운트. 디버깅 어려움
- **개선 방향**:
  - "called"(호출했음) / "produced"(유효 entity/relation 산출) 통계 분리
  - 또는 units_called를 "성공한 LLM 호출 수"로 명확화 (현재 동작) + units_with_output 추가
- **영향 범위**: 운영 디버깅. 정확도 영향 없음
- **심각도**: Medium → High (운영 가시성)
- **공수**: S

---

### F-CG-12 (MEDIUM): body_extractor의 Relation.label이 첫 등장 unit의 section_path만 → 다중 등장 위치 손실

- **위치**: `src/context_loop/processor/body_extractor.py:343-349`
- **현재 동작**: dedup된 관계의 label은 처음 등장한 unit의 section_path. 같은 (source, target, type)이 여러 unit/섹션에 있어도 첫 라벨만 저장
- **문제**: 검색에서 "이 관계가 문서의 어디서 언급되는가" 추적이 한 위치로만 좁혀짐
- **개선 방향**:
  - 라벨을 "; "로 join하여 누적 (길이 제한 적용)
  - 또는 첫 + 마지막 위치만 보존
- **영향 범위**: 본문 그래프의 라벨 정보
- **심각도**: Medium | **공수**: S

---

### F-CG-13 (LOW): graph_vocabulary의 `import` relation_type 이름 — 동사 시제 일관성

- **위치**: `src/context_loop/processor/graph_vocabulary.py:88`
- **현재 동작**: `"import"` (명사/동사 원형). 다른 relation은 `depends_on`, `references` (동사 형용/3인칭)
- **문제**: ast_code_extractor가 사용하는 실제 relation은 `"imports"` (line 269) — 3인칭 단수. vocab(`"import"`)과 불일치
- **재현/근거**:
  ```python
  # ast_code_extractor.py:269
  relations: list[Relation] = [Relation(source=..., target=..., relation_type="imports") for imp in ...]
  # graph_vocabulary.py:88
  VocabEntry("import", "...", "ast_code"),
  ```
- **개선 방향**: vocab을 `"imports"`로 수정 (실제 데이터와 일치)
- **영향 범위**: graph_search_planner LLM 가이드의 정확성
- **심각도**: Low (현재는 vocab이 가이드 외 영향 없음) → High (graph_search_planner가 vocab으로 검증한다면)
- **공수**: S — vocab 한 줄 수정

---

## 검토하지 않은 영역

- graph_store.py 의 `(name, entity_type)` 병합 규칙 세부 (다른 분석 영역에서 다룰 수 있음)
- graph_search_planner가 vocab을 실제로 활용하는 방식
- mentions (ri:user, jira) 의 별도 활용 (현재 OutLink와 별도 list로만 존재, 직접 그래프에 사용 안 됨)
- code_blocks/tables 추출 결과의 활용 (chunker에서 직접 사용 안 함)
