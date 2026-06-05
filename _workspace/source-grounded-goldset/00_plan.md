# 원문 코퍼스 기반 골드셋 생성 — 작업 계획서 (핸드오프)

> 이 문서는 **새 세션이 이어받아 실행**하기 위한 자립형 계획서다. 브랜치
> `claude/source-grounded-goldset` (origin/main = PR #78 머지 포함 기준).
> 선행 맥락은 `_workspace/findings/SUMMARY.md`(감사, 특히 R3)와 PR #78 변경분.

---

## 1. Context — 왜 이 작업을 하나

PR #78 감사는 현재 그래프 골드셋이 **self-fitting(R3)** 임을 확정했다: 골드 정답이
평가 대상 인덱스 노드에서 직접 유래(`build_synthetic_gold_set.py::load_candidate_subgraphs`가
graph_store 노드를 읽음 → `_make_graph_gold_item`이 `sg["entity_name"]`을 복붙)하고,
평가 시 retrieved도 같은 인덱스라 T1 exact가 자명 통과한다. 결과: **"검색 품질이
아니라 골드셋 생성 방식을 측정"** 하게 되어 **절대 품질·인덱싱 개선 측정이 불가**하다.
현재 그래프 메트릭은 "동일 인덱스 위 검색/플래너 변경의 방향성"만 신뢰 가능(상대 A/B).

**목표**: 골드 생성의 *방향을 인덱스→골드에서 원문→골드로 뒤집어*, 정답을 **원문 문서가
말하는 사실(원문 인용에 고정)** 에서 독립 추출한다. 그래야 인덱스가 놓친 사실을 벌할 수
있어 **절대 품질 + 인덱싱 개선 A/B**를 측정할 수 있다.

**이 작업이 대체하지 않는 것**: PR #78의 채점·통계·감사 토대(surface tier 메트릭,
그래프 CI, 비교 가드, `detect_role_collisions`, 매칭 증거)는 그대로 **딛고 선다**. 본
작업은 그 위에 올리는 **생성 레이어 교체 + 그에 맞는 채점 경로 추가**다.

## 2. 제1원리 — 절대 품질용 골드의 필수 조건

| 조건 | 의미 |
|---|---|
| 독립성 | 정답이 평가 대상(인덱스/추출기/검색기/임베딩)에서 유래 금지 |
| 원천 근거 | 정답이 "원문이 실제로 말하는 사실"(verbatim 인용)에 닿아야 함 |
| 음의 공간 | "인덱스가 놓친 사실"을 벌할 수 있어야 함 (인덱스에 있는 것만 골드면 불가) |
| 재현성 | 1회 생성 후 동결·버전·provenance 기록 |
| 인간 앵커 | 진짜 "절대"는 소규모 인간 검증 기준선이 있어야 신뢰됨 |

## 3. 핵심 발상 (생성 방향의 역전)

```
현재:  인덱스 노드  →  골드 (노드명 복붙)            → self-fitting
신규:  원문 문서  →  검증가능 사실(원문 인용)  →  골드  →  그 골드로 인덱스/검색을 시험
```
3대 장치:
- **evidence_span(원문 인용)으로 진실 고정** — "LLM이 그렇다더라"가 아니라 "원문에 이렇게 쓰임". 채점도 이 인용 기준.
- **정답 표기형을 LLM 자유나열에서 분리** — 결정론 정규화 변형 + 검증된 동의어만(누설/trivial 통과 차단; PR #78 `sanitize_*` 재사용).
- **모델 4중 분리** — 추출LLM ≠ 인덱싱LLM ≠ 시스템(planner/HyDE)LLM ≠ judgeLLM, 임베딩도 분리. `detect_role_collisions`(llm.py)를 4중으로 확장.

## 4. 신규 GoldItem 스키마 (하위호환 — 기존 필드 유지, 추가만)

`src/context_loop/eval/gold_set.py::GoldItem`에 옵션 필드 추가:
```python
reference_answer: str = ""                  # 원문 근거 기준답
supporting_facts: list[SupportingFact] = []  # 답에 필요한 사실들
answerable: bool = True                     # 회수 가능한 표적인가(불가능 골드 표시)
provenance: dict = {}                        # extraction/generator/judge/embedding 모델 ID

@dataclass
class SupportingFact:
    entity: str; entity_type: str
    relation: str = ""; target: str = ""
    evidence_span: str = ""                  # 원문 verbatim 인용 (진실 앵커)
    source_doc_id: int | None = None
    acceptable_surface_forms: list[str] = [] # 정규화 변형 + 검증 동의어 (LLM 자유나열 금지)
```
기존 `relevant_doc_ids`/`relevant_graph_entities`는 유지(채점 호환). 새 생성기는 이들도
supporting_facts에서 파생해 채운다.

## 5. 역설계된 생성 파이프라인

```
[1] 원문 샘플링  (인덱스가 아니라 documents.original_content / 섹션을 직접)
      ├ metadata_store에서 source_type별 층화 + 무작위 → 대표성. seed 고정.
[2] 검증가능 사실 추출  (추출 LLM, 인덱싱 추출 LLM과 다른 모델)
      └ SupportingFact{ entity, relation, target, evidence_span(원문 인용) }
      └ evidence_span이 원문에 실제 존재하는지 substring 검증(환각 차단)
[3] 질문 + reference_answer 합성  (그 사실로만 답 가능하게)
[4] Judge 게이트  (generator와 다른 모델)
      ├ (a) 원문으로 답 가능? (b) 무관 청크/일반지식으론 불가?
      ├ (c) evidence_span이 원문에 존재? (d) 누설 차단(sanitize_* 재사용)
[5] 정답키 구성
      ├ acceptable_surface_forms = 결정론 정규화 변형 + judge 검증 동의어
      └ expected_source_doc_ids
[6] 음의 공간/answerable 판정
      ├ "완벽한 시스템도 회수 불가"한 사실 표시(answerable=False) → 분모 위생
      └ (선택) distractor / unanswerable 대조군 주입
[7] 인간 검증 (표본) → generator 정밀도 산출 → 나머지 신뢰 보정
[8] 동결 + 버전 + provenance(4모델 ID + seed) 기록
```

## 6. 채점 변경 (PR #78 위에 추가)

- **evidence-근거 사실 recall** — retrieved 노드/청크가 supporting_facts를 커버하나(tiered
  매칭). 정답이 인덱스 무관이라 **인덱스 누락이 정당히 recall을 깎음**(인덱싱 품질 측정).
- **answer correctness** — reference_answer 대비(팩토이드: 정규화 일치 / 개방형: 분리 judge + CI).
- **answerable 분모 위생** — `answerable=False`는 분모에서 제외하거나 별도 보고.
- **유지**: PR #78의 `graph_recall_surface@k`(T1–T3), 그래프 CI, 비교 가드, 매칭 증거.

## 7. 2-tier 골드 아키텍처 + "잠정→절대" 승격

```
대규모 합성(원천 근거)  ── 추세·인덱싱 A/B·절대-ish recall      [확장성]
        ↑ 검증
소규모 인간 골드(50~100)  ── 절대 앵커 + 합성 생성기 정밀도 보정    [진실 기준선]
```
**첫 run 절대값은 "잠정 기준선"으로 라벨링.** 절대 품질로 인용하려면 반드시 함께 보고:
`값 + 95% CI + 생성기 정밀도(인간검증) + answerable 비율 + provenance(4모델 분리)`.
이게 갖춰지기 전엔 상대 추적·sanity 용도로만.

## 8. 코드 변경 지점 (시작점)

- `scripts/build_synthetic_gold_set.py` — `load_candidate_subgraphs`(인덱스 샘플) 대신/병행
  **원문 샘플러**, `_make_graph_gold_item` → supporting_facts 기반 생성으로 확장.
- `src/context_loop/eval/synth.py` — `extract_verifiable_facts(doc_section, extraction_llm)`
  신설(evidence_span 포함), `filter_question`/`sanitize_*` 재사용·확장.
- `src/context_loop/eval/gold_set.py` — `SupportingFact` + GoldItem 필드, from_dict/to_dict.
- `src/context_loop/eval/llm.py` — `detect_role_collisions`를 추출LLM·임베딩까지 4중 확장.
- `scripts/eval_search.py` — evidence-근거 recall + answer correctness 채점 경로 추가
  (기존 graph surface 메트릭/CI 유지).
- `src/context_loop/storage/metadata_store.py` — 원문/섹션 조회 API 확인·활용(읽기 전용).
- 인간 검증 입출력: CSV export/import + generator 정밀도 메트릭.

## 9. 단계별 실행 계획 (증분·저위험)

| Phase | 산출물 | 회귀 위험 |
|---|---|---|
| **P0 스키마/배선** | GoldItem+SupportingFact 옵션 필드, detect_role_collisions 4중, provenance | 없음(추가만) |
| **P1 원문 샘플러+추출** | 원문 샘플러 + extract_verifiable_facts + evidence_span substring 검증 | 신규 모듈 |
| **P2 질문/기준답+Judge** | 질문·reference_answer 합성 + judge 게이트(분리 모델) + surface_forms 검증 | 신규 경로 |
| **P3 음의공간/answerable** | answerable 판정 + (선택)distractor | 신규 |
| **P4 evidence-근거 채점** | 사실 recall + answer correctness 메트릭(+CI) | eval_search 추가 |
| **P5 인간 앵커** | CSV export/import + generator 정밀도 | 신규 |
| **P6 보고/승격** | 잠정/절대 라벨 + 합산 보고 양식 | 보고 |

**마이그레이션**: 신규 모드를 **플래그 뒤에**(예: `--source-grounded`) 두고 기존 인덱스 기반
모드와 병존. 기존 상대 A/B는 깨지 않는다. 새 모드가 검증·안정화되면 기본값 전환 검토.

## 10. 검증 전략

- 단위: evidence_span substring 검증, surface_forms 누설 게이트, answerable 판정, detect_role_collisions 4중, 채점 메트릭(인덱스 누락→recall 하락 케이스).
- 통합: 소규모 원문 코퍼스로 골드 1회 생성 → 동결 → eval 실행 → 잠정 보고 양식 출력.
- 회귀: 기존 `tests/test_eval` 무영향(플래그 뒤). **주의: origin/main에 기존 실패 5건**
  (`test_fetch_source_text_*`, `test_filter_question_*`)이 있으니 신규 실패 0만 확인.
- 라이브 의존(임베딩/LLM 엔드포인트)은 mock 또는 실제 endpoint 필요.

## 11. 새 세션이 먼저 정할 결정 (착수 전 사용자 확인 권장)

1. **모델 분리**: 추출/generator/judge에 어떤 모델? (서로·시스템과 달라야 함)
2. **인간 검증 규모**: 몇 항목까지 인간 라벨 가능한가? (절대 앵커 신뢰도 결정)
3. **소스 범위**: confluence_mcp + git_code 둘 다 동시인가, 한 source_type부터인가?
4. **사실 매칭**: 기존 graph_match tier 재사용 vs 사실 전용 매처 신설?
5. **답변 채점**: 팩토이드 정규화 일치 vs 개방형 judge — 어디까지 개방형 허용?
6. **불가능 골드 정책**: answerable=False를 분모 제외 vs 별도 "인덱싱 표적" 버킷?

## 12. 참조

- 감사: `_workspace/findings/SUMMARY.md`(R3 등 12위험), `01_gold_set_audit.md`(C1 self-fitting), `03_cross_bias_analysis.md`(Channel A/C).
- A/B 프로토콜(상대): `_workspace/findings/04_graph_ab_protocol.md`.
- PR #78: 채점·통계 토대(surface tier, 그래프 CI, 비교 가드, detect_role_collisions, sanitize_*).
- 핵심 코드: `build_synthetic_gold_set.py`(load_candidate_subgraphs/_make_graph_gold_item), `synth.py`(generate_graph_questions/filter_question/sanitize_*), `gold_set.py`(GoldItem), `graph_match.py`(tiered matching), `eval_search.py`(채점), `llm.py`(detect_role_collisions).

---

**한 줄 요약**: 골드 생성을 *인덱스→골드*에서 *원문→골드(evidence_span 고정)* 로 뒤집고,
모델을 4중 분리하며, 음의 공간 + 소규모 인간 앵커를 더해 절대 품질·인덱싱 개선을 측정한다.
PR #78 채점·통계 토대 위에 신규 모드를 플래그로 얹어 단계적으로(P0–P6) 구현한다.
