# R3 Verification Report

## 한 줄 결론

**PASS** — R3 계획서의 3개 항목 모두 구현, 신규 테스트 5건 + 기존 762건 통과, 전체 회귀 0.

## 테스트 결과

| 명령 | 결과 |
|------|------|
| `pytest tests/test_processor/test_graph_search_planner.py` | 27 passed (21 기존 + 5 신규 + 1 갱신) |
| `pytest tests/test_storage/test_graph_store.py` | 41 passed (변경 없음) |
| `pytest tests/test_mcp/test_context_assembler.py` | 23 passed |
| `pytest tests/ --ignore=tests/test_eval` | **762 passed** (전체 회귀 0) |
| `pytest tests/test_eval/` | 270 passed, 5 failed (사전 실패 5건, baseline 동일, 본 변경 무관) |

ruff check (touched files): **3 errors (baseline 동일 3 errors)** — regression 0. 모두 E501 line-length 사전 위반.

## 계획-구현 매트릭스

| ID | 계획 | 실제 | 일치 |
|----|------|------|------|
| F-LLM-R3-01 | target_entities + target_relations schema | ✓ + has_targets property | ✓+ |
| F-LLM-R3-02 | retrieved priority ordering | ✓ + idempotent priority 승격 | ✓+ |
| F-LLM-R3-03 | system prompt 정렬 (인덱싱 어휘 + 방향성) | ✓ | ✓ |
| 후방 호환 | search_steps 경로 유지 | ✓ (LLM 구식 응답 / 직접 호출자 둘 다 지원) | ✓ |

불일치/누락: 0건.

## 회귀 위험 점검 (사후)

| 변경 | 위험 가설 | 실측 |
|------|----------|------|
| 새 schema 출력 요구 | LLM 이 못 답하면 빈 결과 | 후방 호환 search_steps 경로로 graceful fallback — 기존 27 테스트 모두 통과 |
| priority ordering | rank 변화 → 다른 메트릭 변동 | 신규 테스트 `test_execute_search_with_target_entities_prioritizes_seed` 가 rank-1 검증 |
| system prompt 1.5x 증가 | max_tokens 한도 영향 | 한도 32768 대비 안전 |

## R1 → R2 → R3 funnel 손실 변화

| 단계 | R1 | R2 | R3 |
|------|-----|-----|-----|
| Stage 2 LLM seed 선택 | 70% sink 선택 위험 | 동일 (양방향이 회복) | **schema 변경 — LLM 이 정답을 직접 식별** |
| Stage 3 get_neighbors | 88.9% miss (sink 시) | 0% miss (양방향) | 동일 |
| Stage 4 retrieved 포함성 | 부분 회복 | 거의 회복 | 회복 + **rank-1 priority** |
| Stage 5 T4 매칭 | 임계값만 완화 | 관계 요약 description | 동일 |
| **MRR/NDCG** | 0.065 | 거의 변화 없음 (사용자 보고) | **target_* 가 rank-1 → 큰 폭 ↑ 예상** |

## 평가 메트릭 재측정 권고

```bash
python -m scripts.eval_search --gold-set <path> --label r3-baseline --include-graph
python -m scripts.compare_runs <r2-output.json> <r3-output.json>
```

특히 비교할 지표:
- **MRR**: priority ordering 의 직접 효과 (예상 0.065 → 0.5+)
- **NDCG@10**: rank 가중 (예상 큰 폭 ↑)
- **graph_hit@10**: 포함성은 R2 가 잡았으나 R3 가 정합성 보강
- **graph_recall@k**: 보강 효과 누적
- **graph_rel_recall**: target_relations 의 끝점 priority → edge 추출 정확도

R3 의 정확한 효과 검증은 LLM 의 새 schema 응답 품질에 의존. LLM 이 인덱싱-시점 형태에 더 친숙하면 효과 큼 — 정량 측정은 운영 평가 필요.

## 다음 라운드 (R4) 후보

- LLM 의 target_relations 를 retrieved_graph_relations 에 직접 포함 (실제 edge 부재 시에도)
- target_relations 의 fuzzy 매칭 (source 미상 → target 만으로 incoming 추적)
- 인덱싱 LLM 의 결정성 강화
