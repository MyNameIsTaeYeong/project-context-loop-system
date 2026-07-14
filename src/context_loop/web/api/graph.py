"""그래프 탐색 페이지 및 API 엔드포인트.

구축된 지식 그래프를 웹 대시보드에서 시각화·탐색한다:
  - ``GET /graph``                  : 그래프 탐색 페이지
  - ``GET /api/graph/full``         : 전체 그래프(노드/엣지) JSON (상한 적용)
  - ``GET /api/graph/explore``      : 키워드 → 연결된 모든 엔티티 서브그래프
  - ``GET /api/graph/merges``       : 병합된 노드 그룹 목록
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from langchain_core.embeddings import Embeddings

from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.web.dependencies import (
    get_embedding_client,
    get_graph_store,
    get_meta_store,
    get_templates,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# 전체 그래프 시각화 시 한 번에 그리는 노드 상한. 거대 그래프에서 브라우저
# 렌더가 멈추는 것을 막는다. 초과 시 노드는 차수(degree) 높은 순으로 추린다.
_FULL_GRAPH_MAX_NODES = 300


@router.get("/graph")
async def graph_page(request: Request):
    """그래프 탐색 페이지."""
    templates = get_templates(request)
    return templates.TemplateResponse(request, "graph.html")


def _node_payload(node_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """NetworkX 노드 데이터를 vis-network 노드 dict 로 변환한다."""
    props = data.get("properties") or {}
    description = ""
    if isinstance(props, dict):
        description = str(props.get("description") or "")
    doc_ids = data.get("document_ids")
    doc_count = len(doc_ids) if isinstance(doc_ids, set) else 0
    name = data.get("entity_name", str(node_id))
    etype = data.get("entity_type", "other") or "other"
    title = f"{name} ({etype})"
    if description:
        title += f"\n{description}"
    if doc_count:
        title += f"\n문서 {doc_count}건"
    return {
        "id": node_id,
        "label": name,
        "group": etype,
        "title": title,
    }


def _edge_payload(source: int, target: int, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "from": source,
        "to": target,
        "label": data.get("relation_type", ""),
    }


@router.get("/api/graph/full")
async def graph_full(
    graph_store: GraphStore = Depends(get_graph_store),
) -> dict[str, Any]:
    """전체 그래프를 vis-network 형식으로 반환한다.

    노드 수가 상한을 초과하면 차수(연결 수) 높은 노드부터 추려 반환하고,
    그 노드들 사이의 엣지만 포함한다.
    """
    g = graph_store.graph
    total_nodes = g.number_of_nodes()
    total_edges = g.number_of_edges()

    node_ids = list(g.nodes())
    truncated = False
    if total_nodes > _FULL_GRAPH_MAX_NODES:
        truncated = True
        # 차수 높은 노드 우선 (허브 중심으로 보여줌)
        node_ids = sorted(node_ids, key=lambda n: g.degree(n), reverse=True)
        node_ids = node_ids[:_FULL_GRAPH_MAX_NODES]

    keep = set(node_ids)
    nodes = [_node_payload(n, dict(g.nodes[n])) for n in node_ids]
    edges = [
        _edge_payload(u, v, data)
        for u, v, data in g.edges(data=True)
        if u in keep and v in keep
    ]
    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "shown_nodes": len(nodes),
            "shown_edges": len(edges),
            "truncated": truncated,
        },
    }


@router.get("/api/graph/explore")
async def graph_explore(
    keyword: str = Query(..., min_length=1),
    depth: int | None = Query(
        None, ge=1, le=20,
        description="탐색 깊이(hop). 미지정 시 연결된 전체를 탐색한다.",
    ),
    graph_store: GraphStore = Depends(get_graph_store),
    embedding_client: Embeddings = Depends(get_embedding_client),
) -> dict[str, Any]:
    """키워드 엔티티부터 지정 depth(미지정 시 전체)까지 연결 엔티티를 반환한다.

    표면 매칭(완전/부분/짧은 이름)이 실패하면 키워드 임베딩으로 가장 가까운
    노드를 시드로 사용한다(엔티티 임베딩 캐시가 있을 때). 각 노드에는 시드
    로부터의 hop 거리가 포함된다.
    """
    # 임베딩 fallback 준비 — 아직 임베딩되지 않은 엔티티가 남아 있으면 구축
    # (기동 사전 구축에서 부분 실패한 노드를 점진적으로 보완), 키워드 임베딩 계산.
    embedding_fallback = None
    try:
        if graph_store.unembedded_entity_count > 0:
            await graph_store.build_entity_embeddings(embedding_client)
        if graph_store.entity_embedding_count > 0:
            embedding_fallback = await embedding_client.aembed_query(keyword)
    except Exception:
        logger.warning("키워드 임베딩 fallback 준비 실패", exc_info=True)

    component = graph_store.get_connected_component(
        keyword,
        depth=depth,
        embedding_fallback=embedding_fallback,
    )
    if not component:
        return {
            "nodes": [],
            "edges": [],
            "stats": {"matched": False, "shown_nodes": 0, "shown_edges": 0},
            "keyword": keyword,
            "depth": depth,
        }

    node_ids = [n["id"] for n in component]
    keep = set(node_ids)
    g = graph_store.graph
    nodes = []
    for n in component:
        payload = _node_payload(n["id"], n)
        payload["hop"] = n.get("hop", 0)
        if n.get("is_seed"):
            payload["seed"] = True
        nodes.append(payload)
    edges = [
        _edge_payload(u, v, data)
        for u, v, data in g.edges(data=True)
        if u in keep and v in keep
    ]
    seed_count = sum(1 for n in component if n.get("is_seed"))
    max_hop = max((n.get("hop", 0) for n in component), default=0)
    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "matched": True,
            "shown_nodes": len(nodes),
            "shown_edges": len(edges),
            "seed_count": seed_count,
            "max_hop": max_hop,
        },
        "keyword": keyword,
        "depth": depth,
    }


@router.get("/api/graph/node/{node_id}")
async def graph_node_detail(
    node_id: int,
    graph_store: GraphStore = Depends(get_graph_store),
    meta_store: MetadataStore = Depends(get_meta_store),
) -> dict[str, Any]:
    """노드의 상세 정보 — 출처 문서와 병합 내역을 반환한다.

    - documents: 이 노드(정규 노드)에 연결된 모든 문서 (제목·source_type).
    - merges: graph_merge_log 기반, 이 노드로 흡수된 원본 표기와 병합 방식.
    """
    g = graph_store.graph
    if not g.has_node(node_id):
        raise HTTPException(404, "노드를 찾을 수 없습니다.")

    data = dict(g.nodes[node_id])
    props = data.get("properties") or {}
    description = ""
    if isinstance(props, dict):
        description = str(props.get("description") or "")

    # 출처 문서 — 정규 노드에 연결된 문서 ID 목록.
    doc_ids = await meta_store.get_node_document_ids(node_id)
    documents = []
    for did in doc_ids:
        doc = await meta_store.get_document(did)
        if doc:
            documents.append({
                "document_id": did,
                "title": doc.get("title", f"문서 #{did}"),
                "source_type": doc.get("source_type", ""),
            })
        else:
            documents.append({
                "document_id": did, "title": f"문서 #{did}", "source_type": "",
            })

    # 병합 내역 — 이 정규 노드로 기록된 머지 로그.
    log = await meta_store.get_graph_merge_log(canonical_node_id=node_id)
    merges = [
        {
            "raw_entity_name": r.get("raw_entity_name", ""),
            "raw_entity_type": r.get("raw_entity_type", ""),
            "source_document_id": r.get("source_document_id"),
            "merge_method": r.get("merge_method", ""),
            "created_at": r.get("created_at", ""),
        }
        for r in log
    ]

    return {
        "id": node_id,
        "entity_name": data.get("entity_name", ""),
        "entity_type": data.get("entity_type", "other"),
        "description": description,
        "documents": documents,
        "merges": merges,
    }


@router.get("/api/graph/merges")
async def graph_merges(
    include_deleted: bool = False,
    meta_store: MetadataStore = Depends(get_meta_store),
) -> dict[str, Any]:
    """병합된(크로스-문서 수렴) 노드 그룹 목록을 반환한다.

    기본값은 정규 노드가 이미 삭제된(병합 로그만 남은) 그룹을 제외한다.
    ``include_deleted=true`` 면 삭제된 노드도 함께 반환한다.
    """
    groups = await meta_store.get_merged_node_groups(
        min_variants=2,
        include_deleted=include_deleted,
    )
    return {"groups": groups, "count": len(groups)}
