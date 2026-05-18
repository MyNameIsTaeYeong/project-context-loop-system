# 03 — 그래프 인덱싱 강건성 구현 (2차)

**작성일**: 2026-05-18
**역할**: eval-system-implementer
**선행 산출물**:
- `_workspace/00_requirements.md` (R1/R2/R3 + 비기능)
- `_workspace/01_analysis.md` (시나리오 A~F 깨짐 패턴, 가용 인프라)
- `_workspace/02_design.md` (D1~D8, 설계 결정 11개, §0~§10)

## 1. 변경 파일 목록

### 신규
| 파일 | 한 줄 설명 |
|------|------------|
| `src/context_loop/eval/graph_match.py` | 4-tier cascade 매칭 알고리즘 (`match_entity_tiered`, `run_entity_matching`, `match_relation_tiered`, `run_relation_matching`), 임베딩 LRU 캐시 래퍼 (`build_embed_fn`), 텍스트 배치 임베딩 헬퍼 (`aembed_with_client`), 코사인 유사도, 누적 tier 카운트 집계. |
| `tests/test_eval/test_graph_matching.py` | 시나리오 A~F 회귀 테스트, T1~T4 hit/miss 단위 테스트, strict 모드, threshold override, 임베딩 캐시·async 클라이언트 처리 — **41개 테스트**. |

### 수정
| 파일 | 한 줄 설명 |
|------|------------|
| `src/context_loop/eval/gold_set.py` | `GraphEntityRef` 에 `aliases` / `description` / `description_embedding` 추가. `GraphRelationRef` 신규 데이터클래스. `GoldItem.relevant_graph_relations` 옵셔널 필드 추가. `to_dict` 가 빈 값은 omit, `from_dict` 가 누락 시 기본값으로 graceful. |
| `src/context_loop/eval/synth.py` | `GeneratedGraphQuestion` / `GeneratedGraphRelation` 신규 데이터클래스. `GRAPH_GENERATE_PROMPT_TEMPLATE` 에 evidence_description / entity_aliases / relation 슬롯 추가. `parse_generated_graph_questions` 신규 — 누락 필드 graceful. `generate_graph_questions` 반환 타입 확장. |
| `src/context_loop/processor/graph_search_planner.py` | `GraphSearchResult.entities` 의 각 `GraphEntityRef` 에 노드 properties 의 `description` 채움 (T4 임베딩 매칭용). `GraphSearchResult.relations: list[GraphRelationRef]` 신규 — 관계 채점용 노출. |
| `src/context_loop/mcp/context_assembler.py` | `AssembledContext.retrieved_graph_relations` 패스스루 추가. |
| `scripts/build_synthetic_gold_set.py` | `_make_graph_gold_item` 시그니처 — `GeneratedGraphQuestion` 인자 + `score_relations` 옵션. `_embed_graph_item_descriptions` 신규 — 골드셋의 모든 description 을 1회 배치 임베딩. CLI 옵션 `--embed-graph-evidence` / `--score-relations` / `--graph-match-threshold`. `build()` 메타데이터에 `embedding_model` + `graph_match_threshold_default` 기록 (재현성). |
| `scripts/eval_search.py` | `evaluate_one` 에서 `run_entity_matching` 호출 — 4-tier cascade 채점. 새 보고 시그널 (`graph_match_tiers`, `graph_match_score_avg/min/max`, `graph_ndcg@k`). `--score-relations` 시 `graph_rel_recall@k`, `graph_rel_precision@k`, `graph_rel_hit@k`, `graph_rel_mrr`, `graph_rel_match_tiers/score_*`. `write_summary` 에 `aggregate_tier_counts` 사용. CLI `--graph-match-threshold` / `--graph-match-strict` / `--score-relations`. config_summary 에 graph 매칭 정책 기록. |
| `tests/test_eval/test_gold_set.py` | 확장 필드 round-trip + backward-compat 9개 신규 테스트. |
| `tests/test_eval/test_synth.py` | `parse_generated_graph_questions` + `generate_graph_questions` evidence/aliases/relation 파싱 8개 신규 테스트. |
| `tests/test_eval/test_build_synthetic_gold_set.py` | `_make_graph_gold_item` evidence/aliases/relation emit, `_embed_graph_item_descriptions` 배치 + None client, CLI 옵션 노출 7개 신규 테스트. |

총 **수정 9 + 신규 2 = 11 파일**. (설계 §9 의 12 파일 표 중 `tests/test_eval/test_eval_search.py` 신규는 별도 파일 생성 없이 `test_build_synthetic_gold_set.py` 내에 CLI 노출 테스트로 통합. `tests/fixtures/gold_set_v1.yaml` 픽스처는 backward-compat 테스트가 인라인 dict 로 충분히 검증하므로 생략 — 설계 §9 의 "선택" 표시 항목.)

## 2. 추가 테스트 시나리오

### `test_graph_matching.py` (신규 41 케이스)

| 그룹 | 케이스 수 | 핵심 검증 |
|------|-----------|----------|
| cosine_similarity 단위 | 2 | 동일/직교/반대 벡터, mismatch 처리 |
| T1 exact | 2 | case-insensitive hit, type 미스 시 None |
| T2 alias | 2 | alias OR hit, type 정확 요구 → T4 폴백 |
| T3 normalize | 3 | 공백/punctuation/NFKC 흡수 |
| T4 embedding | 4 | type-agnostic, threshold 미달, 저장 embedding 사용, description 없으면 skip |
| strict 모드 | 1 | T2/T3/T4 skip 확인 |
| 시나리오 A~F | 8 | 각 시나리오가 어떤 tier 로 흡수되는지 |
| 관계 매칭 | 4 | T1 exact, T4 embedding, strict, 빈 후보 |
| Report 집계 | 3 | relevant_keys vs all_relevant_keys, score avg, tier 누적 |
| build_embed_fn | 4 | None client, 캐시, 빈 텍스트, async-only client |
| 종합 | 8 | 다중 retrieved 에서 best score, threshold override, strict cascade 적용, 시나리오 매트릭스 회귀 |

### `test_gold_set.py` (9개 신규)
- `test_graph_entity_ref_roundtrip_with_aliases_and_description`
- `test_graph_entity_ref_to_dict_omits_empty_extension_fields`
- `test_graph_entity_ref_backward_compat_minimal`
- `test_graph_entity_ref_from_dict_filters_non_string_aliases`
- `test_graph_entity_ref_empty_embedding_treated_as_none`
- `test_graph_relation_ref_roundtrip` / `_minimal_roundtrip` / `_empty_embedding_treated_as_none`
- `test_gold_item_with_graph_relations_roundtrip`
- `test_gold_item_no_relations_field_omitted_on_emit`
- `test_gold_item_backward_compat_with_extended_yaml`

### `test_synth.py` (8개 신규)
- `test_parse_generated_graph_questions_full` / `_minimal_backward_compat` / `_invalid_relation_dropped` / `_filters_non_string_aliases` / `_invalid_json_returns_empty`
- `test_generate_graph_questions_emits_evidence_and_aliases`
- `test_generate_graph_questions_graceful_on_minimal_output`

### `test_build_synthetic_gold_set.py` (7개 신규)
- `test_make_graph_gold_item_emits_aliases_and_description`
- `test_make_graph_gold_item_skips_relation_when_disabled`
- `test_make_graph_gold_item_falls_back_to_node_description`
- `test_embed_graph_item_descriptions_fills_embeddings` (배치 + 이미 있는 항목 skip)
- `test_embed_graph_item_descriptions_no_client_silent`
- `test_cli_exposes_new_options` (build CLI)
- `test_eval_search_cli_exposes_new_options` (eval CLI)

## 3. 검증 결과

| 검증 | 결과 |
|------|------|
| `pytest tests/test_eval/ -x` | **258 passed, 0 failed** |
| `pytest -x` (전체) | **985 passed, 0 failed** |
| `ruff check src/context_loop/eval/ src/context_loop/mcp/context_assembler.py src/context_loop/processor/graph_search_planner.py scripts/build_synthetic_gold_set.py scripts/eval_search.py tests/test_eval/` | **All checks passed** (변경 영역 한정) |
| `python scripts/build_synthetic_gold_set.py --help` | `--embed-graph-evidence`, `--score-relations`, `--graph-match-threshold` 모두 노출 |
| `python scripts/eval_search.py --help` | `--graph-match-threshold`, `--graph-match-strict`, `--score-relations` 모두 노출 |

(루트 영역의 다른 스크립트는 사전 존재 lint 경고가 있어 `ruff check src/ scripts/` 전체에 27건 오류가 나오지만 모두 변경 범위 외 — `scripts/run_category_agent.py` 등.)

## 4. 설계와 어긋난 점 + 이유

| 항목 | 설계 | 구현 | 이유 |
|------|------|------|------|
| `--enrich-existing PATH` 모드 | 설계 §4.4 / §6.1 에서 "선택" + "후속 PR 권장" 으로 표시 | **생략** | 설계가 명시한 권장사항 — YAGNI 우선. 사용자가 1차 골드셋 재사용 시 `--graph-match-strict` 로 동일 동작 가능. |
| `tests/fixtures/gold_set_v1.yaml` 픽스처 | 설계 §8.7 / §9 "선택 (신규)" | **생략** | 인라인 dict + `test_gold_item_backward_compat_old_yaml` / `test_gold_item_backward_compat_with_extended_yaml` 두 테스트로 round-trip 회귀가 이미 보장. 파일 픽스처는 의미 중복. |
| `tests/test_eval/test_eval_search.py` 신규 | 설계 §9 의 표 | **생략 — 통합** | 새 CLI 옵션 노출은 정적 inspect 로 `test_build_synthetic_gold_set.py::test_eval_search_cli_exposes_new_options` 에서 검증. 실제 `evaluate_one` 동작은 `test_graph_matching.py` 가 모든 tier hit/miss 패턴을 커버 (eval_search 의 매칭 호출은 단순 위임). |
| `_normalize` 의 punctuation 클래스 | 설계 §2.1 `[\s\-\_\.]+` | **그대로 채택** | 변경 없음. 도메인 예시 확인 후 미세 조정은 implementer 자체 결정 범위였으나, 기본값으로 시나리오 C1/C2 회귀가 모두 통과하여 변경 불필요. |
| relation T4 의 type-agnostic | 설계 §5.5 "T1 exact → T4 embedding. T2/T3 는 관계에 대해 skip" | **준수 — source/target 은 lower 비교, relation_type 만 무시** | 설계와 일치. |
| `aembed_with_client` 헬퍼 | 설계 §10 "build_eval_embedding_client" 패턴 권고 | **신규 헬퍼 추가** | 설계 §10 의 "막힐 때" 가이드에 맞춰 배치 임베딩 헬퍼를 `graph_match.py` 에 둠. build 스크립트의 임베딩 클라이언트는 `_build_embedding_client` 그대로 재사용. |

## 5. 새 CLI 사용 예시

### 생성 (graph evidence 임베딩 + 관계 채점 활성)
```bash
python scripts/build_synthetic_gold_set.py \
    --include-graph-questions \
    --embed-graph-evidence true \
    --score-relations \
    --graph-match-threshold 0.78 \
    --n-graph-nodes 30 \
    --output eval/gold_set.yaml
```

### 평가 (4-tier matching + 관계 채점, 임계값 override)
```bash
python scripts/eval_search.py \
    --gold-set eval/gold_set.yaml \
    --graph-match-threshold 0.80 \
    --score-relations \
    --label robust-v2
```

### 1차 호환 모드 (strict — exact 비교만)
```bash
python scripts/eval_search.py \
    --gold-set eval/gold_set.yaml \
    --graph-match-strict \
    --label baseline-strict
```

## 6. 새 YAML 골드셋 항목 예시

```yaml
version: 1
metadata:
  generated_at: "2026-05-18T11:00:00"
  generation_modes: [chunk, graph]
  embedding_model: "text-embedding-3-small"
  graph_match_threshold_default: 0.78
  score_relations: true
  embed_graph_evidence: true
items:
  - id: q0001
    query: "결제 서비스는 어느 시스템에 의존하나요?"
    relevant_doc_ids: [42]
    relevant_graph_entities:
      - name: "결제 서비스"
        type: "system"
        aliases: ["Payment Service", "결제서비스", "payment-svc"]
        description: "결제 처리 시스템. 주문 서비스에 의존하며 PG 사를 호출한다."
        description_embedding: [0.012, -0.045, 0.118, ...]  # 384/1536-d 모델 의존
    relevant_graph_relations:
      - source_name: "결제 서비스"
        target_name: "주문 서비스"
        relation_type: "depends_on"
        description: "결제 서비스는 주문 서비스에 의존한다"
        description_embedding: [0.034, 0.011, -0.087, ...]
    source_type: "confluence"
    source_document_id: 42
    difficulty: "easy"
    synthesized: true
```

backward-compat — 1차 골드셋 항목 (`{name, type}` 만) 은 그대로 로드되며, `aliases`/`description`/`description_embedding`/`relevant_graph_relations` 가 비어 있어 T1+T3 만 발동한다 (사실상 1차 + name 정규화 추가).

## 7. 시나리오 A~F → tier 흡수 매트릭스

| 시나리오 | 변경 패턴 | 흡수 tier | 메트릭 영향 (대비 1차) |
|----------|-----------|----------|----------------------|
| A (신규 type 추가) | retrieved 분모 증가 | T1 (기존 골든 entity 그대로 hit) | recall 유지, precision 자연 ↓ (보조 메트릭) |
| B (type 명 변경 `system→service`) | retrieved.type 만 변경 | **T4** (embedding, type-agnostic) | 1차에서 0 → 2차에서 cosine score 회복 |
| C1 (공백 변경 `"인증 서비스"→"인증서비스"`) | 공백 차이 | **T3** (normalize) | 1차 0 → 2차 0.9 score |
| C2 (케이스 변경) | lower 차이 | T1 (양쪽 lower 비교) | 영향 없음 (1차도 hit) |
| C3 (동의어, alias 보유) | 전혀 다른 표기 + golden 의 alias 목록 보유 | **T2** (alias OR) | 1차 0 → 2차 hit |
| C3 (동의어, alias 없음 + description) | description 임베딩으로 의미 일치 | **T4** (embedding) | 1차 0 → 2차 cosine score |
| D (병합/canonical 변경) | retrieved.name 이 canonical 표기로 바뀜 | T2 (alias) 또는 T4 (description) | recall 회복 |
| E (관계 타입 변경 `depends_on→requires`) | edge.relation_type 변경, source/target 동일 | **rel T4** (`--score-relations` 활성 시) | 1차에는 관계 채점 자체 없음 → 신규 시그널 |
| F (새 이웃 추출 추가) | retrieved 길이 증가 | T1 (골든은 그대로) | recall 유지, precision 자연 ↓ |

## 8. 결정 기록 (구현 중 마주친 결정점)

- **`build_embed_fn` 의 async 처리**: 동기 `embed_query` 가 있으면 우선 사용. 없으면 비동기 `aembed_query` 를 `asyncio.run` 으로 호출하되, 이미 이벤트 루프가 도는 경우 (예: pytest async 모드) 경고만 남기고 `None` 반환. 운영 환경에서는 `EndpointEmbeddingClient` / `LocalEmbeddingClient` 모두 동기 메서드를 제공하므로 영향 없음.
- **score_relations 활성화 시 metadata flag**: 골드셋 metadata 에 `score_relations=True` 를 기록하여 골드셋만 보고도 관계 채점이 의도된 것인지 알 수 있게 함. 평가 측은 CLI 옵션이 우선이지만 metadata 가 운영 디버깅 단서.
- **`graph_match_tiers` dict 의 metric aggregator 통합**: `metrics.aggregate` 가 숫자만 처리하므로 dict 형 보고는 `aggregate_tier_counts` 로 별도 누적. `write_summary` 가 전체 + mode-별 양쪽에 `graph_match_tiers_total` (그리고 관계 활성 시 `graph_rel_match_tiers_total`) 키로 emit.
- **`retrieved_graph_relations` 노출 위치**: `GraphSearchResult.relations` (planner 측) + `AssembledContext.retrieved_graph_relations` (assembler 측) 두 곳에 패스스루. relation 의 `description` 은 edge properties 의 `label` 이 있으면 사용, 없으면 `"{src} {rel} {tgt}"` 자동 생성 — T4 fallback 의 의미 매칭 입력으로 활용.
- **graph_ndcg@k 신규**: 1차에는 graph 측 ndcg 가 없었지만 2차에서 자연스럽게 cascade matching 의 rank 보존을 활용할 수 있어 추가. 보고 키 추가만으로 기존 동작과 충돌 없음.

## 9. 한 문장 요약

설계 §0 의 11개 결정과 §9 의 변경 파일 표를 모두 따라 4-tier cascade graph matching (exact → alias → normalize → embedding, τ=0.78) 을 구현했고, `GraphEntityRef` 확장 + `GraphRelationRef` 신규로 1차 골드셋과 자연 backward-compat 한 채로 시나리오 A~F 의 모든 깨짐 패턴을 흡수하며, 전체 985 pytest 통과 + ruff clean + 새 CLI 옵션 6개 모두 `--help` 노출 확인했다.
