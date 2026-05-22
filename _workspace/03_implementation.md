# 03_implementation — 골드셋 cross-doc / answer-equivalence 구현

작성: 2026-05-22 (implementer, eval-gold-set-improvement)
입력: `02_design.md` (작업 지시서), `01_analysis.md`, `00_requirements.md`
대상 HEAD: PR#64 병합 후 (브랜치 `claude/gold-set-cross-doc-equivalence`)

> 02_design.md 의 변경 파일/시그니처를 그대로 따랐다. 사용자 확정 결정 3건
> (기존 graph 골드셋 재생성 방식 / R2 하이브리드 / YAML 기준) 모두 반영.

---

## 1. 변경 파일 목록

| 파일 | 변경 | 핵심 |
|---|---|---|
| `src/context_loop/eval/gold_set.py` | 수정 | `GoldItem` 에 `relevant_doc_groups: list[list[int]]` + `cross_document: bool` 2필드. `to_dict` omit-if-empty emit, `from_dict` 기본값 폴백 + 그룹 내 중복제거/빈그룹 드롭. 모듈/`GoldItem` docstring 갱신 |
| `src/context_loop/eval/metrics.py` | **무변경** | (b)안 — 전처리 축약으로 재사용 (test_metrics 23개 무영향, 실측 34개 green) |
| `scripts/eval_search.py` | 수정 | `_reduce_equivalence` 신규(§3.2), `evaluate_one` 채점 진입 분기(§3.3), `_classify_mode` cross_doc 우선 분기(§6.1), `write_summary` `rows_by_mode` 에 `cross_doc` 슬롯(§6.2), CSV 평탄 `relevant_doc_ids` 유지 + `n_answer_units` 추가 |
| `src/context_loop/eval/synth.py` | 수정 | `CROSS_DOC_GENERATE_PROMPT_TEMPLATE` 신규, `generate_cross_doc_questions` 신규 (generator/judge 분리 유지, 반환 타입 `GeneratedGraphQuestion` 재사용) |
| `scripts/build_synthetic_gold_set.py` | 수정 | `_make_graph_gold_item` OR 그룹 자동변환(§4), `load_cross_doc_seeds` 신규(§5.2), `_make_cross_doc_gold_item`/`_cross_doc_seed_snippet`/`_process_cross_doc_item`/`_run_cross_doc_mode` 신규(§5), `build()` 파라미터+호출+metadata+stats, CLI `--enable-cross-doc`/`--cross-doc-max-seeds`, docstring 사용법 예시 |
| `tests/test_eval/test_gold_set.py` | 수정 | 동치 그룹/플래그 round-trip·폴백·dedup·omit 4건 |
| `tests/test_eval/test_equivalence_scoring.py` | **신규** | 동치 채점 9건 (recall/precision cap/AND/mrr/ndcg/보존/miss/폴백가드/결정론) |
| `tests/test_eval/test_build_synthetic_gold_set.py` | 수정 | graph OR 변환 2건 + cross-doc 씨앗 3건 + emit 1건 + classify cross_doc 1건 + CLI 옵션 2건 |
| `tests/test_eval/test_synth.py` | 수정 | cross-doc 프롬프트 1건 |

**diagnose_r3_effect.py / compare_runs.py / metrics.py**: 무변경 (CSV 평탄 컬럼 보존으로 호환).

---

## 2. 사용자 확정 결정 반영

1. **기존 graph 골드셋 = 재생성 방식.** 로더(`from_dict`)는 평탄 list 를 OR 로 자동
   승격하지 **않는다**. `relevant_doc_groups` 누락 시 `[]` → R3 비활성 → 기존 평탄
   채점 폴백만 한다. OR 그룹화는 오직 생성기(`_make_graph_gold_item`)에서만 수행 →
   기존 평탄 골드셋은 명시적 재생성으로만 갱신.
2. **R2 = 하이브리드.** `load_cross_doc_seeds` 가 "노드 소유 문서 서로소" 엣지를
   결정론적으로 추출(정답 씨앗). LLM 은 `generate_cross_doc_questions` 로 자연어 질의
   문장화만 담당. 기존 `filter_question`(judge 3-gate) 그대로 재사용.
3. **YAML 기준.** `save_gold_set`/`load_gold_set` 가 `yaml.safe_dump`/`safe_load` — 변경 없음.

---

## 3. 추가한 테스트 시나리오

### test_gold_set.py
- `test_roundtrip_equivalence_groups` — `[[3,5],[9]]` + `cross_document=True` 무손실.
- `test_legacy_yaml_no_groups_loads` — 옛 YAML → `groups=[]`, `cross_document=False`.
- `test_groups_dedup_and_drop_empty` — `[[3,3,5],[]]` → `[[3,5]]`.
- `test_groups_omitted_when_empty_on_emit` — 빈 필드 to_dict omit.

### test_equivalence_scoring.py (신규)
- `test_reduce_or_group_recall` — `[[3,5]]`, retrieved `[5]` → recall=1.0 (평탄이면 0.5).
- `test_reduce_precision_cap` — `[[3,5]]`, retrieved `[3,5]` → precision@2=0.5 (hits 캡).
- `test_reduce_and_groups` — `[[3],[9]]`, retrieved `[3]` → recall=0.5; 둘 다 → 1.0.
- `test_reduce_mrr_first_member` — `[[3,5]]`, retrieved `[7,5]` → mrr=0.5.
- `test_reduce_ndcg_idcg_denom` — `[[3,5],[9]]` 분모 2단위 → ndcg=1.0.
- `test_reduce_preserves_non_answer_docs` — 정답 외 doc 보존, 그룹 중복 drop.
- `test_reduce_miss_group_counts_in_denominator` — 미검색 그룹도 recall 분모에.
- `test_no_groups_fallback_flat` — 빈 그룹 시 relevant 빈 set (분기 가드 고정).
- `test_reduce_is_deterministic` — 순수 함수 결정성.

### test_build_synthetic_gold_set.py
- `test_graph_multi_doc_becomes_or_group` — `[7,3,5]` → `groups=[[3,5,7]]`.
- `test_graph_single_doc_no_group` — `[42]` → `groups=[]`.
- `test_load_cross_doc_seeds_disjoint` — 엔티티 병합으로 소유문서 겹치면 씨앗 0건.
- `test_load_cross_doc_seeds_true_disjoint` — 완전 서로소 엣지 1건만 씨앗.
- `test_cross_doc_seed_deterministic` — 같은 입력 → 동일 씨앗 리스트.
- `test_make_cross_doc_item_and_groups` — 씨앗 → `groups=[[A],[B]]`, `cross_document=True`.
- `test_classify_mode_cross_doc_priority` — `cross_document=True` → mode="cross_doc".
- CLI: `--enable-cross-doc`, `--cross-doc-max-seeds` 노출 확인.

### test_synth.py
- `test_generate_cross_doc_questions_prompt` — 두 엔티티 + "두 문서" 취지 + purpose 라벨.

---

## 4. 검증 결과

```
pytest tests/test_eval/            → 291 passed, 5 failed (전부 선재 실패 — 아래 §5)
pytest tests/test_eval/test_metrics.py → 34 passed (metrics.py 무변경 확인)
ruff check (변경 파일 8개)         → All checks passed
  (예외: scripts/build_synthetic_gold_set.py:1501 E501 — 선재 위반, 내 변경 아님)
python scripts/build_synthetic_gold_set.py --help → --enable-cross-doc / --cross-doc-max-seeds 노출
import 검증: eval_search, build_synthetic_gold_set, gold_set, synth 모두 OK
```

신규 테스트 추가로 통과 수가 270 → 291 (+21).

---

## 5. 설계와 어긋난 부분 / 주의

### 5.1 선재 실패 5건 (내 변경과 무관 — 손대지 않음)
대상 브랜치(PR#64 병합) HEAD 에서 이미 실패하던 테스트. `git stash` 로 내 변경을
제거한 상태에서도 동일하게 실패함을 확인했다. 설계 원칙(기존 테스트 보존)상 이들은
본 PR 범위 밖이라 수정하지 않았다:

- `test_fetch_source_text_anchor_match`, `test_fetch_source_text_legacy_chunk_id_fallback`
  — `_fetch_source_text` 가 `tuple[str, str]` 를 반환하도록 바뀌었는데 테스트는 옛
  `str` 반환을 가정. (테스트가 stale)
- `test_make_graph_gold_item_falls_back_to_node_description` — `_make_graph_gold_item`
  이 노드 description 폴백을 제거(감사 H6: T4 trivial 1.0 방지)했는데 테스트는 옛
  폴백 동작을 가정. (테스트가 stale)
- `test_filter_question_passes_clean`, `test_filter_question_fails_generic` — `filter_question`
  의 게이트 호출 순서/StubLLM 응답 큐 mismatch. (테스트가 stale)

→ 별도 정리 권장(out of scope). 본 작업 산출물에는 영향 없음.

### 5.2 설계 §3.2 와의 미세 차이 (의도된 보강)
`_reduce_equivalence` 는 설계 의사코드를 그대로 따랐다. 추가로 `test_no_groups_fallback_flat`
에서 빈 그룹 입력 시 동작(빈 relevant set)을 고정해, 빈 그룹은 `evaluate_one` 의 분기
(`if item.relevant_doc_groups:`)에서 호출되지 않음을 명시했다 — 설계의 "R3 비활성 폴백"
보장을 테스트로 못박은 것.

### 5.3 cross-doc distractor 풀 (설계 §5.4 보강)
`_run_cross_doc_mode` 의 일반성 게이트 distractor 는 다른 씨앗들의 snippet 에서 자기
자신을 제외하고 최대 5개를 사용(graph 모드의 `skip_generic_gate < 5` 정책과 동일).
설계가 distractor 출처를 명시하지 않아 graph 모드 패턴을 복제했다.

---

## 6. 새 스키마 / CLI 사용 예시

### 6.1 YAML 스키마 (R3 동치 집합 + R2 cross-doc)

```yaml
version: 1
items:
  # graph 다중-doc → 단일 OR 그룹 (3 또는 5 중 하나면 정답)
  - id: q0007
    query: "인증 게이트웨이는 어느 팀이 운영하나요?"
    relevant_doc_ids: [3, 5]          # 평탄(하위호환/CSV)
    relevant_doc_groups: [[3, 5]]     # OR 그룹 1개
    relevant_graph_entities:
      - {name: "인증 서비스", type: "system"}
    synthesized: true
  # cross-document → AND 다중그룹 (3 과 7 둘 다 봐야 답)
  - id: q0012
    query: "결제 서비스가 의존하는 인증 모듈은 어느 팀이 관리하나요?"
    relevant_doc_ids: [3, 7]
    relevant_doc_groups: [[3], [7]]   # (3) AND (7)
    cross_document: true              # R2 식별 플래그
    relevant_graph_entities:
      - {name: "결제 서비스", type: "system"}
      - {name: "인증 서비스", type: "system"}
    notes: "cross_document"
    synthesized: true
```

옛 골드셋(두 신규 키 없음)은 `relevant_doc_groups=[]` / `cross_document=False` 로
로드되어 기존 평탄 채점과 bit-identical 동작.

### 6.2 CLI

```bash
# cross-document 케이스 포함 골드셋 생성 (R2)
python scripts/build_synthetic_gold_set.py \
    --enable-cross-doc --source-types confluence_mcp,git_code \
    --cross-doc-max-seeds 50 \
    --output eval/gold_set.yaml

# graph 다중-doc OR 그룹은 --include-graph-questions 만으로 자동 생성 (R3)
python scripts/build_synthetic_gold_set.py \
    --include-graph-questions --n-graph-nodes 20 \
    --output eval/gold_set.yaml
```

평가(`eval_search.py`)는 골드셋에 `relevant_doc_groups`/`cross_document` 가 있으면
자동으로 동치-aware 채점 + `metrics_by_mode["cross_doc"]` 분리 집계를 수행한다
(추가 CLI 플래그 불필요).

### 6.3 채점 의미 (metrics.py 무변경, 전처리 축약으로 달성)

| 케이스 | groups | retrieved | recall | 비고 |
|---|---|---|---|---|
| OR 동치 | `[[3,5]]` | `[5]` | 1.0 | 그룹 1단위, 하나면 hit |
| OR precision cap | `[[3,5]]` | `[3,5]` top2 | precision=0.5 | 중복 정답 1로 캡 |
| cross-doc AND | `[[3],[7]]` | `[3]` | 0.5 | 2단위 중 1개 |
| cross-doc AND | `[[3],[7]]` | `[3,7]` | 1.0 | 둘 다 |
