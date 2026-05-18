# 02 — 그래프 인덱싱 강건성 설계 (2차)

**작성일**: 2026-05-18
**역할**: eval-system-designer (설계 전용 — 구현 금지)
**선행 산출물**:
- `_workspace/00_requirements.md` (R1/R2/R3 + 비기능)
- `_workspace/01_analysis.md` (시나리오 A~F, 미해결 질문 Q1~Q11, 영향 파일·테스트 매트릭스)

본 문서는 analyst 가 식별한 11개 미해결 질문에 모두 결정을 내리고, 그 결정을 종합한 구현 가능한 설계를 제시한다. 결정은 YAGNI · backward-compat · 비용 통제 · 결정성 원칙을 따른다.

---

## 0. 미해결 질문에 대한 결정 (요약 표)

| # | 질문 | 결정 | 근거 (한 줄) | 대안 트레이드오프 (한 줄) |
|---|------|------|------------|-----------------------------|
| Q1 | 시멘틱 매칭 채택? | **채택 — 임베딩 기반** | 임베딩 인프라(`EndpointEmbeddingClient`, `GraphStore.build_entity_embeddings`)가 이미 운영 중 + 결정론적·저비용. | LLM 매칭은 비결정성·query 당 N회 추가 호출 (비용 폭증). |
| Q2 | 골드셋 스키마 변경 범위 | **`GraphEntityRef` 확장 (옵션 a)** + 별도 `GraphRelationRef` 신설 | 1차 골드셋 backward-compat 필요 → 새 클래스로 분리 시 마이그레이션 부담. | `GraphFact` 통합 클래스는 의미적으로 더 깔끔하나 1차 YAML 호환 깨짐. |
| Q3 | 관계(엣지) 채점 도입? | **도입 — 단 옵셔널 (`--score-relations` CLI)** | R3 가 "관계 타입 변경 robust" 를 명시 → 시그널 누락 방지. | 항상-on 은 신규 메트릭으로 보고 노이즈 증가, 1차 골드셋엔 관계 정답이 없음. |
| Q4 | 매칭 정책: strict vs fuzzy 분리? | **통합 — 4-tier cascade (exact→alias→normalize→embedding)** + `--graph-match-strict` 로 임베딩만 skip | tier 가 자연스럽게 strict→fuzzy 그라데이션을 형성. 단일 코드 경로가 결정성·디버그 모두 우월. | 모드 분리는 CLI 복잡도↑, 보고 split 도 늘어남. |
| Q5 | 재합성 헬퍼 제공? | **제공 안 함 — 단 `build_synthetic_gold_set.py --enrich-existing PATH` 모드만 추가 (선택)** | 별도 스크립트는 YAGNI, enrich 모드는 기존 entrypoint 재활용. | 별도 스크립트는 명령 surface 증가 + 코드 중복. |
| Q6 | precision 자연 하락 정책 | **현 정책 유지 + 보고에서 recall/hit 우선 표시, precision 은 보조** | 검색 ego-subgraph 노출은 컨텍스트 품질에 필요(W-3 트레이드오프 유효). | 메트릭 변형(`precision_at_searched_only`) 도입은 의미가 약하고 사용자 혼란. |
| Q7 | 검색 측 entity 노출 범위 변경? | **변경 없음** (현재 그대로 유지) | 평가/검색 분리 원칙 + 컨텍스트 활용도 보존. precision 비대칭은 Q6 으로 흡수. | `core_entities` vs `peripheral_entities` 분리는 W-3 트레이드오프 재설계가 필요한 큰 변경. |
| Q8 | 시맨틱 매칭 결정성 | **임베딩 모델 ID 를 골드셋 metadata 에 기록 + 임계값 τ 도 metadata 에 기록** | 모델 버전 변경 시 점수 변동을 명시적 보고로 흡수. | 모델 핀 고정은 운영 부담, 점수 진동 무시는 R 의 결정론 위배. |
| Q9 | alias 추출 출처 | **Generator LLM 이 question 과 함께 생성** (옵션 a) | 별도 패스는 LLM 호출 2배 + Judge 가 이미 generic 게이트로 자기편향 일부 차단. | (c) 인덱스 alias 테이블은 §5 에 따라 부재 — 신규 인프라 필요. |
| Q10 | "정답 N개" 정책 (W-3 재검토) | **유지 — 중심 노드 1개** | 골드 확장은 검색 측 entity 노출과 의미가 어긋남(평가 기준이 모호해짐). | 1-hop 이웃까지 정답에 포함 시 precision 인공 보정 효과는 있으나 채점 단위가 흐려짐. |
| Q11 | Judge 의 graph 답안 채점 재활용? | **재활용 안 함 (현행 `judge_answer` 그대로 유지)** | Judge 는 텍스트 답변 품질용. graph 매칭 시그널은 임베딩으로 충분 → 결정성↑. | semantic-via-judge 는 비결정성·비용↑. |

---

## 1. 설계 목표 (요구사항 매핑)

| 요구사항 | 결정 ID | 매핑 내용 |
|---------|--------|----------|
| **R1**: 정확한 node_id/entity_name 비의존 | D1 (tiered matching), D2 (스키마 확장: aliases/description) | exact match 한 단계가 아닌 4-tier cascade. type 미스매치 / name 표기 변경 / 동의어 모두 흡수. |
| **R2**: 의미 매칭 or 텍스트 앵커 매핑 | D1 (4단계 cascade — 마지막이 임베딩 cosine), D3 (생성 시 `description` evidence + 임베딩 1회 계산) | 임베딩 채택. 텍스트 앵커는 description 필드로 보존(매칭에는 임베딩, 디버그에는 자연어). |
| **R3**: 병합 / 관계 타입 변경 / 신규 추출 robust | D1 (병합·canonical은 alias OR + 임베딩으로 흡수), D4 (관계 채점 옵셔널 도입 — `GraphRelationRef`), Q6 (precision 자연 하락 보조 강등) | 신규 추출의 precision 하락은 보조 메트릭으로 흡수, recall/hit 중심 보고. |
| **비기능: backward-compat** | D2 (필드 추가는 optional, 누락 시 자동 fallback to tier1+3), D5 (마이그레이션 스크립트 없음) | 1차 YAML 항목은 description/aliases 없으므로 tier1 exact + tier3 normalize 만 발동. tier2/4 자연 skip. |
| **비기능: LLM 비용 통제** | D6 (Judge 매 query 호출 X, 임베딩만), D7 (description 임베딩은 생성 시 1회 + 골드셋 YAML 에 박힘) | 평가 시 추가 LLM 호출 0건. 임베딩 호출은 query embedding 만 (이미 존재). |
| **비기능: 결정론** | Q8 (모델 ID + τ 를 metadata 에 기록), D7 (description 은 생성 시 고정) | 같은 골드셋 + 같은 임베딩 모델 → 같은 점수. 모델 변경 시 metadata 로 추적. |
| **비기능: metrics_by_mode 유지** | D8 (기존 split 그대로, graph 보고에 새 시그널 추가) | `metrics_by_mode.graph` 의 dict 키 확장. 기존 키 보존. |

---

## 2. Tiered Matching 알고리즘

평가 시점에 `relevant_graph_entities` 의 각 골든 엔티티를 검색 결과 `retrieved_graph_entities` 의 후보들과 매칭. 4단계 cascade — 첫 tier 에서 hit 하면 즉시 단락. tier 별 score 와 hit 한 tier 를 모두 기록.

### 2.1 매칭 단계

| tier | 키 | 통과 시 score | 비용 |
|------|---|----------------|------|
| **T1** exact | `(name.lower().strip(), type.strip())` | 1.0 | O(1) per pair |
| **T2** alias | `(alias.lower().strip(), type) for alias in golden.aliases` | 1.0 | O(\|aliases\|) |
| **T3** normalize | `_normalize(name) == _normalize(name')` 양쪽 적용 | 0.9 | O(1) |
| **T4** embedding | `cosine(emb(golden.description), emb(retrieved.description \|\| retrieved.name)) ≥ τ` | cosine 값 (≥τ ~ ≤1.0) | embedding cache hit |

- `_normalize(s)`: `unicodedata.normalize("NFKC", s).lower()` → 모든 whitespace 제거 → `re.sub(r"[\s\-\_\.]+", "", ...)` → punctuation 제거.
- type 비교는 T1~T3 에서 strict (단 strip + lower 적용 — 분석 §1.3 의 case-sensitive 한계 해소).
- **T4 는 type 일치 요구하지 않음** — 이것이 R3 의 entity_type 명 변경(`system` → `service`) 시나리오 B 를 흡수하는 핵심.
- T4 에서 검색 측 entity 가 description 을 가지지 않으면 `name` 으로 임베딩 (fallback).

### 2.2 임계값 τ

- 기본 **τ = 0.78** (cosine similarity, [−1, 1] 스케일에서 의미적 일치 ≈ 0.78+ 가 경험적 안정선).
- CLI: `--graph-match-threshold 0.78`.
- 골드셋 metadata 에 사용된 τ 와 임베딩 모델 ID 를 기록.
- `--graph-match-strict` 옵션: T4 skip (1차 골드셋과 동일 동작).

### 2.3 의사코드

```python
def match_golden_to_retrieved(
    golden: GraphEntityRef,                       # extended
    retrieved: list[RetrievedEntity],             # (name, type, description, embedding|None)
    embed_fn: Callable[[str], list[float]],
    threshold: float,
    strict: bool,
) -> MatchResult | None:
    g_name = golden.name.strip()
    g_type = golden.type.strip()

    # T1 — exact
    for r in retrieved:
        if r.name.lower().strip() == g_name.lower() and r.type.strip() == g_type:
            return MatchResult(retrieved=r, tier="exact", score=1.0)

    # T2 — alias OR
    for alias in golden.aliases:
        a = alias.lower().strip()
        for r in retrieved:
            if r.name.lower().strip() == a and r.type.strip() == g_type:
                return MatchResult(retrieved=r, tier="alias", score=1.0)

    # T3 — normalize
    g_norm = _normalize(g_name)
    for r in retrieved:
        if _normalize(r.name) == g_norm and r.type.strip() == g_type:
            return MatchResult(retrieved=r, tier="normalize", score=0.9)

    if strict or not golden.description:
        return None

    # T4 — embedding (type-agnostic)
    g_emb = embed_fn(golden.description)  # cache: golden.description_embedding 이 있으면 그것 사용
    best: MatchResult | None = None
    for r in retrieved:
        r_text = r.description or r.name
        r_emb = embed_fn(r_text)
        sim = cosine(g_emb, r_emb)
        if sim >= threshold and (best is None or sim > best.score):
            best = MatchResult(retrieved=r, tier="embedding", score=sim)
    return best
```

- 캐시: 한 평가 실행 내에서 `(model_id, text)` 키 LRU 캐시. golden 측은 골드셋 YAML 에 `description_embedding` 이 박혀 있으면 그대로 사용 (재계산 0회).
- 검색 측 entity description: §5 에 따라 검색 결과에 노출 필요.

### 2.4 채점 (메트릭) 흐름

기존 `metrics.recall_at_k` 가 `set & set` 패턴이라 점수 0/1 인 것을 일반화하지 않는다. tier 매칭 결과를 **"set 의 멤버 보유 여부"** 로 환원한다:

- `retrieved_keys` (rank 순서 보존, list): 각 골든에 대해 매칭이 hit 한 retrieved entity 의 식별자 (`name.lower(), type`). hit 없으면 unhitted.
- `relevant_keys` (set): 각 골든의 자체 `(name.lower(), type)`.
- 메트릭은 기존 함수에 위 두 컬렉션 그대로 통과 — 단 매칭 결과의 score (1.0 / 0.9 / cosine) 는 별도 보고 시그널로 추출.

대안 (논의용, **불채택**): tier 별 가중 점수로 weighted recall@k 도입. → 메트릭 의미가 바뀌고 기존 보고와 비교 어려움.

---

## 3. 새로운 데이터 모델

### 3.1 확장된 `GraphEntityRef`

```python
@dataclass
class GraphEntityRef:
    name: str
    type: str
    aliases: list[str] = field(default_factory=list)               # NEW (선택)
    description: str = ""                                           # NEW (선택, 자연어 evidence)
    description_embedding: list[float] | None = None                # NEW (선택, 생성 시 1회 계산)
```

- 기존 두 필드 + 세 개의 optional 필드. 1차 YAML 은 새 필드가 없으므로 기본값으로 로드. T1+T3 만 발동 (사실상 1차 동작과 유사).
- `to_dict`: 빈 값/None 은 emit 안 함 (YAML 가독성·크기 보존).
- `from_dict`: 누락 시 기본값.

### 3.2 YAML 예시

```yaml
relevant_graph_entities:
  - name: "결제 서비스"
    type: "system"
    aliases: ["Payment Service", "결제서비스", "payment-svc"]
    description: "결제 처리 시스템. 주문 서비스에 의존하며 PG 사를 호출한다."
    description_embedding: [0.0123, -0.0456, ...]   # 1536 차원 (모델 의존)
```

### 3.3 임베딩 직렬화 결정

| 옵션 | trade-off | 채택 |
|------|-----------|------|
| (a) YAML inline list[float] | 가독성↓·크기↑ (1536-d × 8 chars ≈ 12KB/entity) — 그러나 별도 파일 불필요, round-trip 단순 | **채택** |
| (b) 별도 파일 (`gold_set.embeddings.npy`) | YAML 깔끔, 그러나 두 파일 동기화 부담, git diff 어려움 | 미채택 |
| (c) gzip+base64 inline | 가독성 매우↓, 디버그 어려움 | 미채택 |

**근거**: 사내 평가 도구 — 골드셋 크기 N=100~500 항목 × 1536-d ≈ 6~30MB. 허용 범위. 인간 가독성은 description (자연어) 으로 충분히 확보됨. yaml `sort_keys=False` 유지 + flow style 적용으로 가독성 일부 보완 가능. 미래에 크기 문제 발생 시 (b) 로 전환.

### 3.4 `GraphRelationRef` (옵셔널, Q3 채택)

```python
@dataclass
class GraphRelationRef:
    source_name: str
    target_name: str
    relation_type: str                                            # "depends_on" 등
    relation_description: str = ""                                # NEW (선택, 자연어)
    description_embedding: list[float] | None = None              # NEW (선택)
```

`GoldItem` 에 추가:

```python
relevant_graph_relations: list[GraphRelationRef] = field(default_factory=list)
```

- 빈 리스트면 관계 채점 skip (자연 backward-compat).
- 채점 활성화는 CLI `--score-relations` 로 명시. 골드셋에 relations 가 있고 CLI 가 활성화한 경우만 메트릭 계산.

### 3.5 backward-compat 매트릭스

| 1차 YAML 의 graph 항목 | 새 코드 로드 결과 | 새 코드 평가 결과 |
|---------------------|-------------------|-------------------|
| `{name, type}` 만 | `GraphEntityRef(name=..., type=..., aliases=[], description="", description_embedding=None)` | T1 + T3 발동. T2 skip (aliases 빈). T4 skip (description 빈). 기존과 거의 동일 동작 — T3 의 normalize 가 추가 강건성 제공. |
| `{name, type, aliases, description, description_embedding}` 전체 | 완전 로드 | T1~T4 모두 발동. |
| `{name, type, description}` (임베딩 없음) | 임베딩 None | T4 시 평가 시점 lazy 계산 + 캐시. |
| `relevant_graph_relations` 미존재 | 빈 리스트 | 관계 채점 skip. |

---

## 4. 생성 파이프라인 변경 (`synth.py` / `build_synthetic_gold_set.py`)

### 4.1 LLM Generator 프롬프트 출력 스키마 확장

현재 `generate_graph_questions` (`synth.py:419-451`) 는 question 문자열만 반환. 확장:

```jsonc
// LLM 출력 (graph 질문 1개당):
{
  "question": "결제 서비스가 의존하는 시스템은?",
  "evidence_description": "결제 서비스: 주문 서비스에 의존하는 결제 처리 시스템",
  "entity_aliases": ["Payment Service", "결제서비스"],
  // 관계 채점용 (옵셔널 — subgraph 의 핵심 edge 1개):
  "relation": {
    "source_name": "결제 서비스",
    "target_name": "주문 서비스",
    "relation_type": "depends_on",
    "relation_description": "결제 서비스는 주문 서비스에 의존한다"
  }
}
```

- 프롬프트는 subgraph 정보(`build_subgraph_snippet`, `format_edges_for_prompt`)를 그대로 활용 — 이미 generator 에 주입됨.
- 기존 게이트 (`answerable` / `leakage` / `demonstrative` / `generic`) 는 question 에 그대로 적용. evidence/aliases/relation 은 게이트 없음 (LLM hallucination 위험은 subgraph 컨텍스트가 통제 — generator 가 subgraph 외 사실을 못 만드는 구조).
- LLM 출력 파싱 실패 시 graceful: question 만 추출하고 나머지는 빈 값 (1차와 동일 동작).

### 4.2 생성 시점 임베딩 계산

`build_synthetic_gold_set.py` 의 `_make_graph_gold_item` (현재 `:611-634`) 직후 단계 추가:

```python
# pseudo
description_embedding = await embedding_client.aembed_query(evidence_description)
relation_description_embedding = await embedding_client.aembed_query(relation_description) if relation else None
```

- `embedding_client` 는 `_build_clients` (`scripts/build_synthetic_gold_set.py` — eval_search 와 별도) 에 동일 패턴으로 추가. config 의 `processor.embedding_*` 사용.
- batch 호출: 모든 graph 골드 item 의 description 을 모아 `aembed_documents([...])` 로 1회 호출 (지연 절감).
- `description_embedding` 은 `GraphEntityRef.description_embedding` 으로 채워짐 → YAML 에 emit.

### 4.3 골드셋 metadata 에 임베딩 모델 ID + τ 기록

```yaml
metadata:
  generated_at: "2026-05-18T..."
  generator_model: "gpt-4o-2024-08-06"
  judge_model: "claude-3-5-sonnet-..."
  embedding_model: "text-embedding-3-small"        # NEW
  graph_match_threshold_default: 0.78              # NEW
```

- 평가 시 CLI 로 다른 τ 를 주면 `metrics_by_mode.graph` 에 사용된 τ 도 함께 보고.

### 4.4 enrich-existing 모드 (Q5 — 선택적 도입)

`build_synthetic_gold_set.py --enrich-existing PATH` 추가:
- 기존 YAML 로드 → 각 graph 항목의 description/aliases 가 비어 있으면 LLM 으로 생성 (질문은 재사용) → 임베딩 1회 계산 → 같은 path 에 저장.
- 사용 사례: 1차 골드셋을 버리지 않고 점진 보강.
- 신규 항목 추가 / 질문 재생성 / 게이트 재실행은 안 함 (의도적으로 좁은 책임).
- 도입 안 해도 신규 골드셋 합성으로 동일 효과 달성 가능 — 그래서 우선순위는 낮음. **권장**: 1차 PR 에는 빼고, 사용자 요청 시 후속 PR.

---

## 5. 평가 파이프라인 변경

### 5.1 `GraphSearchResult` / `AssembledContext` 의 entity 노출 확장

현재 `graph_search_planner.execute_graph_search` (`:290-303`) 가 `entities: list[GraphEntityRef]` 만 채움. 평가 시 description 이 필요 → 다음 중 하나:

**옵션 A (채택)**: `GraphEntityRef` 를 `RetrievedGraphEntity` 같은 별도 dataclass 로 분리하지 않고, 동일 `GraphEntityRef` 를 재사용하되 description 필드를 검색 측에서도 채움.
- `graph_store.get_neighbors` 의 node dict 에 이미 `properties` 가 있음 — 그 안의 `description` 을 추출.
- `description_embedding` 은 검색 측에서 채우지 않음 (필요 시 평가 시점 lazy 계산 + LRU 캐시).

**옵션 B**: 별도 `RetrievedGraphEntity(name, type, description, embedding)` 신설.
- 깔끔하지만 호출 경로 다수 변경 + 골드셋의 `GraphEntityRef` 와 의미가 분기됨.

**채택**: 옵션 A — 같은 클래스, optional 필드. `assembled.retrieved_graph_entities` 의 의미가 자연 확장.

### 5.2 tiered matching 적용 위치

`scripts/eval_search.py:190-218` 의 `evaluate_one` 내부:

```python
# 현재
retrieved_entities: list[tuple[str, str]] = [(e.name.lower(), e.type) for e in assembled.retrieved_graph_entities]
relevant_entities: set[tuple[str, str]] = {(e.name.lower(), e.type) for e in item.relevant_graph_entities}

# 변경 후
match_report = run_tiered_matching(
    relevant=item.relevant_graph_entities,
    retrieved=assembled.retrieved_graph_entities,
    embed_fn=embedding_client_wrapper,        # LRU 캐시 포함
    threshold=args.graph_match_threshold,
    strict=args.graph_match_strict,
)
retrieved_keys = match_report.retrieved_keys_in_rank_order   # 매칭된 retrieved 의 (name.lower, type), 미매칭은 더미 키
relevant_keys  = match_report.relevant_keys                  # golden의 (name.lower, type)
```

- `metrics.recall_at_k`, `precision_at_k`, `mrr`, `ndcg_at_k`, `hit_at_k` 호출 시그니처는 변경 없이 (set/list 패턴 보존) 호출. tier 매칭이 "어떤 golden 이 어떤 retrieved 에 hit 했는지" 의 매핑만 제공.

### 5.3 `evaluate_one` row 에 새 시그널 추가

기존 키 (예시):
```
graph_recall@k, graph_precision@k, graph_hit@k, graph_mrr, graph_ndcg@k
```

신규 키:
```
graph_match_tiers: {exact: 2, alias: 0, normalize: 1, embedding: 1}    # 카운트
graph_match_score_avg: 0.94                                            # 매칭 score 평균
graph_match_score_min: 0.78
graph_match_score_max: 1.0
```

- 관계 채점이 활성화된 경우만 추가:
```
graph_rel_recall@k, graph_rel_precision@k, graph_rel_hit@k, graph_rel_mrr
graph_rel_match_tiers: {...}
```

### 5.4 보고 형식 예시 (`metrics_by_mode.graph`)

```json
{
  "metrics_by_mode": {
    "graph": {
      "graph_recall@5": 0.86,
      "graph_precision@5": 0.21,
      "graph_hit@5": 0.92,
      "graph_mrr": 0.74,
      "graph_match_tiers_total": {"exact": 142, "alias": 31, "normalize": 12, "embedding": 18},
      "graph_match_score_avg": 0.96,
      "graph_match_threshold_used": 0.78,
      "embedding_model_used": "text-embedding-3-small",
      "graph_rel_recall@5": 0.62,
      "graph_rel_hit@5": 0.71
    }
  }
}
```

- tier 분포로 어느 단계에서 hit 이 발생하는지 가시화 — exact 가 압도적이면 임베딩이 보조 역할만 한다는 것을 확인 가능. embedding tier 가 많으면 인덱싱이 표기 정규화에 차이가 있음을 시사.

### 5.5 관계(엣지) 매칭

`--score-relations` 활성화 시:
- 검색 결과의 edges: `graph_search_planner._build_subgraph_context` (`:241-288`) 가 사용하는 edges 를 `GraphSearchResult` 에 노출 추가 (현재 텍스트 포맷팅에만 사용).
  - 신규 필드: `GraphSearchResult.relations: list[GraphRelationRef]`.
  - `AssembledContext.retrieved_graph_relations` 패스스루.
- 매칭: tier1 (exact `(source_name.lower(), target_name.lower(), relation_type)`) → tier4 (relation_description 임베딩 cosine ≥ τ). tier2/3 는 관계에 대해 skip (간결성 우선).
- 메트릭: 같은 `metrics.recall_at_k` 등 재사용. relation 의 식별 키는 `(source_name.lower(), target_name.lower(), relation_type)`.

---

## 6. CLI 변경

### 6.1 `build_synthetic_gold_set.py`

| 옵션 | 기본값 | 동작 |
|------|--------|------|
| `--embed-graph-evidence` | `True` | graph 항목의 `description_embedding` 을 생성 시 1회 계산. False 면 임베딩 미보유 → 평가 시 lazy. |
| `--enrich-existing PATH` | 미사용 (선택) | 기존 골드셋의 graph 항목에 description/aliases/embedding 만 추가 합성. 질문 재생성 안 함. (도입은 후속 PR 권장) |

기존 옵션 (`--source-types`, `--seed`, `--n-gold-sets`, ...) 모두 유지.

### 6.2 `eval_search.py`

| 옵션 | 기본값 | 동작 |
|------|--------|------|
| `--graph-match-threshold` | `0.78` | tiered matching 의 T4 임계값. |
| `--graph-match-strict` | `False` | T4 임베딩 단계를 skip (1차 동작 호환). |
| `--score-relations` | `False` | 관계 채점 활성화. 골드셋에 `relevant_graph_relations` 가 있어야 의미 있음. |

기존 옵션 (`--judge`, `--source-types`, `--metrics-by-mode`, ...) 모두 유지.

---

## 7. 마이그레이션 / Backward-compat 시나리오

| 시나리오 | 동작 |
|---------|------|
| 1차 골드셋 (description/aliases/embedding 없음) + 새 코드 + 기본 옵션 | T1 + T3 만 발동. T2/T4 자연 skip. 동작은 1차 + name 정규화 추가 → **모든 1차 메트릭과 같거나 향상**. |
| 1차 골드셋 + `--graph-match-strict` | T1 만 발동 (T3 도 skip — strict 의 의미를 "정확 비교만"으로 정의). 1차와 **정확히 동일** 동작. |
| 1차 골드셋 + `--score-relations` | `relevant_graph_relations` 가 비어 있어 관계 메트릭은 계산되지 않음 (NaN/null). 일반 graph 메트릭은 정상. |
| 2차 골드셋 (확장 필드 포함) + 1차 코드 | `GraphEntityRef.from_dict` 가 알 수 없는 키를 무시 (현재 코드도 그렇게 동작 — `gold_set.py:49-51`). 골드셋 로드 OK, 매칭은 1차 exact. |
| 2차 골드셋 + 새 코드 + 다른 τ | metadata 의 default τ 와 다른 값 사용 → 보고에 `graph_match_threshold_used` 명시. |
| 2차 골드셋 + 새 코드 + 다른 임베딩 모델 | golden 의 `description_embedding` 은 이전 모델로 계산됨 → 새 모델로 재계산? **결정**: 새 모델 사용 시 골드셋 임베딩을 재계산하지 않고 **그대로 사용** (재현성 우선). 검색 측 임베딩만 새 모델로 (LRU 캐시). → 모델 mismatch 경고 stderr 로 출력. |

**마이그레이션 스크립트는 불필요**. 1차 YAML 은 새 코드에서 그대로 로드되며 메트릭은 동등 이상.

---

## 8. 테스트 전략

신규 unit test 목록 (모두 `tests/test_eval/` 하위):

### 8.1 `tests/test_eval/test_gold_set.py` (수정)
- `test_graph_entity_ref_roundtrip_with_aliases_and_description` — 확장 필드 round-trip.
- `test_graph_entity_ref_backward_compat_minimal` — `{name, type}` 만으로 로드, 기본값 확인.
- `test_graph_relation_ref_roundtrip` (관계 채점 활성화 시).
- `test_gold_item_optional_fields_emit_only_when_set` — 빈 값은 YAML 에 emit 안 됨.

### 8.2 `tests/test_eval/test_graph_matching.py` (신규)
- 시나리오 매트릭스 (analyst §3 의 A~F 를 그대로 테스트 함수로):

| 테스트 | 입력 | 기대 결과 |
|--------|------|----------|
| `test_scenario_a_new_type` | golden type 그대로, retrieved 에 새 type 노드 추가 | T1 hit, recall 유지, precision 하락 — 보고만 |
| `test_scenario_b_type_renamed` | golden type=`system`, retrieved type=`service`, 같은 의미 | T1~T3 miss, T4 hit (score=cosine) |
| `test_scenario_c1_whitespace_diff` | `"인증 서비스"` vs `"인증서비스"` | T3 hit (score=0.9) |
| `test_scenario_c2_case_diff` | `"AuthService"` vs `"authservice"` | T1 hit (lower 적용) |
| `test_scenario_c3_synonym` | `"인증 서비스"` vs `"Auth Service"` aliases 보유 | T2 hit (alias) |
| `test_scenario_c3_synonym_no_alias` | aliases 없음, description 보유 | T4 hit |
| `test_scenario_d_merged_node` | 병합으로 canonical 변경 | T2 alias 또는 T4 hit |
| `test_scenario_e_relation_renamed` | `--score-relations` 활성, relation_type 변경 | rel T4 hit |
| `test_scenario_f_new_neighbors` | retrieved 확장 (이웃 추가) | recall 유지, precision 하락 보고 |

### 8.3 `tests/test_eval/test_eval_search.py` (수정 또는 신규)
- `test_match_report_records_tier_counts` — graph_match_tiers 보고 키 존재.
- `test_strict_mode_skips_embedding_tier` — `--graph-match-strict` 시 T4 발동 안 함.
- `test_threshold_override_propagates_to_report` — CLI τ 변경이 보고에 반영.
- `test_metrics_by_mode_graph_keys` — 신규 시그널 모두 존재.

### 8.4 `tests/test_eval/test_synth.py` (수정)
- `test_generate_graph_questions_emits_evidence_and_aliases` — LLM 출력 확장 파싱.
- `test_generate_graph_questions_graceful_on_minimal_output` — 1차 호환 (LLM 이 question 만 반환해도 깨지지 않음).

### 8.5 `tests/test_eval/test_build_synthetic_gold_set.py` (수정)
- `test_make_graph_gold_item_with_embedding` — `_make_graph_gold_item` 에 description_embedding 채워짐.
- `test_make_graph_gold_item_without_embedding_when_flag_off` — `--embed-graph-evidence=False`.

### 8.6 Mock 패턴

**임베딩 mock** (결정론적):
```python
class FakeEmbedder:
    """입력 텍스트를 hash 해서 가짜 벡터를 만든다. 같은 텍스트→같은 벡터, 다른 의미→다른 벡터."""
    def embed_query(self, text: str) -> list[float]:
        h = hashlib.sha256(text.encode()).digest()
        return [b / 255.0 for b in h[:32]]  # 32-d, 결정론
```
- 의미적 유사성을 테스트할 때는 misc fixture 로 "이 두 문자열은 cosine ≥ 0.8" 임을 사전 보장하는 매핑 테이블 사용 (예: 사전 계산된 32-d 벡터 hardcoded).

**LLM mock** (`StubLLM` 확장):
- `generate_graph_questions` 호출 시 evidence_description / entity_aliases / relation 포함한 JSON 을 반환하는 stub.

### 8.7 회귀 방지

- 1차 골드셋 YAML 픽스처(`tests/fixtures/gold_set_v1.yaml`)를 추가하여 "1차 골드셋이 새 코드에서 로드 + 평가 시 1차 메트릭과 동등" 을 회귀 테스트.

---

## 9. 변경 파일 목록 표

| 파일 | 변경 종류 | 핵심 변경 내용 |
|------|---------|--------------|
| `src/context_loop/eval/gold_set.py` | 수정 | `GraphEntityRef` 에 `aliases`, `description`, `description_embedding` 추가. `GraphRelationRef` 신규. `GoldItem.relevant_graph_relations` 추가. `to_dict`/`from_dict` 일관. |
| `src/context_loop/eval/synth.py` | 수정 | `generate_graph_questions` 의 LLM 프롬프트·출력 스키마 확장 (evidence_description, entity_aliases, relation). graceful fallback. |
| `src/context_loop/eval/graph_match.py` | **신규** | tiered matching 알고리즘 (`run_tiered_matching`, `_normalize`, `MatchResult`, `MatchReport`). LRU 캐시 임베딩 wrapper. |
| `scripts/build_synthetic_gold_set.py` | 수정 | `_make_graph_gold_item` 에 description/aliases/embedding 채움. `embedding_client` 주입. `--embed-graph-evidence` CLI. metadata 에 임베딩 모델 ID + 기본 τ 기록. (`--enrich-existing` 는 후속 PR) |
| `scripts/eval_search.py` | 수정 | `evaluate_one` 에서 tiered matching 호출. 새 보고 시그널 (`graph_match_tiers`, `graph_match_score_*`). `--graph-match-threshold`, `--graph-match-strict`, `--score-relations` CLI. |
| `src/context_loop/mcp/graph_search_planner.py` | 수정 (소폭) | `GraphSearchResult.entities` 의 `GraphEntityRef` 에 `description` 채움 (node properties JSON 의 description 추출). `--score-relations` 시 `relations` 도 노출. |
| `src/context_loop/mcp/context_assembler.py` | 수정 (소폭) | `assemble_context_with_sources` 가 `retrieved_graph_relations` 도 패스스루 (옵션). |
| `tests/test_eval/test_gold_set.py` | 수정 | 확장 필드 round-trip + backward-compat 테스트. |
| `tests/test_eval/test_graph_matching.py` | **신규** | 시나리오 A~F 매트릭스. mock 임베딩으로 결정론. |
| `tests/test_eval/test_eval_search.py` | 수정 | tier 카운트, strict/threshold 옵션, metrics_by_mode 키. |
| `tests/test_eval/test_synth.py` | 수정 | 확장 LLM 출력 파싱 + minimal 출력 호환. |
| `tests/test_eval/test_build_synthetic_gold_set.py` | 수정 | `_make_graph_gold_item` 임베딩 채움. |
| `tests/fixtures/gold_set_v1.yaml` | **신규 (선택)** | 1차 호환 회귀용 픽스처. |

**합계**: 수정 9 + 신규 3 = **12 파일**.

---

## 10. 위험 / 미해결 (implementer 가 마주칠 결정점)

### 10.1 명시된 결정 — implementer 는 그대로 따른다
- τ 기본값 0.78 (CLI override 가능).
- 임베딩 YAML inline (별도 파일 X).
- 1차 골드셋 로드 시 description_embedding 없으면 평가 시 lazy 계산 + LRU 캐시.
- `--graph-match-strict` 는 T3 도 skip (정확히 1차 동작 재현).

### 10.2 implementer 가 자체 결정 가능 — 영향 미미
- `_normalize` 의 punctuation 집합 (`[\s\-\_\.]+`) 을 더 넓힐지(`[\s\W]+`)·좁힐지: 도메인 예시 보고 미세 조정. 테스트로 안전망.
- LRU 캐시 크기: 골드셋·검색 entity 수 × 5 정도. 기본값 1024 충분.
- `aembed_documents` batch 사이즈: 32~64. provider 기본값 그대로 OK.

### 10.3 미해결 — 사용자 / 후속 PR 결정
- **`--enrich-existing` 모드의 우선순위**: 1차 PR 에는 빼고 후속 PR. 사용자가 1차 골드셋을 그대로 평가하고 싶다면 strict 모드로 충분.
- **임베딩 모델 변경 시 골드셋 재계산 정책**: 현재 "재계산 안 함 + 경고" — 운영 사용 후 노이즈가 크면 자동 재계산 옵션 추가 검토.
- **관계 채점 (`--score-relations`) 의 default 활성화 시점**: 1차 PR 에서는 default OFF. 2~3주 운영 후 안정성 검증 → ON 전환 검토.
- **시나리오 F (precision 자연 하락)**: 보조 메트릭으로 강등하되, 만약 사용자가 "precision-first 보고" 를 요청하면 별도 메트릭 변형 도입 — 현재는 YAGNI.

### 10.4 코드 측 잠재 위험
- `GraphSearchResult.entities` 에 description 추가 시 기존 호출자(예: 채팅 UI, MCP 서버) 에 영향 — `GraphEntityRef` 의 description 은 optional 이므로 영향 없음. 단 to_dict 직렬화 시 description 이 길어지면 응답 페이로드 증가 → 모니터링 권고.
- 임베딩 비교에서 검색 측 description 이 빈 경우 fallback to name → 짧은 name (예: `"a"`) 으로 임베딩하면 의미 매칭 신뢰도 낮음. T4 발동 빈도가 높으면 검색 측 description 채움 비율을 모니터링.

---

## 한 문장 요약

**핵심 결정 3개** — (1) Tiered matching 채택 (exact→alias→normalize→embedding, τ=0.78, type-agnostic at T4 로 시나리오 B 흡수); (2) `GraphEntityRef` 에 aliases/description/description_embedding 추가 (별도 클래스 분리 X, 1차 backward-compat 자연 유지); (3) 관계 채점은 옵셔널 (`GraphRelationRef` + `--score-relations`, default OFF). **변경 파일 12개** (수정 9 + 신규 3). **새 메트릭 4개** (`graph_match_tiers`, `graph_match_score_avg/min/max`, 관계 활성화 시 `graph_rel_*` 4개 추가).
