# R4 — Confluence 그래프 추출 4-패치 설계서

> implementer 가 그대로 적용할 수 있도록, 4개 미적용 패치(F-CG-04, F-CG2-08,
> F-CG2-02/04, F-CG2-06)의 코드 변경을 *위치 / 의사 코드 / 데이터 / 테스트*
> 단위로 명세한다. 01_analysis.md 의 회귀 위험 평가를 그대로 따른다.

---

## 1. 변경 파일 전체 목록 (sanity check)

| 파일 | 변경 라인 수 (추정) | 변경 종류 |
|---|---|---|
| `src/context_loop/processor/graph_vocabulary.py` | +50 ~ +60 | 신규 상수(alias 표) + 정규화 헬퍼 2개 + stem 정규화 헬퍼 1개 |
| `src/context_loop/processor/llm_body_extractor.py` | +35 ~ +45 / 수정 ~15 | 변수 스코프 이동, 두 경로에 정규화/스템 적용, 신규 예외, JSON 폴백 시그널 |
| `src/context_loop/processor/pipeline.py` | +10 ~ +15 | `OutputTruncatedError` catch 분기 추가 |
| `tests/test_processor/test_llm_body_extractor.py` | +120 ~ +150 (4건 신규) | 새 fixture 4건 + 기존 1건 의미 갱신 |
| `tests/test_processor/test_graph_vocabulary.py` | +25 ~ +40 | alias 매핑 / stem 정규화 / 1:1 unique 검증 단위 테스트 |

신규 파일 없음. 모든 변경은 `processor/` 모듈 내 폐쇄형.

---

## 2. 패치별 상세 설계

### 2.1 F-CG-04 — `valid_entity_names` 의 문서 누적 스코프 전환

**목적**: unit 경로에서 cross-unit relation 의 끝점 검증이 매 unit 초기화되어
드롭되던 문제를 문서 누적 set 으로 해소.

**변경 위치**: `llm_body_extractor.py:216, 232, 246-247` (`extract_llm_body_graph`).

**변경 전 → 변경 후 의사 코드**:

```python
# 변경 전 (라인 204-)
for unit, payload in results:
    ...
    unit_valid_entity_names: set[str] = set()        # <-- 매 루프 초기화
    for ent in raw_entities:
        ...
        unit_valid_entity_names.add(name.lower())
    ...
    for rel in raw_relations:
        ...
        if ( ... or src.lower() not in unit_valid_entity_names
                 or tgt.lower() not in unit_valid_entity_names ... ):
            stats.dropped_relations += 1
            continue
```

```python
# 변경 후
document_valid_entity_names: set[str] = set()        # <-- 루프 밖 (문서 누적)

for unit, payload in results:
    ...
    for ent in raw_entities:
        ...
        document_valid_entity_names.add(name.lower())
    ...
    for rel in raw_relations:
        ...
        if ( ... or src.lower() not in document_valid_entity_names
                 or tgt.lower() not in document_valid_entity_names ... ):
            stats.dropped_relations += 1
            continue
```

**새 함수/필드**: 없음. 변수명 변경 1건만.

**데이터**: 없음.

**상호 영향**:
- F-CG2-06 의 dedup 키 stem 화 *전에* 적용해야 안전 (스코프 이동만으로 의미
  변화 단순). 단 dedup 키 정규화가 들어간 뒤에는 `document_valid_entity_names`
  도 같은 stem 정규화를 적용해야 endpoint 검증이 dedup 키와 정합.
  → **F-CG-04 먼저, F-CG2-06 뒤에 stem 정규화 동시 적용** 순서 권고.
- 함수 호출이 1 문서당 1회이므로 문서 간 누출 없음(01_analysis 검증 완료).

---

### 2.2 F-CG2-08 — relation_type / entity_type alias 정규화

**목적**: LLM 이 만들어내는 형태론적 변형(`depending_on`, `policies` 등)을
canonical vocab 로 정규화하여 vocab 검증에서 탈락 방지.

**변경 위치**:
- `graph_vocabulary.py` 끝부분 (라인 199 뒤) — alias 매핑 상수 + 정규화 헬퍼 2개 추가.
- `llm_body_extractor.py:222-223` (unit 경로 entity 등록), `:243-245` (unit
  경로 relation 검증), `:370-371` (문서 경로 entity 등록), `:389-391`
  (문서 경로 relation 검증) — 4곳 동시 적용.

**변경 전 → 변경 후 의사 코드 (vocab 측)**:

```python
# graph_vocabulary.py 신규 상수
ENTITY_TYPE_ALIASES: dict[str, str] = {
    # canonical 자기 자신 등록은 불필요 — strip+lower 후 매핑 없으면 원본 그대로.
    "policies": "policy",
    "rules":    "policy",
    "components": "module",
    "component":  "module",
    # entity_type 은 의미 위험이 큰 매핑이 많아 보수적으로 2건만 등록.
    # `service`, `user`, `group` 등은 도메인 충돌 가능 → 미등록 (drop 유지).
}

RELATION_TYPE_ALIASES: dict[str, str] = {
    # F-CG2-08 보고서 인용 + 01_analysis 5절 화이트리스트
    "depending_on":      "depends_on",
    "depend_on":         "depends_on",
    "dependent_on":      "depends_on",
    "owns":              "owned_by",   # 방향 같음 (A owned_by B == A owns_by B, 추측: 보존)
    "owner":             "owned_by",
    "implement":         "implements",
    "implementing":      "implements",
    "documents_in":      "documented_in",
    "described_in":      "documented_in",
    # 방향 반전 (has_part ↔ part_of) / 동의어 (offers ↔ provides) 는 본 라운드 제외.
}

def normalize_entity_type(raw: str) -> str:
    """LLM 출력 entity_type 을 canonical 어휘로 정규화.

    1. ``raw.strip().lower()`` 후 ENTITY_TYPE_ALIASES 매핑.
    2. 매핑 없으면 정규화 전 raw (양끝 공백만 제거) 그대로 반환.
    호출자 측에서 canonical 결과를 다시 ``allowed_etypes`` 와 비교한다.
    """
    key = raw.strip().lower()
    return ENTITY_TYPE_ALIASES.get(key, raw.strip())

def normalize_relation_type(raw: str) -> str:
    """LLM 출력 relation_type 을 canonical 어휘로 정규화. 동일 정책."""
    key = raw.strip().lower()
    return RELATION_TYPE_ALIASES.get(key, raw.strip())
```

```python
# llm_body_extractor.py — unit 경로 (라인 222 부근) 적용 예
# 변경 전
etype = str(ent.get("type", "")).strip()
if not name or etype not in allowed_etypes:
    stats.dropped_entities += 1
    continue

# 변경 후
etype_raw = str(ent.get("type", "")).strip()
etype = normalize_entity_type(etype_raw)
if not name or etype not in allowed_etypes:
    stats.dropped_entities += 1
    continue
```

```python
# llm_body_extractor.py — unit 경로 relation 검증 (라인 243-245 부근)
# 변경 전
rtype = str(rel.get("type", "")).strip()
if ( ... rtype not in allowed_rtypes ... ):

# 변경 후
rtype_raw = str(rel.get("type", "")).strip()
rtype = normalize_relation_type(rtype_raw)
if ( ... rtype not in allowed_rtypes ... ):
```

문서 경로(`extract_llm_body_graph_for_document`, 라인 370/389) 도 *동일한*
2줄 패턴 적용 — 한 곳에 빠뜨리면 두 경로가 어긋난다.

**새 함수/필드**:
- `graph_vocabulary.normalize_entity_type(raw: str) -> str`
- `graph_vocabulary.normalize_relation_type(raw: str) -> str`
- 두 매핑 상수 `ENTITY_TYPE_ALIASES`, `RELATION_TYPE_ALIASES` (dict[str, str], 보수적 화이트리스트).

**데이터 (alias 표 — 8건 확정)**:

| canonical | alias (LLM 변형) | 비고 |
|---|---|---|
| `depends_on` | `depending_on`, `depend_on`, `dependent_on` | 동명사/현재형/형용사 변형 (보고서 인용) |
| `owned_by` | `owner`, `owns` | 방향 동일. `owns` 은 보수적 — implementer 가 위험하다 판단하면 빠뜨려도 무방 |
| `implements` | `implement`, `implementing` | 시제 변형 |
| `documented_in` | `documents_in`, `described_in` | 표기 부주의 + 직접 동의어 |
| `policy` (entity) | `policies`, `rules` | 복수형 + 보고서 인용 |
| `module` (entity) | `component`, `components` | 단/복수형 |

> 01_analysis 5절의 *제외 권고* 항목 (`has_part`↔`part_of`, `supersedes`↔
> `replaces`, `provides`↔`offers`, `service`/`system`, `user`/`person`) 은
> 본 라운드에 *적용하지 않음*. 방향 의존이나 도메인 의미 위험이 있어 별도
> 결정 필요.

**상호 영향**:
- F-CG-04 / F-CG2-06 / F-CG2-02·04 와 무관. 어느 순서에 들어가도 안전.
- 단, *4 적용 지점을 모두 패치* 해야 unit 경로/문서 경로 동작이 같음.

---

### 2.3 F-CG2-02/04 — 큰 문서 폴백 보강 (임계값 + JSON 잘림 폴백)

**목적**:
1. 디폴트 입력 임계값을 256K 모델 가정값(`200_000`)에서 R4 환경(qwen2.5:7b@32K)
   에 맞는 `16_000` 으로 낮춘다.
2. 문서 단위 호출이 JSON 파싱 실패하는 경우(출력 잘림 포함)에도 unit 폴백을
   트리거하여 문서 그래프 영구 손실을 막는다.

**변경 위치**:
- `llm_body_extractor.py:38` (예외 정의) — `OutputTruncatedError` 추가.
- `llm_body_extractor.py:88` (`max_input_tokens`) — 디폴트 변경 + 신규 필드.
- `llm_body_extractor.py:331-350` (`extract_llm_body_graph_for_document` 의
  try/except 분기) — 파싱 실패 시 `OutputTruncatedError` raise.
- `pipeline.py:471-480` — catch 분기 확장.

**변경 전 → 변경 후 의사 코드**:

```python
# llm_body_extractor.py — 신규 예외
class OutputTruncatedError(Exception):
    """문서 단위 LLM 호출의 JSON 파싱이 실패했을 때 (출력 잘림 추정 포함) raise.

    호출자는 ``InputTooLargeError`` 와 동일하게 unit 기반 폴백으로 라우팅해야
    한다. JSON 파싱 실패의 원인은 출력 토큰 한도 도달(잘림), 모델 형식 위반
    등 여러 경로가 있지만, 어느 쪽이든 unit 분할로 입력·출력 규모를 줄이면
    회복 가능성이 높다.
    """
```

```python
# llm_body_extractor.py — LLMBodyExtractionConfig (라인 84-88 갱신)
@dataclass(frozen=True)
class LLMBodyExtractionConfig:
    ...
    # R4: 디폴트를 32K 컨텍스트 모델 기준 16_000 으로 낮춘다. 시스템 프롬프트
    # (~500) + 응답 예산(max_tokens) + 안전 마진을 고려한 입력 한도.
    # 256K 컨텍스트 모델 환경에서는 호출자가 cfg 로 override 한다.
    max_input_tokens: int = 16_000
    # R4 추가: JSON 파싱 실패를 unit 폴백 트리거로 승격할지 여부.
    # ``False`` 이면 기존 동작(빈 그래프 반환) 유지. 디폴트 ``True``.
    fallback_on_output_truncation: bool = True
```

```python
# llm_body_extractor.py:345-350 변경 후 (extract_llm_body_graph_for_document)
try:
    response = await llm_client.complete( ... )
    payload = extract_json(response)
    if not isinstance(payload, dict):
        raise ValueError(...)
except InputTooLargeError:
    raise
except Exception:
    logger.warning(
        "문서 단위 LLM 본문 추출 실패 — doc_title=%s", doc_title, exc_info=True,
    )
    if cfg.fallback_on_output_truncation:
        raise OutputTruncatedError(
            f"문서 단위 LLM 응답 JSON 파싱 실패 — doc_title={doc_title}",
        )
    stats.units_failed = 1
    return GraphData(), stats
```

```python
# pipeline.py:471-480 변경 후
try:
    llm_graph, llm_stats = (
        await extract_llm_body_graph_for_document(
            doc_title=title, body=doc_body, llm_client=llm_client,
        )
    )
    ...
except (InputTooLargeError, OutputTruncatedError) as exc:
    logger.info(
        "문서 단위 LLM 추출 폴백 — doc_id=%d, units=%d, reason=%s",
        document_id, len(units), type(exc).__name__,
    )
    llm_graph, llm_stats = await extract_llm_body_graph(
        units, doc_title=title, llm_client=llm_client,
    )
```

**새 함수/필드**:
- `OutputTruncatedError` (Exception 서브클래스).
- `LLMBodyExtractionConfig.max_input_tokens` 디폴트 변경 (`200_000` → `16_000`).
- `LLMBodyExtractionConfig.fallback_on_output_truncation: bool = True`.

**데이터 (디폴트값)**:
- `max_input_tokens=16_000` (qwen2.5:7b @ 32K 환경 권고).
- `fallback_on_output_truncation=True`.
- 환경변수 오버라이드 경로: 본 라운드에서는 추가하지 않는다 (cfg 주입은
  pipeline 호출 시점에 명시). 추측: 후속 라운드에서 `LLMSettings` 와 묶을 수
  있으나 스코프 밖.

**상호 영향**:
- F-CG-04 가 *반드시 먼저* 들어가야 폴백 경로의 unit 기반 함수가 cross-unit
  관계를 보존한다 (그 외 경우 폴백된 문서는 단절된 관계만 보임).
  → **F-CG-04 → F-CG2-02/04 순서**.
- 기존 테스트 `test_for_document_failed_llm_returns_empty_with_failed_stat`
  (라인 611-621) 의 의도가 *변경* 된다: 디폴트 `fallback_on_output_truncation=
  True` 에서는 빈 그래프 대신 `OutputTruncatedError` raise. 테스트 갱신 필요.
  → 두 가지 갱신 옵션: (a) cfg 에 `fallback_on_output_truncation=False` 명시
  하고 기존 assert 유지, (b) `with pytest.raises(OutputTruncatedError):` 로
  rewrite. 권고는 (a) — 기존 동작이 *옵트인으로 보존됨* 을 명시.

---

### 2.4 F-CG2-06 — canonical_name + dedup 키 stem 정규화

**목적**: `AuthService` / `Auth Service` / `auth-service` 같은 표기 변형을
같은 stem 으로 묶어 노드 1개로 수렴.

**변경 위치**:
- `graph_vocabulary.py` (또는 `llm_body_extractor.py`) — `_normalize_name_stem`
  헬퍼 신규. **추측**: graph_vocabulary 가 단일 출처에 부합 (vocab 도메인이
  넓어진다는 명분). 디자이너 자유 판단.
- `llm_body_extractor.py:227` (unit 경로 dedup 키).
- `llm_body_extractor.py:253, 256-257` (unit 경로 rel_key + canonical_name 호출).
- `llm_body_extractor.py:375` (문서 경로 dedup 키).
- `llm_body_extractor.py:398, 400-401` (문서 경로 rel_key + canonical_name 호출).
- `llm_body_extractor.py:522-531` (`_canonical_name` 본체) — stem 매칭으로 전환.

**변경 전 → 변경 후 의사 코드**:

```python
# graph_vocabulary.py (또는 llm_body_extractor.py 내부) 신규 헬퍼
_NAME_STEM_PUNCT_RE = re.compile(r"[\s\-_]+")

def normalize_name_stem(name: str) -> str:
    """표기 변형(공백/하이픈/언더스코어/대소문자)을 통합한 stem 키 생성.

    예) ``AuthService`` / ``Auth Service`` / ``auth-service`` / ``auth_service``
        → ``"authservice"``.

    *형태론적 변형 (복수형 / 동사형)* 은 의도적으로 *통합하지 않는다*. 의미
    경계가 모호한 통합을 피하기 위한 보수적 정책. 회귀 위험 절(01_analysis
    §3 F-CG2-06)에 따른 결정.
    """
    return _NAME_STEM_PUNCT_RE.sub("", name).lower()
```

```python
# llm_body_extractor.py — unit 경로 entity 등록 (라인 227 부근)
# 변경 전
key = (name.lower(), etype)
if key not in entities:
    entities[key] = Entity(...)
unit_valid_entity_names.add(name.lower())

# 변경 후 (F-CG-04 + F-CG2-06 합산)
name_stem = normalize_name_stem(name)
key = (name_stem, etype)
if key not in entities:
    entities[key] = Entity(name=name, entity_type=etype, description=description)
document_valid_entity_names.add(name_stem)
```

```python
# unit 경로 relation 검증 (라인 244-263 부근)
# 변경 전
src_stem, tgt_stem = src.lower(), tgt.lower()
if ( ... src_stem not in valid_names ... ):
    ...
rel_key = (src.lower(), tgt.lower(), rtype)
src_canon = _canonical_name(entities, src, src.lower())
tgt_canon = _canonical_name(entities, tgt, tgt.lower())

# 변경 후
src_stem = normalize_name_stem(src)
tgt_stem = normalize_name_stem(tgt)
if ( ... src_stem not in document_valid_entity_names
         or tgt_stem not in document_valid_entity_names
         or src_stem == tgt_stem ):
    stats.dropped_relations += 1
    continue
rel_key = (src_stem, tgt_stem, rtype)
if rel_key not in relations:
    src_canon = _canonical_name(entities, src, src_stem)
    tgt_canon = _canonical_name(entities, tgt, tgt_stem)
    relations[rel_key] = Relation( ... )
```

```python
# _canonical_name (라인 522-531) — stem 매칭
def _canonical_name(
    entities: dict[tuple[str, str], Entity],
    raw_name: str,
    raw_stem: str,                                   # <-- 명칭만 변경
) -> str:
    """등록된 entity 중 같은 stem 키가 있으면 그 표기를 우선 반환.

    dedup 키도 stem 기반이므로, ``entities`` dict 의 key 첫 요소가 이미
    stem 이다. lookup 도 같은 stem 으로 매칭하면 첫 등장 표기가 보존된다.
    """
    for (name_stem, _etype), ent in entities.items():
        if name_stem == raw_stem:
            return ent.name
    return raw_name
```

문서 경로(`extract_llm_body_graph_for_document`, 라인 375 / 398-401) 도
*완전히 동일한 패턴* 으로 변경. 둘 중 하나만 바꾸면 두 경로 동작이 어긋난다.

**새 함수/필드**:
- `graph_vocabulary.normalize_name_stem(name: str) -> str` (또는
  `llm_body_extractor` 내부 헬퍼).

**데이터**: 정규식 `r"[\s\-_]+"` — 공백/하이픈/언더스코어만 정규화. **형태론적
변형은 제외** (01_analysis §3 위험 가설 1 권고).

**상호 영향**:
- F-CG-04 가 먼저 들어와야 `document_valid_entity_names` 가 존재 — F-CG2-06
  의 endpoint 검증이 이 set 의 stem 키와 정합.
- F-CG2-08 (alias 정규화) 와 무관. 두 변환은 독립적이며 etype/rtype vs name
  서로 다른 필드를 다룬다.

---

## 3. 단위 테스트 설계

### 3.1 신규 테스트 (4건)

#### T1. F-CG-04 회복 검증 — `test_cross_unit_relation_endpoint_preserved`

위치: `tests/test_processor/test_llm_body_extractor.py` 적절한 위치 (cross-unit
관련 테스트 근처, 라인 302 근방).

```python
@pytest.mark.asyncio
async def test_cross_unit_relation_endpoint_preserved() -> None:
    """F-CG-04: unit A 에서 등장한 entity 가 unit B 의 relation 끝점이어도 보존."""
    # unit A: entities 만 (Auth Service, Cache)
    # unit B: relations 만 (Auth Service uses Cache) — entities 비움
    a_payload = {
        "entities": [
            {"name": "Auth Service", "type": "system"},
            {"name": "Cache", "type": "module"},
        ],
        "relations": [],
    }
    b_payload = {
        "entities": [],
        "relations": [{"source": "Auth Service", "target": "Cache", "type": "uses"}],
    }
    llm = _llm_with_responses([a_payload, b_payload])
    units = [_unit(section_path=("A",), ordinal=0), _unit(section_path=("B",), ordinal=1)]
    g, stats = await extract_llm_body_graph(units, doc_title="d", llm_client=llm)
    # 핵심: unit B 의 관계가 보존 (현재 코드는 dropped)
    assert any(
        r.source == "Auth Service" and r.target == "Cache" and r.relation_type == "uses"
        for r in g.relations
    )
    assert stats.dropped_relations == 0
```

회귀 방지 negative case: dangling endpoint (예: `target="Ghost"`) 는 여전히
drop 됨을 같은 테스트 끝에 추가 검증.

#### T2. F-CG2-08 alias 정규화 — `test_relation_type_alias_normalized`

위치: `tests/test_processor/test_llm_body_extractor.py` 또는
`test_graph_vocabulary.py` (vocab 단위 테스트와 함께).

```python
@pytest.mark.asyncio
async def test_relation_type_alias_normalized() -> None:
    """F-CG2-08: depending_on / implement 같은 변형이 canonical 로 정규화."""
    payload = {
        "entities": [
            {"name": "A", "type": "system"},
            {"name": "B", "type": "system"},
            {"name": "C", "type": "system"},
        ],
        "relations": [
            {"source": "A", "target": "B", "type": "depending_on"},  # → depends_on
            {"source": "B", "target": "C", "type": "implement"},      # → implements
            {"source": "A", "target": "C", "type": "blesses"},        # 매핑 없음 → drop
        ],
    }
    llm = _llm_returning(payload)
    g, stats = await extract_llm_body_graph_for_document(
        doc_title="d", body="본문", llm_client=llm,
    )
    rtypes = {r.relation_type for r in g.relations}
    assert rtypes == {"depends_on", "implements"}
    assert stats.dropped_relations == 1  # blesses
```

추가로 `test_graph_vocabulary.py` 에:

```python
def test_relation_type_alias_table_is_unique_mapping() -> None:
    """alias 키는 1:1 매핑 — 중복 키 또는 canonical 자기참조 없음."""
    # canonical 자기 참조 금지 (자기로 매핑되는 alias 는 무의미)
    for alias, canonical in RELATION_TYPE_ALIASES.items():
        assert alias != canonical
    # 모든 canonical 은 실제 vocab 에 존재
    canonicals = set(RELATION_TYPE_ALIASES.values())
    valid = all_relation_type_names()
    assert canonicals.issubset(valid), canonicals - valid

def test_entity_type_alias_table_is_unique_mapping() -> None:
    # 동일 검증
    ...
```

#### T3. F-CG2-02/04 폴백 트리거 — `test_for_document_output_truncation_raises_for_fallback`

위치: `tests/test_processor/test_llm_body_extractor.py` (기존
`test_for_document_failed_llm_returns_empty_with_failed_stat` 직후).

```python
@pytest.mark.asyncio
async def test_for_document_output_truncation_raises_for_fallback() -> None:
    """F-CG2-04: 디폴트(fallback_on_output_truncation=True) 에서 JSON 파싱 실패는
    OutputTruncatedError raise — 호출자(pipeline)가 unit 폴백 라우팅한다."""
    mock = AsyncMock()
    mock.complete = AsyncMock(return_value="잘린-JSON-같은-텍스트")
    with pytest.raises(OutputTruncatedError):
        await extract_llm_body_graph_for_document(
            doc_title="d", body="본문", llm_client=mock,
        )

@pytest.mark.asyncio
async def test_for_document_output_truncation_opt_out_returns_empty() -> None:
    """fallback_on_output_truncation=False 면 기존 동작(빈 그래프) 유지."""
    mock = AsyncMock()
    mock.complete = AsyncMock(return_value="잘린-JSON-같은-텍스트")
    cfg = LLMBodyExtractionConfig(fallback_on_output_truncation=False)
    g, stats = await extract_llm_body_graph_for_document(
        doc_title="d", body="본문", llm_client=mock, config=cfg,
    )
    assert g.entities == [] and stats.units_failed == 1

def test_max_input_tokens_default_is_16k() -> None:
    """F-CG2-02: 디폴트 max_input_tokens 가 32K 모델 기준 16_000."""
    cfg = LLMBodyExtractionConfig()
    assert cfg.max_input_tokens == 16_000
```

#### T4. F-CG2-06 stem 정규화 — `test_name_stem_dedup_across_casings_and_punctuation`

```python
@pytest.mark.asyncio
async def test_name_stem_dedup_across_casings_and_punctuation() -> None:
    """F-CG2-06: AuthService / Auth Service / auth-service 가 단일 노드로 수렴."""
    payload = {
        "entities": [
            {"name": "AuthService", "type": "system"},
            {"name": "Auth Service", "type": "system"},     # 공백 변형
            {"name": "auth-service", "type": "system"},     # 하이픈 + 소문자
            {"name": "Cache", "type": "module"},
        ],
        "relations": [
            {"source": "AuthService", "target": "Cache", "type": "uses"},
            {"source": "auth-service", "target": "Cache", "type": "uses"},  # dedup
        ],
    }
    llm = _llm_returning(payload)
    g, stats = await extract_llm_body_graph_for_document(
        doc_title="d", body="본문", llm_client=llm,
    )
    auths = [e for e in g.entities if e.entity_type == "system"]
    assert len(auths) == 1
    assert auths[0].name == "AuthService"   # 첫 등장 표기 보존
    uses_rels = [r for r in g.relations if r.relation_type == "uses"]
    assert len(uses_rels) == 1              # rel_key 도 stem 화되어 dedup
    assert uses_rels[0].source == "AuthService"
```

회귀 방지 negative case: `User Service` vs `Users` (복수형) 는 같은 stem 으로
*수렴하지 않음* 검증 (별도 mini-fixture).

```python
def test_normalize_name_stem_does_not_collapse_plural_forms() -> None:
    """형태론적 변형은 stem 정규화에서 의도적으로 제외."""
    assert normalize_name_stem("User Service") != normalize_name_stem("Users")
```

### 3.2 기존 테스트 갱신

| 테스트 | 영향 | 조치 |
|---|---|---|
| `test_cross_unit_entity_dedup_keeps_first_casing` (라인 269-298) | 통과 유지 (lowercase 만으로도 통합). stem 화 후에도 동작. | 변경 불요 (확인만). |
| `test_for_document_failed_llm_returns_empty_with_failed_stat` (라인 611-621) | **의미 변경** — 디폴트 fallback opt-in 으로 OutputTruncatedError raise. | cfg 에 `fallback_on_output_truncation=False` 명시하여 기존 assert 보존, *또는* `with pytest.raises(OutputTruncatedError):` 로 rewrite (위 T3 가 새 동작을 별도로 커버하므로 *전자 권고*). |
| `test_disallowed_relation_type_dropped` (라인 218-) | `blesses` 사용 — alias 표에 없으므로 drop 유지. | 변경 불요. |
| `test_disallowed_entity_type_dropped` (라인 193-) | `made_up_type` 사용 — alias 표에 없으므로 drop 유지. | 변경 불요. |
| `test_for_document_oversized_body_raises_input_too_large` (라인 537-) | `max_input_tokens=5` 명시 → 디폴트 변경에 영향 없음. | 변경 불요. |

### 3.3 회귀 (pipeline 측)

`tests/test_processor/test_pipeline*.py` 가 `extract_llm_body_graph_for_document`
의 `OutputTruncatedError` 분기를 mock 하는 경우가 있는지 확인 필요. 현재
조사 시점에 pipeline 측에서 fallback 분기를 직접 검증하는 테스트는 없는
것으로 보임 — 신규 통합 테스트는 본 라운드에서는 *생략 권고* (verifier 가
실제 회귀 측정으로 보강).

---

## 4. config / vocab 데이터 변경

### 4.1 `LLMBodyExtractionConfig` 변경

```python
@dataclass(frozen=True)
class LLMBodyExtractionConfig:
    # 변경
    max_input_tokens: int = 16_000          # 200_000 → 16_000

    # 신규
    fallback_on_output_truncation: bool = True
```

- 영향 호출자: `pipeline.py:457` 는 cfg 인자 미전달 → 디폴트가 16K 로 자동
  적용. 256K 모델 환경이 필요해지면 cfg 를 명시 주입하도록 후속 라운드에서
  확장.
- 환경변수 오버라이드: 본 라운드에서는 추가하지 않는다 (스코프 가드).
  *추측*: 향후 `LLMSettings` 에 `body_extraction_max_input_tokens` 추가 가능.

### 4.2 `graph_vocabulary.py` 신규 데이터

```python
ENTITY_TYPE_ALIASES: dict[str, str] = {
    "policies":   "policy",
    "rules":      "policy",
    "component":  "module",
    "components": "module",
}

RELATION_TYPE_ALIASES: dict[str, str] = {
    "depending_on":   "depends_on",
    "depend_on":      "depends_on",
    "dependent_on":   "depends_on",
    "owns":           "owned_by",
    "owner":          "owned_by",
    "implement":      "implements",
    "implementing":   "implements",
    "documents_in":   "documented_in",
    "described_in":   "documented_in",
}
```

- **확정 8건**: 보수적 화이트리스트. 의미 위험·방향 위험 항목은 *명시적으로
  제외*.
- canonical 자기 참조 금지 (단위 테스트로 강제).
- 1:1 매핑만 허용 — 한 alias 가 두 canonical 로 매핑되는 일은 dict 구조상 불가.

### 4.3 정규화 헬퍼 위치

세 함수 (`normalize_entity_type`, `normalize_relation_type`,
`normalize_name_stem`) 는 `graph_vocabulary.py` 에 두는 것을 *권고* — vocab
도메인 단일 출처 원칙. *추측*: implementer 가 `llm_body_extractor.py` 내부
private helper 로 옮겨도 무방하나, 본 라운드 외 다른 소비자(검색 LLM 정렬용)
가 후속에 같은 정규화를 필요로 할 가능성을 보면 vocab 모듈이 자연.

---

## 5. 적용 순서 권장 (implementer 가 따라갈 순서)

각 단계 후 회귀 테스트 (`pytest src/context_loop/processor/` 와
`pytest tests/test_processor/`) 를 실행하고, 깨진 테스트가 *예상한 것*만
인지 확인. 단계 사이 회귀 신호가 누적되지 않도록 *각 단계 단독 커밋*
권고.

### 단계 1. graph_vocabulary alias / stem 헬퍼 추가 (변경 표면 좁음)

- 파일: `graph_vocabulary.py` 만 변경.
- 내용: `ENTITY_TYPE_ALIASES`, `RELATION_TYPE_ALIASES`, `normalize_entity_type`,
  `normalize_relation_type`, `normalize_name_stem` 추가.
- 새 단위 테스트 추가: `test_relation_type_alias_table_is_unique_mapping`,
  `test_entity_type_alias_table_is_unique_mapping`,
  `test_normalize_name_stem_does_not_collapse_plural_forms`.
- 회귀 실행: `pytest tests/test_processor/test_graph_vocabulary.py -v` — 기존
  테스트 모두 통과해야 함 (vocab 데이터에 *추가만* 했으므로 안전).

### 단계 2. F-CG-04 변수 스코프 이동 (1줄 + 검증 참조 변경)

- 파일: `llm_body_extractor.py` 만 (unit 경로만).
- 내용: `unit_valid_entity_names` → `document_valid_entity_names`, 루프 밖
  초기화.
- 새 테스트: `test_cross_unit_relation_endpoint_preserved` 추가.
- 회귀 실행: `pytest tests/test_processor/test_llm_body_extractor.py -v` —
  기존 cross-unit 테스트 (라인 269-320) 통과 + 신규 통과.

### 단계 3. F-CG2-06 stem 정규화 (dedup 키 + canonical 동시 변경)

- 파일: `llm_body_extractor.py` (unit 경로 + 문서 경로 + `_canonical_name`).
- 내용: 라인 227, 232, 244-247, 253, 256-257, 375, 380, 392-394, 398, 400-401,
  522-531 의 정확 lowercase 키를 stem 키로 일괄 전환.
- **반드시 한 번에 적용** — 한쪽만 바꾸면 dedup 키 mismatch.
- 새 테스트: `test_name_stem_dedup_across_casings_and_punctuation`.
- 회귀 실행: 기존 `test_cross_unit_entity_dedup_keeps_first_casing` 통과 확인.

### 단계 4. F-CG2-08 alias 적용 (4 적용 지점 동시)

- 파일: `llm_body_extractor.py` (unit 경로 entity/relation 등록 2지점 + 문서
  경로 2지점).
- 내용: `etype = normalize_entity_type(etype_raw)`,
  `rtype = normalize_relation_type(rtype_raw)` 한 줄씩 4곳 추가.
- 새 테스트: `test_relation_type_alias_normalized` (`for_document` 경로).
  unit 경로용 가벼운 fixture 도 가능하면 추가.
- 회귀 실행: `test_disallowed_*` 가 여전히 drop 함을 확인.

### 단계 5. F-CG2-02/04 임계값 + JSON 폴백 (가장 외부 영향 큼)

- 파일: `llm_body_extractor.py` (`OutputTruncatedError` 신규, cfg 디폴트 변경,
  파싱 실패 시 raise), `pipeline.py` (catch 분기 확장).
- 내용: 위 §2.3 의사 코드 그대로.
- 기존 테스트 갱신: `test_for_document_failed_llm_returns_empty_with_failed_stat`
  에 `fallback_on_output_truncation=False` 명시.
- 새 테스트: T3 세 건.
- 회귀 실행: `pytest tests/test_processor/ -v` 전체. pipeline 테스트가 있다면
  같이 통과해야 함.

### 단계 6. 통합 회귀

- `pytest tests/test_processor/`
- `ruff check src/context_loop/processor/`
- (가능하면) verifier 가 사내 fixture 로 그래프 통계 diff 측정.

---

## 부록: 변경 후 의사 코드 요약 (unit 경로 — 한눈에)

```python
async def extract_llm_body_graph(units, *, doc_title, llm_client, config=None):
    ...
    entities: dict[tuple[str, str], Entity] = {}
    relations: dict[tuple[str, str, str], Relation] = {}
    allowed_etypes = set(cfg.allowed_entity_types)
    allowed_rtypes = set(cfg.allowed_relation_types)
    document_valid_entity_names: set[str] = set()        # F-CG-04

    for unit, payload in results:
        if payload is None:
            stats.units_failed += 1
            continue
        stats.units_called += 1

        for ent in raw_entities:
            ...
            name = str(ent.get("name", "")).strip()
            etype = normalize_entity_type(str(ent.get("type", "")))   # F-CG2-08
            if not name or etype not in allowed_etypes:
                stats.dropped_entities += 1
                continue
            stem = normalize_name_stem(name)                          # F-CG2-06
            key = (stem, etype)
            if key not in entities:
                entities[key] = Entity(name=name, entity_type=etype, ...)
            document_valid_entity_names.add(stem)

        for rel in raw_relations:
            ...
            src = str(rel.get("source", "")).strip()
            tgt = str(rel.get("target", "")).strip()
            rtype = normalize_relation_type(str(rel.get("type", "")))  # F-CG2-08
            src_stem = normalize_name_stem(src)                         # F-CG2-06
            tgt_stem = normalize_name_stem(tgt)
            if (
                not src or not tgt or rtype not in allowed_rtypes
                or src_stem not in document_valid_entity_names           # F-CG-04+06
                or tgt_stem not in document_valid_entity_names
                or src_stem == tgt_stem
            ):
                stats.dropped_relations += 1
                continue
            rel_key = (src_stem, tgt_stem, rtype)
            if rel_key not in relations:
                src_canon = _canonical_name(entities, src, src_stem)
                tgt_canon = _canonical_name(entities, tgt, tgt_stem)
                relations[rel_key] = Relation(...)
```

문서 경로(`extract_llm_body_graph_for_document`) 도 *완전히 동일한 패턴*
적용. 차이는 (a) 단일 unit (`valid_names` 가 함수 지역), (b) `label=""`.
