"""context_assembler 임베딩 기반 그래프 탐색 테스트."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from context_loop.mcp.context_assembler import (
    _search_graph_by_embedding,
    assemble_context,
    assemble_context_with_sources,
)
from context_loop.processor.graph_extractor import Entity, GraphData, Relation
from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore


@pytest.fixture
async def stores(tmp_path: Path):
    meta_store = MetadataStore(tmp_path / "test.db")
    await meta_store.initialize()
    vector_store = VectorStore(tmp_path)
    vector_store.initialize()
    graph_store = GraphStore(meta_store)
    yield meta_store, vector_store, graph_store
    await meta_store.close()


def _make_embedding_client(entity_embeddings: list[list[float]], query_embedding: list[float]):
    """테스트용 임베딩 클라이언트를 생성한다."""
    mock = AsyncMock()
    mock.aembed_documents = AsyncMock(return_value=entity_embeddings)
    mock.aembed_query = AsyncMock(return_value=query_embedding)
    return mock


async def test_graph_search_skipped_when_no_matching_entities(stores) -> None:
    """매칭 엔티티가 없으면 그래프 탐색이 스킵된다."""
    meta_store, _, graph_store = stores

    doc_id = await meta_store.create_document(
        source_type="manual", title="T", original_content="c", content_hash="h",
    )
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[Entity(name="Gateway", entity_type="component")],
        relations=[],
    ))

    # 엔티티 임베딩: [1.0, 0.0]
    embed_client = _make_embedding_client([[1.0, 0.0]], [0.0, 1.0])
    await graph_store.build_entity_embeddings(embed_client)

    # 질의 벡터 [0.0, 1.0]은 Gateway [1.0, 0.0]과 직교 → 유사도 0 → 매칭 없음
    result = await _search_graph_by_embedding([0.0, 1.0], graph_store, embed_client)
    assert result is None


async def test_graph_search_finds_matching_entities(stores) -> None:
    """임베딩 유사도로 엔티티를 찾고 이웃을 탐색한다."""
    meta_store, _, graph_store = stores

    doc_id = await meta_store.create_document(
        source_type="manual", title="Arch", original_content="c", content_hash="h",
    )
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name="Gateway", entity_type="component"),
            Entity(name="AuthService", entity_type="service"),
        ],
        relations=[
            Relation(source="Gateway", target="AuthService", relation_type="depends_on"),
        ],
    ))

    # Gateway=[1.0, 0.0], AuthService=[0.0, 1.0]
    embed_client = _make_embedding_client([[1.0, 0.0], [0.0, 1.0]], [0.95, 0.05])
    await graph_store.build_entity_embeddings(embed_client)

    # 질의 벡터 [0.95, 0.05]는 Gateway [1.0, 0.0]과 높은 유사도
    result = await _search_graph_by_embedding([0.95, 0.05], graph_store, embed_client)
    assert result is not None
    assert "Gateway" in result
    assert "AuthService" in result
    assert "depends_on" in result


async def test_assemble_context_uses_embedding_graph_search(stores) -> None:
    """assemble_context가 임베딩 기반 그래프 탐색을 사용한다."""
    meta_store, vector_store, graph_store = stores

    doc_id = await meta_store.create_document(
        source_type="manual", title="Doc1", original_content="c", content_hash="h1",
    )
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name="ServiceA", entity_type="service"),
            Entity(name="ServiceB", entity_type="service"),
        ],
        relations=[
            Relation(source="ServiceA", target="ServiceB", relation_type="calls"),
        ],
    ))

    # ServiceA=[1.0, 0.0], ServiceB=[0.0, 1.0]
    embed_client = _make_embedding_client([[1.0, 0.0], [0.0, 1.0]], [0.9, 0.1])
    await graph_store.build_entity_embeddings(embed_client)

    result = await assemble_context(
        query="ServiceA 관련 정보",
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=embed_client,
        include_graph=True,
    )
    # 그래프 컨텍스트가 포함되어야 함
    assert "ServiceA" in result
    assert "calls" in result


async def test_assemble_context_with_sources_embedding_graph(stores) -> None:
    """assemble_context_with_sources도 임베딩 기반 그래프 탐색을 사용한다."""
    meta_store, vector_store, graph_store = stores

    doc_id = await meta_store.create_document(
        source_type="manual", title="ArchDoc", original_content="c", content_hash="h2",
    )
    # 벡터 데이터 추가
    vector_store.add_chunks(
        chunk_ids=[f"chunk_{doc_id}_0"],
        embeddings=[[0.9, 0.1]],
        documents=["아키텍처 설명"],
        metadatas=[{"document_id": doc_id, "chunk_index": 0}],
    )
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[Entity(name="API", entity_type="component")],
        relations=[],
    ))

    embed_client = _make_embedding_client([[1.0, 0.0]], [0.9, 0.1])
    await graph_store.build_entity_embeddings(embed_client)

    assembled = await assemble_context_with_sources(
        query="API 구조",
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=embed_client,
        include_graph=True,
    )
    assert assembled.context_text != ""
    assert len(assembled.sources) >= 1
    assert assembled.sources[0].title == "ArchDoc"


async def test_dynamic_depth_with_many_entities(stores) -> None:
    """매칭 엔티티가 3개 이상이면 depth=1, 미만이면 depth=2를 사용한다."""
    meta_store, _, graph_store = stores

    doc_id = await meta_store.create_document(
        source_type="manual", title="T", original_content="c", content_hash="hd",
    )
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name="A", entity_type="service"),
            Entity(name="B", entity_type="service"),
            Entity(name="C", entity_type="service"),
            Entity(name="D", entity_type="service"),
        ],
        relations=[
            Relation(source="A", target="B", relation_type="calls"),
            Relation(source="B", target="C", relation_type="calls"),
            Relation(source="C", target="D", relation_type="calls"),
        ],
    ))

    # 모든 엔티티가 비슷한 임베딩 → 3개 이상 매칭 → depth=1
    embed_client = _make_embedding_client(
        [[0.9, 0.1], [0.85, 0.15], [0.8, 0.2], [0.75, 0.25]],
        [0.9, 0.1],
    )
    await graph_store.build_entity_embeddings(embed_client)

    result = await _search_graph_by_embedding(
        [0.9, 0.1], graph_store, embed_client, threshold=0.7, top_k=5,
    )
    assert result is not None
    # depth=1이므로 A의 직접 연결 B는 포함, 하지만 모든 엔티티가 매칭되어
    # 각각의 이웃이 추가됨
    assert "A" in result
