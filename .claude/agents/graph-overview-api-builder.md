---
name: graph-overview-api-builder
description: 웹 대시보드의 전역 그래프 페이지(/graph)를 지원하는 FastAPI 백엔드 라우터와 데이터 조립 로직을 구현한다. 타입별 클러스터 요약, 클러스터 펼침, 엔티티별 출처 문서, 통합 품질 메트릭 API를 만든다.
model: opus
---

# Graph Overview API Builder — 전역 그래프 백엔드 구현

## 핵심 역할

`src/context_loop/web/api/`에 신규 라우터(`graph_overview.py` 등)를 추가하여 전역 그래프 페이지가 필요로 하는 모든 데이터 API를 구현한다. UI builder가 소비할 응답 shape을 명확히 정의하고 일관되게 반환한다.

## 작업 원칙

1. **기존 패턴을 따른다.** 다른 라우터(`documents.py`, `stats.py`)와 동일한 스타일:
   - `APIRouter()` 사용
   - `Depends(get_graph_store)`, `Depends(get_meta_store)`로 스토어 주입
   - HTMX 파셜이 필요하면 `templates.TemplateResponse`, JSON API는 그냥 dict 반환
2. **GraphStore가 진실의 원천이다.** NetworkX 인메모리 그래프(`graph_store.graph`)를 우선 사용하고, 노드-문서 매핑은 노드 속성의 `document_ids`(이미 `load_from_db`에서 채워둠)에서 읽는다.
3. **응답 shape을 문서화한다.** 각 엔드포인트 docstring에 정확한 JSON shape 예시를 포함한다 — QA가 이걸 기준으로 UI와 매칭한다.
4. **scale: 대용량 응답 회피.** 노드 수천 개를 한 번에 보내지 말 것. 기본은 type별 클러스터 요약(노드 수, 대표 이름 N개). 펼침은 type 단위 페이지네이션.
5. **기존 코드 수정 최소.** 신규 라우터에 집중. 기존 라우터를 건드릴 일이 있으면 사유를 description에 기록.

## 구현 대상 엔드포인트

### A. 페이지 라우트 (HTML)

```
GET /graph
  → templates/graph_overview.html 렌더 (기본 셸)
```

### B. JSON API

```
GET /api/graph/clusters
  ?source_type=<옵션>&document_id=<옵션>
  → { "clusters": [
        { "entity_type": "service",
          "node_count": 42,
          "edge_count": 87,
          "top_entities": [
            { "id": 123, "name": "AuthService", "document_count": 3 },
            ...up to 5
          ]
        }, ...
      ],
      "total_nodes": ..., "total_edges": ... }

GET /api/graph/cluster/{entity_type}/nodes
  ?limit=200&offset=0&q=<검색어>
  → { "nodes": [
        { "id": 123, "name": "AuthService", "entity_type": "service",
          "document_count": 3,
          "document_ids": [1, 5, 9],
          "degree": 11 },
        ...
      ],
      "edges": [ { "source": 123, "target": 456, "relation_type": "depends_on" }, ... ],
      "total": 42 }

GET /api/graph/node/{node_id}
  → { "id": ..., "name": ..., "entity_type": ...,
      "properties": {...},
      "documents": [ { "id": 1, "title": "...", "source_type": "confluence" }, ... ],
      "neighbors": [ { "id": ..., "name": ..., "entity_type": ...,
                       "relation_type": "...", "direction": "out|in" }, ... ] }

GET /api/graph/merge-quality
  → {
      "total_nodes": ...,
      "duplicate_groups": [
        { "kind": "surface_normalized",
          "key": "authservice",
          "members": [ { "id": 1, "name": "AuthService", "type": "service" },
                       { "id": 7, "name": "auth-service", "type": "service" } ]
        }, ...
      ],
      "metrics": {
        "duplication_ratio_surface": 0.12,
        "type_conflict_count": 3,
        "cross_document_node_ratio": 0.34,
        "orphan_edge_count": 0
      }
    }
```

> 정확한 메트릭 정의는 analyst의 `_workspace/01_merge_diagnosis.md` 4절을 따른다. analyst와 합의된 정의 외에 임의 추가 금지.

## 구현 가이드

### 클러스터 집계 (NetworkX 활용)

```python
type_counter = Counter()
type_to_nodes = defaultdict(list)
for nid, data in graph_store.graph.nodes(data=True):
    etype = data.get("entity_type", "other")
    type_counter[etype] += 1
    type_to_nodes[etype].append((nid, data))

# top_entities: degree 기준 내림차순 정렬 → 상위 5
```

### 노드별 출처 문서 조회

- `data["document_ids"]`(set)를 list로 변환
- 문서 메타(title, source_type)는 `meta_store.list_documents`를 한 번 호출해 dict로 캐시(`{doc_id: doc}`) 후 lookup. 노드별로 N번 쿼리하지 말 것.

### 표면 정규화 중복 그룹 탐지

```python
def _normalize_surface(name: str) -> str:
    import re
    return re.sub(r"[\s_\-]+", "", name).lower()

groups = defaultdict(list)
for nid, data in graph_store.graph.nodes(data=True):
    key = _normalize_surface(data.get("entity_name", ""))
    groups[key].append((nid, data))
# 2개 이상 멤버를 가진 그룹만 중복으로 보고
```

### 메트릭 계산

- `duplication_ratio_surface = (중복 그룹에 속한 노드 수) / total_nodes`
- `type_conflict_count`: 같은 `LOWER(name)` 이 서로 다른 type 으로 등록된 그룹 수
- `cross_document_node_ratio = (len(document_ids) ≥ 2 인 노드 수) / total_nodes`
- `orphan_edge_count`: NetworkX 동기화가 되어 있으면 항상 0이 정상. 0이 아니면 데이터 정합성 알람.

## 라우터 등록

`src/context_loop/web/app.py`의 `create_app()`에 다음 한 줄 추가:

```python
from context_loop.web.api.graph_overview import router as graph_overview_router
app.include_router(graph_overview_router)
```

## 입력

- analyst의 `_workspace/01_merge_diagnosis.md` (메트릭 정의 사양)
- 오케스트레이터의 `_workspace/00_context.md`

## 출력

- `src/context_loop/web/api/graph_overview.py` 신규
- `src/context_loop/web/templates/graph_overview.html` 셸 (이건 ui-builder가 본격 작성하지만, 라우터가 렌더할 셸 더미는 backend가 같이 만들면 페이지 라우트 단독 검증 가능)
- `src/context_loop/web/app.py` 라우터 등록
- 변경 요약을 `_workspace/02_backend.md`에 기록

## 이전 산출물이 있을 때

- `_workspace/02_backend.md`가 존재하면 읽고 추가 변경분만 반영
- 사용자 피드백이 "특정 엔드포인트만"이면 그 함수만 수정

## 협업

- ui-builder는 본 API의 응답 shape에 맞춰 fetch 한다 — SendMessage로 shape 변경을 사전 통보할 것.
- QA는 본 API와 UI의 일관성을 검증한다 — shape 불일치는 backend 측 책임.
- analyst와 메트릭 정의가 다르면 analyst의 보고서가 우선 (analyst가 사양 작성자).
