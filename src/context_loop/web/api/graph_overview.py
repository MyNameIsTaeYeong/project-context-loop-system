"""전역 그래프(/graph) 페이지 및 엔티티 통합 품질 API.

본 라우터는 웹 대시보드의 새로운 nav 메뉴 `/graph` 를 위한 백엔드를 제공한다.
설계는 `_workspace/graph-overview-merge-quality/01_merge_diagnosis.md` 4절의
메트릭 정의 사양과 `.claude/agents/graph-overview-api-builder.md` 의 응답 shape
명세를 따른다.

데이터 소스 우선순위:
    1. `GraphStore.graph` (NetworkX 인메모리 그래프) — 진실의 원천.
       `load_from_db()` 가 노드의 `document_ids` 속성과 엣지의 `relation_type`
       을 이미 채워두므로, 별도 SQLite 조회 없이 클러스터/노드/엣지/메트릭을
       산출할 수 있다.
    2. `MetadataStore.list_documents()` — 노드의 출처 문서 메타(title,
       source_type) lookup 용. 한 번만 호출하여 dict 로 캐시한 뒤 N+1
       쿼리를 피한다.

알려진 제약:
    - 현 인덱스 DB 는 레거시 스키마(`graph_node_documents` 부재)일 수 있다
      (analyst 2.1절). `load_from_db` 가 fallback 으로 `graph_nodes.document_id`
      를 `document_ids = {document_id}` 로 채우므로 라우터 측 코드는 영향
      없다. `cross_document_node_ratio` 측정값은 `0.0` 으로 자연히 떨어진다.
    - `duplication_ratio_semantic` 은 임베딩 캐시가 활성화된 경우에만
      산출 가능. 캐시가 비어 있으면 ``null`` + ``status="uncomputed"`` 로
      응답하며, `/api/graph/entity-embeddings/build` 로 idempotent 하게
      빌드할 수 있다.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from langchain_core.embeddings import Embeddings

from context_loop.storage.graph_store import GraphStore, _cosine_similarity
from context_loop.storage.metadata_store import MetadataStore
from context_loop.web.dependencies import (
    get_embedding_client,
    get_graph_store,
    get_meta_store,
    get_templates,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# --- 정규화 헬퍼 ---


_SURFACE_NORM_RE = re.compile(r"[\s_\-.]+")


def _normalize_surface(name: str) -> str:
    """공백/하이픈/언더스코어/마침표를 제거하고 lower-case 한 키.

    analyst 보고서 3.1절 정의와 일치. (역할 정의서 가이드는 `[\\s_\\-]+` 만
    포함하나, analyst 가 최종 채택한 사양은 마침표까지 포함하므로 후자를
    구현 기준으로 사용한다.)
    """
    return _SURFACE_NORM_RE.sub("", name or "").lower()


def _build_document_lookup(documents: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """문서 목록을 id → meta dict 로 캐시한다."""
    return {d["id"]: d for d in documents}


# --- 페이지 라우트 ---


@router.get("/graph")
async def graph_overview_page(request: Request):
    """전역 그래프 페이지 셸을 렌더한다.

    `templates/graph_overview.html` 셸을 반환한다. 본격 UI 는 frontend
    builder 가 작성하지만, 본 라우터 단독으로도 nav → /graph → 200 OK
    가 보장되도록 더미 셸을 함께 제공한다.
    """
    templates = get_templates(request)
    return templates.TemplateResponse("graph_overview.html", {"request": request})


# --- JSON API: 클러스터 ---


@router.get("/api/graph/clusters")
async def get_clusters(
    source_type: str | None = None,
    document_id: int | None = None,
    graph_store: GraphStore = Depends(get_graph_store),
    meta_store: MetadataStore = Depends(get_meta_store),
) -> dict[str, Any]:
    """entity_type 별 클러스터 요약을 반환한다.

    Query params:
        source_type: 특정 source_type 의 문서에 연결된 노드만 필터 (옵션).
        document_id: 특정 문서에 연결된 노드만 필터 (옵션).

    Returns:
        {
          "clusters": [
            {
              "entity_type": "service",
              "node_count": 6,
              "edge_count": 11,
              "top_entities": [
                {"id": 1, "name": "Auth Service", "document_count": 1},
                ...up to 5
              ]
            },
            ...
          ],
          "total_nodes": 21,
          "total_edges": 16,
          "filters": {"source_type": null, "document_id": null}
        }
    """
    # 문서 필터링을 위해 메타 문서 lookup 캐시 구축
    all_docs = await meta_store.list_documents()
    doc_lookup = _build_document_lookup(all_docs)

    # source_type 필터 → 허용 document_id set
    allowed_doc_ids: set[int] | None = None
    if source_type:
        allowed_doc_ids = {
            d["id"] for d in all_docs if d.get("source_type") == source_type
        }
    if document_id is not None:
        allowed_doc_ids = ({document_id} if allowed_doc_ids is None
                           else allowed_doc_ids & {document_id})

    g = graph_store.graph

    def _node_in_scope(data: dict[str, Any]) -> bool:
        if allowed_doc_ids is None:
            return True
        doc_ids = data.get("document_ids") or set()
        return bool(doc_ids & allowed_doc_ids)

    # 타입별 노드 그룹핑 (필터 적용)
    type_to_nodes: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for nid, data in g.nodes(data=True):
        if not _node_in_scope(data):
            continue
        etype = data.get("entity_type", "other") or "other"
        type_to_nodes[etype].append((nid, data))

    # 타입별 엣지 카운트 (양쪽 노드가 같은 type 인 엣지만 — 내부 엣지)
    in_scope_ids = {nid for nodes in type_to_nodes.values() for nid, _ in nodes}
    type_edge_count: Counter[str] = Counter()
    total_edges = 0
    for u, v, _edata in g.edges(data=True):
        if u not in in_scope_ids or v not in in_scope_ids:
            continue
        total_edges += 1
        u_type = g.nodes[u].get("entity_type", "other") or "other"
        v_type = g.nodes[v].get("entity_type", "other") or "other"
        if u_type == v_type:
            type_edge_count[u_type] += 1
        else:
            # 다른 타입을 잇는 엣지는 양쪽 타입에 각각 카운트 (cross-type 표시용)
            type_edge_count[u_type] += 1
            type_edge_count[v_type] += 1

    clusters: list[dict[str, Any]] = []
    for etype, nodes in sorted(type_to_nodes.items(), key=lambda kv: -len(kv[1])):
        # degree 기준 내림차순 정렬 → 상위 5 top_entities
        scored = []
        for nid, data in nodes:
            deg = g.degree(nid)
            scored.append((deg, nid, data))
        scored.sort(key=lambda x: -x[0])
        top_entities = [
            {
                "id": nid,
                "name": data.get("entity_name", ""),
                "document_count": len(data.get("document_ids") or set()),
            }
            for _deg, nid, data in scored[:5]
        ]
        clusters.append({
            "entity_type": etype,
            "node_count": len(nodes),
            "edge_count": int(type_edge_count.get(etype, 0)),
            "top_entities": top_entities,
        })

    # `doc_lookup` 은 향후 확장을 위해 호출만 유지 — 본 응답에는 미사용
    _ = doc_lookup

    return {
        "clusters": clusters,
        "total_nodes": sum(len(v) for v in type_to_nodes.values()),
        "total_edges": total_edges,
        "filters": {"source_type": source_type, "document_id": document_id},
    }


@router.get("/api/graph/cluster/{entity_type}/nodes")
async def get_cluster_nodes(
    entity_type: str,
    limit: int = 200,
    offset: int = 0,
    q: str | None = None,
    graph_store: GraphStore = Depends(get_graph_store),
) -> dict[str, Any]:
    """클러스터(entity_type) 내부의 노드 + 엣지를 반환한다.

    Query params:
        limit: 최대 노드 수 (기본 200).
        offset: 페이지네이션 offset.
        q: entity_name 부분 일치 검색어 (대소문자 무시).

    Returns:
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
            },
            ...
          ],
          "edges": [
            {
              "id": 12,
              "source": 1,
              "target": 3,
              "relation_type": "depends_on"
            },
            ...
          ],
          "total": 6,
          "limit": 200,
          "offset": 0
        }
    """
    if limit < 1 or limit > 2000:
        raise HTTPException(400, "limit 는 1..2000 범위여야 합니다.")
    if offset < 0:
        raise HTTPException(400, "offset 는 0 이상이어야 합니다.")

    g = graph_store.graph
    q_lower = (q or "").strip().lower()

    matched: list[tuple[int, dict[str, Any]]] = []
    for nid, data in g.nodes(data=True):
        etype = data.get("entity_type", "other") or "other"
        if etype != entity_type:
            continue
        if q_lower and q_lower not in (data.get("entity_name", "") or "").lower():
            continue
        matched.append((nid, data))

    # 안정적인 정렬 — degree 내림차순, 이름 사전순
    matched.sort(key=lambda nd: (-g.degree(nd[0]), (nd[1].get("entity_name") or "").lower()))

    total = len(matched)
    page = matched[offset:offset + limit]
    page_ids = {nid for nid, _ in page}

    nodes_out = [
        {
            "id": nid,
            "name": data.get("entity_name", ""),
            "entity_type": data.get("entity_type", "other") or "other",
            "document_count": len(data.get("document_ids") or set()),
            "document_ids": sorted(data.get("document_ids") or set()),
            "degree": int(g.degree(nid)),
        }
        for nid, data in page
    ]

    # 페이지에 들어간 노드들 사이의 엣지만 반환 (시각화 일관성)
    edges_out: list[dict[str, Any]] = []
    for u, v, edata in g.edges(data=True):
        if u in page_ids and v in page_ids:
            edges_out.append({
                "id": edata.get("id"),
                "source": u,
                "target": v,
                "relation_type": edata.get("relation_type", "related_to"),
            })

    return {
        "entity_type": entity_type,
        "nodes": nodes_out,
        "edges": edges_out,
        "total": total,
        "limit": limit,
        "offset": offset,
    }


# --- JSON API: 노드 상세 ---


@router.get("/api/graph/node/{node_id}")
async def get_node_detail(
    node_id: int,
    graph_store: GraphStore = Depends(get_graph_store),
    meta_store: MetadataStore = Depends(get_meta_store),
) -> dict[str, Any]:
    """단일 노드의 상세 + 출처 문서 + 이웃을 반환한다.

    Returns:
        {
          "id": 1,
          "name": "Auth Service",
          "entity_type": "service",
          "properties": {"description": "..."},
          "document_ids": [5],
          "documents": [
            {"id": 5, "title": "백엔드 아키텍처", "source_type": "manual", "url": null}
          ],
          "neighbors": [
            {
              "id": 3,
              "name": "PostgreSQL DB",
              "entity_type": "component",
              "relation_type": "depends_on",
              "direction": "out"
            },
            ...
          ]
        }
    """
    g = graph_store.graph
    if not g.has_node(node_id):
        raise HTTPException(404, f"노드를 찾을 수 없습니다: {node_id}")

    data = g.nodes[node_id]
    doc_ids = sorted(data.get("document_ids") or set())

    # 출처 문서 메타 lookup (N+1 회피 — list_documents 1회 호출)
    all_docs = await meta_store.list_documents()
    doc_lookup = _build_document_lookup(all_docs)
    documents = []
    for did in doc_ids:
        d = doc_lookup.get(did)
        if not d:
            # graph_node_documents 에 stale link 가 있을 수 있음 — graceful
            continue
        documents.append({
            "id": d["id"],
            "title": d.get("title", ""),
            "source_type": d.get("source_type", ""),
            "url": d.get("url"),
        })

    # 이웃 노드 (out + in)
    neighbors: list[dict[str, Any]] = []
    for succ in g.successors(node_id):
        edata = g.edges[node_id, succ]
        s_data = g.nodes[succ]
        neighbors.append({
            "id": succ,
            "name": s_data.get("entity_name", ""),
            "entity_type": s_data.get("entity_type", "other") or "other",
            "relation_type": edata.get("relation_type", "related_to"),
            "direction": "out",
        })
    for pred in g.predecessors(node_id):
        edata = g.edges[pred, node_id]
        p_data = g.nodes[pred]
        neighbors.append({
            "id": pred,
            "name": p_data.get("entity_name", ""),
            "entity_type": p_data.get("entity_type", "other") or "other",
            "relation_type": edata.get("relation_type", "related_to"),
            "direction": "in",
        })

    return {
        "id": node_id,
        "name": data.get("entity_name", ""),
        "entity_type": data.get("entity_type", "other") or "other",
        "properties": data.get("properties") or {},
        "document_ids": doc_ids,
        "documents": documents,
        "neighbors": neighbors,
    }


# --- JSON API: 통합 품질 메트릭 ---


def _compute_surface_duplicates(
    nodes: list[tuple[int, dict[str, Any]]],
) -> tuple[list[dict[str, Any]], int]:
    """표면 정규화 기반 잠재 중복 그룹 + 영향 노드 수.

    analyst 4.1절: `key(node) = normalize_surface(entity_name)`, 2개 이상의
    노드를 가진 그룹만 보고. 다국어/Unicode 보존.
    """
    groups: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for nid, data in nodes:
        key = _normalize_surface(data.get("entity_name", ""))
        if not key:
            continue
        groups[key].append((nid, data))

    result_groups: list[dict[str, Any]] = []
    affected = 0
    for key, members in groups.items():
        if len(members) < 2:
            continue
        affected += len(members)
        result_groups.append({
            "kind": "surface_normalized",
            "key": key,
            "members": [
                {
                    "id": nid,
                    "name": data.get("entity_name", ""),
                    "type": data.get("entity_type", "other") or "other",
                }
                for nid, data in members
            ],
        })
    return result_groups, affected


def _compute_surface_same_type_duplicates(
    nodes: list[tuple[int, dict[str, Any]]],
) -> int:
    """`(surface_key, entity_type)` 동일 그룹의 영향 노드 수 (4.2)."""
    groups: dict[tuple[str, str], int] = defaultdict(int)
    for _nid, data in nodes:
        key = (
            _normalize_surface(data.get("entity_name", "")),
            data.get("entity_type", "") or "",
        )
        if not key[0]:
            continue
        groups[key] += 1
    return sum(c for c in groups.values() if c >= 2)


def _compute_type_conflicts(
    nodes: list[tuple[int, dict[str, Any]]],
) -> tuple[list[dict[str, Any]], int]:
    """`LOWER(name)` 동일하지만 type 이 2종 이상으로 분기된 그룹 (4.4)."""
    by_lower: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for nid, data in nodes:
        key = (data.get("entity_name") or "").strip().lower()
        if not key:
            continue
        by_lower[key].append((nid, data))

    conflict_groups: list[dict[str, Any]] = []
    for key, members in by_lower.items():
        types = {(m[1].get("entity_type", "") or "") for m in members}
        if len(types) >= 2:
            conflict_groups.append({
                "kind": "type_conflict",
                "key": key,
                "members": [
                    {
                        "id": nid,
                        "name": data.get("entity_name", ""),
                        "type": data.get("entity_type", "other") or "other",
                    }
                    for nid, data in members
                ],
            })
    return conflict_groups, len(conflict_groups)


def _compute_fuzzy_candidates(
    nodes: list[tuple[int, dict[str, Any]]],
    *,
    threshold: float = 0.85,
    max_pairs: int = 50,
) -> list[dict[str, Any]]:
    """동일 type 내부에서 SequenceMatcher ratio ≥ threshold 쌍 (analyst 3.2 대체 진단).

    O(N^2) 이므로 type 별로 그룹핑 후 작은 type 내부에서만 비교한다.
    실용적으로는 인덱스가 < 1k 노드 규모라는 가정하에 동작.
    """
    type_to_nodes: dict[str, list[tuple[int, dict[str, Any]]]] = defaultdict(list)
    for nid, data in nodes:
        etype = data.get("entity_type", "other") or "other"
        type_to_nodes[etype].append((nid, data))

    pairs: list[dict[str, Any]] = []
    for etype, group in type_to_nodes.items():
        n = len(group)
        if n < 2:
            continue
        for i in range(n):
            for j in range(i + 1, n):
                a_name = group[i][1].get("entity_name", "") or ""
                b_name = group[j][1].get("entity_name", "") or ""
                # 표면 정규화가 동일하면 4.1 그룹에서 이미 보고됨 — 스킵
                if _normalize_surface(a_name) == _normalize_surface(b_name):
                    continue
                ratio = SequenceMatcher(None, a_name.lower(), b_name.lower()).ratio()
                if ratio >= threshold:
                    pairs.append({
                        "a": {"id": group[i][0], "name": a_name, "type": etype},
                        "b": {"id": group[j][0], "name": b_name, "type": etype},
                        "similarity": round(ratio, 4),
                        "method": "sequence_matcher",
                    })
                    if len(pairs) >= max_pairs:
                        return pairs
    return pairs


def _compute_semantic_duplicates(
    graph_store: GraphStore,
    *,
    threshold: float = 0.85,
) -> tuple[float | None, int, str]:
    """임베딩 cosine ≥ threshold 쌍을 union-find 로 클러스터링.

    Returns:
        (duplication_ratio_semantic | None, dup_node_count, status)
        status 는 "computed" / "uncomputed" / "empty_graph".
    """
    g = graph_store.graph
    total = g.number_of_nodes()
    if total == 0:
        return (0.0, 0, "empty_graph")

    # 임베딩 캐시 활성화 여부
    if graph_store.entity_embedding_count == 0:
        return (None, 0, "uncomputed")

    # union-find
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    # 모든 캐시된 임베딩 쌍 비교 — O(N^2). 작은 인덱스 가정.
    items = list(graph_store._entity_embeddings.items())  # noqa: SLF001
    for nid, _ in items:
        parent[nid] = nid

    for i in range(len(items)):
        nid_i, (_name_i, emb_i) = items[i]
        for j in range(i + 1, len(items)):
            nid_j, (_name_j, emb_j) = items[j]
            sim = _cosine_similarity(emb_i, emb_j)
            if sim >= threshold:
                union(nid_i, nid_j)

    # 클러스터 크기 집계
    cluster_size: Counter[int] = Counter()
    for nid in parent:
        cluster_size[find(nid)] += 1
    dup_nodes = sum(c for c in cluster_size.values() if c >= 2)

    ratio = dup_nodes / max(total, 1)
    return (ratio, dup_nodes, "computed")


async def _compute_orphan_edge_count(meta_store: MetadataStore) -> int:
    """SQLite 측에서 양쪽 끝 노드가 모두 graph_nodes 에 없는 엣지 수.

    NetworkX 는 노드 삭제 시 엣지도 함께 정리하므로 0 이 정상. SQLite 에
    직접 쿼리하여 정합성 알람을 노출한다.
    """
    cursor = await meta_store.db.execute(
        """SELECT COUNT(*) FROM graph_edges e
           WHERE NOT EXISTS (SELECT 1 FROM graph_nodes n WHERE n.id = e.source_node_id)
              OR NOT EXISTS (SELECT 1 FROM graph_nodes n WHERE n.id = e.target_node_id)"""
    )
    row = await cursor.fetchone()
    return int(row[0]) if row else 0


@router.get("/api/graph/merge-quality")
async def get_merge_quality(
    include_semantic: bool = False,
    semantic_threshold: float = 0.85,
    fuzzy_threshold: float = 0.85,
    graph_store: GraphStore = Depends(get_graph_store),
    meta_store: MetadataStore = Depends(get_meta_store),
) -> dict[str, Any]:
    """엔티티 통합 품질 메트릭 + 잠재 중복 그룹 인벤토리.

    Query params:
        include_semantic: True 면 임베딩 기반 의미 중복도 계산 시도.
            캐시가 비어있으면 status="uncomputed" 로 응답.
        semantic_threshold: 의미 중복 cosine 임계 (기본 0.85).
        fuzzy_threshold: 표면 fuzzy 쌍 SequenceMatcher 임계 (기본 0.85).

    Returns:
        {
          "total_nodes": 21,
          "duplicate_groups": [
            {
              "kind": "surface_normalized",
              "key": "authservice",
              "members": [
                {"id": 1, "name": "AuthService", "type": "service"},
                {"id": 7, "name": "auth-service", "type": "service"}
              ]
            },
            ...
          ],
          "type_conflict_groups": [
            {"kind": "type_conflict", "key": "kafka",
             "members": [{"id": 2, "name": "Kafka", "type": "system"},
                         {"id": 9, "name": "Kafka", "type": "component"}]}
          ],
          "fuzzy_candidates": [
            {"a": {"id": 3, "name": "User", "type": "entity"},
             "b": {"id": 8, "name": "Users", "type": "entity"},
             "similarity": 0.88, "method": "sequence_matcher"}
          ],
          "metrics": {
            "duplication_ratio_surface": 0.0,
            "duplication_ratio_surface_same_type": 0.0,
            "duplication_ratio_semantic": null,
            "semantic_status": "uncomputed",
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
    """
    g = graph_store.graph
    nodes = [(nid, data) for nid, data in g.nodes(data=True)]
    total_nodes = len(nodes)

    # 4.1 surface duplicates
    dup_groups, surface_affected = _compute_surface_duplicates(nodes)
    duplication_ratio_surface = (
        surface_affected / total_nodes if total_nodes else 0.0
    )

    # 4.2 surface + same type
    same_type_affected = _compute_surface_same_type_duplicates(nodes)
    duplication_ratio_surface_same_type = (
        same_type_affected / total_nodes if total_nodes else 0.0
    )

    # 4.4 type conflicts
    type_conflict_groups, type_conflict_count = _compute_type_conflicts(nodes)

    # 보조: fuzzy 후보
    fuzzy_candidates = _compute_fuzzy_candidates(nodes, threshold=fuzzy_threshold)

    # 4.3 semantic — 임베딩 캐시 필요
    sem_ratio: float | None
    sem_status: str
    if include_semantic:
        sem_ratio, _sem_dup_nodes, sem_status = _compute_semantic_duplicates(
            graph_store, threshold=semantic_threshold,
        )
    else:
        sem_ratio, sem_status = (None, "skipped")

    # 4.5 cross-document
    cross_doc_count = sum(
        1 for _nid, data in nodes if len(data.get("document_ids") or set()) >= 2
    )
    cross_document_node_ratio = (
        cross_doc_count / total_nodes if total_nodes else 0.0
    )

    # 4.6 orphan edges (SQLite 직접 점검)
    try:
        orphan_edge_count = await _compute_orphan_edge_count(meta_store)
    except Exception:
        logger.warning("orphan_edge_count 계산 실패 — 0 반환", exc_info=True)
        orphan_edge_count = 0

    # 4.7 trim 불일치 노드
    trim_unsafe = sum(
        1 for _nid, data in nodes
        if (data.get("entity_name") or "") != (data.get("entity_name") or "").strip()
    )

    return {
        "total_nodes": total_nodes,
        "duplicate_groups": dup_groups,
        "type_conflict_groups": type_conflict_groups,
        "fuzzy_candidates": fuzzy_candidates,
        "metrics": {
            "duplication_ratio_surface": round(duplication_ratio_surface, 4),
            "duplication_ratio_surface_same_type": round(
                duplication_ratio_surface_same_type, 4,
            ),
            "duplication_ratio_semantic": (
                round(sem_ratio, 4) if sem_ratio is not None else None
            ),
            "semantic_status": sem_status,
            "type_conflict_count": type_conflict_count,
            "cross_document_node_ratio": round(cross_document_node_ratio, 4),
            "cross_document_node_count": cross_doc_count,
            "orphan_edge_count": orphan_edge_count,
            "leading_trailing_whitespace_node_count": trim_unsafe,
        },
        "scale": {
            "total_nodes": total_nodes,
            "total_edges": g.number_of_edges(),
            "entity_embedding_count": graph_store.entity_embedding_count,
        },
    }


# --- JSON API: 엔티티 임베딩 빌드 ---


@router.post("/api/graph/entity-embeddings/build")
async def build_entity_embeddings(
    graph_store: GraphStore = Depends(get_graph_store),
    embedding_client: Embeddings = Depends(get_embedding_client),
) -> dict[str, Any]:
    """엔티티 이름 임베딩 캐시를 idempotent 하게 빌드한다.

    `GraphStore._entity_embeddings` 는 인메모리 캐시이며 이미 채워진
    노드는 다시 임베딩하지 않는다. `duplication_ratio_semantic` 메트릭이
    필요한 시점에 UI 에서 호출.

    Returns:
        {
          "added": 21,
          "total_cached": 21,
          "total_nodes": 21,
          "status": "ok"
        }
    """
    try:
        added = await graph_store.build_entity_embeddings(embedding_client)
    except Exception as e:
        logger.exception("엔티티 임베딩 빌드 실패")
        raise HTTPException(500, f"엔티티 임베딩 빌드 실패: {e}") from e

    return {
        "added": added,
        "total_cached": graph_store.entity_embedding_count,
        "total_nodes": graph_store.graph.number_of_nodes(),
        "status": "ok",
    }
