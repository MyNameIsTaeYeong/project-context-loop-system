# R4 — Confluence 그래프 추출 4개 패치 적용 가능성 분석

> 본 단계는 *새 발견 탐색이 아니라* R1/R2/R3 에서 이미 결정된 4개 미적용 패치
> (F-CG-04 / F-CG2-08 / F-CG2-02·04 / F-CG2-06) 가 **현재 코드와 일관되게 적용
> 가능한가**를 검증한다. 회귀 위험과 끼워넣기 지점을 확정하는 게 목적이다.

분석 대상 파일 기준:
- `src/context_loop/processor/llm_body_extractor.py` (532라인)
- `src/context_loop/processor/graph_vocabulary.py` (199라인)
- `src/context_loop/processor/pipeline.py:443-496` (LLM body 호출 분기)
- `tests/test_processor/test_llm_body_extractor.py`, `test_graph_vocabulary.py`

---

## 1. 현재 코드 상태 확인

### F-CG-04 — unit-scoped `valid_entity_names`

R2 보고서 가정:  unit 마다 `unit_valid_entity_names` 를 새로 만들고 끝점 검증해
*cross-unit* 관계 끝점이 모두 드롭. 위치는 라인 206-241.

현재 코드 (`llm_body_extractor.py:204-263`) 와 100% 일치.
- 라인 216: `unit_valid_entity_names: set[str] = set()` — 매 unit 루프마다 초기화.
- 라인 232: 엔티티 등록 시 `unit_valid_entity_names.add(name.lower())`.
- 라인 246-247: 관계 검증 시 `src.lower() not in unit_valid_entity_names or
  tgt.lower() not in unit_valid_entity_names` 조건으로 드롭.

→ R2 가정 그대로. **패치 표면은 한 변수의 스코프 끌어올림 1곳 + 검증 시점에서
참조 대상 변수 1곳**. 매우 좁고 안전.

### F-CG2-08 — relation_type alias 정규화

R2 보고서 가정: 현재는 `allowed_rtypes = set(cfg.allowed_relation_types)` 에 대해
strict `in` 검사만 수행 (라인 213-235). `depending_on`/`part_of` 같은 LLM
표기 변형은 드롭. alias 정규화 함수 없음.

현재 코드 확인:
- 라인 201-202: `allowed_etypes/rtypes` strict 집합.
- 라인 213/243-251: 검증 시 단순 `rtype not in allowed_rtypes` 만 검사.
- `graph_vocabulary.py` 어디에도 alias 매핑/`aliases` 필드 없음 (VocabEntry 정의
  라인 19-31 = `name/description/source` 3필드).
- 문서 단위 경로(`extract_llm_body_graph_for_document`, 라인 277-421) 도 같은
  strict 검사 (라인 359-360, 389-397).

→ R2 가정 그대로. alias 매핑 도입 시 entity_type / relation_type 양쪽에 같은
정규화 헬퍼를 만들어 unit 경로·문서 경로 양쪽에서 호출해야 한다.

### F-CG2-02/04 — 문서/unit 자동 폴백

R2 보고서 가정: 거대 문서(>16K input tokens) 입력 시 LLM 출력 잘림 → 폴백
필요.

현재 코드 변화:
- `extract_llm_body_graph_for_document` (라인 277-421) **이미 구현돼 있음**.
  `LLMBodyExtractionConfig.max_input_tokens` 필드 존재 (라인 88, 기본값
  `200_000`).
- 입력 초과 시 `InputTooLargeError` raise (라인 316-319).
- `pipeline.py:453-480` 에서 문서 단위 호출이 **디폴트**, `InputTooLargeError`
  catch 후 `extract_llm_body_graph(units, ...)` 로 폴백 — *폴백 라우팅 본체는
  이미 동작 중.*
- **그러나** R4 요구는 임계값 **16K 디폴트** + 환경별 configurable. 현재 디폴트
  `200_000` 은 256K 컨텍스트 모델 가정. R4 환경(qwen2.5:7b @ 32K) 와 어긋남.
- 출력 잘림(JSON parse 실패) → 자동 폴백 분기는 **없음**. 현재는 파싱 실패 시
  단순히 `units_failed=1` 로 빈 그래프를 반환 (라인 345-350) → 그 문서의
  LLM 그래프 영구 손실.

→ R2 보고서가 시점에 비해 코드는 일부 선행 구현(폴백 골격 + 임계값 필드).
R4 패치 표면은 **(a) 임계값 디폴트 16K 변경, (b) JSON 파싱 실패 시 폴백 추가,
(c) configurable 필드 노출**.

### F-CG2-06 — `_canonical_name` stem 정규화

R2 보고서 가정: `_canonical_name` 이 `name.lower()` 만 비교하여 공백/하이픈/
대소문자 변형을 통합 못 함.

현재 코드 (라인 522-531):
```python
def _canonical_name(entities, raw_name, raw_lower):
    for (name_lower, _etype), ent in entities.items():
        if name_lower == raw_lower:
            return ent.name
    return raw_name
```

→ R2 가정 그대로. `name.lower()` 정확 일치만. 같은 stem (`AuthService` vs
`Auth Service` vs `auth-service`) 통합 없음. dedup 키 (라인 227, 375) 도 같은
정확 lowercase 키만 사용.

추가 확인: dedup 키는 두 경로(unit/문서)에서 모두 `(name.lower(), etype)`. stem
정규화를 도입한다면 dedup 키와 `_canonical_name` lookup **둘 다** 같은 정규화
함수를 거쳐야 일관성 유지.

---

## 2. 패치별 변경 표면적

### F-CG-04

| 항목 | 위치 |
|---|---|
| 수정 대상 | `extract_llm_body_graph` (`llm_body_extractor.py:152-274`) |
| 변경 | 라인 216 의 `unit_valid_entity_names: set[str] = set()` 를 for-loop **밖**으로 이동 → `document_valid_entity_names`. |
| 신규 함수/필드 | 없음. |
| config/vocab | 없음. |
| 호출자 | `pipeline.py:478` (폴백 경로). 시그니처/반환형 무변. |
| 테스트 영향 | `test_cross_unit_entity_dedup_keeps_first_casing` (290-298) 의 의도는 *cross-unit 표기 통합*. 패치 후에도 통과 가능. 새 fixture (unit A `entities={Auth Service}`, unit B `relations=[Auth Service→Cache uses]` 만 — entities 비움) 가 추가 필요. |

### F-CG2-08 (alias 정규화)

| 항목 | 위치 |
|---|---|
| 수정 대상 | `extract_llm_body_graph` (라인 222-223, 243-245), `extract_llm_body_graph_for_document` (라인 370-371, 389-391) |
| 신규 함수 | `_normalize_entity_type(raw)` / `_normalize_relation_type(raw)` — 추측: `graph_vocabulary.py` 에 두는 게 단일 출처 원칙에 맞음. |
| vocab 변경 | `VocabEntry` 에 `aliases: tuple[str, ...] = ()` 추가 또는 별도 `ENTITY_TYPE_ALIASES` / `RELATION_TYPE_ALIASES` 매핑 상수. 후자가 영향 작음 (frozen dataclass 변경 부담 없음). |
| 호출자 | 위 두 함수 내부에서만 호출. 외부 시그니처 불변. |
| 테스트 영향 | `test_disallowed_entity_type_dropped`, `test_disallowed_relation_type_dropped` 는 *완전 무관* 타입 (`made_up_type`, `blesses`) 사용하므로 통과. 새 alias 단위 테스트 추가. |

### F-CG2-02/04 (큰 문서 폴백 보강)

| 항목 | 위치 |
|---|---|
| 수정 대상 | `LLMBodyExtractionConfig` (라인 53-88) — `max_input_tokens` 디폴트 변경 + 신규 필드 후보. `extract_llm_body_graph_for_document` (라인 277-421) — 파싱 실패 시 폴백 시그널. `pipeline.py:453-480` — JSON 파싱 실패 시도 폴백으로 라우팅. |
| 신규 함수/필드 | (a) `max_input_tokens` 디폴트 `200_000`→`16_000` (R4 환경). (b) 폴백 트리거를 `InputTooLargeError` 단일에서 출력 잘림까지 확장. 추측: 신규 예외 `OutputTruncatedError` 도입하거나, `for_document` 가 `(GraphData, stats, truncated: bool)` 시그널 반환. |
| vocab 변경 | 없음. |
| 호출자 | `pipeline.py:471-480` 의 `except InputTooLargeError` 분기 확장. 그 외 호출자는 `test_llm_body_extractor.py` (테스트). |
| 테스트 영향 | `test_for_document_oversized_body_raises_input_too_large` (라인 537-551) — `max_input_tokens=5` 고정값으로 통과 보장. 그러나 디폴트 변경은 **다른 테스트가 디폴트를 명시적으로 의존하지 않으므로 무영향** (확인: 모든 `LLMBodyExtractionConfig()` 사용처가 `max_input_tokens` 를 직접 지정 안 함, 그리고 기본 본문 길이는 5~50 토큰 → 16K 임계 미달). 새 임계값/폴백 트리거 단위 테스트 필요. |

### F-CG2-06 (`_canonical_name` stem 매칭)

| 항목 | 위치 |
|---|---|
| 수정 대상 | `_canonical_name` (라인 522-531). dedup 키 (라인 227, 375) **도 같이** 정규화 stem 키 도입 필요 — 안 하면 dedup 으로 분리된 두 노드를 _canonical_name 이 동일 노드로 잘못 매핑. |
| 신규 함수 | `_normalize_stem(name)` — 추측: `lower() → 공백/하이픈/언더스코어 모두 빈문자 → 알파벳숫자만`. 또는 보수적으로 `lower() → 공백/하이픈/언더스코어 통일 → strip`. R4 보고서 요구는 "공백·하이픈·대소문자 normalize". |
| config/vocab | 없음. |
| 호출자 | 두 추출 함수 내부 전용. |
| 테스트 영향 | `test_cross_unit_entity_dedup_keeps_first_casing` (290-298) — 현재 `Auth Service` vs `AUTH SERVICE` (공백 동일, 대소문자만 차이) → 현재 `lower()` 로도 통합. stem 도입 후에도 통과. 새 fixture (`AuthService` vs `Auth Service`) 필요. |

---

## 3. 회귀 위험 평가

### F-CG-04

- **위험 가설**: cumulative `document_valid_entity_names` 가 *다른* 문서의
  entity 와 격리되는지 검증 필요. 현재 함수 호출이 1 문서당 1회 (`pipeline.py`
  분기) 이므로 안전 — 함수 진입 시 set 이 초기화되면 문서 간 누출 없음.
- **위험 가설 2**: LLM 이 모든 unit 에 같은 entity 를 다시 emit 하면 raw 엔티티
  중복 카운트 증가. 단, `entities` dict 가 lowercase 키로 dedup 되므로 final
  카운트는 영향 없음. `stats.raw_entities` 만 살짝 부풀 수 있음 — 운영 정보일
  뿐 정확도 영향 없음.
- **영향받을 테스트**: 9건 정도가 unit 1개 fixture (`_unit()` 단독) — 동작 변화
  없음. `test_cross_unit_entity_dedup_keeps_first_casing`(290) 가 가장 비슷한
  cross-unit 케이스이지만 unit B 의 entities 에 동일 이름이 *있는* 케이스라
  통과. *cross-unit relation but no entity in same unit* 새 fixture 가 회복
  검증의 핵심.

### F-CG2-08

- **위험 가설 1**: 의도 외 정규화 — `depending_on` 외에도 의미상 다른 단어
  (`mention` vs `mentions`, `imports` vs `import` 같은 vocab 충돌) 가 묶이면
  LLM 의 본래 의도 변형. → 패치 표는 **보수적으로** (오타/현재형/과거형 등
  명백한 형태론적 변형만). 5절 참조.
- **위험 가설 2**: alias 가 entity_type 에 적용되면 LLM 이 ad-hoc 타입을 만들고
  alias 매핑을 우회로 사용. → alias 매핑은 *확실히 알려진* 변형 집합만 화이트
  리스트로 두고, 모르는 타입은 여전히 drop.
- **위험 가설 3**: 같은 alias 가 두 vocab 항목에 매핑되면 우선순위 모호.
  → 1:1 매핑만 허용 (이중 매핑 검증 테스트 추가).
- **영향받을 테스트**: `test_disallowed_*` 류는 명백히 무관계 타입 (`blesses`,
  `made_up_type`) 이라 통과. 어휘 외 타입을 패치 후에도 *여전히* 떨어뜨리는지
  확인.

### F-CG2-02/04

- **위험 가설 1**: 임계값 16K 디폴트 변경 → 작은 문서에는 영향 0. 그러나 16K
  넘는 문서가 폴백 경로(unit 분할)로 떨어지면 F-CG-04 패치 후라도 cumulative
  검증은 한 호출 안에서만 유효 — 문서 내 cross-unit 관계는 다시 복구 보장이
  애매. **단**, F-CG-04 가 cumulative 로 통일되면 unit 폴백 경로에서도
  cross-unit entity 노출 → R4 패치 4개가 같이 들어가야 보호.
- **위험 가설 2**: JSON 파싱 실패 → 폴백 진입 시 LLM 호출이 두 번 일어남
  (`for_document` + `extract_llm_body_graph(units, ...)`). 비용/latency 증가
  가능. 단 폴백은 실패 시에만 일어나므로 빈도 낮음.
- **위험 가설 3**: 폴백 분기 추가로 stats 의미 변화 — `units_total/called`
  필드가 호출자 시점에서 "문서 단위" vs "unit 단위" 의 합산 인지 별개 인지
  애매. → 폴백 시점에서 stats 객체를 새로 만들어 호출자에게 명시적 알림
  필드 (예: `fallback_used: bool`) 추가가 안전.
- **영향받을 테스트**: `test_for_document_failed_llm_returns_empty_with_failed_stat`
  (라인 611-621) — JSON 파싱 실패 시 빈 그래프 반환을 검증. 폴백 도입 시 이
  테스트의 의도가 변함. *패치 시 의도를 명시적으로 갱신* 필요 (실패 시 빈
  그래프 vs 폴백 자동 시도).

### F-CG2-06

- **위험 가설 1**: 과도한 정규화 (예: `User Service` 와 `UserService` 통합)
  → 의도. `User Service` vs `Users` (복수형) 같은 의미상 다른 이름이 같은 stem
  으로 통합되면 잘못된 노드 병합. → stem 함수를 **공백/하이픈/언더스코어/대소
  문자만** 정규화하고, *형태론적 변형* (복수형, 동사형) 은 **제외**.
- **위험 가설 2**: `_canonical_name` 만 stem 매칭으로 바꾸고 dedup 키는 안
  바꾸면, 표기 변형이 2개 노드로 emit 되고 그 중 1개로 관계가 라벨링 → 노드는
  여전히 분리. 두 곳을 *반드시 같이* 변경.
- **위험 가설 3**: 정규화 키와 표시 이름이 어긋남 — 같은 stem 의 두 표기 중
  첫 등장 표기 보존 정책 (현재 `_cross_unit_entity_dedup_keeps_first_casing`
  의 정신) 유지 필요.
- **영향받을 테스트**: `test_cross_unit_entity_dedup_keeps_first_casing` (290)
  — 통과 가능. 새 stem 매칭 단위 테스트 필요.

---

## 4. 두 추출 경로(unit vs 문서) 운용 상태 확인

### 현재 디폴트

`pipeline.py:453-480` 분기:

```
# 디폴트: extract_llm_body_graph_for_document (문서 단위 1회)
# 폴백 (InputTooLargeError catch): extract_llm_body_graph(units, ...)
```

→ 디폴트는 **문서 단위**. R2 가 권고한 하이브리드는 *부분* 구현된 상태:
- (O) 문서 단위 1회 호출
- (O) 입력 한도 초과 시 unit 폴백
- (X) 임계값 디폴트가 R4 환경(qwen2.5:7b @ 32K) 에 맞지 않음 (`200_000` →
      `16_000` 변경 필요)
- (X) 출력 JSON 잘림 시 폴백 없음 (현재는 빈 그래프 반환)
- (X) configurable 임계값을 LLMBodyExtractionConfig 외부에서 주입할 경로 없음
      (`pipeline.py:457` 가 `config` 인자 없이 호출 → 항상 기본 cfg 사용)

### F-CG2-02/04 가 끼워질 자리

3가지 변경이 모두 같은 위치에 동시 적용 가능:

1. `LLMBodyExtractionConfig.max_input_tokens` 디폴트 `200_000` → `16_000` 변경.
   - 영향: 16K~200K 범위 문서가 폴백 경로로 떨어짐. 폴백 경로의 cross-unit
     검증은 F-CG-04 패치로 보호.
2. `extract_llm_body_graph_for_document` 의 JSON 파싱 실패 (라인 345-350) →
   `InputTooLargeError` 와 *동급의* 별도 예외 raise. `pipeline.py:471` 분기를
   확장하여 같은 폴백 경로로 흡수.
3. `pipeline.py:457` 가 `config=...` 명시적으로 전달하도록 변경하면 환경별
   임계값 override 가능. 현재는 항상 기본값. — **추측**: 운영 가드에서 R4
   환경별 cfg 주입 경로가 필요할 수도 있음. 본 분석에서는 디폴트 변경만으로도
   요구 충족.

→ R2 의 "하이브리드" 권고가 *50% 구현* 된 상태. R4 패치는 나머지 50% 완성
(임계값 + 출력 잘림 대응).

---

## 5. alias 매핑 표 초안 (F-CG2-08)

> 보수적 화이트리스트. graph_vocabulary 의 `RELATION_TYPES` (라인 74-99) 와
> `ENTITY_TYPES` (라인 38-67) 기준. LLM 이 흔히 만드는 **형태론적 변형 / 표기
> 부주의** 만 포함. 추측 명시.

### relation_type alias (현용 어휘 → 변형 후보)

| canonical | aliases (LLM 변형 후보) | 근거 |
|---|---|---|
| `depends_on` | `depending_on`, `depend_on`, `dependent_on` | 동명사/현재형/형용사 변형 — R2 보고서 예시 인용 |
| `has_part` | `part_of`, `contains_part`, `has_parts` | "A has part B" 와 "B part_of A" 의 방향 혼동 — *주의: 방향 뒤집힘이라 매핑 시 source/target 도 swap 해야 정확. 단순 type 매핑만 하면 의미 왜곡* (추측: 보수적으로 *제외* 권장) |
| `owned_by` | `owner`, `owned`, `owns_by` | 분사 누락 변형 |
| `uses` | `uses_of`, `using`, `utilizes` | 추측: `uses` 와 `utilizes` 는 의미 동일하지만 LLM 이 굳이 변형할 가능 |
| `calls` | `call`, `calling`, `invokes` | 단수/동명사 — *주의: `invokes` 는 `calls` 와 의미 거의 동일하지만 별개 entry 가 될 가치도 있음. 추측: 보수적으로 매핑* |
| `implements` | `implement`, `implementing`, `implementation_of` | 시제 변형 |
| `provides` | `provide`, `providing`, `offers` | *주의: `offers` 는 의미 분리 가능. 추측: 제외* |
| `supersedes` | `supersede`, `replaces`, `deprecated_by` | *주의: `replaces` 와 `deprecated_by` 는 방향 다름. 추측: `supersede` 만 매핑* |
| `documented_in` | `documents_in`, `documented_at`, `described_in` | 표기 부주의 |

### entity_type alias

| canonical | aliases | 근거 |
|---|---|---|
| `system` | `service`, `application`, `app` | 영문 동의어. *주의: `service` 는 LLM 이 자주 사용 — 매핑 시 노드 통합 효과 큼* |
| `module` | `component`, `package`, `library` | 추측: `library` 는 외부 라이브러리 의미라 다를 수도. 보수적으로 `component` 만 |
| `team` | `org`, `organization`, `group` | 동의어 — *주의: `group` 은 너무 광범위. 추측: 제외* |
| `policy` | `rule`, `guideline`, `policies` | 복수형 + 동의어 |
| `person` | `user`, `developer`, `engineer` | 추측: `user` 는 도메인 의미라 다를 수도 — `person` 으로 통합되면 도메인 사용자 vs 팀원 구분 손실 |

### 매핑 적용 시 의사결정 필요 사항

1. **방향 의존 관계** (`has_part` ↔ `part_of`, `supersedes` ↔ `superseded_by`)
   는 단순 type 매핑이 아니라 *source/target swap* 까지 필요. 안전을 위해
   **이번 라운드에서는 동일 방향 변형만** alias 로 포함하고, 방향 반전 변형은
   drop 유지 권장. 추측: 이 정책이 R4 운영 가드의 "보수적" 원칙에 부합.
2. **의미상 동의어** (`invokes`/`calls`, `service`/`system`) 는 alias 로 통합
   하면 LLM 신호를 정상 흡수할 수 있어 가치가 큼. 다만 graph_vocabulary 어휘
   확정 의도와 충돌 가능 — 추측: design 단계에서 어휘 단일 출처 원칙과의
   양립성 결정 필요.
3. **위 매핑은 단일 패치 라운드 안에 모두 적용하지 않아도 됨**. 가장 안전한
   순서: relation_type 시제/동명사 변형 → relation_type 동의어 → entity_type.

---

## 결론

4개 패치 모두 *현재 코드 상태와 R2 보고서 가정이 일치* 하며 적용 가능.
- F-CG-04: 변수 스코프 1줄 이동 + 새 fixture.
- F-CG2-08: vocab 모듈에 alias 상수 + 두 함수에 정규화 호출 추가.
- F-CG2-02/04: 폴백 골격은 이미 있음. 임계값 디폴트 변경 + 출력 잘림 대응
  분기 추가 + pipeline.py 폴백 분기 확장.
- F-CG2-06: stem 정규화 함수 도입 + dedup 키와 `_canonical_name` 두 곳 동시
  적용.

회귀 위험은 모두 **보수적 패치 표 채택 + dedup 키와 lookup 키 동시 변경 +
새 fixture 추가** 로 통제 가능. 4개를 *함께* 적용하면 F-CG2-02/04 의 unit
폴백 경로에서도 F-CG-04 의 cross-unit 보호가 자동 유지되는 시너지가 있어
함께 가는 게 안전.
