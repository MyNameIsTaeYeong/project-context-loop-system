# 01 — 그래프 인덱싱 강건성 분석 (2차)

**작성일**: 2026-05-18
**역할**: eval-system-analyst (분석 전용 — 설계·구현 금지)
**선행 산출물**: `_workspace_prev_20260518_105045/02_design.md` (1차 설계, D-1~D-8)
**대상 요구사항**: `_workspace/00_requirements.md` (R1 그래프 표기 비의존, R2 의미·앵커·alias 매칭, R3 병합·관계 타입 변경·신규 추출 robustness)

본 보고서는 **현재 코드의 사실만** 인용하며, 설계 권고를 포함하지 않는다.

---

## 1. 현재 그래프 매칭 로직 (사실 인용 — 파일:라인)

### 1.1 매칭 키 구성 — `eval_search.py:190-196`

```python
retrieved_entities: list[tuple[str, str]] = [
    (e.name.lower(), e.type) for e in assembled.retrieved_graph_entities
]
relevant_entities: set[tuple[str, str]] = {
    (e.name.lower(), e.type) for e in item.relevant_graph_entities
}
```

- 매칭 키: `(name.lower(), type)` 의 **2-tuple**.
- `relevant_entities` 가 `set` 인 반면 `retrieved_entities` 는 `list` (순서 보존 → `mrr`, `ndcg` 계산에 사용).
- 매칭은 `metrics.recall_at_k` 등 generic 함수에 위 두 컬렉션을 그대로 전달 (`eval_search.py:215-218`).
  - `metrics.recall_at_k` 는 `set(retrieved) & set(relevant)` 패턴 (호출 시 자동으로 hashable 한 tuple 비교).

### 1.2 name 정규화 단계

평가 측 (`eval_search.py:190-196`):
- `str.lower()` 한 단계만. 공백/punctuation 정리 없음. 유니코드 정규화 (NFKC 등) 없음. alias 처리 없음.
- 비교는 정확 문자열 (대소문자 무시) — `"Auth Service"` 와 `"auth service"` 는 일치하지만 `"Auth Service"` 와 `"AuthService"` 또는 `"인증 서비스"` 와 `"인증서비스"` 는 불일치.

저장 측 (`metadata_store.py:402-415` — `find_graph_node_by_entity`):
- `WHERE LOWER(entity_name) = LOWER(?) AND entity_type = ?` — entity_type 은 **exact** (대소문자 구분).
- 인덱싱 단계에서도 `entity_name` lowercase, `entity_type` exact 가 canonical key.

`graph_store.get_neighbors` 의 lookup (`graph_store.py:326-346`):
- 1차: `entity_name.lower()` 완전 일치.
- 2차: `_extract_scoped_name(entity_name).lower()` — `::` 뒤만 남기는 부분 매칭 (코드 심볼 FQN 용).
- 3차: `_extract_short_name(entity_name).lower()` — 마지막 `.` 뒤의 짧은 이름.
- 이는 **검색 측의 entity 식별** 에만 쓰이며 평가 측의 채점 키 비교에는 사용되지 않는다.

### 1.3 type 비교 방식

- 평가: `e.type` 끼리 **exact** 비교 (대소문자 구분, 트림 없음) — `eval_search.py:192-195`.
- 저장: `LOWER(entity_name) AND entity_type = ?` — entity_type 은 case-sensitive — `metadata_store.py:410`.

### 1.4 검색 측 `GraphSearchResult.entities` 가 포함하는 노드

`graph_search_planner.execute_graph_search` (`graph_search_planner.py:204-309`):

1. `all_nodes` 집계 — `for step in plan.search_steps: neighbors = graph_store.get_neighbors(step.entity_name, depth=step.depth)` 결과를 누적 (`:220-234`).
2. `get_neighbors(name, depth)` 는 `nx.single_source_shortest_path_length(..., cutoff=depth)` 로 **depth-홉 ego-subgraph** 의 모든 노드를 반환 (`graph_store.py:351-362`). depth=1 이면 시작 노드 + 1-hop 이웃. depth=2 면 2-hop 까지.
3. `plan.search_steps[i].depth` 는 LLM 이 선택하지만 `[1, 2]` 로 clamp 됨 (`graph_search_planner.py:187`).
4. `entities: list[GraphEntityRef]` 채움 (`graph_search_planner.py:290-303`):
   ```python
   for node in all_nodes:
       name = str(node.get("entity_name", ""))
       etype = str(node.get("entity_type", ""))
       if not name: continue
       key = (name.lower(), etype)
       if key in seen_pairs: continue
       seen_pairs.add(key)
       entities.append(GraphEntityRef(name=name, type=etype))
   ```
   - **검색에서 hit 한 중심 엔티티 + 모든 1~2-hop 이웃 노드** 가 entities 에 포함된다.
   - 중복 제거 키 또한 `(name.lower(), etype)` — 평가 측 키와 동일.
5. `context_assembler.assemble_context_with_sources` (`:433`) 가 `retrieved_entities = list(graph_result.entities) if graph_result else []` 로 패스스루하여 `AssembledContext.retrieved_graph_entities` 에 그대로 노출.

### 1.5 entity 매칭 부분 일치 여부

- 평가 채점: **완전 일치만** (`(name.lower(), type)`).
- 검색 측 entity 식별 (`get_neighbors` 1~3차 fallback) 은 부분 일치를 허용하지만, 이는 **planner 의 단계별 entity_name 입력** 을 노드와 연결할 때만 사용. 일단 노드가 식별되면 그 노드의 원래 `entity_name` (예: `file.py::Class.method`) 그대로가 `retrieved_graph_entities` 의 키로 노출된다.

---

## 2. 현재 골드셋이 보존하는 그래프 정보

### 2.1 `GraphEntityRef` 의 보존 필드 — `gold_set.py:34-51`

```python
@dataclass
class GraphEntityRef:
    name: str
    type: str
```

- 단 두 필드. alias / description / aliases / embedding / canonical_id 모두 **없음**.
- `to_dict` / `from_dict` 도 두 필드만 round-trip.

### 2.2 subgraph 정보의 골드셋 보존 여부

- `synth.build_subgraph_snippet`, `synth.format_edges_for_prompt` 의 산출물 (`subgraph_snippet`, `edges_text`) 는 **LLM Generator 의 입력으로만** 사용된다 — `build_synthetic_gold_set.py:552-557` 에서 `generate_graph_questions(sg, ...)` 호출 시 사용 후 폐기.
- `description` 도 LLM 입력에만 들어가고 골드셋 YAML 에는 emit 되지 않음 — `_make_graph_gold_item` (`build_synthetic_gold_set.py:611-634`) 이 `relevant_graph_entities=[GraphEntityRef(name=sg["entity_name"], type=sg["entity_type"])]` 만 세팅.
- subgraph 의 `edges`, `document_ids` 중 `document_ids` 만 `relevant_doc_ids` 로 골드셋에 반영. edges 는 완전히 누락.

### 2.3 "핵심 노드 1개만 정답" 결정 (W-3) 의 코드 위치

- 결정 출처: `_workspace_prev_20260518_105045/02_design.md:556` (W-3: "v1 은 핵심 노드 1개만 정답").
- 코드 적용 지점: `scripts/build_synthetic_gold_set.py:611-634` (`_make_graph_gold_item`)
  ```python
  return GoldItem(
      ...
      relevant_graph_entities=[
          GraphEntityRef(name=sg["entity_name"], type=sg["entity_type"])
      ],
      ...
  )
  ```
- 즉 골드셋에는 subgraph 의 **중심 노드 1개만** 정답으로 들어간다. 이웃 노드는 골드셋 정답에 포함되지 않는다.
- 검색 측 (`retrieved_graph_entities`) 는 중심 + 1~2-hop 이웃 모두 노출 → **자연스럽게 precision 이 낮아지는 구조** (이웃이 hit 으로 안 잡혀 분모는 크고 분자는 1로 캡됨). 이 비대칭은 1차 작업의 명시적 트레이드오프.

---

## 3. 그래프 인덱싱 변경 시나리오별 영향

평가 데이터 흐름:
```
relevant_graph_entities (golden) ──┐
                                   ├─► set 비교: (name.lower(), type)
retrieved_graph_entities (search) ─┘
                                   └─► relevant_doc_ids 비교 (graph_result.document_ids 가 sources 로 들어가지만 graph 채점 키는 entity 만)
```

### 시나리오 A — 신규 entity_type 추가 (예: `concept` 외에 `framework` 신설)

- **매칭 실패 지점**: 없음 (기존 골드셋 항목의 type 은 그대로 존재) — 단 새 type 으로 분류된 노드가 검색 결과로 추가되면 `retrieved_entities` 가 더 길어진다.
- **메트릭 영향**:
  - `graph_recall@k` — 영향 없음 (golden 의 type 은 그대로). hit 분자는 동일.
  - `graph_precision@k` — **하락**. retrieved 분모는 새 type 노드까지 포함하므로 커진다. golden 매칭은 늘지 않으므로 비율 ↓.
  - `graph_mrr` — 1순위 hit 의 위치가 새 type 노드 뒤로 밀릴 수 있으면 ↓ 가능.
- **doc-level**: 새 type 노드의 `document_ids` 도 sources 에 반영되지만 `relevant_doc_ids` 와 무관한 노이즈 — `recall@k` 는 안전. precision 은 doc 다양성에 따라 변동.
- **위험도**: **낮음** (recall 유지, precision 약간 하락).

### 시나리오 B — 기존 entity_type 명 변경 (예: `system` → `service`)

- **매칭 실패 지점**: `eval_search.py:194-196` 에서 `relevant_entities = {(name.lower(), "system")}` vs `retrieved_entities = [..., (name.lower(), "service"), ...]` 가 **type exact 비교** 로 어긋남.
- **메트릭 영향**:
  - `graph_recall@k`, `graph_hit@k` — **0 으로 하락**. 같은 의미의 노드가 검색되어도 type 이 달라 unmatched.
  - `graph_precision@k`, `graph_mrr`, `graph_ndcg@k` — 모두 0.
- **doc-level**: 같은 노드의 `document_ids` 는 그대로 sources 에 합쳐지므로 `recall_at_k(retrieved_doc_ids, relevant_doc_ids)` 는 **유지**.
- **위험도**: **매우 높음** — graph_* 메트릭 전부 무력화. R3 가 명시적으로 요구하는 케이스.

### 시나리오 C — name 정규화 변경 (공백, 케이스, 동의어)

세 가지 하위 케이스:

- **C1 (공백 변경)**: `"인증 서비스"` → `"인증서비스"` (혹은 그 반대). `eval_search.py:190-196` 의 `lower()` 만으로는 정규화 안 됨. 매칭 실패 → graph_* 메트릭 0.
- **C2 (케이스 변경)**: `"AuthService"` → `"authservice"`. `lower()` 가 적용되므로 영향 없음.
- **C3 (동의어 / 표기 변경)**: `"인증 서비스"` → `"인증 서버"` 또는 `"Auth Service"`. 매칭 실패.
- **doc-level**: 영향 없음 — entity name 과 무관.
- **위험도**: **높음** (C1, C3). 한국어 공백·복합어 차이는 일상적 변경.

### 시나리오 D — entity 병합 / canonical 변경

현재 인덱싱 측 병합 정책 (`graph_store.py:144-209`):
- `find_graph_node_by_entity(name, type)` (대소문자 무시 + type exact) 로 **이미 통합되어 있음**.
- 즉 같은 `(name.lower(), type)` 면 신규 인덱싱에서도 동일 노드로 수렴.

- **두 노드가 하나로 합쳐지는 케이스**: 예전에 두 별개 노드 `("Auth Service", "system")`, `("AuthService", "system")` 가 lower 만 다르고 공백이 달랐다면 — 현재 정책으론 lower 만 비교하므로 별개 노드. 새 정책이 공백 제거를 추가하면 하나로 병합됨.
  - 골드셋이 두 표기 중 한쪽으로 기록되어 있었다면 새 노드의 canonical name 이 다른 쪽일 때 매칭 실패.
- **`canonical_id` 도입**: 현재 스키마에 canonical 컬럼 없음 (`metadata_store.py:47-53` — `graph_nodes` 컬럼은 `id, document_id, entity_name, entity_type, properties` 만). 새로 canonical 도입 시 골드셋의 name 이 어떤 표기였는지에 따라 갈림.
- **매칭 실패 지점**: `eval_search.py:194-196` — name 표기 차이로 unmatched.
- **메트릭 영향**: graph_recall/precision/hit/mrr 모두 영향. recall 만 0 이 될 수도 있음.
- **doc-level**: 병합으로 `graph_node_documents` 가 양쪽 doc 을 모두 보유 → `graph_result.document_ids` 가 풍부해짐. `recall_at_k(retrieved_doc_ids, ...)` 는 **유지 또는 향상**.
- **위험도**: **중~높음**.

### 시나리오 E — 관계 타입 명 변경 (예: `depends_on` → `requires`)

- **현재 평가**: 관계(엣지) 채점이 **전혀 없음**. `eval_search.evaluate_one` 의 row 에는 edge 관련 키가 없다 (`:198-220`).
- **매칭 실패 지점**: 없음 — entity 매칭만 보므로 relation_type 변경은 graph_* 메트릭에 직접 영향 없음.
- **메트릭 영향**:
  - `graph_recall@k`, `graph_precision@k` — 영향 없음.
  - Judge LLM 점수 (`judge_answer`) — 검색된 컨텍스트 텍스트에 `--[depends_on]-->` vs `--[requires]-->` 표기가 바뀌어 노출되지만 Judge 가 의미를 판단하므로 영향 미미. 결정론은 아니므로 잔차 진동 가능.
- **doc-level**: 영향 없음.
- **위험도**: **낮음 (현행 평가 한정)**. 단, 00_requirements.md R3 가 "관계 타입 변경에도 robust" 를 요구하므로 **관계 채점이 평가 시그널에 누락되어 있음** 자체가 designer 결정 대상.

### 시나리오 F — 새 1-hop 이웃 추출 추가 (검색이 풍부해짐)

- **유입 경로**: 새 추출기가 추가되어 같은 중심 노드의 1-hop neighbor 가 늘면 `get_neighbors(entity_name, depth=1)` 결과가 더 많아지고, `GraphSearchResult.entities` 길이도 증가.
- **매칭 영향**:
  - `graph_recall@k` — golden 의 중심 노드가 top-k 안에 있으면 hit 유지. 단 entities 는 set 가 아닌 **순서가 있는 list** 로 채워지므로 (`graph_search_planner.py:294-303`) golden 노드가 top-k 밖으로 밀리면 recall@k 가 하락할 수 있다.
  - `graph_precision@k` — 분모는 retrieved 의 top-k, 분자는 golden 1개로 캡 → **명백히 ↓**.
  - `graph_mrr` — golden 노드의 1-based rank 가 새 이웃 뒤로 밀리면 ↓.
- **doc-level**: 신규 이웃의 `document_ids` 도 합쳐져 doc recall ↑ 가능, precision ↓ 가능.
- **위험도**: **중**. 00_requirements.md R3 의 "precision 하락이 과도하지 않아야" 와 정확히 맞물림.

### 시나리오 요약표

| 시나리오 | graph_recall@k | graph_precision@k | graph_mrr | doc recall@k | 위험도 |
|----------|---------------|-------------------|-----------|--------------|--------|
| A 신규 type | — | ↓ | (변동) | 안전 | 낮 |
| B type 명 변경 | **0** | **0** | **0** | 안전 | **매우 높음** |
| C1 공백 변경 | **0** | **0** | **0** | 안전 | **높음** |
| C2 케이스 변경 | — | — | — | 안전 | 무 |
| C3 동의어 | **0** | **0** | **0** | 안전 | **높음** |
| D 병합/canonical | 0 가능 | 0 가능 | 0 가능 | ↑/유지 | 중~높 |
| E 관계 타입 변경 | — | — | — | 안전 | 낮(현행) |
| F 새 이웃 추출 | ↓ 가능 | ↓ | ↓ | 변동 | 중 |

---

## 4. 검색 측 entity 노출 범위 정리

- `GraphSearchResult.entities` 의 멤버 (`graph_search_planner.py:290-303`):
  - planner 가 호출한 `get_neighbors(step.entity_name, depth)` 의 ego-subgraph 노드 (중심 + 1~2-hop 이웃) **전부**.
  - searched_entities (planner 가 명시한 중심 노드 이름) 만이 아닌, 그 ego-net 의 모든 노드.
  - 중복 제거 키 `(name.lower(), entity_type)`.
- focus_relations 필터링 (`:241-250`) 은 **edges 출력** 에만 적용되고 entities 리스트는 줄이지 않는다.
- depth=1 인 경우에도 시작 노드 + 1-hop 이웃이 들어가므로 entities 크기는 보통 정답 1개를 훨씬 초과 → graph_precision 이 본질적으로 낮음. 1차 작업 W-3 의 트레이드오프 그대로.
- entity 매칭 형태: **완전 일치** (`(name.lower(), type)` exact tuple in set).

---

## 5. metadata_store 그래프 스키마 현황

`metadata_store.py:47-68`:

```sql
CREATE TABLE IF NOT EXISTS graph_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    entity_name TEXT NOT NULL,
    entity_type TEXT,
    properties TEXT          -- JSON (현재는 {"description": "..."} 만 사용)
);

CREATE TABLE IF NOT EXISTS graph_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    source_node_id INTEGER REFERENCES graph_nodes(id) ON DELETE CASCADE,
    target_node_id INTEGER REFERENCES graph_nodes(id) ON DELETE CASCADE,
    relation_type TEXT,
    properties TEXT          -- JSON (현재는 {"label": "..."} 만 사용)
);

CREATE TABLE IF NOT EXISTS graph_node_documents (
    node_id INTEGER REFERENCES graph_nodes(id) ON DELETE CASCADE,
    document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
    PRIMARY KEY (node_id, document_id)
);
```

- **alias / canonical 전용 컬럼 없음**. 별도 `entity_aliases` 테이블도 없음.
- `properties` (JSON TEXT) 컬럼은 자유 형식 — `description` 외에 `aliases`, `canonical_id`, `embedding_ref` 등을 추가 데이터 없이 확장 가능. 단 인덱스 부재라 lookup 시 풀스캔 + JSON 파싱 필요.
- `find_graph_node_by_entity` 는 단일 `(LOWER(entity_name), entity_type)` 키로만 조회 (`metadata_store.py:402-415`) — alias 조회 경로 없음.
- 새 컬럼을 추가하려면 `_migrate_schema` (`metadata_store.py:157`) 에 `ALTER TABLE graph_nodes ADD COLUMN ...` idempotent 마이그레이션 추가 필요.
- **결론**: 평가 시스템 변경만으로 인덱싱 측 스키마 확장은 불필요. 다만 alias 를 활용하려면 `properties JSON` 또는 신규 `entity_aliases` 테이블 중 designer 선택.

---

## 6. semantic matching 도입 시 가용 인프라

### 6.1 임베딩 클라이언트

- `src/context_loop/processor/embedder.py`:
  - `EndpointEmbeddingClient` (langchain `Embeddings` 상속) — `aembed_documents(texts)`, `aembed_query(text)` 비동기 제공 (`embedder.py:81-118`).
  - `LocalEmbeddingClient` — sentence-transformers 백엔드 (`embedder.py:122-181`).
- `eval_search.py` 가 이미 `_build_clients` 로 `embedding_client` 를 빌드해 보유 (`scripts/eval_search.py:419-430`).
- `GraphStore` 에는 이미 `build_entity_embeddings(embedding_client)` (`graph_store.py:591-614`) 와 `search_entities_by_embedding(query_embedding, threshold=0.7, top_k=5)` (`graph_store.py:616-643`) 이 존재 — 그래프 탐색 planner 가 query 관련 schema 만 보여줄 때 활용 중.
- 비용: 평가 단계에서 entity 임베딩은 1회만 (캐시). golden entity 임베딩도 1회 batch 가능. query embedding 은 `assemble_context_with_sources` 내부에서 이미 매 질의마다 한 번 생성 (`context_assembler.py:366-368`).

### 6.2 LLM Judge 사용 현황 및 추가 비용

- 현재 Judge 호출 (`scripts/eval_search.py:222-235`): `--judge` 옵션이 켜져 있을 때 **query 당 정확히 1회** `judge_answer` 호출 (LLM 호출 1회).
- Judge 응답 길이: max_tokens=256, temperature=0.0.
- 골드셋 생성에는 `is_answerable` 호출이 (정답 청크 1 + distractor N) 회 발생하지만 이것은 평가가 아닌 생성 단계.
- 의미 매칭을 매 평가 query 마다 LLM 으로 한다면 query 당 추가 1~M회 호출 (M = retrieved entity 수 or pair 수). 임베딩 기반은 추가 LLM 호출 0건, batch embedding 한 번.

### 6.3 결정성

- 임베딩 기반: 임베딩 결과가 결정론적이면 (대부분 fix-seed 또는 cached) 점수 결정론적.
- LLM 기반: temperature=0.0 + 동일 프롬프트라도 모델 캐싱·라우팅·비결정성 가능. 00_requirements.md "결정론" 항목과 충돌 우려.

---

## 7. backward-compat 영향 범위

### 7.1 기존 YAML 로드 동작

- `GraphEntityRef.from_dict` (`gold_set.py:49-51`) 는 `{"name", "type"}` 만 본다 — 신규 필드가 dict 에 추가되어도 호환 (단 `from_dict` 가 추가 필드를 무시).
- `GoldItem.from_dict` 가 `relevant_graph_entities` 없는 옛 YAML 에는 `[]` 기본값 (`gold_set.py:107-111`).
- 신규 필드 (`aliases`, `description`, `evidence`, `embedding` 등) 가 YAML 에 없을 때 — 기본값 `None` 또는 빈 컬렉션이면 OK.

### 7.2 1차에서 생성된 (현행) 골드셋의 graceful degradation

- 1차 골드셋 항목은 `relevant_graph_entities: [{name, type}]` 만 보유.
- 새 매칭 로직이 alias / embedding / evidence 등을 활용하더라도, 이 필드들이 비어 있으면 **기존 exact-tuple 매칭으로 fallback** 해야 backward-compat 유지 — 이는 designer 가 결정해야 할 정책 (Q4).
- name/type 둘만 있는 항목에 대해 semantic match 만 적용하면 검색 측 entity 가 의미적으로 유사하기만 하면 무조건 hit — 1차 골드셋의 strictness 가 약화되는 부작용 가능.

### 7.3 영향이 있는 코드 경로

- `eval_search.py:190-218` — 매칭 키 생성 + metrics 호출. 신규 필드를 활용한 정책 분기 필요.
- `gold_set.py:34-51, 105-133` — `GraphEntityRef` / `GoldItem` 의 to_dict/from_dict.
- `build_synthetic_gold_set.py:611-634` — 골드셋 emit. 새 필드 채우려면 여기서.
- `synth.py:419-451` — graph 질문 생성. evidence / aliases 를 동시에 생성하려면 별도 LLM 호출 추가.

---

## 8. 영향 파일·테스트 매트릭스

| 변경할 영역 | 영향 파일 | 영향 테스트 | 마이그레이션 필요? |
|------------|-----------|-------------|--------------------|
| `GraphEntityRef` 스키마 확장 (aliases / description / evidence 등) | `src/context_loop/eval/gold_set.py:34-51` | `tests/test_eval/test_gold_set.py:107-213` (round-trip, backward-compat) | YAML 측 마이그레이션 불필요 (옵셔널 필드). 코드 측은 새 필드 추가. |
| 평가 매칭 정책 변경 (alias OR / semantic / evidence-based) | `scripts/eval_search.py:190-218` (`evaluate_one` 의 entity 비교) | `tests/test_eval/test_build_synthetic_gold_set.py:308-330` 만 직접 닿음. 신규 테스트 필요 (alias OR, semantic 캐시 hit/miss). | 없음. CLI 옵션 추가 시 호환 유지. |
| 시멘틱 매칭 (임베딩 기반) | `scripts/eval_search.py` (+ helper 신설 — designer 결정) | 신규 테스트: mock embedding 으로 fixture 임베딩 비교. | 없음 (런타임 캐시). |
| 시멘틱 매칭 (LLM Judge 기반) | `scripts/eval_search.py` + `src/context_loop/eval/synth.py` 또는 신규 모듈 | mock LLMClient 테스트. | 없음. |
| 관계(엣지) 채점 추가 | `scripts/eval_search.py` (`evaluate_one` row 에 graph_edge_* 메트릭 추가), `gold_set.py` (`relevant_graph_edges` 신규 필드), `context_assembler.py` / `graph_search_planner.py` (검색 결과에 edges 노출) | `test_gold_set.py`, `test_build_synthetic_gold_set.py` 갱신 + 신규 graph edge round-trip 테스트. | YAML 옵셔널 필드 추가 (graceful). |
| alias 인덱스 (메타스토어) | `metadata_store.py:47-53` 의 `properties` JSON 활용 또는 신규 `entity_aliases` 테이블 + `_migrate_schema` | 신규 storage 테스트. | **YES** — 인덱스 측 변경이라 평가 범위 외, designer 검토 필요. |
| 재합성 헬퍼 (인덱싱 변경 후 type/alias 갱신) | 신규 스크립트 `scripts/refresh_gold_graph_refs.py` (또는 build_synthetic_gold_set 에 모드 추가) | 신규 테스트. | 없음. |
| `metrics_by_mode` 보고 유지 | `scripts/eval_search.py:325-411` 변경 없음. | 변동 없음. | 없음. |

테스트 인벤토리 (참고):
- `tests/test_eval/test_gold_set.py` — 13개 함수, 모두 graph 필드 round-trip 또는 backward-compat 검증.
- `tests/test_eval/test_synth.py` — 50+ 함수, graph 부분은 `build_subgraph_snippet`, `format_edges_for_prompt`, `generate_graph_questions` 일부 (line 493+).
- `tests/test_eval/test_build_synthetic_gold_set.py` — `load_candidate_subgraphs_*` 4개 + `test_classify_mode_chunk_graph_hybrid`.
- `tests/test_eval/test_metrics.py`, `tests/test_eval/test_llm.py` — graph 무관.

---

## 9. designer 에게 넘길 미해결 질문

00_requirements.md "핵심 의사결정 후보" 5개를 그대로 포함하고 분석에서 추가로 발견한 항목을 덧붙인다.

### 9.1 00_requirements.md 인용 (재정렬·재기록)

**Q1 — 시멘틱 매칭 채택 여부**
- 채택 시 임베딩 vs LLM 분기. 임베딩은 결정론적·저비용·캐시 가능, LLM 은 정밀하지만 비결정성·비용 위험.
- 분석 결과: 임베딩 인프라 (`EndpointEmbeddingClient`, `LocalEmbeddingClient`, `GraphStore.build_entity_embeddings`) 가 이미 운영 중 → 임베딩 채택 시 추가 의존성 없이 가능.

**Q2 — 골드셋 스키마 변경 범위**
- 옵션 (a): `GraphEntityRef` 에 `aliases: list[str]`, `description: str`, `embedding_id: str` 추가.
- 옵션 (b): 새 클래스 `GraphFact { subject, predicate, object, evidence }` 도입 → 관계 채점도 자연 지원.
- 옵션 (c): 추가 데이터 클래스 없이 `properties: dict` 같은 자유 dict 슬롯.
- 분석 결과: 1차 골드셋 backward-compat 가 R 의 명시 비기능요구 — 옵션 (a) 가 최소 침습.

**Q3 — 관계(엣지) 평가 위상**
- 현재 평가에 edge 단위 채점이 **전혀 없음** (§3 시나리오 E).
- 추가하면 시나리오 E 의 robustness 도 평가 시그널에 반영. 단 `GraphSearchResult` 에 edges 노출 없음 (현재 entities 만) → 검색 측에서 edges 도 노출하는 패스스루 필요 (`graph_search_planner.py:281-288` 의 edges 는 텍스트 포맷팅에만 사용).

**Q4 — 매칭 정책: strict vs fuzzy 모드 분리 vs 통합**
- strict (현재) 와 fuzzy (semantic) 를 CLI 옵션 또는 골드셋 메타데이터로 분기할지.
- 분리 시 `--match-policy=strict|alias|semantic` 같은 옵션. metrics_by_mode 와 직교.

**Q5 — 재합성 헬퍼 제공 여부**
- 인덱싱 변경 후 골드셋의 (name, type, aliases) 만 일괄 갱신해 주는 도구.
- 신규 LLM 호출 비용 발생 — 하지만 골드셋 재합성 (질문 재생성) 전체보다 훨씬 저렴.

### 9.2 분석에서 추가로 발견한 질문

**Q6 — 시나리오 F (precision 자연 하락) 의 명시 정책**
- 현재 `retrieved_entities` 가 ego-subgraph 전체이므로 precision 이 낮은 것은 정상.
- 새 추출이 추가되어 분모가 더 커지면 precision 하락폭이 더 커진다.
- "recall 우선" 임을 보고에 명시하고 precision 은 보조 지표로 강등할지, 아니면 `graph_precision@k_at_searched_entities_only` 같은 변형 메트릭을 도입할지.

**Q7 — 검색 측 `entities` 가 노출하는 노드 범위 변경 가능성**
- 현재 ego-subgraph 전체. 평가용으로 "searched_entities (planner 가 지목한 중심 노드들) 만" 노출하는 변종을 옵션화하면 strict 평가가 가능 — 단 검색 컨텍스트 활용도와 어긋남.
- designer 가 "평가는 평가, 검색은 검색" 의 분리 정책을 정해야 함.

**Q8 — 시맨틱 매칭의 결정성·재현성**
- 임베딩 채택 시 임베딩 결과의 결정성 의존 (모델 버전 고정 + 캐시 일관성). 모델 버전이 바뀌면 평가 점수가 변동 — 골드셋 변경 없이도. 이를 "재현성" 정의에 어떻게 반영할지.

**Q9 — alias 추출의 출처**
- 골드셋의 `aliases` 를 어디서 가져올지: (a) Generator LLM 이 질문 생성 시 함께 생성, (b) 별도 LLM 패스, (c) 인덱스 측 alias 테이블 (현재 없음 — §5) 에서 추출.
- (a) 가 가장 저렴하지만 Generator 자기 편향 (자기 골드를 자기 alias 로 매칭) 가능성.

**Q10 — graph 채점에서 "정답 N개" 정책 (W-3 재검토)**
- 현재 정답 1개. semantic / alias 채택 시에도 그대로 1개 유지할지, 아니면 핵심 노드 + 직접 1-hop 이웃 일부 포함으로 확장할지.
- 시나리오 F 의 precision 자연 하락과 강하게 연동된 결정.

**Q11 — Judge 의 graph 답안 채점 활용**
- 이미 `judge_answer` 가 retrieved_context (텍스트) 를 받아 0~5 점을 매김 (`eval_search.py:104-139`).
- 그래프 텍스트가 같은 의미를 담는지 0~5 점으로 평가하는 흐름이 이미 있음 — 이를 새 시맨틱 매칭의 한 변형 (semantic-via-judge) 으로 재활용할지, 별도 호출을 신설할지.

---

## 마지막 요약

영향 파일 수 **7개** (gold_set.py, synth.py, build_synthetic_gold_set.py, eval_search.py, graph_search_planner.py, context_assembler.py, metadata_store.py 중 최대 활용 — 평가 한정 범위면 앞 4개), 미해결 질문 **11개** (00_requirements.md 5 + 추가 6), 가장 위험한 시나리오는 **B (entity_type 명 변경)** — graph_recall/precision/hit/mrr 가 동시에 0 으로 무력화되며 R3 가 명시적으로 robust 를 요구하는 케이스다.
