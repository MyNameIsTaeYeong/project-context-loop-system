"""MCP Tools 테스트."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from context_loop.mcp.context_assembler import assemble_context
from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore


@pytest.fixture
async def meta_store(tmp_path: Path) -> MetadataStore:
    store = MetadataStore(tmp_path / "test.db")
    await store.initialize()
    return store


@pytest.fixture
def vector_store(tmp_path: Path) -> VectorStore:
    store = VectorStore(tmp_path / "vector")
    store.initialize()
    return store


@pytest.fixture
async def graph_store(meta_store: MetadataStore) -> GraphStore:
    store = GraphStore(meta_store)
    await store.load_from_db()
    return store


# --- list_documents 로직 테스트 ---


async def test_list_documents_empty(meta_store: MetadataStore) -> None:
    """문서가 없을 때 빈 목록을 반환한다."""
    docs = await meta_store.list_documents()
    assert docs == []


async def test_list_documents_with_data(meta_store: MetadataStore) -> None:
    """문서 목록을 올바르게 반환한다."""
    await meta_store.create_document(
        source_type="manual", title="테스트 문서", original_content="내용", content_hash="h1",
    )
    docs = await meta_store.list_documents()
    assert len(docs) == 1
    assert docs[0]["title"] == "테스트 문서"


async def test_list_documents_filter_source_type(meta_store: MetadataStore) -> None:
    """source_type으로 필터링한다."""
    await meta_store.create_document(
        source_type="manual", title="수동", original_content="a", content_hash="h1",
    )
    await meta_store.create_document(
        source_type="upload", title="업로드", original_content="b", content_hash="h2",
    )
    manual_docs = await meta_store.list_documents(source_type="manual")
    assert len(manual_docs) == 1
    assert manual_docs[0]["title"] == "수동"


# --- get_document 로직 테스트 ---


async def test_get_document_original(meta_store: MetadataStore) -> None:
    """원본 문서를 조회한다."""
    doc_id = await meta_store.create_document(
        source_type="manual", title="문서", original_content="원본 내용", content_hash="h1",
    )
    doc = await meta_store.get_document(doc_id)
    assert doc is not None
    assert doc["original_content"] == "원본 내용"


async def test_get_document_not_found(meta_store: MetadataStore) -> None:
    """존재하지 않는 문서는 None을 반환한다."""
    doc = await meta_store.get_document(99999)
    assert doc is None


# --- get_graph_context 로직 테스트 ---


async def test_get_graph_context_empty(graph_store: GraphStore) -> None:
    """그래프가 비어있으면 빈 결과를 반환한다."""
    neighbors = graph_store.get_neighbors("nonexistent")
    assert neighbors == []


async def test_get_graph_context_with_data(
    meta_store: MetadataStore,
    graph_store: GraphStore,
) -> None:
    """그래프 탐색이 올바르게 동작한다."""
    from context_loop.processor.graph_extractor import Entity, GraphData, Relation

    doc_id = await meta_store.create_document(
        source_type="manual", title="아키텍처", original_content="내용", content_hash="h1",
    )
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name="Auth Service", entity_type="service"),
            Entity(name="User DB", entity_type="system"),
        ],
        relations=[
            Relation(source="Auth Service", target="User DB", relation_type="uses"),
        ],
    ))

    neighbors = graph_store.get_neighbors("Auth Service", depth=1)
    assert len(neighbors) >= 1
    entity_names = [n.get("entity_name") for n in neighbors]
    assert "Auth Service" in entity_names


# --- context_assembler 테스트 ---


async def test_assemble_context_no_results(
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
) -> None:
    """결과가 없으면 안내 메시지를 반환한다."""
    mock_embedding = AsyncMock()
    mock_embedding.aembed_query = AsyncMock(return_value=[0.0] * 384)

    result = await assemble_context(
        query="테스트 질의",
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=mock_embedding,
    )
    assert "찾을 수 없습니다" in result


async def test_assemble_context_with_graph(
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
) -> None:
    """LLM 기반 그래프 탐색으로 컨텍스트가 포함된 결과를 반환한다."""
    from context_loop.processor.graph_extractor import Entity, GraphData, Relation

    doc_id = await meta_store.create_document(
        source_type="manual", title="시스템", original_content="내용", content_hash="h1",
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

    mock_embedding = AsyncMock()
    mock_embedding.aembed_query = AsyncMock(return_value=[1.0, 0.0])

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value=json.dumps({
        "should_search": True,
        "reasoning": "Gateway 관련 구조 파악 필요",
        "search_steps": [
            {"entity_name": "Gateway", "depth": 1, "focus_relations": ["depends_on"]},
        ],
    }))

    result = await assemble_context(
        query="Gateway",
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=mock_embedding,
        llm_client=mock_llm,
        include_graph=True,
    )
    assert "Gateway" in result
    assert "그래프 컨텍스트" in result
