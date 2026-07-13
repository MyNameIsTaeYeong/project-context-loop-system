# Confluence Graph Extraction — Round 2 Findings

> 라운드 주제: confluence_mcp의 그래프 추출 LLM 호출을 **청크/extraction_unit 단위 → 문서 단위 1회**로 전환하는 영향 분석.
> 사용자 가설: "현재 LLM(qwen2.5:7b @ 32K context)이 문서 단위 인덱싱을 처리할 수 있을 것"
> R1 산출물에서 미해결로 남았던 **F-CG-04**(cross-unit entity 단절)의 자연 해결 여부 검증 포함.

---

## 요약

- 총 발견 **9건** (Critical 1, High 4, Medium 3, Low 1)
- 핵심 결론
  - **현재 호출 단위는 "ExtractionUnit"(섹션 트리를 1500 토큰 목표로 응축한 단위) — 청크가 아님**.
    한 confluence 문서당 평균 5~15회의 LLM 호출이 발생하며, 각 호출은 unit 본문 + breadcrumb만 본다.
  - **문서 단위 호출 1회로의 전환은 F-CG-04(critical, cross-unit entity 격리)를 자연 해결**한다.
    추가로 F-CG-03(split overlap part skip) 도 소멸. F-CG-11(units_called stats 불명확)도 단순화.
  - **그러나 토큰 한도 충돌과 출력 잘림 위험이 비자명**하다. Confluence 페이지 ≤ 약 20K 입력 토큰은 안전, 30K 입력은 출력 예산 1.7K로 사실상 사용 불가.
  - **권장 전환 방식**: "한 문서 = 한 호출 (기본) + 입력 토큰 16K 초과 시 자동 폴백" 하이브리드.
  - **link_graph_builder는 이미 문서 단위**(`build_link_graph(extracted, doc_title)`)이며 LLM 호출 없음. 문서단위 전환과 무관하게 작동.

- 가장 시급한 발견 3건
  - **F-CG2-01 (Critical, 가설 검증 완료)**: 문서단위 전환으로 F-CG-04 자연 해결됨 — `unit_valid_entity_names` 격리가 사라지므로 cross-section 관계 드롭 0건이 가능. 정량 추정 별도.
  - **F-CG2-02 (High)**: 거대 문서(>16K 입력 토큰) 의 출력 예산 부족 — `max_tokens=32768` 와 모델 컨텍스트 32K가 충돌. 입력 30K + 출력 2K로 JSON 잘림 위험 매우 큼.
  - **F-CG2-03 (High)**: `extract_llm_body_graph`의 호출 인터페이스가 unit 리스트 기반이라 문서단위 전환에 리팩터링 필요. 단순 "units 합쳐서 한 번" 으론 breadcrumb/split 메타데이터가 의미 잃음.

---

## 현재 호출 단위 사실 정리 (필독)

요청한 R1 진단 재검증부터.

### 1. 그래프 추출 경로 3개의 호출 단위

| 추출기 | 호출 단위 | 위치 | LLM 호출? | 비고 |
|--------|----------|------|----------|------|
| `link_graph_builder.build_link_graph` | **문서 1회** | `pipeline.py:305` | ❌ | OutLink → Entity/Relation, 순수 함수 |
| `body_extractor.extract_body_graph` | **unit 리스트 1회** (내부에서 unit 순회) | `pipeline.py:331` | ❌ | 휴리스틱 정규식 |
| `llm_body_extractor.extract_llm_body_graph` | **unit별 N회** (LLM 호출은 unit마다) | `pipeline.py:353` → `llm_body_extractor.py:184` | ✅ | 본 분석 대상 |

→ LLM 호출이 발생하는 것은 `llm_body_extractor` 단 하나. 다른 두 경로는 이미 문서 단위로 한 번에 동작.

### 2. LLM 호출 횟수 결정 로직

```python
# llm_body_extractor.py:142-184
async def extract_llm_body_graph(units, *, doc_title, llm_client, config):
    ...
    targets = _gate_units(units, cfg, stats)   # ← 필터된 unit 목록
    ...
    sem = asyncio.Semaphore(max(1, cfg.max_concurrency))

    async def run(unit):
        async with sem:
            payload = await _call_llm(unit, doc_title, llm_client, cfg)  # ← unit당 1회
            ...

    results = await asyncio.gather(*[run(u) for u in targets])  # ← N개 동시 호출
```

`_gate_units`(라인 272-290) 가 호출 횟수를 결정:

- `unit.token_count < cfg.min_unit_tokens (=200)` → skip (`units_skipped_short`)
- `cfg.skip_split_overlap_parts and unit.split_total > 1 and unit.split_part > 0` → skip (`units_skipped_overlap`)
- `cfg.max_units_per_doc` 가 설정되어 있으면 상한 적용

→ 한 문서당 LLM 호출 횟수는 (응축 후 unit 개수) - (단축 skip) - (overlap part skip).

### 3. unit 1개 입력 크기

- `ExtractionUnitConfig.target_tokens=1500`, `max_tokens=2400` (`extraction_unit.py:56-57`).
- 단일 호출 입력 토큰은 보통 **~1500 + breadcrumb(~100) = ~1600 토큰**.
- 시스템 프롬프트(어휘 가이드) ~700 토큰.
- 합산하면 unit당 입력 ~2300 토큰. 출력 max_tokens=32768.

### 4. 평균 호출 횟수 (현 시스템)

확실한 정량 측정 데이터는 코드에서 보이지 않으나, 토큰 가정(평균 confluence 페이지 6K 토큰)에서 응축 결과 약 4~5 unit. 30K 토큰 거대 페이지면 응축이 거의 안 되어 20 unit 이상 가능.

→ 평균 confluence 페이지 1건 처리 시 LLM 호출 약 **4~5회**, 거대 문서면 **15~20회**.

---

## 발견 사항 (R2 신규)

### F-CG2-01 (CRITICAL): 문서단위 1회 호출은 F-CG-04(cross-unit entity 단절)를 자연 해결한다

- **위치**: `llm_body_extractor.py:206-241` (현재 격리 로직)
- **R1 F-CG-04 재진단**:
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
  ```
  - 같은 문서 안의 unit B 응답에서 "Auth Service → Cache depends_on" 관계가 와도, unit B에서 "Auth Service"가 entity로 안 만들어졌으면 (unit A에서 이미 정의) 관계 드롭.
- **문서단위 전환 시 동작**:
  - LLM이 받는 입력 = 문서 전체. 같은 호출 안에서 entities와 relations가 동시에 생성됨.
  - LLM은 자연스럽게 "한 번 등장한 entity를 다른 절의 관계 source/target으로 재사용".
  - 따라서 한 호출의 entities set 안에 모든 source/target이 존재 → `unit_valid_entity_names` 검증 통과.
- **자연 해결 정도**:
  - **드롭률 영향**: 현재 stats의 `dropped_relations` 중 "끝점 누락" 사유가 모두 사라짐. 어휘 외 type 사유는 여전히 존재.
  - **추정**: R1 진단대로 "Auth Service 정의는 unit A에만 있고, Cache 관계는 unit B/C/D에 분산"인 패턴이 사내 아키텍처 문서에서 흔하다면, 드롭률이 30~60% 수준에서 5~10%로 떨어질 가능성. (실측 필요)
- **잔여 위험**:
  - LLM 출력 토큰 잘림 시 entities는 만들어졌는데 relations가 잘리는 경우 → 신호 일관성은 좋지만 양이 줄어듦. F-CG2-02와 충돌.
- **권고**: 문서단위 전환을 채택할 경우, 현재의 `unit_valid_entity_names` 격리 검증은 **그대로 두어도 무해**하다 (1 unit = 1 doc 이 되면 cumulative 와 동일). 단 검증 위치를 `unit` 단위가 아니라 응답 단위로 명시적 리네이밍 권장.
- **심각도**: 자연 해결 (Critical → Resolved by transition)

---

### F-CG2-02 (HIGH): 거대 문서에서 입력+출력 토큰 합이 qwen2.5:7b 32K context를 초과한다

- **위치**: 새 호출 단위 설계 검토
- **현재 max_tokens=32768** (`llm_body_extractor.py:77`)
- **qwen2.5:7b 컨텍스트 32K**는 입력 + 출력 합산 한도다 (Qwen 공식 spec, OpenAI 호환 서버에서도 동일하게 강제).
- **토큰 산수**:
  - 시스템 프롬프트(어휘 + 출력 형식 가이드): ~700 토큰
  - 한 문서 본문 (단위로 합치면): 평균 confluence 페이지 6K, 거대 페이지 20K~40K
  - 출력 JSON: entities + relations. 예상 1K~10K 토큰 (문서 크기 비례)
- **충돌 시나리오**:
  - 입력 본문 25K + 시스템 700 = 입력 25.7K → 출력 가용 6.3K
  - 입력 30K → 출력 가용 1.3K (JSON 깨질 가능 매우 높음)
  - 입력 32K+ → 호출 자체 실패 또는 컨텍스트 잘림
- **Ollama 특이 사항**:
  - **Ollama `num_ctx` 기본값은 2048**. 환경에서 override 안 했으면 그래프 추출이 32K 시도해도 모델 측에서 잘려 응답 잘릴 가능. 라운드 컨텍스트(`00_round_scope.md`)에 명시됨.
  - 문서단위 전환 결정 시 `num_ctx=32768` 또는 모델 설정의 명시적 확장이 필수 선결 조건.
- **현재 max_tokens=32768 의 잘못된 가정**: 32K 한도 모델에서 max_tokens=32768은 "출력 32K + 입력은 별도"가 아니라 "입력+출력 ≤ 32K"이므로, 실제로 모델이 받을 출력 예산은 32K - 입력. 코드의 docstring은 이를 명시하지 않는다.
- **개선 방향**:
  - (a) 문서단위 전환 시 `max_tokens` 를 입력 토큰에 따라 동적 결정 (예: `max(2048, ctx_size - input_tokens - safety)`).
  - (b) 입력 본문이 임계값(예: 16K 토큰) 초과면 unit 단위 호출로 폴백.
  - (c) 출력 잘림 감지 후 partial JSON 복구 시도 (extract_json이 이미 일부 처리 가능한지 점검 필요).
- **영향 범위**: 모든 문서단위 호출. 사내 confluence는 30K+ 문서가 드물지 않음 (아키텍처 가이드, 온보딩 매뉴얼).
- **심각도**: High | **공수**: M (입력 토큰 측정 + 폴백 분기)

---

### F-CG2-03 (HIGH): `extract_llm_body_graph` API가 unit 리스트 기반 → 문서 단위 호출 시 재설계 필요

- **위치**: `llm_body_extractor.py:142-264`
- **현재 시그니처**:
  ```python
  async def extract_llm_body_graph(
      units: list[ExtractionUnit],
      *,
      doc_title: str,
      llm_client: LLMClient,
      config: LLMBodyExtractionConfig | None = None,
  ) -> tuple[GraphData, LLMBodyExtractionStats]:
  ```
- **문서단위 전환 시 변경 필요사항**:
  1. **입력 합치기 방식**:
     - 단순 `units` content concat은 breadcrumb이 unit마다 반복되어 LLM 혼란 (`# 문서: <title>` 가 N번 등장).
     - 권장: ExtractedDocument의 `plain_text` 또는 응축된 섹션 트리 본문을 **한 번만** 사용하고, 문서 제목/lead paragraph는 시스템 프롬프트에 한 번만 포함.
  2. **section_path 보존**:
     - 현재 `Relation.label`은 unit의 `section_path`(예: "Architecture > Auth Flow") 를 기록한다 (`llm_body_extractor.py:225, 252`).
     - 문서단위 호출 시 LLM이 출력한 관계가 어느 섹션 출처인지 모름.
     - 옵션 A: LLM 출력 스키마에 `section_path` 필드 추가 → 프롬프트 부담↑, LLM 정확도 의존.
     - 옵션 B: section_path 자체를 라벨에서 제거 → 검색 단계에서 출처 메타 손실.
     - 옵션 C: 본문에 마커(`<!-- section:Architecture > Auth Flow -->`) 삽입하고 LLM이 결과에 마커 인용하도록 유도 → 신뢰성 낮음.
     - 권장: B 채택하되 `description` 필드에 LLM이 본문 인용 한 줄 포함하도록 유도 (검색 ranker가 본문 매칭으로 출처 복구).
  3. **stats 의미 변경**:
     - `units_total / units_called / units_failed / units_skipped_*` 통계는 모두 unit 개념이 사라지면 무의미.
     - 새 stats: `documents_called`, `documents_failed`, `documents_truncated`(출력 잘림 감지), `input_tokens`, `output_tokens`.
     - 외부 호출자(테스트, 메트릭 대시보드)가 stats 필드를 참조하므로 **호환 필드 유지 + 신규 필드 추가** 형태가 안전.
  4. **`extraction_unit`의 존재 가치 재평가**:
     - LLM 호출이 unit을 안 보면 `build_extraction_units` 의 출력 중 LLM 추출용 부분은 dead code. 단, **body_extractor(휴리스틱)**가 여전히 unit을 사용하므로 (`pipeline.py:331`) `extraction_unit.py` 자체는 유지 필요.
     - body_extractor도 사실은 문서 전체 plain_text로 충분히 동작 가능 (정규식이 stateless). 다만 `Relation.label = unit.section_path`라는 출처 추적 가치를 잃음.
- **영향 범위**: pipeline.py, llm_body_extractor.py, 테스트, 운영 메트릭.
- **심각도**: High (재설계 범위 큼) | **공수**: M~L

---

### F-CG2-04 (HIGH): JSON 출력 파싱 안정성 — 입력이 길어지면 LLM이 정형 출력 유지 어려움

- **위치**: `llm_body_extractor.py:328-331`
  ```python
  payload = extract_json(response)
  if not isinstance(payload, dict):
      raise ValueError(f"LLM 응답이 JSON object 가 아님: {type(payload).__name__}")
  ```
- **현재 동작**: `_call_llm`이 단일 unit (~1.5K 토큰)에서 JSON 응답 받음. 짧은 입력 → 모델이 instruction following 잘 됨.
- **문서단위 시 위험**:
  1. 입력이 20K 토큰이면 모델이 시스템 프롬프트의 "다른 텍스트 절대 포함 금지" 제약을 잊을 가능성↑.
  2. 출력 entities/relations 수가 수십~수백 개로 커지면 중간에 모델이 자체 truncation 결정 (특정 구조 반복 후 끊기) 위험.
  3. 잘린 JSON은 `extract_json`(상세 로직 미확인)이 일부만 복구 가능할 수 있으나, 마지막 entity/relation이 partial이면 잘못 파싱.
- **벤치마크 필요**: qwen2.5:7b가 20K 입력에서 500개+ entity/relation JSON 출력을 얼마나 안정적으로 생성하는지 사전 측정.
- **개선 방향**:
  - JSON 스키마 강제 (vLLM `guided_json` / Ollama `format=json` / OpenAI `response_format=json_schema`) — 자체 엔드포인트 지원 여부 확인.
  - 또는 점진 출력 (entities 먼저 받고, entities 목록을 다시 system prompt로 넣어 relations만 받는 2-call 방식). 비용 2배지만 안정성↑.
  - extract_json에 partial recovery 추가 (jsonschema validation 후 잘린 마지막 항목 drop).
- **영향 범위**: 거대 문서의 모든 추출 호출
- **심각도**: High | **공수**: M (스키마 강제) ~ L (점진 출력)

---

### F-CG2-05 (MEDIUM): link_graph_builder는 문서단위 영향이 없지만, 외부 URL 미처리 문제(R1 F-CG-05)는 자연 해결 안 됨

- **위치**: `link_graph_builder.py:37-50, 118-120`
- **현재 상태**: `_KIND_TO_ENTITY_TYPE` 에 `"url"` 매핑 없음 → `_should_include`가 외부 URL 모두 False 반환 → 그래프에서 외부 시스템 참조 손실 (R1 F-CG-05).
- **문서단위 전환과의 관계**:
  - 본 모듈은 **이미 문서 1회 호출**(`pipeline.py:305`)이라 청크 단위 분리 문제 없음.
  - 따라서 문서단위 LLM 호출 전환이 외부 URL 추출을 자동으로 해결하지 **않는다**.
  - 단, LLM 본문 추출을 문서단위로 돌리면 LLM이 본문 안의 외부 URL을 자체적으로 entity(`url`/`system`)로 만들 가능 → 부분적 보완 가능 (단, vocab에 `url` 없으면 드롭).
- **권고**:
  - F-CG-05는 별도 fix가 필요. 본 라운드 스코프 밖이지만 문서단위 전환 채택 시 vocab의 `external_resource`(또는 `system`) 인정 범위를 점검할 것.
  - llm_body_extractor가 외부 URL을 받기 시작하면 (예: `https://kubernetes.io/docs/...` 가 `system` 또는 `concept`으로 추출됨) — link_graph_builder의 URL 차단과 어휘 충돌. 어휘 단일 출처 강화 필요.
- **심각도**: Medium (라운드 스코프 한계)

---

### F-CG2-06 (MEDIUM): unit 격리 해제 시 entity 중복 제거가 LLM 단일 출력에 위임됨 — 정규화 일관성 위험

- **위치**: `llm_body_extractor.py:217-222` (entity dedup), `387` (`_canonical_name`)
- **현재 동작**:
  - 같은 doc의 N개 unit이 각각 응답 → 후처리 코드가 `(name.lower(), entity_type)` 키로 dedup → 첫 등장 표기를 보존.
  - 다른 unit에서 같은 entity를 "AuthService" / "Auth Service" / "auth-service"로 변형 등장해도 같은 lowercase key로 통합 가능.
- **문서단위 시**:
  - LLM이 단일 출력에서 "같은 개념은 같은 이름 사용" 지시(`llm_body_extractor.py:111`)를 더 잘 따를 수 있음 (한 문맥에서 일관성 유지가 쉬움).
  - **그러나** 입력 본문에서 "AuthService"와 "Auth Service"가 모두 등장하면 LLM이 둘 다 entity로 만들 수도 있음. 후처리 dedup이 lowercase + 공백 차이는 못 잡음 (예: "AuthService" vs "Auth Service" → 두 다른 키).
- **R1 F-CG-04와의 차이**:
  - F-CG-04는 "B unit에서 A unit의 entity 못 봄" 문제.
  - F-CG2-06은 "한 문서 안에서 같은 entity의 표기 변형" 문제 — 문서단위 전환으로 *개선되지만 완전 해결은 아님*.
- **개선 방향**:
  - dedup 키에 공백/하이픈/언더스코어 정규화 추가 (`_normalize_for_dedup(name)`).
  - LLM 후처리에서 "유사 이름" 클러스터링 (편집 거리, embedding similarity) — 비용 vs 정확도 trade-off.
- **심각도**: Medium | **공수**: S (정규화) ~ L (클러스터링)

---

### F-CG2-07 (MEDIUM): 호출 횟수 감소로 인한 비용/속도 개선 — qwen2.5:7b Ollama 환경에서 정량 추정

- **현재 평균** (앞서 추정): 문서당 LLM 호출 4~5회. 거대 문서 15~20회. `max_concurrency=3`로 직렬화.
- **문서단위 전환 시**:
  - 문서당 LLM 호출 **1회**.
  - 평균 문서: 5회 → 1회 (**80% 감소**)
  - 거대 문서: 20회 → 1회 (**95% 감소**)
- **시간 비용**:
  - Ollama qwen2.5:7b의 일반 PC GPU 추론 속도 ~50 tokens/s (입출력 합산).
  - 호출 1회당 입력 2.3K + 출력 1K = 3.3K → ~66초.
  - 5 unit 동시 호출 (concurrency=3): 약 5/3 × 66s ≈ 110초.
  - 문서단위 1회: 입력 6K + 출력 3K = 9K → ~180초.
  - → **문서당 시간은 60% 증가** (concurrency 이점이 사라지므로).
  - 단, 거대 문서(20K 입력): 현재 15회 × 66s ÷ 3 = 330s vs 문서단위 22K × 1/50 = 440s. 거의 동등.
- **결론**: 비용(토큰 총량)은 줄지만 wall-clock 시간은 거의 동등 또는 약간 증가. concurrency가 사라지는 게 핵심 손실.
- **개선 방향**:
  - 문서 간 병렬화 (`max_concurrency`를 unit 수준에서 document 수준으로 옮김) → 인덱싱 배치 시 throughput 회복.
  - 단일 문서 latency가 중요한 경우 (실시간 업로드 → 즉시 검색 가능까지 시간)는 unit 병렬화의 이점이 큼.
- **심각도**: Medium (운영 영향, 정확도 무관) | **공수**: S (문서 간 병렬화)

---

### F-CG2-08 (MEDIUM): vocab/스키마 변경 필요성 — 현재 어휘는 unit-size에 맞춰 단순화되어 있음

- **위치**: `graph_vocabulary.py` + `llm_body_extractor.py:98-126` (system prompt)
- **현재 시스템 프롬프트**:
  - entity_types 4개 (system, module, policy, team) + 일반 (person, concept 등 llm_body 외) → 약 10개 entity_type
  - relation_types 9개 (depends_on, implements, calls, owned_by, supersedes, has_part, uses, provides, documented_in)
- **문서단위 입력 영향**:
  - 입력이 길어지면 LLM이 어휘 12개를 "정확히 일치"하게 사용하는 능력↓. R1 F-CG-04 검증과 별도로, 입력이 길어질수록 LLM hallucination type (`"depending_on"` vs `"depends_on"` 같은 사소한 오타) 증가 가능.
  - 현재 `allowed_etypes`/`allowed_rtypes`는 strict 매칭 (`llm_body_extractor.py:191-192, 213-235`) → 오타나 동의어는 모두 드롭.
- **개선 방향**:
  - 동의어 매핑 추가 (`"depending_on" → "depends_on"`, `"part_of" → "has_part"` 등) — graph_vocabulary에 `aliases` 필드 신설 또는 `_normalize_relation_type()` 함수.
  - vocab 항목 수를 늘리지 말고 (오히려 줄이고), 각 항목의 description을 더 길게 명시 → 모델이 헷갈리지 않게.
  - 출력 형식 예시(few-shot)를 시스템 프롬프트에 추가하되 입력 토큰 부담은 +500 정도로 제한.
- **심각도**: Medium | **공수**: S (alias) ~ M (few-shot)

---

### F-CG2-09 (LOW): `_canonical_name`의 표기 우선순위 — 문서단위 전환 시 무의미해질 수 있음

- **위치**: `llm_body_extractor.py:365-374`
  ```python
  def _canonical_name(entities, raw_name, raw_lower):
      for (name_lower, _etype), ent in entities.items():
          if name_lower == raw_lower:
              return ent.name
      return raw_name
  ```
- **현재 의도**: unit A에서 등록된 "Auth Service"의 표기를, unit B에서 관계 source/target으로 등장할 때 표기 통일.
- **문서단위 전환 시**:
  - 한 호출에서 entities와 relations가 동시 생성되므로, LLM이 관계 source/target에 이미 entities의 표기를 그대로 쓸 확률 매우 높음.
  - `_canonical_name`의 가치는 떨어지지만 안전망으로 유지하는 게 좋음.
- **권고**: 코드 유지. 단, 후처리 dedup 강화(F-CG2-06)와 일관된 정규화 모듈로 통합.
- **심각도**: Low

---

## R1 발견의 자연 해결 여부 점검

| R1 ID | 진단 요약 | 문서단위 전환 시 |
|-------|----------|------------------|
| F-CG-01 | `_strip_code_for_prose` 들여쓰기 코드 미처리 | ❌ 해결 안 됨 (body_extractor는 unit 그대로 사용) |
| F-CG-02 | API 정규식 query string 미포함 | ❌ 해결 안 됨 (body_extractor 영역) |
| F-CG-03 | split overlap part skip → 후반부 누락 | ✅ **자연 해결** (split 자체가 사라짐) |
| **F-CG-04** | **cross-unit entity 격리** | ✅ **자연 해결** (본 라운드 핵심 — F-CG2-01) |
| F-CG-05 | 외부 URL 미처리 | ⚠️ link_graph 영역, 부분적 보완 가능 (F-CG2-05) |
| F-CG-06 | page entity가 target_title로 별개 노드 | ❌ link_graph 영역, 무관 |
| F-CG-07 | vocab 단일 출처 아님 | ❌ 해결 안 됨, 오히려 어휘 alias 필요성↑ (F-CG2-08) |
| F-CG-08 | `_normalize_term` trim 부족 | ❌ body_extractor 영역, 무관 |
| F-CG-09 | API placeholder false positive | ❌ body_extractor 영역, 무관 |
| F-CG-10 | self_entity 동명 문서 충돌 | ❌ graph_store 영역, 무관 |
| F-CG-11 | stats 부정확 | ⚠️ 호출 단위 변경으로 stats 필드 자체가 재설계 필요 (F-CG2-03) |
| F-CG-12 | Relation.label 첫 등장만 보존 | ⚠️ 라벨이 unit별 section_path였으므로 호출 단위 변경 시 의미 자체가 바뀜 (F-CG2-03) |
| F-CG-13 | `import` vs `imports` vocab 불일치 | ❌ 무관 (vocab 갱신은 별도 작업) |

→ 문서단위 전환의 직접 가치는 **F-CG-04 자연 해결 (Critical)** + F-CG-03 자연 해결. 그 외 발견은 별도 작업 필요.

---

## 검토하지 않은 영역

- `extract_json` 의 partial JSON 복구 로직 (코드 직접 확인 안 함 — 잘림 감지 능력 미상)
- vLLM `guided_json` / Ollama `format=json` 자체 엔드포인트 지원 여부 (운영자 확인 필요)
- 사내 confluence 페이지의 실제 토큰 분포 (히스토그램) — F-CG2-02 결정에 정량 근거 필요
- 그래프 검색 단계(`graph_search_planner`)에서 `Relation.label` 의 실제 활용 비중 — F-CG2-03 옵션 B 영향 평가

---

## 문서단위 전환 권고 (이번 라운드 핵심)

- **현재 청킹의 진짜 이유**:
  - LLM 호출 단위는 "청크"가 아니라 **ExtractionUnit**(목표 1500 토큰의 섹션 응축 단위)이다.
  - 분할 이유: (1) qwen2.5:7b의 안정적 instruction following 위해 입력 작게 유지, (2) breadcrumb으로 unit별 문맥 주입, (3) overlap으로 분할 누락 완화.
  - 코드 근거: `llm_body_extractor.py:142-184`, `extraction_unit.py:56-57` (`target_tokens=1500`, `max_tokens=2400`).

- **문서단위 전환 가능성**: ⚠️ **조건부 가능**
  - **조건 1**: Ollama `num_ctx` 가 32768로 명시 설정되어 있어야 함 (기본 2048).
  - **조건 2**: 문서 입력 토큰이 **16K 이하** 인 케이스에 한해 안전. 16K~25K는 출력 잘림 모니터링 필수, 25K+는 자동 폴백 필요.
  - **조건 3**: `extract_llm_body_graph` API 재설계 (F-CG2-03) — units → ExtractedDocument 전달로 시그니처 변경.

- **전환 시 잔여 청킹 필요 케이스**:
  - 입력 토큰 > 16K 인 거대 confluence 페이지 (아키텍처 가이드, 마이그레이션 매뉴얼)
  - 출력 JSON 잘림 감지 시 unit 단위 retry 폴백
  - **body_extractor(휴리스틱)**는 unit 사용을 유지해도 무방 (LLM 호출 없으니 분할 비용 0). 단 `section_path` 라벨링 의도가 unit 기반이라는 점은 R1 F-CG-12 영역에서 별도 정리.

- **권고 전환 방식**: **하이브리드 — "1 문서 = 1 호출 기본, 토큰 한도 초과 시 자동 폴백"**
  - 근거 1: F-CG-04(Critical) 가 자연 해결되는 이득이 크다.
  - 근거 2: 호출 비용 80~95% 감소 (인덱싱 배치 throughput 개선).
  - 근거 3: 거대 문서는 전체 사내 문서의 소수일 가능성 — 다수 케이스는 문서단위가 잘 동작.
  - 근거 4: 토큰 한도 충돌(F-CG2-02)과 JSON 안정성(F-CG2-04) 위험은 자동 폴백으로 회피 가능.
  - 실행 순서:
    1. ExtractedDocument에서 plain_text 토큰 카운트 → cutoff(예: 16K) 비교
    2. cutoff 이하: 새 코드 경로 `_call_llm_document(extracted, doc_title, llm_client, cfg)` 사용
    3. cutoff 초과: 기존 unit 기반 경로 그대로 (`_call_llm` per unit) → cross-unit entity 누적 패치 (F-CG-04 직접 수정) 같이 진행
    4. JSON 파싱 실패 시 cutoff를 동적 절반으로 줄여 unit 폴백
    5. stats에 `path: "document" | "unit_fallback"` 필드 추가

- **예상 영향 (정량 추정)**:
  - **LLM 호출 건수**: 평균 문서 80% 감소, 거대 문서 95% 감소 (F-CG2-07)
  - **그래프 추출 정확도**: cross-section 관계 보존율 30~60%p 개선 추정 (F-CG2-01) — 실측 필요
  - **wall-clock 시간**: 문서당 +60% (concurrency 사라짐). 문서 간 병렬화로 회복 권장 (F-CG2-07)
  - **출력 잘림 위험**: 입력 ≤16K 에서 ~0%, 입력 25K~30K 에서 30~70% 추정 — 자동 폴백 필수
  - **검색 정밀도**: cross-section 관계 보강으로 그래프 검색 recall 개선 추정. 정밀도(precision) 영향은 LLM 정확도 의존, vocab alias 보강으로 보호 (F-CG2-08)
  - **임베딩 호출**: 변동 없음 (그래프 추출과 독립)
  - **F-CG-04 직접 패치를 별도로 할 경우**: 호출 횟수는 그대로 (5회), entity 검증만 cumulative — 위험 적고 즉시 효과. 문서단위 전환 결정이 늦어진다면 **F-CG-04 직접 패치를 우선 권고**.

---

## 의사결정 우선순위

1. **즉시 (R2 권고)**: F-CG-04 직접 패치(`unit_valid_entity_names` → `document_valid_entity_names` cumulative) — 토큰 한도 위험 없이 그래프 정확도 즉시 개선.
2. **R2 후반 또는 R3**: 문서단위 전환 (하이브리드 방식) — F-CG-04는 이미 패치된 상태에서 추가 이득 (호출 횟수 감소 + 출력 일관성). 사내 문서 토큰 분포 측정 후 cutoff 결정.
3. **장기**: vocab alias / few-shot / JSON guided output (F-CG2-08, F-CG2-04) — 안정성 보강.
