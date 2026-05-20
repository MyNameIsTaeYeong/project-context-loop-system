# Graph Eval Metric Diagnosis

> 정적 코드 분석 기준. 메트릭 정의 자체는 대체로 정확하지만, **검색이 빈 결과를 반환하면 메트릭은 정의상 0**.

## 메트릭 계산 흐름 (eval_search.py)

```
GoldItem.relevant_graph_entities   ─┐
                                    ├─► run_entity_matching(threshold=0.78)
AssembledContext.retrieved_graph_entities ─┘
                                    │
                                    ├─ T1 exact (name.lower, type)
                                    ├─ T2 alias (golden.aliases × type)
                                    ├─ T3 normalize (NFKC + 구두점 제거 × type)
                                    └─ T4 embedding (description cosine ≥ 0.78, type-agnostic)
                                    ▼
                              MatchReport
                                    │
                                    ├─ retrieved_keys_in_rank_order (= 매칭된 gold의 key list)
                                    └─ all_relevant_keys (= 모든 gold key set)
                                    ▼
       recall@k / precision@k / hit@k / mrr / ndcg@k (metrics.py)
```

## 메트릭 측 핵심 발견

### F-METRIC-01 (HIGH): `DEFAULT_GRAPH_MATCH_THRESHOLD = 0.78` 이 매우 보수적

- **위치**: `src/context_loop/eval/graph_match.py:33`
- **현재 동작**: T4 embedding 매칭의 cosine 임계값 0.78
- **문제**:
  - 설계 §2.2 결정값이라 명시되어 있으나, description이 짧거나 노이즈 있는 경우 0.78 통과 어려움
  - 특히 retrieved entity의 description이 비어있어 name fallback인 경우 (F-SRCH-06), 짧은 이름끼리는 비특이적 임베딩 → cosine이 낮음
  - 의미 매칭의 실제 기준: 0.65~0.70 정도가 합리적
- **개선 방향**:
  - `DEFAULT_GRAPH_MATCH_THRESHOLD = 0.65`
  - 또는 tier 별로 임계값을 분리 — T4은 0.65, T1~T3은 binary
- **심각도**: High (메트릭 측 손실의 한 축) | **공수**: S

### F-METRIC-02 (MEDIUM): T4 시 골든 `description` 부재면 즉시 None — fallback 없음

- **위치**: `src/context_loop/eval/graph_match.py:300-301`
- **현재 동작**:
  ```python
  if not golden.description:
      return None
  ```
- **문제**: 골드셋 합성기가 description을 안 채운 케이스가 있으면 T4 전체 skip → T1~T3에서 표면 매칭이 실패한 골든은 모두 미매칭
- **개선 방향**:
  - 골든 description 비어있을 때 `golden.name` 자체로 fallback (검색 측의 r_text fallback과 대칭)
  - 또는 별도 가드 메시지로 골드셋 신뢰성 문제 노출
- **심각도**: Medium | **공수**: S

### F-METRIC-03 (HIGH): retrieved의 description fallback이 짧은 이름이라 의미 임베딩이 비특이적

- **위치**: `src/context_loop/eval/graph_match.py:311`
- **현재 동작**:
  ```python
  r_text = (r.description or "").strip() or (r.name or "").strip()
  ```
- **문제**:
  - retrieved.description이 빈 경우 (F-SRCH-06 와 연결) r.name으로 임베딩
  - r.name이 짧은 이름(예: `"main"`, `"foo"`)이면 임베딩이 비특이적 → 무작위 cosine 분포 → 0.78 통과 못함
- **개선 방향**: 검색 측에서 description 폴백 보강(F-SRCH-06)과 묶음
- **심각도**: High | **공수**: S (검색 측과 함께)

### F-METRIC-04 (LOW): tier 별 score 차이 — embedding=cosine 점수, normalize=0.9 고정

- **위치**: `src/context_loop/eval/graph_match.py:297, 321`
- **현재 동작**: T3 score=0.9 고정, T4 score=실제 cosine
- **문제**: 메트릭 평균 score 비교 시 두 tier가 다른 분포 — `graph_match_score_avg`가 의미 해석하기 어려움
- **개선 방향**: 운영 영향 없음. 보고만.
- **심각도**: Low

## 골드셋-검색 결과 키 정합성

- **gold side**: `GoldItem.relevant_graph_entities` = `list[GraphEntityRef(name, type, description, aliases)]`
- **retrieved side**: `AssembledContext.retrieved_graph_entities` = `list[GraphEntityRef(name, type, description)]` (검색이 채움)
- **키**: `(name.lower(), type)` — 양측 동일 공간

**키 형식 정합성**: 일치 (둘 다 `(lower name, type)`). 문제는 **값**이 안 맞는 케이스가 다수.

## funnel 손실 시뮬레이션

10건의 골드 entity 중 검색이 정확히 5건만 표면 표기로 매칭한다고 가정:
- T1 exact: 5건 hit (50%)
- T2 alias: aliases가 있는 골드의 경우 추가 매칭 가능 (현실적으로 골드 항상 alias 보유는 아님)
- T3 normalize: 표기 변형 정규화 — 한국어 공백/하이픈 차이 흡수 일부
- T4 embedding: description이 양측 모두 살아있고 임계값 통과 시
- 합계 60% 정도 매칭 가능 — recall ≈ 0.6

그러나 사용자 보고: `< 10%`. 즉 검색이 **거의 빈 결과**를 반환하고 있다는 강한 증거. 메트릭 측보다 검색 측이 funnel의 손실 큰 위치.

## 권고

| ID | 우선 | 권고 |
|----|------|------|
| F-METRIC-01 | High | 임계값 0.78 → 0.65 (운영 변경) |
| F-METRIC-02 | Medium | 골드 description 부재 시 name fallback |
| F-METRIC-03 | High | 검색 측 description fallback 보강과 동기 |
| 골드셋 신뢰성 | (별도) | rag-eval-audit 하네스 영역 |

## 범위 외

- 메트릭 정의 자체의 정확성 검토 (rag-eval-audit 영역)
- 골드셋 합성 정확성 (eval-gold-set-improvement 영역)
- 본 진단은 "왜 이번 run의 메트릭이 0인가"에 한정
