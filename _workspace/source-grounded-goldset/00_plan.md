# 통합 원천(source-grounded) 골드셋 생성 — 작업 계획서 (핸드오프)

> 새 세션이 이어받아 실행하기 위한 자립형 계획서. 브랜치
> `claude/source-grounded-goldset` (origin/main = PR #78 머지 포함 기준).
> 선행 맥락: `_workspace/findings/SUMMARY.md`(감사, 특히 R3)와 PR #78 변경분.
>
> **범위(갱신됨)**: 그래프 전용이 아니라 **하나의 원문 기반 골드가
> 청크/문서 검색 · 답변 품질 · 그래프 회수 세 측정 단위를 모두 서빙**한다.

---

## 1. Context — 왜 이 작업을 하나

PR #78 감사는 그래프 골드가 **self-fitting(R3)** 임을 확정했다: 골드 정답이 평가 대상
인덱스 노드에서 직접 유래(`build_synthetic_gold_set.py::load_candidate_subgraphs`가
graph_store 노드를 읽음 → `_make_graph_gold_item`이 `sg["entity_name"]` 복붙)하고, 평가
시 retrieved도 같은 인덱스라 T1 exact가 자명 통과한다 → **"검색 품질이 아니라 골드셋
생성 방식을 측정"**.

같은 결함의 약한 버전이 **청크 골드에도** 있다: 인덱싱된 청크/문서에서 질문을 역생성하면
**인덱스가 놓친 청크는 애초에 골드가 안 만들어져** 청크 절대 recall도 인덱싱 누락을 못
잡는다(음의 공간 부재). 따라서 그래프뿐 아니라 **청크·답변 모두 "절대 품질·인덱싱 개선"
측정이 막혀 있다.** 현재 메트릭은 "동일 인덱스 위 검색/플래너 변경의 방향성"(상대 A/B)만
신뢰 가능.

**목표**: 골드 생성의 *방향을 인덱스→골드에서 원문→골드로 뒤집어*, 정답을 **원문이 말하는
사실(verbatim 인용에 고정)** 에서 독립 추출한다. 한 번의 추출 산출물로 **청크/문서 · 답변 ·
그래프** 세 단위를 동시에 채점하여, 인덱스가 놓친 사실을 벌할 수 있게 한다(절대 측정).

**대체하지 않는 것**: PR #78의 채점·통계·감사 토대(surface tier 메트릭, 그래프 CI, 비교
가드, `detect_role_collisions`, `sanitize_*`)는 그대로 **딛고 선다**. 본 작업은 그 위에
올리는 **생성 레이어 교체 + 3단위 채점 경로 추가**다.

## 2. 핵심 발상 — 하나의 원천 사실이 3단위를 서빙

```
현재:  인덱스(노드/청크) → 골드            → self-fitting / 음의공간 부재
신규:  원문 문서 → 검증가능 사실(evidence_span 인용) → 통합 골드 → 인덱스/검색을 시험
```

**예시** — 원문(문서 #12): *"Auth Service가 결제 인증을 처리하며 Token Validator에 의존한다."*

원문 사실 1개 → 골드 1개 →

| 측정 단위 | 채점 기준 |
|---|---|
| **청크/문서 검색** | 검색이 evidence_span 출처 문서(#12)/청크를 회수했나? (context recall) |
| **답변 품질** | 생성 답변이 `reference_answer`와 일치/충실한가? (correctness/faithfulness) |
| **그래프** | 탐색이 `Auth Service --depends_on--> Token Validator`를 회수했나? |

세 단위 모두 정답이 **원문 인용에 고정**되어 인덱스 독립 → 인덱스 누락이 정당하게 점수를 깎는다.

3대 장치:
- **evidence_span(원문 인용)으로 진실 고정** — "LLM이 그렇다더라"가 아니라 "원문에 이렇게 쓰임". 채점도 이 인용 기준.
- **정답 표기형을 LLM 자유나열에서 분리** — 결정론 정규화 변형 + 검증 동의어만(누설/trivial 통과 차단; PR #78 `sanitize_*` 재사용).
- **모델 4중 분리** — 추출LLM ≠ 인덱싱LLM ≠ 시스템(planner/HyDE)LLM ≠ judgeLLM, 임베딩도 분리. `detect_role_collisions`(llm.py)를 4중 확장.

## 3. 제1원리 — 절대 품질용 골드의 필수 조건 (3단위 공통)

| 조건 | 의미 |
|---|---|
| 독립성 | 정답이 평가 대상(인덱스/추출기/검색기/임베딩)에서 유래 금지 |
| 원천 근거 | 정답이 "원문이 실제로 말하는 사실"(verbatim 인용)에 닿아야 함 |
| 음의 공간 | "인덱스가 놓친 사실/청크/노드"를 벌할 수 있어야 함 |
| 재현성 | 1회 생성 후 동결·버전·provenance 기록 |
| 인간 앵커 | 진짜 "절대"는 소규모 인간 검증 기준선 필요 |

## 4. 통합 GoldItem 스키마 (하위호환 — 기존 필드 유지, 추가만)

`src/context_loop/eval/gold_set.py::GoldItem`에 옵션 필드 추가:
```python
reference_answer: str = ""                   # [답변] 원문 근거 기준답
supporting_facts: list[SupportingFact] = []  # [그래프/청크] 답에 필요한 사실들
answerable: bool = True                      # 회수 가능한 표적인가(불가능 골드 표시)
measurement_units: list[str] = []            # {"doc","answer","graph"} 중 이 항목이 서빙하는 단위
provenance: dict = {}                         # extraction/generator/judge/embedding 모델 ID + seed

@dataclass
class SupportingFact:
    entity: str; entity_type: str
    relation: str = ""; target: str = ""
    evidence_span: str = ""                  # 원문 verbatim 인용 (진실 앵커)
    source_doc_id: int | None = None
    acceptable_surface_forms: list[str] = [] # 정규화 변형 + 검증 동의어 (LLM 자유나열 금지)
```
- **청크/문서 단위**: 기존 `relevant_doc_ids`/`relevant_doc_groups`를 supporting_facts의
  `source_doc_id`(+ evidence_span 소속 문서)에서 파생해 채운다 → 기존 청크 채점과 호환.
- **답변 단위**: `reference_answer`.
- **그래프 단위**: 기존 `relevant_graph_entities`/`relevant_graph_relations`를
  supporting_facts에서 파생.
한 GoldItem이 `measurement_units`에 따라 1~3개 단위를 동시에 서빙.

## 5. 역설계된 생성 파이프라인 (통합)

```
[1] 원문 샘플링  (인덱스가 아니라 documents.original_content / 섹션을 직접)
      ├ metadata_store에서 source_type별 층화 + 무작위 → 대표성. seed 고정.
[2] 검증가능 사실 추출  (추출 LLM, 인덱싱 추출 LLM과 다른 모델)
      └ SupportingFact{ entity, relation, target, evidence_span(원문 인용), source_doc_id }
      └ evidence_span이 원문에 실제 존재하는지 substring 검증(환각 차단)
[3] 질문 + reference_answer 합성  (그 사실로만 답 가능하게)
      └ 한 질문이 doc/answer/graph 중 어떤 단위를 평가하는지 measurement_units 태깅
[4] Judge 게이트  (generator와 다른 모델)
      ├ (a) 원문으로 답 가능? (b) 무관 청크/일반지식으론 불가?
      ├ (c) evidence_span이 원문에 존재? (d) 누설 차단(sanitize_* 재사용)
[5] 정답키 구성
      ├ doc: source_doc_ids   ├ answer: reference_answer
      ├ graph: supporting_facts → entity/relation + acceptable_surface_forms(정규화변형+검증동의어)
[6] 음의 공간/answerable 판정 (3단위 공통)
      ├ "완벽한 시스템도 회수 불가"한 사실 표시(answerable=False) → 분모 위생
      └ (선택) distractor / unanswerable 대조군 주입
[7] 인간 검증 (표본) → generator 정밀도 산출 → 나머지 신뢰 보정
[8] 동결 + 버전 + provenance(4모델 ID + seed) 기록
```

## 6. 채점 변경 — 3단위 일급 (PR #78 위에 추가)

세 단위 모두 **원문 근거라 인덱스 독립**이고, PR #78의 **bootstrap CI**를 그대로 적용한다.

- **[문서] context recall@k** — retrieved 청크/문서가 evidence_span 출처 문서를 커버하나.
  인덱스가 그 청크를 놓쳤으면 recall 하락(인덱싱 품질 측정). 기존 chunk recall/precision 재사용.
- **[답변] answer correctness / faithfulness** — `reference_answer` 대비(팩토이드: 정규화
  일치 / 개방형: 분리 judge + CI). RAGAS 보완 레이어와 정렬 가능(reference 기반).
- **[그래프] 사실 recall** — supporting_facts를 retrieved 그래프 노드에 tiered 매칭(PR #78
  `graph_recall_surface@k` 등). 인덱스에 노드 없으면 true miss.
- **answerable 분모 위생** — `answerable=False`는 분모 제외 또는 별도 보고.
- **유지**: PR #78 surface tier 분리, 그래프 CI, 비교 가드, 매칭 증거.

## 7. 2-tier 골드 + "잠정→절대" 승격 (3단위 공통)

```
대규모 합성(원천 근거)  ── 추세·인덱싱 A/B·절대-ish 측정       [확장성]
        ↑ 검증
소규모 인간 골드(50~100)  ── 절대 앵커 + 합성 생성기 정밀도 보정   [진실 기준선]
```
**첫 run 절대값은 "잠정 기준선" 라벨링.** 절대 인용 시 반드시 함께 보고:
`값 + 95% CI + 생성기 정밀도(인간검증) + answerable 비율 + provenance(4모델 분리)`.
갖춰지기 전엔 상대 추적·sanity 용도로만. (단위별로 각각 산출.)

## 8. 코드 변경 지점 (시작점)

- `scripts/build_synthetic_gold_set.py` — `load_candidate_subgraphs`(인덱스 샘플) 대신/병행
  **원문 샘플러**; `_run_chunk_mode`/`_make_graph_gold_item`/`_make_cross_doc_gold_item`을
  통합 원천 생성 경로로 흡수(또는 신규 `_run_source_grounded_mode`).
- `src/context_loop/eval/synth.py` — `extract_verifiable_facts(doc_section, extraction_llm)`
  신설(evidence_span 포함), 질문/reference_answer 합성, `filter_question`/`sanitize_*` 재사용·확장.
- `src/context_loop/eval/gold_set.py` — `SupportingFact` + GoldItem 필드 + measurement_units, from_dict/to_dict.
- `src/context_loop/eval/llm.py` — `detect_role_collisions`를 추출LLM·임베딩까지 4중 확장.
- `scripts/eval_search.py` — 단위별 채점: context recall(문서) / answer correctness(답변) /
  evidence-근거 사실 recall(그래프). 기존 chunk·graph 메트릭/CI 재사용·확장.
- `src/context_loop/storage/metadata_store.py` — 원문/섹션 조회 API 확인·활용(읽기 전용).
- 인간 검증 입출력: CSV export/import + generator 정밀도 메트릭.

## 9. 단계별 실행 계획 (증분·저위험)

| Phase | 산출물 | 비고 |
|---|---|---|
| **P0 스키마/배선** | GoldItem+SupportingFact+measurement_units, detect_role_collisions 4중, provenance. **현 chunk 골드의 음의공간/독립성 점검 메모.** | 추가만, 회귀 없음 |
| **P1 원문 샘플러+추출** | 원문 샘플러 + extract_verifiable_facts + evidence_span substring 검증 | 신규 모듈 |
| **P2 질문/기준답+Judge** | 질문·reference_answer 합성 + measurement_units 태깅 + judge 게이트(분리 모델) + surface_forms 검증 | 신규 경로 |
| **P3 음의공간/answerable** | answerable 판정 + (선택)distractor (3단위 공통) | 신규 |
| **P4 3단위 채점** | context recall(문서) + answer correctness(답변) + 사실 recall(그래프), 각 +CI | eval_search 추가 |
| **P5 인간 앵커** | CSV export/import + generator 정밀도(단위별) | 신규 |
| **P6 보고/승격** | 단위별 잠정/절대 라벨 + 합산 보고 양식 | 보고 |

**마이그레이션**: 신규 모드를 **플래그 뒤에**(예: `--source-grounded`) 두고 기존 인덱스 기반
chunk/graph 모드와 병존. 기존 상대 A/B는 깨지 않는다. 검증·안정화 후 기본값 전환 검토.

## 10. 검증 전략

- 단위: evidence_span substring 검증, surface_forms 누설 게이트, answerable 판정,
  detect_role_collisions 4중, 3단위 채점(인덱스 누락→recall 하락 케이스 각각).
- 통합: 소규모 원문 코퍼스로 통합 골드 1회 생성 → 동결 → eval → 단위별 잠정 보고 출력.
- 회귀: 기존 `tests/test_eval` 무영향(플래그 뒤). **주의: origin/main에 기존 실패 5건**
  (`test_fetch_source_text_*`, `test_filter_question_*`)이 있으니 신규 실패 0만 확인.
- 라이브 의존(임베딩/LLM 엔드포인트)은 mock 또는 실제 endpoint 필요.

## 11. 새 세션이 먼저 정할 결정 (착수 전 사용자 확인 권장)

1. **단위 범위/우선순위**: 3단위(doc/answer/graph) 동시 vs 하나(예: doc)부터 증분? (통합
   스키마는 P0에서 모두 깔되, P4 채점은 우선순위대로 단계 도입 가능)
2. **모델 분리**: 추출/generator/judge에 어떤 모델? (서로·시스템과 달라야 함)
3. **인간 검증 규모**: 몇 항목까지 인간 라벨 가능한가? (절대 앵커 신뢰도 결정)
4. **소스 범위**: confluence_mcp + git_code 둘 다 동시인가, 한 source_type부터인가?
5. **사실/문서 매칭**: 기존 graph_match tier·chunk recall 재사용 vs 사실 전용 매처?
6. **답변 채점**: 팩토이드 정규화 일치 vs 개방형 judge — 어디까지 개방형 허용? (RAGAS 연계 여부)
7. **불가능 골드 정책**: answerable=False를 분모 제외 vs 별도 "인덱싱 표적" 버킷?

## 12. 참조

- 감사: `_workspace/findings/SUMMARY.md`(R3 등 12위험), `01_gold_set_audit.md`(C1 self-fitting), `03_cross_bias_analysis.md`(Channel A/C).
- A/B 프로토콜(상대): `_workspace/findings/04_graph_ab_protocol.md`.
- PR #78: 채점·통계 토대(surface tier, 그래프 CI, 비교 가드, detect_role_collisions, sanitize_*).
- 핵심 코드: `build_synthetic_gold_set.py`(load_candidate_subgraphs/_run_chunk_mode/_make_graph_gold_item), `synth.py`(generate_graph_questions/filter_question/sanitize_*), `gold_set.py`(GoldItem), `graph_match.py`(tiered matching), `eval_search.py`(채점), `llm.py`(detect_role_collisions).

---

**한 줄 요약**: 골드 생성을 *인덱스→골드*에서 *원문→골드(evidence_span 고정)* 로 뒤집어,
**하나의 원천 사실이 청크/문서·답변·그래프 세 단위를 동시에** 절대 측정하게 한다. 모델
4중 분리 + 음의 공간 + 소규모 인간 앵커를 더하고, PR #78 채점·통계 토대 위에 신규 모드를
플래그로 얹어 P0–P6로 증분 구현한다.
