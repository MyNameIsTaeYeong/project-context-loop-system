# Graph Eval Metric + Gold Set Diagnosis (R2)

> R1은 정적 분석으로 임계값 완화·name fallback 2건 머지. R2는 **gold ↔ index 정합성**과 **메트릭 정의의 funnel 손실 기여도**를 정량 분석.

## 골드셋 생성 메커니즘 (코드 추적)

흐름: `scripts/build_synthetic_gold_set.py:_run_graph_mode` → `load_candidate_subgraphs` → `generate_graph_questions` (synth.py) → `_make_graph_gold_item`.

### 핵심 코드 흐름

1. **`load_candidate_subgraphs`** (build_synthetic_gold_set.py:183-303):
   - `graph_store.get_neighbors(name, depth=1)` 호출
   - `if len(neighbors) < min_neighbors + 1: continue` (min_neighbors=1 default)
   - → **outgoing edge >= 1 인 노드만 gold subgraph 후보**
   - 실측: 21 노드 중 **6개만 통과** (Auth/Order/Payment/Notification/Search Service + API Gateway)

2. **`generate_graph_questions`** (synth.py:703-745):
   - 프롬프트 (synth.py 의 `GRAPH_GENERATE_PROMPT_TEMPLATE`, 추정 위치):
     ```
     엔티티: {entity_name} ({entity_type})
     설명: {entity_description}
     주변 관계: {edges_text}
     이 엔티티 또는 관계에서 답을 찾을 수 있는 한국어 질문을 N개 생성해라
     ```
   - LLM 자연어 질의 생성 + evidence_description / aliases / relation 보조.

3. **`_make_graph_gold_item`** (build_synthetic_gold_set.py:949-1005):
   ```python
   entity_ref = GraphEntityRef(
       name=sg["entity_name"],            # SEED 노드 entity_name 그대로
       type=sg["entity_type"],
       aliases=list(gq.entity_aliases),
       description=gq.evidence_description,
   )
   relevant_graph_entities=[entity_ref]   # SEED 1개만
   ```
   - → gold 의 `relevant_graph_entities[0].name` == 인덱스 노드의 `entity_name` (글자 단위 동일).

### 결론: gold ↔ index 표면 키 정합성은 OK

- gold-side `(name.lower().strip(), type.strip())` 키 == index-side `(entity_name.lower().strip(), entity_type.strip())` 키
- T1 exact 매칭이 retrieved 에 seed 가 들어가기만 하면 무조건 hit.
- **그러나 retrieved 에 seed 가 들어가지 않는 게 funnel 의 결정적 손실** — `02_search_pipeline_diagnosis.md` 의 F-SRCH-R2-01 참조.

## F-GOLD-R2-01 (MEDIUM): gold subgraph 후보가 outgoing 의존 → 인덱스 노드의 71% (15/21) 가 영원히 gold seed 안됨

- 인덱스의 sink 노드(예: KakaoPay, Elasticsearch, 결제 팀 등)는 gold question 의 정답으로 결코 등장하지 못함.
- 결과: gold question 분포가 service 중심으로 편향.
- 평가 신호의 한쪽 (subgraph 다양성) 제한.
- **해결**: `load_candidate_subgraphs` 가 양방향 이웃(outgoing+incoming)을 보도록 변경 → 21 노드 중 17개가 후보. 단, 본 변경은 **gold 빌드 분포에 영향**이라 별도 라운드 고려 (rag-eval-audit / eval-gold-set-improvement 하네스 영역 — 본 라운드는 검색 funnel 회복이 우선).

## F-METRIC-R2-01 (MEDIUM): T4 의 description 자연어 fallback 매칭의 비특이성

### 코드 위치

- 검색 측 (graph_search_planner.py:386-393):
  ```python
  if not description:
      description = (
          f"이 entity 는 {etype} 유형의 '{name}' 이며 그래프 노드로 등록되어 있다."
          ...
      )
  ```
- 매칭 측 (graph_match.py:310-318):
  ```python
  g_text = golden.description or golden.name or ""
  ...
  g_emb = embed_fn(g_text)
  ```

### 문제

- gold 측 description 은 LLM 합성 `evidence_description` (자연 문장).
- retrieved 측 description 은 R1 의 metadata-스타일 fallback ("이 entity 는 ... 등록되어 있다").
- 두 description 의 임베딩 cosine 이 threshold 0.65 를 못 넘김 (둘이 의미적으로 무관 — 한쪽은 정답 evidence, 다른 쪽은 metadata 보일러플레이트).
- T4 가 사실상 발동되지 않음 → T1 표면 매칭에 전적 의존.

### R1 fix 평가

- R1 의 description fallback (F-SRCH-06) 은 **T4 가 작동하기 위한 텍스트 채우기** 였지만, 텍스트의 *의미*가 비특이라 T4 매칭 자체는 회복하지 못함.
- threshold 0.78 → 0.65 (F-METRIC-01) 도 noise 영역까지 떨어뜨려 다른 페어 false-positive 위험 살짝 ↑.

### 개선 방향 (R2)

- retrieved description fallback 을 **인접 관계 정보로 채움** — entity_name 자체보다 "이 entity 는 X 에 의존하며 Y 에 사용된다" 같은 1-hop 관계 요약 → 임베딩이 더 의미적.
- 또는 T4 score 를 entity_name 임베딩 매칭으로 보조 — name 자체 vs golden description 임베딩 비교 path 추가.

## MRR/NDCG = 0.065 의 정량 해석

- MRR ≈ 1/E[rank]. 0.065 → E[rank] ≈ 15. top_k=10 이면 거의 outside.
- recall@10 < 0.1 → top-10 안에 매칭된 gold 가 매우 적음.
- **분포 추정**:
  - ~5~10% 의 query 만 T1 hit (LLM 시드 정확 + retrieved 에 seed 들어감)
  - 나머지 ~90~95% 는 retrieved 자체에 seed 누락 → 메트릭 0
  - 평균이 ~0.065 라는 건 hit 케이스의 평균 score 가 ~0.65~0.7 정도 (1.0 hit @ 5~10% × score) → mostly hit-at-rank-1

→ 메트릭은 **검색이 retrieved 에 seed 를 넣지 못한 게 90%+ 의 case 라는 의미**. F-SRCH-R2-01 가 정답.

## R1 메트릭 fix 효과 평가

| R1 fix | 실제 효과 추정 |
|--------|--------------|
| F-METRIC-01 threshold 0.78→0.65 | T4 임계값 완화이나 T4 자체가 R2 funnel 에서 활성화 안됨 (description fallback 의 비특이성) → **효과 ~0** |
| F-METRIC-02 golden description name fallback | 골드 description 비어있을 때만 발동 — 합성 골드는 거의 항상 description 채움 → **효과 미미** |

→ R1 메트릭 fix 는 funnel 의 **T4 단계 회복**을 시도했지만, 실제 funnel 손실은 **T1 전 단계(retrieve)** 에 있어서 효과 미미.

## 권고 (메트릭/골드셋 측)

| ID | 권고 | 우선 |
|----|------|------|
| F-METRIC-R2-01 | retrieved description fallback 을 관계 요약으로 강화 (T4 회복) | Medium |
| F-GOLD-R2-01 | load_candidate_subgraphs 양방향 (인덱스의 71% sink 노드도 gold 가능) | Medium (별도 라운드) |
| 골드셋 신뢰성 일반 | 자체 매칭 self-test (gold 생성 직후 retrieved 시뮬레이션해서 hit rate 측정) | High (rag-eval-audit 영역) |

## 범위 외 (본 R2 하네스에서 다루지 않음)

- gold 합성 로직 자체 (synth.py) — eval-gold-set-improvement / rag-eval-audit 영역
- 메트릭 정의 자체 (metrics.py) — rag-eval-audit 영역
- 본 R2 는 **검색 funnel 회복(F-SRCH-R2-01)** 이 핵심.
