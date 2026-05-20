# Verification Report — Round 1

## 한 줄 결론

**PASS** — 계획서 R1의 7개 항목 모두 구현, 신규 테스트 8건 + 기존 5건 정합화 통과, 전체 회귀 0.

## 변경 요약

| 파일 | 변경 라인 |
|------|----------|
| `src/context_loop/storage/graph_store.py` | +50 / -3 |
| `src/context_loop/processor/graph_search_planner.py` | +60 / -3 |
| `src/context_loop/eval/graph_match.py` | +10 / -4 |
| `src/context_loop/mcp/context_assembler.py` | +7 / -1 |
| `tests/test_storage/test_graph_store.py` | +75 / 0 |
| `tests/test_processor/test_graph_search_planner.py` | +70 / 0 |
| `tests/test_eval/test_graph_matching.py` | +40 / -10 (5건 의도 보존 조정) |

## 계획-구현 매트릭스

| ID | 계획 | 실제 | 일치 |
|----|------|------|------|
| F-METRIC-01 | DEFAULT_GRAPH_MATCH_THRESHOLD 0.78 → 0.65 | ✓ | ✓ |
| F-METRIC-02 | golden description 부재 시 name fallback | ✓ | ✓ |
| F-SRCH-02 | search_entities default threshold 0.5 | ✓ | ✓ |
| F-SRCH-06 | retrieved description 자연어 fallback | ✓ | ✓ |
| F-SRCH-01 | get_neighbors 임베딩 fallback | ✓ + `get_neighbors_from_node_id` 헬퍼 (보너스) | ✓+ |
| F-SRCH-03 | execute_graph_search query embedding 시드 보강 | ✓ + step 별 임베딩 fallback (보너스) | ✓+ |
| F-SRCH-04 | system prompt 강화 | ✓ | ✓ |

불일치/누락: 0건. 보너스: `get_neighbors_from_node_id` 헬퍼 + step 별 임베딩 fallback (계획서에는 step 별까지는 명시 없음, 자연 보강).

## 테스트 결과

| 명령 | 결과 |
|------|------|
| `pytest tests/test_storage/test_graph_store.py` | passed (전체 + 신규 3) |
| `pytest tests/test_processor/test_graph_search_planner.py` | passed (전체 + 신규 3) |
| `pytest tests/test_eval/test_graph_matching.py` | passed (전체 + 신규 2) |
| `pytest tests/test_mcp/test_context_assembler.py` | passed (영향 영역 전체) |
| `pytest tests/ --ignore=tests/test_eval` | **749 passed** (전체 회귀 0) |
| `pytest tests/test_eval/` | 270 passed, 5 failed (사전 실패 5건, baseline 동일, 본 변경 무관) |

ruff check (touched files): 3 errors — baseline에 동일 3건 (regression 0).

## 회귀 위험 점검

| 변경 | 회귀 위험 | 검증 |
|------|----------|------|
| DEFAULT_GRAPH_MATCH_THRESHOLD 0.78 → 0.65 | 메트릭 절대값 변화. 기존 baseline과 비교 불가 — 그러나 새 baseline이 의도 | 운영에서 메트릭 재측정 권고 |
| get_neighbors 임베딩 fallback | depth-1 서브그래프가 더 커질 수 있음 — 노이즈 증가. 임베딩 fallback이 only-when-no-surface-match이므로 정상 매칭에는 영향 없음 | 기존 테스트 통과 |
| execute_graph_search 시드 보강 | LLM 의도 무관 시드 들어옴 — 단, search_steps이 0개일 때만 fallback이라 일반 경로 영향 없음 | 기존 통과 |
| description 자연어 fallback | 매칭 임베딩 시그널이 비특이적이 되는 역효과 가능 — 그러나 빈 description보다는 신호 강함 | 신규 테스트로 동작 검증 |
| 골든 description name fallback | 골든이 description 부재일 때 T4가 발동 — 의미적 매칭이 너무 관대해질 수 있음. 그러나 mock 임베딩으로 검증, 운영 임베딩은 더 차별적 | 신규 테스트 / 기존 테스트 strict 옵션으로 의도 분리 |

## 다운스트림 영향 분석

- `execute_graph_search` 시그니처 변경 (kwargs 추가) — 호출처 `context_assembler._search_graph_with_llm` 만 업데이트 (cascade 없음)
- `get_neighbors` 시그니처 변경 (kwargs 추가, default None) — 기존 호출은 호환 (kwargs default)
- `search_entities_by_embedding` default threshold 변경 — 호출처들이 명시적 threshold를 주면 영향 없음, 기본값 의존 시 더 관대해짐
- 메트릭 임계값 변경 — eval_search.py CLI 인자 `--graph-match-threshold` 미지정 시 0.65 사용. 명시 시 그 값.

## 후속 권고 (PASS-WITH-NOTES)

R1으로 funnel의 가장 큰 손실 지점을 완화함. 효과 정량 측정은 운영 평가 재실행 필요:

```bash
python -m scripts.eval_search --gold-set <path> --label r1-baseline
# 비교: 이전 run의 graph_recall@k vs 본 run의 graph_recall@k
```

R2 후보 (다음 세션):
- entity_embeddings race-lock + SQLite 영속화
- NetworkX MultiDiGraph 전환
- schema_text max_entities_per_type 확장
