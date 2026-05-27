# Confluence MCP — 그래프 추출 (5단계) 심층 분석

> 범위: `source_type='confluence_mcp'` 문서가 `processor/pipeline.py::process_document` 안의 **그래프 추출 블록**(라인 398~496)에서 어떻게 처리되는지를 단독으로 서술한다. 수집/전처리/청킹/임베딩/저장 자체의 동작은 다루지 않는다 — 단, 그래프 결과가 **저장소에 어떻게 머지되는지**(`graph_store.save_graph_data`)는 그래프 추출의 마지막 행위이므로 포함한다.
>
> 분석 전용. 개선 제안 없음. 모든 서술은 코드 인용 기반.

---

## 1. 개요 — 세 개의 서브-파이프라인

confluence_mcp 그래프 추출은 **세 개의 독립 서브-파이프라인이 같은 `document` 노드로 수렴**하도록 설계되어 있다. 셋 모두 `GraphData(entities, relations)`(`processor/graph_extractor.py:48`)를 산출하고, 셋 모두 동일 함수(`graph_store.save_graph_data(document_id, graph_data)`)로 저장된다. 저장소의 `(LOWER(name), entity_type)` 기반 정규 노드 병합 덕분에 self-document 엔티티(`Entity(name=doc_title, entity_type="document")`)가 동일 ID로 자연 통합된다.

세 서브-파이프라인과 `pipeline.py` 의 분기 위치는 다음과 같다.

| 서브-파이프라인 | 진입 라인 | 분기 조건 | LLM | 비고 |
|---|---|---|---|---|
| 5a. **링크 그래프** (`build_link_graph`) | `pipeline.py:399-415` | `extracted is not None and extracted.outbound_links` | ❌ | 결정론, 순수 함수 |
| 5b. **본문 휴리스틱 그래프** (`build_extraction_units` → `extract_body_graph`) | `pipeline.py:421-441` | `extracted is not None and extracted.sections` (그리고 `units` 비어있지 않음) | ❌ | 결정론, 정규식 기반 |
| 5c. **LLM 의미 그래프** (`extract_llm_body_graph_for_document`) | `pipeline.py:453-496` | `cfg.enable_llm_body_extraction and llm_client is not None` (5b 블록 안쪽, 즉 `extracted.sections`와 `units` 조건도 함께 충족 필요) | ✅ | 비용 발생, 폴백 경로 있음 |

세 블록은 모두 같은 `else`(git_code가 아닌 경로) 가지의 confluence 분기 안에 있다(`pipeline.py:153`의 `if source_type == "git_code"` 와 짝). `extracted`는 직전 전처리 단계(`extract_confluence(raw_html)`)에서 채워지며, `raw_content`가 없으면 `extracted=None` 상태로 남아 세 블록 모두 스킵된다.

전역 분기 가드 요약:

```
pipeline.py:399  if extracted is not None and extracted.outbound_links:   # 5a
pipeline.py:421  if extracted is not None and extracted.sections:          # 5b/5c 공통 가드
pipeline.py:425      if units:                                             # 5b/5c 둘 다 진입
pipeline.py:427          if body_graph.entities: ...                       # 5b 저장
pipeline.py:453          if cfg.enable_llm_body_extraction and llm_client: # 5c 진입
```

**핵심:** 5c는 5b와 동일한 가드(`extracted.sections`, `units` 존재) 하에서만 동작한다. 섹션이 하나도 없는(평문만 있는) 문서는 5b/5c가 모두 스킵된다. 단, 5a는 outbound_links만 있으면 단독으로 실행된다.

---

## 2. 5a. 링크 그래프 — `build_link_graph`

### 2.1 진입점
- 호출: `pipeline.py:400`  → `link_graph = build_link_graph(extracted, doc_title=title)`
- 정의: `processor/link_graph_builder.py:52` — `build_link_graph(extracted: ExtractedDocument, *, doc_title: str) -> GraphData`

### 2.2 입력
- `extracted.outbound_links: list[OutLink]` — 전처리 단계(`confluence_extractor.py::_extract_links_and_mentions`)가 HTML에서 추출한 링크 목록.
- `doc_title: str` — 현재 문서 제목. self-entity 이름으로 사용된다(`link_graph_builder.py:73-77`).

`doc_title`이 빈 문자열이면 즉시 `GraphData()`(빈) 반환(`link_graph_builder.py:66-67`).

### 2.3 처리 흐름
1. `_should_include(link)`(`link_graph_builder.py:118-120`)로 1차 필터. `link.kind in _KIND_TO_ENTITY_TYPE` 만 통과 — 즉 `page`/`user`/`jira`/`attachment` 4종만 유효. `kind=="url"` 인 외부 링크는 SKIP.
2. self-entity 생성: `Entity(name=doc_title, entity_type="document", description="")`(라인 73-77).
3. 각 outbound link에 대해:
   - `_target_name(link)`(라인 123-136)로 타겟 이름 결정. `page` 링크는 `target_title` 우선, 없으면 `f"page:{target_id}"`로 폴백. 그 외 kind는 `target_id` 그대로.
   - 타겟 이름이 비어있으면 그 링크는 스킵(라인 86-87).
   - `target_type`, `relation_type`을 매핑 테이블에서 조회(라인 88-89).
   - `_entity_key(name, type) = (name.strip().lower(), type)`(라인 139-141)를 키로 엔티티 dedup.
   - `(self_key, target_key, relation_type)` 3-튜플로 관계 dedup. 같은 target이 여러 섹션에 등장해도 첫 등장 섹션 경로만 `Relation.label`에 기록(라인 104-110, `_relation_label` 라인 157-161).
4. 산출: `GraphData(entities=[self+targets], relations=[...])`(라인 112-115).

### 2.4 매핑 테이블 (`link_graph_builder.py:37-49`)

| OutLink.kind | entity_type | relation_type |
|---|---|---|
| `page` | `document` | `references` |
| `user` | `person` | `mentions_user` |
| `jira` | `ticket` | `mentions_ticket` |
| `attachment` | `attachment` | `has_attachment` |
| `url` | (제외) | — |

### 2.5 정규화 규칙
- **엔티티 이름 정규화**: `_entity_key`에서 `name.strip().lower()`로 정규화 후 키 비교(라인 141). 표시 이름은 첫 등장 표기를 보존.
- **타입 정규화**: 없음. 매핑 테이블이 직접 enum 역할.
- **설명 정규화**: `_entity_description(link)`(라인 144-154)가 kind별 고정 문구 생성. 예: `page + target_space` → `"Confluence page in space {space}"`.

### 2.6 산출
`Entity(self) + Entity(target)*N`, `Relation(self→target)*M`. 추출된 outbound가 0이면 빈 그래프(라인 70-71).

---

## 3. 5b. 본문 휴리스틱 그래프 — `build_extraction_units` → `extract_body_graph`

5b는 두 단계로 구성된다: 먼저 `ExtractedDocument`를 추출에 적합한 단위(unit)로 묶고, 그 다음 unit 본문에서 정규식·휴리스틱으로 엔티티/관계를 뽑는다.

### 3.1 진입점
- 가드: `pipeline.py:421` — `if extracted is not None and extracted.sections:`
- Step 1 (unit 빌드): `pipeline.py:422-424`
  ```python
  units = build_extraction_units(
      extracted, document_id=document_id, doc_title=title,
  )
  ```
- Step 2 (그래프 추출): `pipeline.py:426` — `body_graph = extract_body_graph(units, doc_title=title)`
- 정의 위치: `processor/extraction_unit.py:147` (`build_extraction_units`), `processor/body_extractor.py:100` (`extract_body_graph`).

### 3.2 Step 1: ExtractionUnit 빌드 (`extraction_unit.py`)

#### 3.2.1 시그니처
```python
def build_extraction_units(
    extracted: ExtractedDocument, *,
    document_id: int,
    doc_title: str,
    config: ExtractionUnitConfig | None = None,
) -> list[ExtractionUnit]:                # extraction_unit.py:147-201
```

#### 3.2.2 `ExtractionUnitConfig` 기본값 (`extraction_unit.py:40-64`)

| 필드 | 기본값 | 설명 |
|---|---|---|
| `target_tokens` | **1500** (라인 56) | unit 목표 크기. 응축/분할 결정 기준. |
| `max_tokens` | **2400** (라인 57) | unit 상한. 단일 섹션 본문이 이를 초과하면 강제 분할. |
| `min_tokens` | 400 (라인 58) | 부모 own_body가 이 미만이면 첫 자식 unit에 prepend로 흡수. |
| `overlap_tokens` | 200 (라인 59) | 거대 섹션 분할 시 인접 part 간 토큰 overlap. |
| `breadcrumb_doc_title` | True (라인 60) | content 상단에 `# 문서: <title>` 포함. |
| `breadcrumb_path` | True (라인 61) | content 상단에 `## 위치: A > B > C` 포함. |
| `include_lead_paragraph` | True (라인 62) | 첫 헤딩 이전 머리말을 모든 unit에 prefix. |
| `lead_paragraph_max_tokens` | 200 (라인 63) | 머리말 최대 토큰. |
| `encoding_model` | `"cl100k_base"` (라인 64) | tiktoken 인코딩. |

#### 3.2.3 처리 흐름 (`extraction_unit.py:165-201`)
1. **lead paragraph 추출** — `_extract_lead_paragraph(extracted.plain_text)`(라인 578-585): 첫 `^#{1,6}\s+` 헤딩 직전까지 텍스트.
2. **sections-less 폴백** — `extracted.sections`가 비어 있으면 `_build_from_plain_text`(라인 617-667)로 전환. plain_text 길이가 `max_tokens` 이하면 1 unit, 초과면 `_split_oversized`로 문단 분할.
3. **트리 복원** — `_build_tree`(라인 209-245): H1~H6의 `Section.level` 기반 스택으로 부모-자식 트리 복원. 각 노드에 `own_body = _self_render(section)`(라인 248-256, 헤딩 + md_content) 캐시.
4. **bottom-up merged_tokens** — `_compute_merged_tokens`(라인 264-269): 후위 순회로 자신+자손 토큰 합 계산.
5. **top-down 응축/분할** — `_collect_units`(라인 277-379):
   - `merged_tokens <= target_tokens(1500)` 인 첫 노드는 **서브트리 전체를 한 unit으로 응축**(`_render_and_collect_ids`, 라인 382-401).
   - `own_tokens > max_tokens(2400)` 면 자기 본문을 `_split_oversized`(라인 409-453)로 문단 경계 분할, `overlap_tokens(200)`만큼 직전 part 꼬리를 다음 part 머리로 복제. 펜스 코드블록·테이블은 atomic 보호.
   - `0 < own_tokens < min_tokens(400)` + 자식 존재면 부모 own_body를 **첫 자식 unit 머리에 prepend**(absorb_pending, 라인 329-331/347-363).
6. **breadcrumb 주입 + 최종화** — `_finalize`(라인 475-520): unit content = `breadcrumb + "\n\n---\n\n" + body`. `unit_id = f"{document_id}:{ordinal:04d}"`.

#### 3.2.4 산출
`list[ExtractionUnit]` — `ExtractionUnit` 스키마(`extraction_unit.py:67-102`)는 `unit_id, document_id, ordinal, section_ids, primary_section_id, section_path, breadcrumb, content, body, token_count, has_table, has_code_block, split_part, split_total` 보유.

### 3.3 Step 2: `extract_body_graph` (`body_extractor.py:100`)

#### 3.3.1 시그니처
```python
def extract_body_graph(
    units: list[ExtractionUnit], *,
    doc_title: str,
    config: BodyExtractionConfig | None = None,
) -> GraphData:                            # body_extractor.py:100-180
```

#### 3.3.2 `BodyExtractionConfig` 기본값 (`body_extractor.py:61-92`)

| 필드 | 기본값 | 설명 |
|---|---|---|
| `extract_api_endpoints` | **True** (라인 85) | `GET /path` → `api` 엔티티, `documents` 관계. |
| `extract_jira_keys` | **True** (라인 86) | `PROJ-123` → `ticket`, `mentions_ticket` 관계. |
| `extract_bold_terms` | **False** (라인 87) | `**X**` / `__X__` → `concept`, `mentions`. 작성 컨벤션 의존도 높아 OFF. |
| `extract_table_headers` | **False** (라인 88) | 마크다운 표 헤더 셀 → `concept`, `has_attribute`. 추상 헤더 노이즈로 OFF. |
| `bold_min_length` / `bold_max_length` | 2 / 60 (라인 89-90) | 강조 용어 길이 가드. |
| `max_table_columns` | 12 (라인 91) | 표 헤더 셀 수 상한 초과 시 표 전체 스킵. |
| `skip_inside_code_blocks` | True (라인 92) | 펜스/인라인 코드는 prose에서 제외. **단 `extract_api_endpoints`만은 코드블록 내부도 스캔**(라인 80-82 docstring, 라인 144는 `unit.body` 사용). |

#### 3.3.3 정규식 (`body_extractor.py:42-50`)
- `_BOLD_RE = r"\*\*([^\n*]+?)\*\*|__([^\n_]+?)__"`
- `_API_RE = r"\b(GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)\s+(/[A-Za-z0-9_\-/{}:.~]+)"`
- `_JIRA_RE = r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b"`
- `_CODE_FENCE_RE = r"```.*?```" (DOTALL)`, `_INLINE_CODE_RE = r"\`[^\`\n]+\`"`

#### 3.3.4 처리 흐름 (`body_extractor.py:118-180`)
1. `doc_title`이 비었거나 `units`이 비었으면 빈 그래프(라인 119-120).
2. self-entity = `Entity(name=doc_title, entity_type="document")`(라인 128). 자신 키는 `(doc_title.lower(), "document")`(라인 127).
3. 각 unit 순회:
   - `section_path_label = " > ".join(unit.section_path)`(라인 131) — 관계 label로 사용.
   - `prose` = `_strip_code_for_prose(unit.body)` if `skip_inside_code_blocks` else `unit.body` (라인 132).
   - `extract_bold_terms` ON: `_find_bold_terms(prose, cfg)`(라인 188-202) → `concept`/`mentions`(라인 134-141).
   - `extract_api_endpoints` ON: `_find_api_endpoints(unit.body)`(라인 225-239, **prose 아닌 raw body**) → `api`/`documents`(라인 143-150).
   - `extract_table_headers` ON: `_find_table_headers(unit.body, cfg)`(라인 242-266) → `concept`/`has_attribute`(라인 152-159).
   - `extract_jira_keys` ON: `_find_jira_keys(prose)`(라인 279-289) → `ticket`/`mentions_ticket`(라인 161-168).
4. **self-entity 미방출 규칙** (`body_extractor.py:170-172`): 추출 관계가 0이면 self-entity도 emit하지 않고 빈 그래프 반환. (`link_graph_builder`와 동일 정책.)
5. self-entity는 결과 entity 리스트의 첫 번째로 prepend(라인 175-178).

#### 3.3.5 엔티티/관계 dedup (`_add`, `body_extractor.py:312-349`)
- 엔티티 키: `(target_name.lower(), target_type)`.
- 관계 키: `(self_name.lower(), target_name.lower(), relation_type)` 3-튜플 — 같은 self→target 관계는 첫 unit에서 발견된 `section_path_label`만 유지.

#### 3.3.6 unit별 누적
- `extract_body_graph`는 모든 unit을 같은 `entities`/`relations` 딕셔너리에 누적한다. 즉 **단일 `GraphData` 한 개**가 모든 unit 시그널을 합친 결과로 반환된다. 호출자(`pipeline.py:428-430`)는 `await graph_store.save_graph_data(document_id, body_graph)` 단 한 번만 호출.

### 3.4 산출 (5b 전체)
unit별로 그래프를 따로 만드는 게 아니라 **문서 단위 단일 GraphData**. relations이 비면 self-entity까지 미방출.

---

## 4. 5c. LLM 의미 그래프 — `extract_llm_body_graph_for_document` (+ unit 폴백)

### 4.1 진입 조건 (`pipeline.py:453`)
```python
if cfg.enable_llm_body_extraction and llm_client is not None:
```
- `PipelineConfig.enable_llm_body_extraction` 기본값 **True**(`pipeline.py:80`).
- `llm_client`는 `process_document(..., llm_client=None)`(`pipeline.py:101`) 인자로 주입. 호출자가 주입하지 않으면 5c 전체 스킵.
- 추가 가드: `pipeline.py:421/425`의 `extracted.sections` + `units` 존재 (5b와 같은 블록 안).

### 4.2 정상 경로 — 문서 단위 1회 호출

#### 4.2.1 입력 본문 조립
`pipeline.py:454` — `doc_body = _assemble_document_body(extracted)` (`pipeline.py:551-572`).
- `extracted.sections` 있으면 각 섹션의 `"#"*level + " " + title + "\n\n" + md_content.strip()` 을 트리 순서로 `"\n\n"`로 join.
- 섹션 없으면 `extracted.plain_text` 그대로.
- breadcrumb(문서 제목/위치/lead) 추가하지 않음 — 문서 제목은 user prompt의 `# 문서 제목` 필드로 별도 노출.

#### 4.2.2 호출
`pipeline.py:456-462`:
```python
llm_graph, llm_stats = await extract_llm_body_graph_for_document(
    doc_title=title,
    body=doc_body,
    llm_client=llm_client,
)
```
정의: `llm_body_extractor.py:277-421`.

#### 4.2.3 입력 한도 검사
`llm_body_extractor.py:315-319`:
```python
body_tokens = count_tokens(body)
if body_tokens > cfg.max_input_tokens:    # 기본 200_000
    raise InputTooLargeError(
        f"문서 본문 {body_tokens} 토큰 > 한도 {cfg.max_input_tokens}",
    )
```
- `max_input_tokens=200_000` (`llm_body_extractor.py:88`). 256K 컨텍스트 모델 기준 시스템 프롬프트(~500) + 응답(32768) + 마진을 뺀 값.
- 한도 초과 시 `InputTooLargeError`를 raise; 호출자(`pipeline.py:471-480`)가 잡아 unit 단위 폴백으로 전환.

#### 4.2.4 LLM 호출
`llm_body_extractor.py:332-339`:
```python
response = await llm_client.complete(
    user,
    system=system,
    max_tokens=cfg.max_tokens,        # 32768
    temperature=cfg.temperature,      # 0.0
    reasoning_mode="off",
    purpose="body_extraction_doc",
)
```
- `max_tokens=32768` (`llm_body_extractor.py:82`). reasoning 모델의 thinking 토큰까지 고려한 큰 값.
- `temperature=0.0` (라인 83). 결정적.
- `reasoning_mode="off"` (라인 337). Qwen3/DeepSeek 등 reasoning 모드를 끔 — JSON 응답이 thinking에 토큰을 다 쓰고 잘리는 문제 방지(`_call_llm` docstring 라인 457-463).

#### 4.2.5 JSON 파싱
`llm_body_extractor.py:340-344`: `payload = extract_json(response)`. dict가 아니면 `ValueError`. 예외 시 `units_failed = 1`로 기록하고 빈 그래프 반환(라인 345-350).

#### 4.2.6 어휘 검증·등록
- `allowed_etypes = set(cfg.allowed_entity_types)` / `allowed_rtypes = set(cfg.allowed_relation_types)`(라인 359-360).
- raw entities 순회(라인 365-380): `name`+`type` 추출, `type not in allowed_etypes`면 `dropped_entities += 1` 후 스킵. 통과한 이름은 `valid_names` 셋에 등록.
- raw relations 순회(라인 383-397): `source`/`target`이 동일 문서의 `valid_names`에 둘 다 있어야 하고, `rtype in allowed_rtypes`여야 하며, self-loop(`src.lower() == tgt.lower()`) 금지. 위반은 `dropped_relations += 1`.
- 관계 정규 표기: `_canonical_name`(라인 522-531) — 등록된 entity의 표기를 우선 사용(대소문자 일관성).
- 문서 단위 경로는 `section_path`를 특정할 수 없어 `Relation.label=""` 빈 라벨(라인 402-410, 주석 참고).

### 4.3 폴백 경로 — unit 단위 호출

`pipeline.py:471-480`에서 `InputTooLargeError`를 캐치:
```python
except InputTooLargeError:
    logger.info("문서 본문이 LLM 입력 한도 초과 — unit 기반 폴백 ...")
    llm_graph, llm_stats = await extract_llm_body_graph(
        units, doc_title=title, llm_client=llm_client,
    )
```

`extract_llm_body_graph(units, ...)` 정의: `llm_body_extractor.py:152-274`.

#### 4.3.1 비용 게이트 — `_gate_units` (`llm_body_extractor.py:429-447`)
```python
for unit in units:
    if unit.token_count < cfg.min_unit_tokens:         # 기본 200
        stats.units_skipped_short += 1
        continue
    if cfg.skip_split_overlap_parts and unit.split_total > 1 and unit.split_part > 0:
        stats.units_skipped_overlap += 1
        continue
    targets.append(unit)
if cfg.max_units_per_doc is not None and len(targets) > cfg.max_units_per_doc:
    targets = targets[: cfg.max_units_per_doc]         # 기본 None=무제한
```
- `min_unit_tokens=200` (`llm_body_extractor.py:73`) — 응축된 미니 섹션은 의미 관계가 거의 없어 스킵.
- `skip_split_overlap_parts=True` (라인 74) — 거대 섹션 분할의 `split_part > 0`인 part는 overlap으로 첫 part와 중복 추출되어 비용 낭비이므로 스킵.

#### 4.3.2 동시성 제어 (`llm_body_extractor.py:181`)
```python
sem = asyncio.Semaphore(max(1, cfg.max_concurrency))   # 기본 3
```
unit별로 `_call_llm`을 `asyncio.gather` + 세마포어로 병렬 실행.

#### 4.3.3 unit별 처리
`_call_llm`(라인 450-488)은 `extract_llm_body_graph_for_document`의 단일 호출과 동일한 시스템 프롬프트, `max_tokens=32768`, `temperature=0.0`, `reasoning_mode="off"`를 사용하되 `purpose="body_extraction"`. user prompt에 `unit.content`(breadcrumb 포함)를 넣는다.

unit별 응답에서 추출된 엔티티 검증은 문서 단위와 동일하나, **관계 검증의 valid_names 셋이 unit 단위로 리셋**(`unit_valid_entity_names`, 라인 216)되어 cross-unit 관계는 허용되지 않는다. unit별 `section_label = " > ".join(unit.section_path)`(라인 235)가 Relation.label로 기록된다 — 문서 단위 경로(빈 label)와 다른 점.

### 4.4 LLM 프롬프트 구성

#### 4.4.1 System prompt (`llm_body_extractor.py:108-136`)
- 템플릿 `_SYSTEM_PROMPT_TEMPLATE`에 `{entity_types}`와 `{relation_types}` 슬롯.
- `_format_vocab_with_descriptions(cfg.allowed_entity_types, llm_body_entity_types_vocab())`(라인 501-519)로 어휘를 `- **name**: description` 형태로 렌더.
- 출력 형식 강제: JSON object with `entities`(name/type/description) and `relations`(source/target/type).
- 규칙(라인 117-123): "본문에 명시적으로 등장하는 관계만", "약어보다 풀네임", "본문에 언급되지 않은 끝점 금지", "어휘 외 타입 금지", "시그널 없으면 빈 배열".

#### 4.4.2 User prompt (`llm_body_extractor.py:138-144`)
```
# 문서 제목
{doc_title}

# 본문
{body}
```
문서 단위 경로는 `body=doc_body`, unit 폴백 경로는 `body=unit.content`(breadcrumb 포함).

### 4.5 어휘 통제 — `graph_vocabulary.py`

`llm_body_extractor`는 자체 어휘 상수를 들고 있지 않고 `graph_vocabulary` 모듈에서 가져온다(`llm_body_extractor.py:29-34, 49-50`):
```python
_DEFAULT_ENTITY_TYPES = llm_body_entity_type_names()
_DEFAULT_RELATION_TYPES = llm_body_relation_type_names()
```
`LLMBodyExtractionConfig.allowed_entity_types/allowed_relation_types`의 기본값이 이것들(라인 71-72).

#### 4.5.1 전체 어휘 정의 (`graph_vocabulary.py:38-99`)

**ENTITY_TYPES — 총 15종** (라인 38-67):
`document`, `person`, `ticket`, `attachment`, `api`, `concept`, `system`, `module`, `policy`, `team`, `function`, `class`, `method`, `struct`, `interface`.

각 항목은 `VocabEntry(name, description, source)` 튜플. `source` 필드에 `"llm_body"`가 포함된 항목만 `llm_body_entity_types_vocab()`(라인 169-174)가 필터링하여 반환 — 즉 LLM 본문 추출기는 다음 9종만 본다: `person`, `api`, `concept`, `system`, `module`, `policy`, `team` (`llm_body`를 source에 포함하는 항목). 정확한 set은 `_has_source(entry, "llm_body")` 필터 결과:

| name | source 필드 | LLM에 노출? |
|---|---|---|
| document | "link_graph + body" | ❌ |
| person | "link_graph + llm_body" | ✅ |
| ticket | "link_graph + body" | ❌ |
| attachment | "link_graph" | ❌ |
| api | "body + llm_body" | ✅ |
| concept | "body + llm_body" | ✅ |
| system | "llm_body" | ✅ |
| module | "llm_body + ast_code" | ✅ |
| policy | "llm_body" | ✅ |
| team | "llm_body" | ✅ |
| function/class/method/struct/interface | "ast_code" | ❌ |

**RELATION_TYPES — 총 19종** (라인 74-99):
`references`, `mentions_user`, `mentions_ticket`, `has_attachment`, `mentions`, `documents`, `has_attribute`, `depends_on`, `implements`, `calls`, `owned_by`, `supersedes`, `has_part`, `uses`, `provides`, `documented_in`, `imports`, `contains` — **18종으로 보이지만 코드상 19개 VocabEntry 정의 존재**(주의: `references` ~ `documented_in`까지 16개 + `imports`, `contains` 2개 = 18개. 코드 검수 결과 실제 18개. 사용자 입력의 "19종"은 사용자 표기 — 실제 카운트는 `RELATION_TYPES` 튜플 길이로 검증 가능. 본 보고서는 코드 그대로 18개를 기록.)

> 정정: `graph_vocabulary.py:74-99`의 `RELATION_TYPES` 튜플 라인을 한 줄씩 세면 `references, mentions_user, mentions_ticket, has_attachment, mentions, documents, has_attribute, depends_on, implements, calls, owned_by, supersedes, has_part, uses, provides, documented_in, imports, contains` 로 **18개**다. 사용자 요청의 "19종" 표기는 코드 진실과 다르며 본 분석은 코드를 따른다.

`llm_body_relation_types_vocab()`(라인 177-179)는 `source`에 `"llm_body"`가 포함된 9종만 반환: `depends_on`, `implements`, `calls`, `owned_by`, `supersedes`, `has_part`, `uses`, `provides`, `documented_in`.

#### 4.5.2 어휘 강제 메커니즘
1. **프롬프트 노출**: system prompt에 `_format_vocab_with_descriptions`로 허용 목록을 명시.
2. **출력 필터**: `etype not in allowed_etypes` / `rtype not in allowed_rtypes`인 항목은 drop, `stats.dropped_entities/dropped_relations`로 카운트(`llm_body_extractor.py:223-225, 244-251, 371-373, 390-397`).
3. **끝점 무결성**: relation의 source/target이 같은 호출에서 통과한 valid_names에 없으면 drop.

### 4.6 통계 (`LLMBodyExtractionStats`, `llm_body_extractor.py:91-105`)
`units_total, units_skipped_short, units_skipped_overlap, units_called, units_failed, raw_entities, raw_relations, dropped_entities, dropped_relations, final_entities, final_relations`. pipeline 로그에 `units_called/units_total`이 출력된다(`pipeline.py:494-495`).

---

## 5. 3중 그래프의 머지/저장

### 5.1 저장 호출 패턴
세 서브-파이프라인 각각이 별도로 `graph_store.save_graph_data(document_id, graph_data)`를 호출한다.

| 서브 | 호출 라인 | 가드 |
|---|---|---|
| 5a 링크 | `pipeline.py:402-404` | `if link_graph.entities:` (라인 401) |
| 5b 본문 | `pipeline.py:428-430` | `if body_graph.entities:` (라인 427) |
| 5c LLM | `pipeline.py:482-484` | `if llm_graph.entities:` (라인 481) |

세 호출 모두 `await` — 순차 실행. 같은 `document_id`로 호출되므로 각 호출의 노드 병합/엣지 추가가 누적 적용된다.

### 5.2 정규 노드 병합 — `graph_store.save_graph_data`

정의: `storage/graph_store.py:160-214` (엔티티 단계), 그 뒤 엣지 단계.

엔티티 단계 흐름:
1. `existing = await self._store.find_graph_node_by_entity(entity.name, entity.entity_type)`(라인 164-166) — 메타스토어에서 `(LOWER(name), entity_type)` 매칭으로 기존 노드 검색.
2. 기존 노드가 있으면:
   - `merged_count += 1` (라인 170).
   - `description` 보강: 기존이 비어있으면 새 description으로 채움(라인 173-177).
   - `add_node_document_link(node_id, document_id)`로 문서 연결 추가(라인 179) — **다른 문서에서 들어오는 같은 엔티티가 같은 노드로 수렴하는 메커니즘**.
   - NetworkX 인메모리 그래프 갱신(라인 181-192).
3. 신규 노드:
   - `create_graph_node_with_link`(라인 199-204) — `graph_nodes` INSERT + `graph_node_documents` link INSERT를 단일 트랜잭션으로(주석 라인 195-198 race 방지). `new_count += 1`.
4. `name_to_node_id[entity.name] = node_id`(라인 214) — 엣지 매칭용 매핑.

엣지 단계(라인 216~):
- `relation.source/target`을 위 매핑으로 변환해 엣지 INSERT.
- 같은 (src, tgt, relation_type, document_id) 엣지는 중복 방지.

반환값(`storage/graph_store.py:152-154` docstring):
```
{"nodes": 생성된 노드 수, "edges": 생성된 엣지 수, "merged": 기존 노드에 병합된 수}
```

### 5.3 3중 그래프가 같은 document 노드로 수렴하는 이유
- 5a, 5b 모두 `Entity(name=doc_title, entity_type="document")`를 self-entity로 emit.
- 5c는 `document` 타입을 LLM 어휘에 노출하지 않으므로 self-entity를 직접 만들지 않지만, **같은 `document_id`로 저장하기 때문에** `add_node_document_link`가 5a/5b가 만든 self 노드에 5c의 다른 엔티티들도 문서 단위로 연결한다.
- 키 정규화: `find_graph_node_by_entity`가 `LOWER(name)` + 정확한 `entity_type` 매칭을 한다(주석 `link_graph_builder.py:140-141` 및 `body_extractor.py:127`에서 동일 키 사용 확인).

### 5.4 pipeline 메트릭 누적 (`pipeline.py:407-486`)
```python
# 5a
link_node_count = link_result["nodes"]
link_edge_count = link_result["edges"]
node_count += link_node_count        # pipeline.py:407
edge_count += link_edge_count        # pipeline.py:408
# 5b
node_count += body_result["nodes"]   # pipeline.py:431
edge_count += body_result["edges"]   # pipeline.py:432
# 5c
node_count += llm_result["nodes"]    # pipeline.py:485
edge_count += llm_result["edges"]    # pipeline.py:486
```
각 서브가 자기 직접 추가한 노드/엣지 수를 반환하며 pipeline이 합산. `merged`는 카운터에 누적되지 않고 로그(`merged=%d`)로만 출력된다(`pipeline.py:414, 439, 493`).

`link_node_count`/`link_edge_count`는 process_document 반환 dict에 별도 필드로 노출(`pipeline.py:127-128` docstring) — 링크 그래프만 별도 메트릭이 따로 유지됨.

### 5.5 storage_method 파생
`_derive_storage_method(has_chunks=chunk_count>0, has_graph=node_count>0)`(`pipeline.py:498-501, 542-548`):
- chunks + graph 모두 있으면 `"hybrid"`.
- graph만 있으면 `"graph"`.
- 그 외 `"chunk"`. confluence는 청크가 (거의) 항상 있고 5a/5b/5c 중 하나라도 노드를 만들면 `"hybrid"`.

---

## 6. 데이터 타입 참조 표

| 타입 | 정의 위치 | 필드 |
|---|---|---|
| `Entity` | `processor/graph_extractor.py:15` (dataclass, 라인 15 데코레이터, 클래스 라인 16) | `name: str`, `entity_type: str = "other"`, `description: str = ""` |
| `Relation` | `processor/graph_extractor.py:30` | `source: str`, `target: str`, `relation_type: str = "related_to"`, `label: str = ""` |
| `GraphData` | `processor/graph_extractor.py:47` | `entities: list[Entity]`, `relations: list[Relation]` |
| `ExtractionUnit` | `processor/extraction_unit.py:67` | `unit_id, document_id, ordinal, section_ids, primary_section_id, section_path, breadcrumb, content, body, token_count, has_table, has_code_block, split_part, split_total` |
| `ExtractionUnitConfig` | `processor/extraction_unit.py:40` | `target_tokens=1500, max_tokens=2400, min_tokens=400, overlap_tokens=200, ...` |
| `BodyExtractionConfig` | `processor/body_extractor.py:61` | api=True, jira=True, bold=False, table=False, ... |
| `LLMBodyExtractionConfig` | `processor/llm_body_extractor.py:53` | allowed_entity_types, allowed_relation_types, min_unit_tokens=200, max_input_tokens=200_000, max_tokens=32768, temperature=0.0, max_concurrency=3 |
| `LLMBodyExtractionStats` | `processor/llm_body_extractor.py:91` | units_total, units_called, units_failed, raw_entities, dropped_entities, ... |
| `VocabEntry` | `processor/graph_vocabulary.py:19` | `name, description, source` |
| `ENTITY_TYPES` (어휘 enum) | `processor/graph_vocabulary.py:38` (튜플) | 15 entries |
| `RELATION_TYPES` (어휘 enum) | `processor/graph_vocabulary.py:74` (튜플) | 18 entries (코드 기준) |
| `InputTooLargeError` | `processor/llm_body_extractor.py:38` | `Exception` 서브클래스 |

---

## 7. 호출 그래프 다이어그램 (ASCII)

```
pipeline.process_document(document_id, ..., llm_client=...)        pipeline.py:93
│
├─ (앞단: 수집/전처리/청킹/임베딩/저장 — 본 보고서 범위 외)
│  └─ extracted: ExtractedDocument | None
│
├─ [5a] if extracted and extracted.outbound_links:                 pipeline.py:399
│       ├─ link_graph = build_link_graph(extracted, doc_title)     link_graph_builder.py:52
│       │   ├─ self_entity = Entity(doc_title, "document")
│       │   ├─ for link in extracted.outbound_links:
│       │   │   ├─ _should_include(link)  # url 제외                  :118
│       │   │   ├─ _target_name(link)                                  :123
│       │   │   ├─ map kind→entity_type/relation_type                  :37-49
│       │   │   └─ dedup by (name.lower(), type) / (src,tgt,rel)
│       │   └─ return GraphData(entities, relations)
│       └─ await graph_store.save_graph_data(document_id, link_graph)
│           └─ graph_store.py:160-214 — LOWER(name)+type 병합, document_id 링크
│
├─ [5b] if extracted and extracted.sections:                       pipeline.py:421
│       ├─ units = build_extraction_units(extracted, ...)          extraction_unit.py:147
│       │   ├─ _extract_lead_paragraph(plain_text)                  :578
│       │   ├─ _build_tree(sections)                                 :209  (H1~H6 스택)
│       │   ├─ _compute_merged_tokens(root)  # bottom-up              :264
│       │   ├─ _collect_units(root, ...)     # top-down 응축/분할     :277
│       │   │   ├─ merged_tokens <= 1500 → 서브트리 한 unit
│       │   │   ├─ own_tokens > 2400  → _split_oversized (overlap 200)
│       │   │   └─ own < 400 + 자식  → 첫 자식에 prepend
│       │   └─ _finalize → ExtractionUnit[]
│       │
│       ├─ if units:                                                pipeline.py:425
│       │   │
│       │   ├─ body_graph = extract_body_graph(units, doc_title)   body_extractor.py:100
│       │   │   ├─ for unit:
│       │   │   │   ├─ prose = _strip_code_for_prose(unit.body)
│       │   │   │   ├─ _find_api_endpoints(unit.body)     [ON]      → api / documents
│       │   │   │   ├─ _find_jira_keys(prose)             [ON]      → ticket / mentions_ticket
│       │   │   │   ├─ _find_bold_terms(prose)            [OFF]
│       │   │   │   └─ _find_table_headers(unit.body)     [OFF]
│       │   │   ├─ if no relations: return GraphData()  # self도 미방출  :170
│       │   │   └─ return GraphData([self, ...], [...])
│       │   └─ await graph_store.save_graph_data(document_id, body_graph)
│       │
│       └─ [5c] if cfg.enable_llm_body_extraction and llm_client:  pipeline.py:453
│               ├─ doc_body = _assemble_document_body(extracted)   pipeline.py:551
│               │
│               ├─ try:
│               │   llm_graph, stats = await extract_llm_body_graph_for_document(
│               │       doc_title=title, body=doc_body, llm_client=llm_client,
│               │   )                                              llm_body_extractor.py:277
│               │   ├─ if count_tokens(body) > 200_000:
│               │   │     raise InputTooLargeError                  :316-319
│               │   ├─ system = _SYSTEM_PROMPT_TEMPLATE.format(
│               │   │     entity_types=llm_body_entity_types_vocab(),  # 9종
│               │   │     relation_types=llm_body_relation_types_vocab() # 9종
│               │   │   )                                           :321
│               │   ├─ user = "# 문서 제목\n{title}\n\n# 본문\n{body}"
│               │   ├─ await llm_client.complete(
│               │   │     user, system, max_tokens=32768,
│               │   │     temperature=0.0, reasoning_mode="off",
│               │   │     purpose="body_extraction_doc")             :332
│               │   ├─ extract_json(response)
│               │   ├─ filter raw entities by allowed_etypes
│               │   ├─ filter raw relations by allowed_rtypes
│               │   │   + 끝점이 valid_names에 있는지 + no self-loop
│               │   └─ return (GraphData, stats)
│               │
│               ├─ except InputTooLargeError:                       pipeline.py:471
│               │   llm_graph, stats = await extract_llm_body_graph(
│               │       units, doc_title, llm_client)                llm_body_extractor.py:152
│               │   ├─ _gate_units(units, cfg, stats)                :429
│               │   │   ├─ skip if token_count < 200
│               │   │   └─ skip if split_total>1 and split_part>0
│               │   ├─ asyncio.Semaphore(max_concurrency=3)
│               │   ├─ asyncio.gather(*[_call_llm(u) for u in targets])
│               │   │   └─ _call_llm: user=unit.content, system 동일
│               │   └─ merge per-unit results into single GraphData
│               │
│               └─ if llm_graph.entities:
│                     await graph_store.save_graph_data(document_id, llm_graph)
│                                                                   pipeline.py:482
│
└─ _derive_storage_method(has_chunks, has_graph) → "hybrid" 등      pipeline.py:542
```

---

## 부록: 결정론 vs LLM 어휘 매핑

| 서브 | self-entity 방출 | 사용 entity_type | 사용 relation_type |
|---|---|---|---|
| 5a 링크 | 항상 (outbound 있으면) | document, person, ticket, attachment | references, mentions_user, mentions_ticket, has_attachment |
| 5b 본문 | 추출 시그널 있을 때만 | document(self), api, concept, ticket | documents, mentions, has_attribute, mentions_ticket |
| 5c LLM | 직접 방출 안 함 (어휘에 document 없음) | person, api, concept, system, module, policy, team (9종 중 LLM이 선택) | depends_on, implements, calls, owned_by, supersedes, has_part, uses, provides, documented_in (9종) |

세 서브의 결과는 `graph_store`에서 `(LOWER(name), entity_type)` 정규화로 자연 통합. 같은 `document_id`로 저장되므로 `graph_node_documents` 링크 테이블이 세 서브의 모든 노드를 한 문서에 묶어준다.
