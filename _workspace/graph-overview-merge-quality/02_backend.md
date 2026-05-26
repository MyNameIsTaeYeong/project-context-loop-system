# Backend — 전역 그래프 API 구현 보고서

> 일시: 2026-05-26
> 담당: graph-overview-api-builder
> 입력 사양: `00_context.md`, `01_merge_diagnosis.md` (특히 4절 메트릭 정의)

---

## 1. 변경 요약

### 신규 파일

| 경로 | 역할 |
|------|------|
| `src/context_loop/web/api/graph_overview.py` | 전역 그래프 페이지(`/graph`) + JSON API (`/api/graph/*`) 6개 엔드포인트 |
| `src/context_loop/web/templates/graph_overview.html` | 페이지 셸 (frontend builder 가 이미 본격 UI 로 채워둠 — backend 가 만든 것은 셸 의도였으나 frontend builder 가 선행 작성한 상태) |

### 수정 파일

| 경로 | 변경 |
|------|------|
| `src/context_loop/web/app.py` | `graph_overview_router` import + `app.include_router(graph_overview_router)` 등록 (한 줄) |
| `src/context_loop/web/templates/base.html` | nav 메뉴에 `<li><a href="/graph">Graph</a></li>` 추가 (Dashboard 와 Chat 사이) |

> 본 builder 는 라우터 + 셸 보장만 책임진다. `graph_overview.html` 은 frontend builder 가 이미 본격 UI (Alpine.js + vis-network) 로 작성한 상태였으므로 그대로 두었다. 백엔드 라우터는 `templates/graph_overview.html` 을 단순 렌더하면 되므로 호환된다 (nav → /graph → 200 OK 보장).

---

## 2. 엔드포인트 사양

### 2.1 `GET /graph`

페이지 셸 렌더. `templates/graph_overview.html` 반환.

**상태 보증**: 라우터 단독 검증 시 200 OK 반환. UI 본격 구현은 frontend builder 산출물.

---

### 2.2 `GET /api/graph/clusters`

`entity_type` 별 클러스터 요약.

**Query**: `source_type` (str, optional), `document_id` (int, optional).
필터가 둘 다 주어지면 둘 모두 만족하는 노드만 포함.

**Response shape**:

```json
{
  "clusters": [
    {
      "entity_type": "service",
      "node_count": 6,
      "edge_count": 11,
      "top_entities": [
        {"id": 1, "name": "Auth Service", "document_count": 1},
        {"id": 4, "name": "Payment Service", "document_count": 1},
        {"id": 5, "name": "User Service", "document_count": 1}
      ]
    },
    {
      "entity_type": "team",
      "node_count": 6,
      "edge_count": 4,
      "top_entities": [
        {"id": 10, "name": "KakaoPay", "document_count": 1}
      ]
    }
  ],
  "total_nodes": 21,
  "total_edges": 16,
  "filters": {"source_type": null, "document_id": null}
}
```

**구현 노트**:
- NetworkX `graph.nodes(data=True)` 한 번 순회로 type 별 그룹핑.
- `top_entities` 는 `graph.degree(nid)` 기준 내림차순 정렬 → 상위 5개.
- `edge_count` 는 클러스터 내부 + 외부로 향하는 모든 엣지를 양쪽 type 에 합산 (양방향 카운트). 즉 cross-type 엣지는 양 클러스터 카운트에 모두 반영. `total_edges` 는 in-scope 노드 간 엣지의 단일 카운트.

---

### 2.3 `GET /api/graph/cluster/{entity_type}/nodes`

클러스터 내부 노드 + 엣지 페이지네이션.

**Query**: `limit=200` (1..2000), `offset=0`, `q` (entity_name 부분 일치, 대소문자 무시).

**Response shape**:

```json
{
  "entity_type": "service",
  "nodes": [
    {
      "id": 1,
      "name": "Auth Service",
      "entity_type": "service",
      "document_count": 1,
      "document_ids": [5],
      "degree": 7
    }
  ],
  "edges": [
    {"id": 12, "source": 1, "target": 3, "relation_type": "depends_on"}
  ],
  "total": 6,
  "limit": 200,
  "offset": 0
}
```

**구현 노트**:
- 정렬 — `(-degree, name.lower())` 안정적 ordering.
- `edges` 는 페이지에 들어간 노드들 사이의 엣지만 반환 (시각화 일관성).

---

### 2.4 `GET /api/graph/node/{node_id}`

노드 상세 + 출처 문서 + 이웃.

**Response shape**:

```json
{
  "id": 1,
  "name": "Auth Service",
  "entity_type": "service",
  "properties": {"description": "인증 토큰 발급/검증을 담당하는 서비스"},
  "document_ids": [5],
  "documents": [
    {"id": 5, "title": "백엔드 시스템 아키텍처", "source_type": "manual", "url": null}
  ],
  "neighbors": [
    {
      "id": 3,
      "name": "PostgreSQL DB",
      "entity_type": "component",
      "relation_type": "depends_on",
      "direction": "out"
    },
    {
      "id": 6,
      "name": "API Gateway",
      "entity_type": "service",
      "relation_type": "uses",
      "direction": "in"
    }
  ]
}
```

**구현 노트**:
- `meta_store.list_documents()` 1회 호출 후 `{id: doc}` dict 캐시로 lookup — N+1 회피.
- 노드가 없으면 404.
- 이웃: `successors` (direction=out) + `predecessors` (direction=in) 양쪽.
- `documents` 가 빈 배열이어도 graceful — graph_node_documents 가 비어있어도 NetworkX 의 `document_ids` 가 비어있는 정상 응답.

---

### 2.5 `GET /api/graph/merge-quality`

엔티티 통합 품질 메트릭 + 잠재 중복 그룹 인벤토리.

**Query**:
- `include_semantic=false` — true 면 임베딩 캐시를 활용해 의미 중복 계산 시도.
- `semantic_threshold=0.85`, `fuzzy_threshold=0.85`.

**Response shape**:

```json
{
  "total_nodes": 21,
  "duplicate_groups": [
    {
      "kind": "surface_normalized",
      "key": "authservice",
      "members": [
        {"id": 1, "name": "Auth Service", "type": "service"},
        {"id": 7, "name": "auth-service", "type": "service"}
      ]
    }
  ],
  "type_conflict_groups": [
    {
      "kind": "type_conflict",
      "key": "kafka",
      "members": [
        {"id": 2, "name": "Kafka", "type": "system"},
        {"id": 9, "name": "Kafka", "type": "component"}
      ]
    }
  ],
  "fuzzy_candidates": [
    {
      "a": {"id": 3, "name": "User", "type": "entity"},
      "b": {"id": 8, "name": "Users", "type": "entity"},
      "similarity": 0.8889,
      "method": "sequence_matcher"
    }
  ],
  "metrics": {
    "duplication_ratio_surface": 0.0,
    "duplication_ratio_surface_same_type": 0.0,
    "duplication_ratio_semantic": null,
    "semantic_status": "skipped",
    "type_conflict_count": 0,
    "cross_document_node_ratio": 0.0,
    "cross_document_node_count": 0,
    "orphan_edge_count": 0,
    "leading_trailing_whitespace_node_count": 1
  },
  "scale": {
    "total_nodes": 21,
    "total_edges": 16,
    "entity_embedding_count": 0
  }
}
```

**메트릭 매핑 (analyst 4절 ↔ 응답 필드)**:

| analyst 정의 | 응답 필드 | 비고 |
|--------------|-----------|------|
| 4.1 `duplication_ratio_surface` | `metrics.duplication_ratio_surface` | `re.sub(r"[\s_\-.]+", "", name).lower()` 기준 |
| 4.2 `duplication_ratio_surface_same_type` | `metrics.duplication_ratio_surface_same_type` | (surface_key, entity_type) 튜플 키 |
| 4.3 `duplication_ratio_semantic` | `metrics.duplication_ratio_semantic` + `semantic_status` | `include_semantic=true` + 캐시 있을 때만 실제 값. 아니면 `null`. status ∈ {"computed", "uncomputed", "skipped", "empty_graph"} |
| 4.4 `type_conflict_count` | `metrics.type_conflict_count` | `entity_name.strip().lower()` 기준 그룹 후 type 종류 ≥ 2 |
| 4.5 `cross_document_node_ratio` | `metrics.cross_document_node_ratio` + `cross_document_node_count` | 노드의 `document_ids` 크기 ≥ 2 |
| 4.6 `orphan_edge_count` | `metrics.orphan_edge_count` | SQLite 직접 쿼리 (정합성 알람) |
| 4.7 `leading_trailing_whitespace_node_count` | `metrics.leading_trailing_whitespace_node_count` | `name.strip() != name` 노드 수 |

**보조 결과**:
- `fuzzy_candidates` — 같은 type 내부 SequenceMatcher ratio ≥ `fuzzy_threshold` 쌍. 표면 정규화가 동일한 쌍은 4.1 그룹에서 이미 보고되므로 제외. 최대 50쌍.
- `duplicate_groups` — surface 키 동일 그룹 (자동 머지 후보).
- `type_conflict_groups` — LOWER(name) 동일이지만 type 분기.

---

### 2.6 `POST /api/graph/entity-embeddings/build`

엔티티 이름 임베딩 캐시 idempotent 빌드. `duplication_ratio_semantic` 메트릭이 필요할 때 UI 에서 호출.

**Response shape**:

```json
{
  "added": 21,
  "total_cached": 21,
  "total_nodes": 21,
  "status": "ok"
}
```

**구현 노트**:
- `GraphStore.build_entity_embeddings(embedding_client)` 위임. 이미 캐시된 노드는 재계산 안 함.
- 임베딩 클라이언트는 `Depends(get_embedding_client)` 로 주입 (앱 시작 시 생성된 인스턴스).
- 실패 시 500 + 로그.

---

## 3. 응답 일관성 / 데이터 정합성 처리

### 3.1 `document_ids` source-of-truth

`GraphStore.load_from_db()` 이미 다음 우선순위로 `document_ids` 를 채운다:
1. `graph_node_documents` 링크 테이블 (신스키마)
2. fallback: `graph_nodes.document_id` (레거시 단일 문서)

따라서 라우터는 항상 NetworkX 노드 속성 `document_ids` (set) 만 읽으면 되고, `meta_store.get_all_node_document_links` 가 빈 dict 를 반환하더라도(레거시) 영향 없음. analyst 발견 1번에 대해 graceful.

### 3.2 N+1 회피

`/api/graph/node/{node_id}` 와 `/api/graph/clusters?source_type=...` 모두 `meta_store.list_documents()` 1회 호출 후 dict lookup. 노드별 쿼리 없음.

### 3.3 표면 정규화 키

`re.sub(r"[\s_\-.]+", "", name).lower()` 적용. analyst 보고서 3.1 의 최종 사양 (`.` 포함)을 따름. 역할 정의서의 가이드 (`[\s_\-]+`) 보다 보수적 (즉, 마침표 차이도 동일 키로 처리). 다국어/Unicode 는 보존.

### 3.4 `entity_name.strip()` 비적용

analyst 6.3-A 권고 (`save_graph_data` / `find_graph_node_by_entity` 측 trim 보강) 는 본 라운드에서 적용하지 않음. 대신 `metrics.leading_trailing_whitespace_node_count` 로 진단만 노출 (사용자 결정 사항: 자동 병합 없음, 가시화/메트릭만).

---

## 4. 알려진 제약 및 한계

### 4.1 의미 중복 (semantic) 메트릭 비활성 상태

- 초기에는 `entity_embedding_count == 0` 이므로 `duplication_ratio_semantic = null`, `semantic_status = "skipped"` (또는 `include_semantic=true` 인 경우 `"uncomputed"`).
- UI 는 "미계산" 회색 배지 + `[계산하기]` 버튼 노출 후 `POST /api/graph/entity-embeddings/build` 호출 → 다시 `GET /api/graph/merge-quality?include_semantic=true` 호출 흐름이 필요.
- 임베딩 캐시는 인메모리이므로 앱 재시작 시 재계산 필요.

### 4.2 `orphan_edge_count` 측정 경로

NetworkX 동기화가 정상이면 항상 0이 정상이지만, 본 메트릭은 **SQLite 측 정합성** 알람용으로 직접 SQL 쿼리. NetworkX 만 보면 정의상 0 이 되므로 정합성 점검 의의가 없음.

### 4.3 fuzzy_candidates O(N^2) 비용

타입별로 그룹핑 후 group 내부에서만 비교하지만, 단일 type 에 노드가 매우 많으면 비용이 커진다. 현재 인덱스(21 노드)에서는 무시 가능. 향후 N>1000 이면 LSH / minhash 등 인덱스 기반 후보 추림이 필요.

### 4.4 `/api/graph/clusters` 의 `edge_count` 의미

클러스터 카드의 `edge_count` 는 "이 type 의 노드가 한쪽 끝점이라도 참여한 엣지의 수". cross-type 엣지는 양쪽 클러스터에 모두 카운트되므로, 모든 클러스터의 `edge_count` 합 ≠ `total_edges` 가 정상 (가시화 카드 용도). 정확한 type-internal 엣지 수가 필요하면 `GET /api/graph/cluster/{etype}/nodes` 의 `edges` 배열을 사용.

### 4.5 페이지 셸 상태

`graph_overview.html` 은 frontend builder 가 이미 본격 UI (Alpine.js + vis-network) 로 작성한 상태이므로 backend 는 셸을 새로 생성하지 않고 그대로 사용. 백엔드 단독 검증 시 `GET /graph` 200 OK 가 보장되며, 본격 UI 의 동작은 frontend builder + QA 검증 영역.

---

## 5. 라우터 등록 확인

`src/context_loop/web/app.py` 의 `create_app()` 내부:

```python
from context_loop.web.api.graph_overview import router as graph_overview_router
...
app.include_router(graph_overview_router)
```

`documents_router` 보다 먼저 등록 (`/` prefix 충돌 없음, 둘 다 다른 path 패턴이지만 안전한 순서로 배치).

`base.html` 의 nav 에 `<li><a href="/graph">Graph</a></li>` 추가 (Dashboard 와 Chat 사이).

---

## 6. UI builder / QA 와의 인터페이스 계약

- **UI builder**: 본 API 의 응답 shape 에 맞춰 fetch. shape 변경이 필요하면 본 보고서 갱신 후 SendMessage 로 통보 필요. 현재 frontend `graph_overview.html` 은 본 API shape 과 호환되도록 작성된 것으로 보임 (clusters / merge-quality / node detail / cluster nodes 모두 호환).
- **QA**: 본 API 6개 엔드포인트 + nav → /graph → 200 OK 가 검증 대상. semantic 메트릭은 `include_semantic` 파라미터 분기까지 점검.
