"""그래프 탐색 페이지·API 테스트."""

from __future__ import annotations

import pytest

from context_loop.processor.graph_extractor import Entity, GraphData, Relation


async def _seed_graph(meta_store, graph_store):
    """두 문서를 만들고, 표기 변형으로 병합되는 그래프를 구성한다.

    doc1: Gateway --depends_on--> AuthService
    doc2: gateway(표기변형, 정규화 병합) --uses--> TokenStore
    => Gateway 노드는 doc1/doc2 양쪽에서 수렴(병합), AuthService/TokenStore 가
       Gateway 를 통해 한 연결 컴포넌트를 이룬다.
    """
    doc1 = await meta_store.create_document(
        source_type="manual", title="Doc1",
        original_content="c", content_hash="h1",
    )
    doc2 = await meta_store.create_document(
        source_type="manual", title="Doc2",
        original_content="c", content_hash="h2",
    )
    await graph_store.save_graph_data(doc1, GraphData(
        entities=[
            Entity(name="Gateway", entity_type="component"),
            Entity(name="AuthService", entity_type="service"),
        ],
        relations=[
            Relation(source="Gateway", target="AuthService", relation_type="depends_on"),
        ],
    ))
    await graph_store.save_graph_data(doc2, GraphData(
        entities=[
            # 표기 변형 — 정규화 키로 doc1 의 "Gateway" 와 병합되어야 한다.
            Entity(name="gateway", entity_type="component"),
            Entity(name="TokenStore", entity_type="component"),
        ],
        relations=[
            Relation(source="gateway", target="TokenStore", relation_type="uses"),
        ],
    ))
    return doc1, doc2


@pytest.mark.asyncio
async def test_graph_page_renders(client):
    """그래프 페이지가 정상 렌더링된다."""
    resp = await client.get("/graph")
    assert resp.status_code == 200
    assert "지식 그래프" in resp.text
    # 세 탭과 탐색기 스크립트가 포함된다
    assert "키워드 탐색" in resp.text
    assert "graph_explorer.js" in resp.text


@pytest.mark.asyncio
async def test_graph_full_empty(client):
    """그래프가 비면 빈 노드/엣지와 0 통계를 반환한다."""
    resp = await client.get("/api/graph/full")
    assert resp.status_code == 200
    data = resp.json()
    assert data["nodes"] == []
    assert data["stats"]["total_nodes"] == 0


@pytest.mark.asyncio
async def test_graph_full_returns_nodes_and_edges(client, stores):
    """전체 그래프가 노드/엣지를 vis-network 형식으로 반환한다."""
    meta_store, _, graph_store = stores
    await _seed_graph(meta_store, graph_store)

    resp = await client.get("/api/graph/full")
    assert resp.status_code == 200
    data = resp.json()
    # Gateway 병합으로 노드는 3개(Gateway, AuthService, TokenStore)
    assert data["stats"]["total_nodes"] == 3
    labels = {n["label"] for n in data["nodes"]}
    assert "AuthService" in labels
    assert "TokenStore" in labels
    # 각 노드는 group(타입)과 vis 형식 필드를 가진다
    assert all("group" in n and "id" in n for n in data["nodes"])
    assert all("from" in e and "to" in e for e in data["edges"])


@pytest.mark.asyncio
async def test_graph_explore_returns_connected_component(client, stores):
    """키워드로 시드 엔티티 + 연결된 모든 엔티티를 반환한다."""
    meta_store, _, graph_store = stores
    await _seed_graph(meta_store, graph_store)

    resp = await client.get("/api/graph/explore", params={"keyword": "Gateway"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["stats"]["matched"] is True
    labels = {n["label"] for n in data["nodes"]}
    # Gateway 에서 양방향으로 연결된 모든 엔티티가 포함되어야 한다
    assert "Gateway" in labels
    assert "AuthService" in labels  # depends_on (outgoing)
    assert "TokenStore" in labels   # uses (병합된 gateway 의 outgoing)
    # 시드 노드가 표시된다
    assert data["stats"]["seed_count"] >= 1
    assert any(n.get("seed") for n in data["nodes"])


@pytest.mark.asyncio
async def test_graph_explore_no_match(client, stores):
    """일치하는 엔티티가 없으면 matched=False 를 반환한다."""
    meta_store, _, graph_store = stores
    await _seed_graph(meta_store, graph_store)

    resp = await client.get(
        "/api/graph/explore", params={"keyword": "존재하지않는엔티티XYZ"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["stats"]["matched"] is False
    assert data["nodes"] == []


@pytest.mark.asyncio
async def test_graph_merges_lists_merged_nodes(client, stores):
    """병합된 노드 그룹이 흡수된 표기와 함께 반환된다."""
    meta_store, _, graph_store = stores
    await _seed_graph(meta_store, graph_store)

    resp = await client.get("/api/graph/merges")
    assert resp.status_code == 200
    data = resp.json()
    # Gateway 가 "Gateway"/"gateway" 두 표기 + 2개 문서로 수렴 → 병합 그룹 1개 이상
    assert data["count"] >= 1
    names = {g["entity_name"] for g in data["groups"]}
    assert "Gateway" in names
    gw = next(g for g in data["groups"] if g["entity_name"] == "Gateway")
    # 두 문서에서 수렴
    assert len(gw["document_ids"]) == 2
    # 병합 방식 라벨 노출 (exact / normalized / new 중)
    assert gw["methods"]


@pytest.mark.asyncio
async def test_graph_merges_empty_when_no_merge(client, stores):
    """병합이 없으면 빈 그룹 목록을 반환한다."""
    meta_store, _, graph_store = stores
    doc = await meta_store.create_document(
        source_type="manual", title="Solo",
        original_content="c", content_hash="hs",
    )
    # 단일 문서, 표기 변형 없음 → 병합 그룹 없음
    await graph_store.save_graph_data(doc, GraphData(
        entities=[Entity(name="Solo", entity_type="component")],
        relations=[],
    ))

    resp = await client.get("/api/graph/merges")
    assert resp.status_code == 200
    assert resp.json()["count"] == 0
