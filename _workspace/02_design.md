# 02_design — 골드셋 cross-doc / answer-equivalence 설계

작성: 2026-05-22 (designer, eval-gold-set-improvement)
입력: `00_requirements.md` (R1/R2/R3 + NFR), `01_analysis.md` (analyst — 영향 파일/라인, 미해결 7)
대상 HEAD: PR#64 병합 후

> 본 문서는 implementer 작업 지시서다. 파일:라인 / 함수 시그니처 / dataclass 필드 단위로
> 그대로 구현 가능하게 작성했다. 코드는 본 단계에서 수정하지 않는다 (예시 스니펫만).

---

## 0. 요약 (TL;DR)

- **R1**: 이미 충족 (analyst §2). 변경 없음.
- **R3 (동치 집합)**: `GoldItem` 에 옵셔널 필드 `relevant_doc_groups: list[list[int]]` 추가.
  의미 = "그룹 내 OR, 그룹 간 AND". 평탄 `relevant_doc_ids` 는 보존 (하위호환 + CSV +
  외부 진단 스크립트). 채점은 **`eval_search.py` 전처리에서 동치 그룹을 대표 1개로 축약**한
  뒤 기존 `metrics.py` 재사용 (analyst §8-Q3 의 (b)안 — test_metrics 무영향).
- **graph 다중-doc (`build:997`)**: `relevant_doc_groups=[list(document_ids)]` 로 **자동
  변환**(1개 OR 그룹). 평탄 `relevant_doc_ids` 도 함께 채워 하위호환 유지. → analyst §2.1 의
  conjunction 오채점 해소.
- **R2 (cross-doc)**: 그래프 엣지 중 **서로 다른 문서를 잇는 엣지**를 결정론적으로 추출해
  질의 시드로 사용. LLM 은 질문 문장 생성에만 사용(기존 generator/judge 재사용). 식별은
  `GoldItem.cross_document: bool` 플래그 + `relevant_doc_groups` 의 **AND 다중그룹**으로 표현.
- **마이그레이션**: 1회성 스크립트 불필요. `from_dict` 기본값 폴백으로 기존 YAML 무수정 로드.

**사용자 결정 필요 항목** (§9에 상세): Q1(스키마 형태), Q2(graph 자동변환 → 기존 골드셋
재생성 여부), Q5(R2 LLM 의존 정책), Q6(JSON vs YAML 표기), Q7(cross-doc 식별 방식).

---

## 1. 설계 목표 (요구사항 → 결정 추적)

| 요구사항 | 상태 | 설계 결정 |
|---|---|---|
| R1 저장 문서 기준 질의 | 충족 | 변경 없음 (D0) |
| R2 cross-document 생성/식별/집계 | 신규 | D6(생성), D7(식별 필드), D8(분리 집계) |
| R3 answer equivalence set 스키마 | 신규 | D1(스키마), D2(graph 자동변환) |
| R3 동치-aware 채점 | 신규 | D3(전처리 축약), D4(메트릭 일관성) |
| NFR 하위호환 | — | D5(폴백) |
| NFR LLM 의존 명시 | — | D6 (결정론 시드 + LLM 문장화) |

---

## 2. R3 — 동치 집합 스키마 설계 (D1)

### 2.1 스키마 결정: `relevant_doc_groups: list[list[int]]`

**`GoldItem` 에 추가할 필드** (`src/context_loop/eval/gold_set.py:171` 아래):

```python
@dataclass
class GoldItem:
    id: str
    query: str
    relevant_doc_ids: list[int] = field(default_factory=list)
    # NEW (R3) — 정답 문서 동치 집합. 의미: "각 inner list 내부는 OR(아무거나 1개면
    # 그 그룹 만족), 그룹 간은 AND(모든 그룹이 만족돼야 완전 정답)".
    #   - 빈 리스트([])  → R3 비활성. 기존 평탄 채점으로 폴백 (relevant_doc_ids 사용).
    #   - [[3, 5]]       → "3 또는 5 중 하나" (단일 OR 그룹) — graph 다중-doc 의 정석.
    #   - [[3, 5], [9]]  → "(3 또는 5) AND (9)" — cross-doc (R2) 의 정석.
    relevant_doc_groups: list[list[int]] = field(default_factory=list)
    # NEW (R2) — cross-document 식별 플래그 (분리 집계용). §6 참조.
    cross_document: bool = False
    relevant_graph_entities: list[GraphEntityRef] = field(default_factory=list)
    relevant_graph_relations: list[GraphRelationRef] = field(default_factory=list)
    # ... 기존 필드 동일 ...
```

**왜 `list[list[int]]` 인가** (analyst §8-Q1 결정):
- OR(동치) 와 AND(cross-doc) 를 **한 구조로 동시 표현**한다. inner=OR, outer=AND 는
  CNF(논리곱 표준형)와 동일하여 채점 알고리즘이 단순하다 (§3).
- `{any_of: [...]}` dict 그룹 객체 대안보다 YAML 가독성·라운드트립이 단순하고, 기존
  `GraphEntityRef.to_dict` omit-if-empty 패턴과 일관된다.
- 트레이드오프: dict 형식이면 그룹별 라벨/난이도 부여가 쉽지만 YAGNI — 현 요구사항은
  그룹 메타데이터를 요구하지 않는다.

**불변식 (정규화 규칙)**:
1. `relevant_doc_groups` 가 비었으면 R3 비활성 → `relevant_doc_ids` 기반 기존 채점.
2. `relevant_doc_groups` 가 있으면 그것이 **정답의 정본(canonical)**. 단, 하위호환·CSV·외부
   진단을 위해 `relevant_doc_ids` 도 항상 **평탄화하여 동시 유지**한다:
   `relevant_doc_ids = sorted(set(flatten(relevant_doc_groups)))`. 생성 시 둘 다 채운다.
3. inner list 의 빈 리스트(`[]`)는 무시(드롭). outer 가 전부 비면 그룹 없음으로 간주.
4. 중복 제거: 각 inner list 내부 `int` 중복 제거. 그룹 간 중복은 허용(서로 다른 AND 조건이
   같은 doc 를 포함할 수 있음 — 단 §3 채점에서 그룹별 독립 평가하므로 무해).

### 2.2 `to_dict` / `from_dict` 변경 (라운드트립 무손실)

`gold_set.py:185-215` `to_dict` 에 **omit-if-empty** 로 추가:

```python
# to_dict 내부, relevant_doc_ids 직후
if self.relevant_doc_groups:
    out["relevant_doc_groups"] = [list(g) for g in self.relevant_doc_groups]
if self.cross_document:
    out["cross_document"] = True
```

`gold_set.py:217-251` `from_dict` 에 **기본값 폴백** 으로 추가:

```python
raw_groups = d.get("relevant_doc_groups") or []
groups: list[list[int]] = []
for g in raw_groups:
    if isinstance(g, list):
        inner = [int(x) for x in g]
        # 그룹 내 중복 제거(순서 보존)
        seen: set[int] = set()
        dedup = [x for x in inner if not (x in seen or seen.add(x))]
        if dedup:
            groups.append(dedup)
# ... cls(...) 호출 인자에 추가 ...
relevant_doc_groups=groups,
cross_document=bool(d.get("cross_document", False)),
```

**docstring 갱신**: `gold_set.py:1-39` 모듈 docstring 의 YAML 예시에 `relevant_doc_groups`,
`cross_document` 추가. `GoldItem` docstring(`158-167`)에 동치/AND 의미 1줄.

---

## 3. R3 — 채점 로직 설계 (D3, D4)

### 3.1 핵심 결정: `eval_search.py` 전처리 축약 (analyst §8-Q3 → (b)안 채택)

**근거**: `metrics.py` 함수를 동치-aware 로 직접 고치면 `test_metrics.py:20-114` 23개 테스트가
전부 영향. (b)안 = "동치 그룹을 대표 doc 1개로 축약한 retrieved/relevant 를 만들어 **기존
메트릭 함수를 그대로** 호출" 은 `metrics.py` 무변경 → 회귀 리스크 0.

### 3.2 신규 헬퍼 (위치: `eval_search.py`, `_classify_mode` 근처 ~`560` 부근에 추가)

```python
def _reduce_equivalence(
    retrieved_doc_ids: list[int],
    groups: list[list[int]],
) -> tuple[list[int], set[int]]:
    """동치 그룹을 '대표 1개' 단위로 축약하여 (retrieved', relevant') 반환.

    각 동치 그룹 = 정답 1개 단위. 그룹의 대표 ID 는 'retrieved 안에서 가장 먼저
    등장하는 그룹 멤버'. retrieved 에 그룹 멤버가 없으면 그룹의 첫 원소(정렬된)를
    대표로 둔다(=miss 로 카운트되어 recall 분모에 잡힘).

    반환된 retrieved' 는 원래 retrieved 의 순서를 보존하되, 한 그룹의 멤버는
    '첫 등장 1회'만 남기고(중복 정답 캡), relevant' 는 그룹별 대표 set.
    그룹 외 doc(정답 아님)은 retrieved' 에 그대로 보존 → precision 분모 정확.
    """
    # 1. 그룹별 대표 결정
    rep_of_group: list[int] = []
    member_to_rep: dict[int, int] = {}
    rank = {d: i for i, d in enumerate(retrieved_doc_ids)}
    for g in groups:
        # retrieved 안 멤버 중 최소 rank, 없으면 min(g) 를 대표로
        present = [d for d in g if d in rank]
        rep = min(present, key=lambda d: rank[d]) if present else min(g)
        rep_of_group.append(rep)
        for d in g:
            member_to_rep[d] = rep
    relevant_reduced = set(rep_of_group)

    # 2. retrieved 축약: 그룹 멤버는 대표로 치환 + 그룹당 첫 등장만 유지
    seen_reps: set[int] = set()
    retrieved_reduced: list[int] = []
    for d in retrieved_doc_ids:
        if d in member_to_rep:
            rep = member_to_rep[d]
            if rep in seen_reps:
                continue          # 같은 그룹 두 번째 출현 → drop (중복 정답 캡)
            seen_reps.add(rep)
            retrieved_reduced.append(rep)
        else:
            retrieved_reduced.append(d)   # 정답 아닌 doc → 보존(precision 분모)
    return retrieved_reduced, relevant_reduced
```

### 3.3 `evaluate_one` 진입점 변경 (`eval_search.py:386`)

```python
# 기존:
# relevant = set(item.relevant_doc_ids)

# 변경:
if item.relevant_doc_groups:
    scoring_retrieved, relevant = _reduce_equivalence(
        retrieved_doc_ids, item.relevant_doc_groups,
    )
else:
    # R3 비활성 — 기존 평탄 경로 (하위호환)
    scoring_retrieved, relevant = retrieved_doc_ids, set(item.relevant_doc_ids)
```

그리고 메트릭 호출(`eval_search.py:409-413`)의 첫 인자를 `retrieved_doc_ids` →
`scoring_retrieved` 로 교체:

```python
f"recall@{top_k}":   recall_at_k(scoring_retrieved, relevant, top_k),
f"precision@{top_k}": precision_at_k(scoring_retrieved, relevant, top_k),
f"hit@{top_k}":       int(hit_at_k(scoring_retrieved, relevant, top_k)),
f"ndcg@{top_k}":      ndcg_at_k(scoring_retrieved, relevant, top_k),
"mrr":                mrr(scoring_retrieved, relevant),
```

**CSV/디버그 컬럼은 평탄 유지** (analyst §5.3 — `diagnose_r3_effect.py:96` 호환):
- `eval_search.py:405` `"retrieved_doc_ids": retrieved_doc_ids[:top_k]` — **원본** 유지(축약 X).
- `eval_search.py:407` `"relevant_doc_ids": sorted(relevant)` → `sorted(set(item.relevant_doc_ids))`
  로 **평탄 원본** 유지(축약된 대표 set 아님). 외부 진단 스크립트가 평탄 int 리스트를 기대.
- 신규 디버그 컬럼 추가(선택): `"relevant_doc_groups": item.relevant_doc_groups` — CSV 에선
  `write_csv` 가 list 를 콤마결합(`eval_search.py:638`)하므로 중첩 list 는 `str()` 처리됨.
  → 권장: `"n_answer_units": len(item.relevant_doc_groups or item.relevant_doc_ids)` 같은
  스칼라로 기록(중첩 list 의 CSV 직렬화 모호성 회피).

### 3.4 각 메트릭이 동치 의미로 정확히 동작함을 검증 (D4)

축약 후 기존 `metrics.py` 가 자동으로 동치 의미를 만족한다:

| 메트릭 | 동치 의미 | 축약으로 충족되는 이유 |
|---|---|---|
| **recall@k** | 동치 집합은 1개 정답 단위로 분모 | `relevant_reduced` 는 그룹당 대표 1개 → `len(rel_set)` = 그룹 수. analyst §4.2 의 분모 왜곡 해소 |
| **precision@k** | 같은 그룹 2개가 top-k 에 들어도 hits=1 캡 | 축약 시 그룹당 첫 등장만 남김(§3.2 step2) → hits 자동 캡. analyst §8-Q4 = **캡 채택** |
| **hit@k** | 그룹 중 하나라도 있으면 True | 대표가 top-k 안에 있으면 True. 자연 호환 |
| **mrr** | 그룹당 첫 매칭 위치 | 대표 = retrieved 내 최소 rank 멤버 → 그룹 첫 매칭과 동일 rank |
| **ndcg@k** | idcg 분모가 그룹 수 기준 | `min(len(rel_set), k)` 의 `rel_set` 이 대표 set → idcg 정상 |

**미묘점 — mrr 다중그룹**: AND 다중그룹(cross-doc)에서 mrr 은 "여러 그룹의 첫 매칭들"이
retrieved' 에 대표로 흩어져 있고, `metrics.mrr` 은 그 중 **가장 빠른 1개**의 역수를 준다.
이는 "최선의 정답 위치"로 cross-doc 에서도 합리적 정의. 별도 변경 불필요.

---

## 4. graph 다중-doc 자동 변환 (D2)

`build_synthetic_gold_set.py:994-1005` `_make_graph_gold_item` 의 emit 변경:

```python
# 기존:
#   relevant_doc_ids=list(sg["document_ids"]),
# 변경:
doc_ids = list(sg["document_ids"])
return GoldItem(
    id="",
    query=gq.query,
    relevant_doc_ids=sorted(set(doc_ids)),          # 평탄 — 하위호환/CSV용
    relevant_doc_groups=[sorted(set(doc_ids))] if len(doc_ids) > 1 else [],
    # ↑ 같은 엔티티가 N개 문서에 등장 = "그 중 어디서든 찾으면 OK" = 단일 OR 그룹.
    #   doc 가 1개뿐이면 그룹 불필요(평탄 채점과 동일) → 빈 그룹으로 둠.
    relevant_graph_entities=[entity_ref],
    relevant_graph_relations=relations,
    source_type=sg["source_type"],
    source_document_id=sg["primary_document_id"],
    source_text_anchor=None,
    difficulty=gq.difficulty,
    synthesized=True,
)
```

**효과**: analyst §2.1 의 conjunction 오채점이 OR 로 교정. 신규 graph 골드셋은 자동으로 올바른
recall. **기존 graph 골드셋**(평탄 다중 doc)은 `relevant_doc_groups` 가 없어 여전히 conjunction
으로 채점됨 → **사용자 결정 필요 (§9 Q2)**: 재생성할지, 또는 로드시 자동 승격할지.

> 권장 default (사용자 미응답 시): **재생성 권장, 자동 승격은 하지 않음**. 자동 승격은
> "graph 모드의 평탄 다중 doc 은 항상 OR" 라는 가정을 로더에 박는 것인데, 수동 작성된
> chunk 골드셋의 평탄 다중 doc 은 AND 의도일 수 있어 로더 레벨 일괄 변환은 위험. 변환은
> 생성기(`_make_graph_gold_item`)에서만 수행.

---

## 5. R2 — cross-document 생성 설계 (D6)

### 5.1 LLM 의존 정책 (NFR 명시 — analyst §8-Q5)

**결정: 하이브리드. 시드 선택은 결정론, 질문 문장화만 LLM.**
- **결정론 부분**: "서로 다른 문서를 잇는 그래프 엣지"를 DB 에서 결정적으로 추출 →
  cross-doc 질의의 **씨앗(seed pair)** 으로 사용. 같은 시드+seed_base 면 같은 후보.
- **LLM 부분**: 씨앗(엔티티 A@docA — relation — 엔티티 B@docB)을 기존 `generate_graph_questions`
  계열로 자연어 질문화. generator/judge 분리(self-bias 방지) 그대로 유지.
- **근거**: 순수 결정론 질문은 템플릿 티가 나서 judge 의 not-generic 게이트를 통과 못 하거나
  검색 난이도가 비현실적. 순수 LLM(두 문서 본문을 통째로 주고 cross-doc 질문 요청)은
  비용↑·재현성↓. 하이브리드가 NFR(재현성) 와 품질의 균형.

### 5.2 cross-doc 씨앗의 결정론적 추출 — 신규 로더

`build_synthetic_gold_set.py` 에 신규 함수 추가 (`load_candidate_subgraphs` 직후 ~`300`):

```python
async def load_cross_doc_seeds(
    meta_store: MetadataStore,
    graph_store: GraphStore,
    *,
    source_types: list[str] | None,
    max_seeds: int | None = None,
) -> list[dict[str, Any]]:
    """서로 다른 문서를 잇는 엣지에서 cross-doc 질의 씨앗을 결정론적으로 추출.

    각 씨앗 dict::

        {
            "source_entity": {"name": str, "type": str, "doc_id": int},
            "target_entity": {"name": str, "type": str, "doc_id": int},
            "relation_type": str,
            "document_ids": [src_doc_id, tgt_doc_id],   # AND 그룹의 재료
            "source_type": str,                          # 대표 source_type
            "snippet": str,                              # LLM 입력 (양쪽 엔티티+관계)
        }

    'cross-doc' 정의: 엣지의 source 노드 소유 문서집합과 target 노드 소유 문서집합이
    서로소(겹치지 않음)인 엣지. → 한 문서만 봐서는 양쪽 엔티티를 모두 알 수 없음.
    """
```

**알고리즘 (결정론)**:
1. `nodes = await meta_store.get_all_graph_nodes()`; node_id→doc_ids 는
   `get_node_document_ids` (메타스토어, `metadata_store.py:486`).
2. 전체 엣지 순회: NetworkX 엣지는 `document_id` 속성을 보유(`graph_store.py:126`). 단,
   엣지 단위 doc 보다 **노드 소유 문서 기준**으로 cross-doc 판정한다 (더 견고):
   각 엣지 `(u, v)` 에 대해 `docs_u = doc_ids(u)`, `docs_v = doc_ids(v)`.
3. `if docs_u and docs_v and docs_u.isdisjoint(docs_v):` → cross-doc 후보.
   대표 doc: `src_doc = min(docs_u)`, `tgt_doc = min(docs_v)` (결정성).
4. source_type 필터: `docs_u ∪ docs_v` 의 소유 문서 중 하나라도 화이트리스트면 통과
   (`load_candidate_subgraphs:236` 와 동일 정책).
5. snippet: `build_subgraph_snippet` 재사용 또는 신규 `build_cross_doc_snippet(src, rel, tgt)` —
   양쪽 엔티티 이름/타입/관계를 한 블록으로. (구현 시 §5.4 프롬프트 참조)
6. **결정론적 정렬**: `(src_doc, tgt_doc, source_name, target_name, relation_type)` 키로 sort.
   `max_seeds` 로 상한(샘플은 `stratified_sample` 또는 단순 head — seed 고정).

> graph_store 에 노드별 소유문서를 한 번에 주는 메서드가 없으면, 루프 전에
> `node_docs = {nid: set(await meta_store.get_node_document_ids(nid)) for nid in node_ids}`
> 로 1회 캐시(N 쿼리). 신규 store 메서드 추가는 YAGNI — 기존 `get_node_document_ids` 재사용.

### 5.3 cross-doc GoldItem emit (신규 `_make_cross_doc_gold_item`)

```python
def _make_cross_doc_gold_item(seed: dict, gq: GeneratedGraphQuestion) -> GoldItem:
    src_doc = seed["source_entity"]["doc_id"]
    tgt_doc = seed["target_entity"]["doc_id"]
    return GoldItem(
        id="",
        query=gq.query,
        relevant_doc_ids=sorted({src_doc, tgt_doc}),     # 평탄(하위호환)
        # 두 문서 '모두' 봐야 답 가능 → AND 다중그룹. 각 그룹은 단일 doc(OR 자명).
        relevant_doc_groups=[[src_doc], [tgt_doc]],
        cross_document=True,                             # R2 식별 플래그
        relevant_graph_entities=[
            GraphEntityRef(name=seed["source_entity"]["name"],
                           type=seed["source_entity"]["type"]),
            GraphEntityRef(name=seed["target_entity"]["name"],
                           type=seed["target_entity"]["type"]),
        ],
        source_type=seed["source_type"],
        source_document_id=src_doc,
        difficulty=gq.difficulty,
        synthesized=True,
        notes="cross_document",
    )
```

**채점 의미 확인**: `relevant_doc_groups=[[A],[B]]` → §3 축약 후 `relevant={A,B}` (2개 단위),
"A 도 B 도 찾아야 recall=1.0". cross-doc 의 정확한 conjunction 의미 = ✓. 그래프 탐색이
A→B 도달로 B 를 끌어오면 벡터 단독 대비 recall 이득이 측정됨 (PR#64 와 직결, analyst §3).

### 5.4 생성 파이프라인 통합 + CLI

- 신규 모드 함수 `_run_cross_doc_mode(...)` — `_run_graph_mode`(~`740`)를 패턴 복제. 씨앗별로
  `generate_cross_doc_questions(seed, ...)` 호출. **재사용**: 기존 generator/judge/seed 전파
  (`sg_seed = generator_seed_base + idx`) 로직 그대로.
- 신규 generator 함수 `generate_cross_doc_questions` 는 `synth.py` 의
  `generate_graph_questions`(synth.py:729) 를 복제하되 프롬프트만 교체:
  > "다음 두 엔티티는 서로 다른 문서에 등장하며 관계로 연결돼 있다. 두 문서를 모두 참고해야
  > 답할 수 있는 질문 N개를 생성하라. 한 문서만 봐서는 답이 불완전해야 한다."
  + few-shot 1개. judge 의 answerable/no-leakage/not-generic 게이트는 **그대로 재사용**.
- **CLI 옵션 추가** (`build_synthetic_gold_set.py` argparse):
  - `--enable-cross-doc` (store_true, default False) — `--enable-graph-mode` 와 동급.
  - `--cross-doc-max-seeds INT` (default None=무제한) — 씨앗 상한.
  - `generation_modes` 리스트(`build:487-489`)에 `"cross_doc"` append.
- **metadata 기록** (`build:491-518`): `metadata["cross_doc_enabled"]`,
  `metadata["cross_doc_max_seeds"]`, `metadata["cross_doc_generation"] = "deterministic_seed+llm_phrasing"`
  추가 → 생성 정책 추적성(analyst §5.3).
- 모듈 docstring "사용법" 섹션에 예시 1줄 추가 (도메인 컨벤션):
  `python scripts/build_synthetic_gold_set.py --enable-cross-doc --source-types confluence_mcp git_code ...`

---

## 6. R2 — cross-doc 식별 + 분리 집계 (D7, D8)

### 6.1 식별 필드 (analyst §8-Q7 결정)

**결정: boolean 플래그 `cross_document` + mode 라벨링 둘 다.**
- `GoldItem.cross_document: bool` (§2.1) — 명시적·하위호환(누락=False). 생성기가 cross-doc
  경로에서만 True.
- `_classify_mode`(`eval_search.py:547-560`) 는 chunk/graph/hybrid 유지하되, **`cross_document=True`
  면 `"cross_doc"` 를 우선 반환**하도록 분기 추가:

```python
def _classify_mode(item: GoldItem) -> str:
    if item.cross_document:
        return "cross_doc"
    has_doc = bool(item.relevant_doc_ids)
    has_graph = bool(item.relevant_graph_entities)
    if has_doc and has_graph:
        return "hybrid"
    if has_graph:
        return "graph"
    return "chunk"
```

> mode 확장 vs 별도 플래그를 **둘 다** 둔 이유: 플래그는 정본 식별자(스키마 안정),
> mode="cross_doc" 는 `write_summary` 의 기존 split 인프라(`rows_by_mode`)에 무비용으로 편승.

### 6.2 분리 집계 (`eval_search.py:663-685`)

`rows_by_mode` 딕셔너리에 슬롯 추가:

```python
rows_by_mode: dict[str, list[dict[str, Any]]] = {
    "chunk": [], "graph": [], "hybrid": [], "cross_doc": [],   # NEW
}
```

나머지 집계 루프(`672-685`)는 무변경 — `metrics_by_mode["cross_doc"]` 가 자동 생성됨.
`aggregate` / `aggregate_with_variance` 무변경(analyst §4.2 — 키 추가만으로 동작).

---

## 7. 마이그레이션 전략 (D5)

- **1회성 스크립트 불필요.** 신규 필드 전부 옵셔널 + `from_dict` 기본값 폴백(§2.2). 기존
  `eval/*.yaml` 무수정 로드 (analyst §5.2).
- **누락 필드 처리**: `relevant_doc_groups` 누락 → `[]` → R3 비활성 → `relevant_doc_ids` 평탄
  채점(=과거 동작 정확 보존). `cross_document` 누락 → `False` → mode 분류 과거와 동일.
- **표기 불일치 (analyst §8-Q6)**: 요구사항은 "JSON" 이라 했으나 코드 실측은 **YAML**
  (`gold_set.py:282 yaml.safe_dump`, 확장자 `.yaml`). → **YAML 기준 진행** (사용자 확인 §9 Q6).
  요구사항 본문의 "JSON" 은 표기 오류로 간주.
- **기존 graph 골드셋 재해석 (Q2)**: §4 참조 — 로더 자동 승격 안 함. 재생성 권장.
- **CSV/외부 진단 호환**: CSV `relevant_doc_ids` 컬럼을 평탄 유지(§3.3) → `diagnose_r3_effect.py:96`
  무영향. CSV 직렬화 형태 보존.
- **`GoldSet.metadata` 기록**: cross-doc/equiv 생성 정책을 metadata 에 기록(§5.4) → 재현 추적.

---

## 8. 테스트 전략

**LLM 호출은 전부 mock** (도메인 컨벤션 — 실제 API 호출 금지).

### 8.1 신규/수정 unit test

| 파일 | 테스트 | 시나리오 |
|---|---|---|
| `tests/test_eval/test_gold_set.py` | `test_roundtrip_equivalence_groups` | `relevant_doc_groups=[[3,5],[9]]`, `cross_document=True` → to_dict→from_dict 무손실 |
| `" ` | `test_legacy_yaml_no_groups_loads` | 그룹 키 없는 옛 YAML → groups=[], cross=False |
| `" ` | `test_groups_dedup_and_drop_empty` | `[[3,3,5],[]]` → `[[3,5]]` |
| `tests/test_eval/test_metrics.py` | (무변경 — metrics.py 안 건드림) | 회귀 보장: 기존 23개 그대로 통과 |
| `tests/test_eval/test_equivalence_scoring.py` (신규) | `test_reduce_or_group_recall` | groups=[[3,5]], retrieved=[5] → recall=1.0 (과거엔 0.5) |
| `" ` | `test_reduce_precision_cap` | groups=[[3,5]], retrieved=[3,5] top2 → precision=0.5 (hits 캡=1) |
| `" ` | `test_reduce_and_groups` | groups=[[3],[9]], retrieved=[3] → recall=0.5 |
| `" ` | `test_reduce_mrr_first_member` | groups=[[3,5]], retrieved=[7,5] → mrr=0.5 |
| `" ` | `test_reduce_ndcg_idcg_denom` | groups=[[3,5],[9]] → idcg 가 2단위 기준 |
| `" ` | `test_no_groups_fallback_flat` | groups=[] → 기존 평탄 채점과 bit-identical |
| `tests/test_eval/test_build_synthetic_gold_set.py` | `test_graph_multi_doc_becomes_or_group` | sg.document_ids=[A,B,C] → groups=[[A,B,C]] |
| `" ` | `test_graph_single_doc_no_group` | sg.document_ids=[A] → groups=[] |
| `" ` | `test_load_cross_doc_seeds_disjoint` | 두 노드 소유문서 서로소 엣지만 씨앗으로(mock store) |
| `" ` | `test_cross_doc_seed_deterministic` | 같은 입력 → 같은 정렬·동일 씨앗 리스트 |
| `" ` | `test_make_cross_doc_item_and_groups` | 씨앗 → groups=[[A],[B]], cross_document=True |
| `tests/test_eval/test_synth.py` | `test_generate_cross_doc_questions_prompt` | 프롬프트에 "두 문서 모두" 취지 + judge mock 통과 |

`_classify_mode` 의 cross_doc 분기, `_reduce_equivalence` 순수성(결정론)도 위에 포함.

### 8.2 회귀 방지
- `eval_search.py` 채점 진입 변경(§3.3)이 R3 비활성 항목에서 과거와 **bit-identical** 임을
  `test_no_groups_fallback_flat` 로 고정.
- `write_summary` cross_doc 슬롯 추가가 cross-doc 없는 골드셋에서 빈 슬롯 무시됨을 확인
  (`metrics_by_mode` 에 cross_doc 키 부재 → 기존 출력 동일).

### 8.3 통과 기준
- `pytest tests/test_eval/ -q` 전부 green. `ruff check` / `ruff format --check` 통과.
- 한국어 docstring 유지.

---

## 9. 변경 파일 목록

| 파일 | 변경 종류 | 핵심 변경 |
|---|---|---|
| `src/context_loop/eval/gold_set.py` | 수정 | `GoldItem` 에 `relevant_doc_groups`, `cross_document` 2필드 + to/from_dict + docstring (§2) |
| `src/context_loop/eval/metrics.py` | **무변경** | (b)안 — 전처리 축약으로 재사용 |
| `scripts/eval_search.py` | 수정 | `_reduce_equivalence` 신규(§3.2), `evaluate_one:386,405-413` 채점 진입 변경(§3.3), `_classify_mode` cross_doc 분기(§6.1), `write_summary:663` 슬롯 추가(§6.2) |
| `scripts/build_synthetic_gold_set.py` | 수정 | `_make_graph_gold_item:994` OR 그룹 자동변환(§4), `load_cross_doc_seeds` 신규(§5.2), `_make_cross_doc_gold_item` 신규(§5.3), `_run_cross_doc_mode` 신규(§5.4), CLI/metadata(§5.4) |
| `src/context_loop/eval/synth.py` (또는 build 내부) | 수정 | `generate_cross_doc_questions` 신규(§5.4) — `generate_graph_questions` 복제 + 프롬프트 교체 |
| `tests/test_eval/test_gold_set.py` | 수정 | 라운드트립/폴백 3건 |
| `tests/test_eval/test_equivalence_scoring.py` | 신규 | 동치 채점 7건 |
| `tests/test_eval/test_build_synthetic_gold_set.py` | 수정 | graph 변환 + cross-doc 씨앗/emit 5건 |
| `tests/test_eval/test_synth.py` | 수정 | cross-doc 프롬프트 1건 |

**diagnose_r3_effect.py / compare_runs.py**: 변경 불필요 (CSV 평탄 컬럼 보존 — analyst §0 의
이름 충돌 R3 와 무관, §5.3 호환 확인됨).

---

## 10. 위험 / 미해결 (analyst 7개 질문에 대한 설계 결정)

| # | analyst 질문 | 설계 결정 | 상태 |
|---|---|---|---|
| Q1 | 스키마 형태 (list[list] vs dict 그룹) | `list[list[int]]` (inner OR, outer AND). CNF 단일 구조로 R2/R3 동시 표현(§2.1) | **결정** |
| Q2 | graph 다중-doc 자동변환 + 기존 골드셋 | 생성기에서만 OR 그룹화(§4). 로더 자동승격 안 함. 기존 graph 골드셋 **재생성 권장** | **사용자 결정 필요** (재생성 vs 현행 유지) |
| Q3 | 메트릭 구현 위치 | `eval_search.py` 전처리 축약 (b)안 — metrics.py 무변경, test_metrics 무영향(§3.1) | **결정** |
| Q4 | precision 동치 중복 캡 | **캡 채택** — 그룹당 첫 등장만 유지(§3.2 step2, §3.4). recall 과 일관 | **결정** |
| Q5 | R2 LLM 의존 | **하이브리드** — 결정론 씨앗 추출 + LLM 문장화. 재현성·품질 균형(§5.1) | **결정** (정책 채택 — 사용자 비용 우려 시 §9 Q5 확인) |
| Q6 | "JSON" vs "YAML" | 코드 실측 **YAML** 기준 진행. 요구사항 "JSON" 은 표기 오류로 간주(§7) | **사용자 결정 필요** (확인용 — 이견 없으면 YAML) |
| Q7 | cross-doc 식별 (mode vs 플래그) | **둘 다** — `cross_document` 플래그(정본) + mode="cross_doc"(집계 편승)(§6.1) | **결정** |

### 추가 위험 (구현 중 주의)
- **R-1 cross-doc 정의의 견고성**: "노드 소유 문서 서로소" 판정(§5.2 step3)은 같은 엔티티가
  양쪽 문서에 모두 등장하면 cross-doc 아님으로 떨어진다(의도된 동작). 후보가 너무 적으면
  `min_neighbors` 류 완화가 아니라 데이터(인덱싱된 그래프) 문제 — 생성 로그에 후보 0건 경고.
- **R-2 mrr 다중그룹 의미**: cross-doc(AND) 에서 mrr 은 "가장 빠른 정답 1개" 위치(§3.4 미묘점).
  recall 과 다른 관점이지만 표준 정의 — 문서화로 충분, 코드 변경 없음.
- **R-3 graph_*-level 채점에 R3 미적용**: 본 설계의 동치 축약은 **doc-level 만**. graph entity
  채점(`eval_search.py:416-439`)의 4-tier 매칭은 그대로 둔다(analyst §4.2 말미 — designer 결정).
  근거: graph entity 의 "동치"는 이미 alias/embedding tier 로 부분 처리되며, entity-level
  equivalence-set 은 현 요구사항 범위 밖(YAGNI). → **결정: graph entity 동치는 본 PR 범위 제외.**

---

## 11. 구현 순서 권장 (implementer)

1. `gold_set.py` 스키마 2필드 + to/from_dict + 테스트(test_gold_set) → 독립 검증.
2. `eval_search.py` `_reduce_equivalence` + 진입 변경 + `_classify_mode` + test_equivalence_scoring.
3. `build_synthetic_gold_set.py` `_make_graph_gold_item` OR 변환 + 테스트.
4. cross-doc 씨앗 로더 + emit + `_run_cross_doc_mode` + generator 프롬프트 + CLI/metadata + 테스트.
5. `write_summary` cross_doc 슬롯.
6. 전체 `pytest tests/test_eval/` + `ruff` 통과 확인.
