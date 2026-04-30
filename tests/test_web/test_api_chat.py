"""채팅 API 엔드포인트 테스트."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore
from context_loop.web.app import create_app

_DIM = 4  # 테스트용 임베딩 차원


@pytest.fixture
async def chat_stores(tmp_path: Path):
    meta_store = MetadataStore(tmp_path / "test.db")
    await meta_store.initialize()
    vector_store = VectorStore(tmp_path)
    vector_store.initialize()
    graph_store = GraphStore(meta_store)
    yield meta_store, vector_store, graph_store
    await meta_store.close()


@pytest.fixture
async def chat_client(chat_stores):
    meta_store, vector_store, graph_store = chat_stores

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value="테스트 답변입니다.")

    mock_embedding = AsyncMock()
    mock_embedding.aembed_query = AsyncMock(return_value=[1.0, 0.0, 0.0, 0.0])

    app = create_app()
    app.state.meta_store = meta_store
    app.state.vector_store = vector_store
    app.state.graph_store = graph_store
    app.state.llm_client = mock_llm
    app.state.embedding_client = mock_embedding

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


async def test_chat_page(chat_client: AsyncClient) -> None:
    """채팅 페이지가 정상 렌더링된다."""
    resp = await chat_client.get("/chat")
    assert resp.status_code == 200
    assert "Chat" in resp.text


async def test_chat_api_no_documents(chat_client: AsyncClient) -> None:
    """문서가 없으면 안내 메시지를 반환한다."""
    resp = await chat_client.post(
        "/api/chat",
        json={"query": "테스트 질문"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "answer" in data
    assert "sources" in data
    assert isinstance(data["sources"], list)


async def test_chat_api_returns_answer_and_sources(chat_stores, chat_client: AsyncClient) -> None:
    """문서가 있으면 답변과 출처를 반환한다."""
    meta_store, vector_store, _ = chat_stores

    doc_id = await meta_store.create_document(
        source_type="manual",
        title="테스트 문서",
        original_content="테스트 내용입니다.",
        content_hash="h1",
    )
    vector_store.add_chunks(
        chunk_ids=[f"chunk_{doc_id}_0"],
        embeddings=[[1.0, 0.0, 0.0, 0.0]],
        documents=["테스트 내용입니다."],
        metadatas=[{"document_id": doc_id, "chunk_index": 0}],
    )

    resp = await chat_client.post(
        "/api/chat",
        json={"query": "테스트 질문"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["answer"] == "테스트 답변입니다."
    assert len(data["sources"]) >= 1
    assert data["sources"][0]["title"] == "테스트 문서"
    assert data["sources"][0]["document_id"] == doc_id


async def test_chat_api_with_graph_context(chat_stores, chat_client: AsyncClient) -> None:
    """그래프 컨텍스트가 포함된 질의도 정상 동작한다."""
    from context_loop.processor.graph_extractor import Entity, GraphData, Relation

    meta_store, _, graph_store = chat_stores

    doc_id = await meta_store.create_document(
        source_type="manual",
        title="아키텍처 문서",
        original_content="시스템 구조",
        content_hash="h2",
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

    # LLM mock: 첫 호출은 그래프 탐색 계획, 두 번째는 최종 답변
    app = chat_client._transport.app  # type: ignore[attr-defined]
    plan_response = json.dumps({
        "should_search": True,
        "reasoning": "Gateway 구조 파악 필요",
        "search_steps": [{"entity_name": "Gateway", "depth": 1, "focus_relations": []}],
    })
    app.state.llm_client.complete = AsyncMock(
        side_effect=[plan_response, "테스트 답변입니다."]
    )

    resp = await chat_client.post(
        "/api/chat",
        json={"query": "Gateway"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["answer"] == "테스트 답변입니다."
    # 그래프 탐색에서 출처가 추출되어야 한다
    assert len(data["sources"]) >= 1
    assert data["sources"][0]["title"] == "아키텍처 문서"
    assert data["sources"][0]["document_id"] == doc_id
