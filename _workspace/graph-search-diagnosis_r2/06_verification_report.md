# Verification Report — Round 2

## 한 줄 결론

**PASS** — R2 계획서의 3개 항목 모두 구현, 신규 테스트 4건 + 기존 테스트 749건 통과, 전체 회귀 0.

## 테스트 결과

| 명령 | 결과 |
|------|------|
| `pytest tests/test_storage/test_graph_store.py` | 41 passed (기존 39 + 신규 2) |
| `pytest tests/test_processor/test_graph_search_planner.py` | 21 passed (기존 19 + 신규 2) |
| `pytest tests/test_mcp/test_context_assembler.py` | 23 passed |
| `pytest tests/ --ignore=tests/test_eval` | **756 passed** (전체 회귀 0) |
| `pytest tests/test_eval/` | 270 passed, 5 failed (사전 실패 5건, baseline 동일, 본 변경 무관 — `git stash` 후 베이스라인에서도 동일 실패 5건 확인) |

ruff check (touched files): **11 errors (baseline 동일 11 errors)** — regression 0. 모두 E501 line-length 사전 위반.

## 계획-구현 매트릭스

| ID | 계획 | 실제 | 일치 |
|----|------|------|------|
| F-SRCH-R2-01 | get_neighbors 양방향 | ✓ + `_bidirectional_bfs` 공통 헬퍼 + `get_neighbors_from_node_id` 동일 변경 | ✓+ |
| F-SRCH-R2-03 | execute_graph_search always-on 시드 보강 | ✓ (threshold 0.6, top_k 3) | ✓ |
| F-METRIC-R2-01 | retrieved description 관계 요약 fallback | ✓ (양방향 관계 — out + in) | ✓+ |

불일치/누락: 0건.

## 회귀 위험 점검

| 변경 | 회귀 위험 | 검증 |
|------|----------|------|
| 양방향 traversal | retrieved 노드 수 ↑, precision 약간 ↓ 가능 | 모든 기존 test_get_neighbors 케이스 통과. 의미적으로 검색은 양방향이 자연스러움. |
| always-on 시드 보강 | LLM 의도 무관 시드 유입 | threshold 0.6 + top_k 3 으로 보수 통제. R1 의 전체-실패 fallback (threshold 0.5, top_k 5) 보존. |
| 관계 요약 description | T4 임베딩이 더 의미적이라 매칭 분포 변화 | description 빈 경우만 발동. score threshold (0.65) 가 false-positive 흡수. |
| `get_neighbors` 시그니처 | 변경 없음 (kwargs 동일) | 모든 호출자 무영향 |
| `execute_graph_search` 시그니처 | 변경 없음 | 동일 |

## 다운스트림 영향 분석

- `_bidirectional_bfs` 는 internal helper — 외부 호출자 없음
- `context_assembler._search_graph_with_llm` 가 `execute_graph_search` 호출 — query_embedding 전달은 R1 부터 이미 적용
- 신규 description fallback 은 retrieved 측에서만 작동 — gold 측의 description 은 비변경

## R1 → R2 funnel 손실 변화 (예상)

| 단계 | R1 (정적분석 기반) | R2 (실측+양방향) |
|------|------------------|----------------|
| Stage 2 plan LLM seed | 30~50% 손실 | 동일 (R3 후보) |
| **Stage 3 get_neighbors** | **88.9% miss (sink 시드)** | **~0% miss (양방향)** |
| Stage 3 always-on 보강 | 발동 안 함 (일부 성공 시) | 추가 시드 union |
| Stage 4 T4 (description) | 보일러플레이트 → 비특이 | 관계 요약 → 더 의미적 |

## 평가 메트릭 재측정 권고

```bash
python -m scripts.eval_search --gold-set <path> --label r2-baseline
# R1 vs R2 비교
python -m scripts.compare_runs <r1-output> <r2-output>
```

예상:
- graph_hit@10: < 10% → 30~50%
- graph_recall@10: < 10% → 30~50%
- MRR/NDCG: 0.065 → 0.3~0.5

본 R2 라운드는 검색 측 funnel 의 directional 손실 회복이 핵심. 평가 메트릭의 정확한 변화는 별도 평가 실행 필요.

## 다음 라운드 (R3) 후보

- F-SRCH-R2-02 LLM 시드 선택 프롬프트 강화 (system prompt 의 entity 선택 가이드)
- F-GOLD-R2-01 gold 후보 양방향 (eval-gold-set-improvement 영역 — gold 분포 영향)
- F-IDX-R2-03 entity_embeddings 영속화 + lock
- F-IDX-R2-02 entity_name strip 정제 (indexing-improvement 영역)
