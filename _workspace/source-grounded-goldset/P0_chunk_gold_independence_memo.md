# P0 메모 — 현 chunk 골드의 음의공간/독립성 점검

> PR #79 P0 산출물. 계획서 `00_plan.md` §9 P0 항목 "현 chunk 골드의
> 음의공간/독립성 점검 메모". 본 메모는 코드 변경이 아니라 **현 상태 진단**이며,
> P1+ (원문 샘플러·추출) 설계의 근거가 된다.

## 1. 결론 (요약)

현 chunk 골드 생성은 그래프 골드의 self-fitting(R3)보다 **약한** 버전의 같은
결함을 가진다. 정답키가 평가 대상 인덱스(=`documents` 테이블에 적재된 본문)에서
유래하므로, **인덱스가 놓친 문서·청크는 애초에 골드가 만들어지지 않는다(음의 공간
부재)**. 그 결과 chunk 절대 recall 은 "인덱싱 누락"을 벌하지 못하고, 현재로선
**동일 인덱스 위 검색/플래너 변경의 방향성(상대 A/B)** 만 신뢰 가능하다.

## 2. 근거 — 생성 경로 추적

- **후보 모집**: `scripts/build_synthetic_gold_set.py` 의 chunk 후보는
  `store.list_documents()` 가 돌려주는 문서들에서 `original_content` 길이로만
  필터된다(`load_candidate_documents` 류, build script L183 부근). 즉 후보 모집단 =
  **이미 메타스토어에 적재된 문서 집합**.
- **샘플링**: `stratified_sample(...)` (L511) 로 source_type 층화 후 추출 →
  `sampled`. 모집단이 인덱스이므로 샘플도 인덱스의 부분집합.
- **질문 역생성**: `_run_chunk_mode` → `_process_chunk_item` 이 샘플 문서의
  `original_content` 를 generator 에 통째로 넘겨 질문을 만든다. 정답키
  `relevant_doc_ids` = **그 질문이 유래한 문서 id**.
- **채점**: `eval_search.py` 가 "검색이 그 문서를 회수했나"로 채점. 질문과 정답이
  같은 인덱스 항목에서 나왔으므로, 인덱스에 없는 문서를 표적으로 하는 질문은
  **존재할 수 없다**.

(그래프 골드는 한 단계 더 직접적이다: `load_candidate_subgraphs` 가 graph_store
노드를 읽고 `_make_graph_gold_item` 이 `sg["entity_name"]` 을 정답으로 복붙 →
평가 시 retrieved 도 같은 인덱스라 T1 exact 자명 통과 = R3.)

## 3. 제1원리 대비 — 어떤 조건이 깨졌나

| 조건 (계획서 §3) | chunk 골드 현 상태 |
|---|---|
| 독립성 (정답이 평가 대상에서 유래 금지) | ✗ — 정답 문서 = 인덱스 적재 문서 |
| 원천 근거 (verbatim 인용에 고정) | △ — generator 가 본문을 읽지만 `evidence_span` 고정 없음 |
| 음의 공간 (인덱스 누락을 벌함) | ✗ — 누락 문서는 골드 자체가 없음 |
| 재현성 (동결·버전·provenance) | ○ — seed/메타데이터 기록은 있음 |
| 인간 앵커 | ✗ — 합성 전용 |

## 4. 그래프 골드와의 차이 (왜 "약한" 버전인가)

- 그래프: 정답 **문자열**(entity_name)이 인덱스 노드에서 복붙 → exact 매칭이
  **자명 통과**. self-fitting 이 정답 *표기형*까지 오염.
- chunk: 정답이 **문서 id**(표기형이 아님)라 매칭 자체는 자명하지 않다. 그러나
  **표적 모집단이 인덱스로 제한**되는 음의 공간 부재는 동일. → 절대 recall 의
  분모가 "인덱스가 아는 것"으로 닫혀 있어 인덱싱 개선을 측정 못 함.

## 5. P1+ 가 고쳐야 할 지점 (이 메모의 함의)

1. **샘플 모집단을 원문으로**: 인덱스(`documents`)가 아니라 원천 코퍼스/섹션을
   직접 샘플 → 인덱스가 놓친 문서도 표적이 될 수 있게 (음의 공간 확보).
2. **`evidence_span` 으로 정답 고정**: 정답을 "LLM 이 그렇다더라"가 아니라
   "원문에 이렇게 쓰임"(verbatim 인용)에 묶고 substring 검증으로 환각 차단.
   → P0 에서 추가한 `SupportingFact.evidence_span` 가 이 앵커.
3. **`answerable=False` 표적**: 완벽한 시스템도 회수 불가한 사실을 명시해
   분모 위생 + 인덱싱 표적 버킷 분리. → P0 의 `GoldItem.answerable`.
4. **모델 4중 분리 강제**: 추출 ≠ 인덱싱(system) ≠ generator ≠ judge + 임베딩.
   → P0 의 `eval.extraction.*` / `eval.embedding.*` + `detect_role_collisions`
   의 `any_collision_full`.

## 6. P0 가 깐 토대 (이 PR 범위)

- `gold_set.py`: `SupportingFact`(evidence_span, source_doc_id,
  acceptable_surface_forms) + `GoldItem.{reference_answer, supporting_facts,
  answerable, measurement_units, provenance}` — 모두 추가만, 기존 채점 무영향.
- `llm.py`: `detect_role_collisions` 4중 확장(extraction 역할 + 임베딩 충돌)
  + `EvalRole` 에 `extraction` 추가. 기존 3-way 키/`any_collision` 의미는 보존,
  종합 판정은 신규 `any_collision_full`.
- `config/default.yaml`: `eval.extraction.*` / `eval.embedding.*` 블록.

P1 (원문 샘플러 + `extract_verifiable_facts`) 부터는 라이브 LLM 엔드포인트(추출
모델 분리)가 필요하므로 별도 세션/단계에서 진행한다.
