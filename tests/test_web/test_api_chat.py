"""채팅 API 엔드포인트 테스트."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore
from context_loop.web.app import create_app

_DIM = 4  # 테스트용 임베딩 차원


def _stream_returning(*chunks: str):
    """``llm_client.stream`` mock을 만든다 (async generator)."""
    async def _gen(*_args, **_kwargs):
        for chunk in chunks:
            yield chunk
    return MagicMock(side_effect=_gen)


def _parse_ndjson(body: str) -> list[dict]:
    """응답 본문을 NDJSON 이벤트 리스트로 파싱한다."""
    return [json.loads(line) for line in body.splitlines() if line.strip()]


def _collect_answer(events: list[dict]) -> str:
    return "".join(e.get("content", "") for e in events if e.get("type") == "delta")


def _collect_sources(events: list[dict]) -> list[dict]:
    for e in events:
        if e.get("type") == "sources":
            return e.get("sources", [])
    return []


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
    from context_loop.config import Config

    meta_store, vector_store, graph_store = chat_stores

    mock_llm = AsyncMock()
    mock_llm.complete = AsyncMock(return_value="테스트 답변입니다.")
    mock_llm.stream = _stream_returning("테스트 답변입니다.")

    mock_embedding = AsyncMock()
    mock_embedding.aembed_query = AsyncMock(return_value=[1.0, 0.0, 0.0, 0.0])

    app = create_app()
    app.state.meta_store = meta_store
    app.state.vector_store = vector_store
    app.state.graph_store = graph_store
    app.state.llm_client = mock_llm
    app.state.embedding_client = mock_embedding
    app.state.reranker_client = None
    app.state.config = Config()

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
    """문서가 없으면 안내 메시지를 NDJSON 스트림으로 반환한다."""
    resp = await chat_client.post(
        "/api/chat",
        json={"query": "테스트 질문"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    events = _parse_ndjson(resp.text)
    types = [e["type"] for e in events]
    # 답변 완료 후 sources, 마지막에 done
    assert "sources" in types
    assert types.index("sources") > types.index("delta")
    assert types[-1] == "done"
    assert _collect_answer(events)  # 안내 메시지가 비어 있지 않음
    assert _collect_sources(events) == []


async def test_chat_api_returns_answer_and_sources(chat_stores, chat_client: AsyncClient) -> None:
    """문서가 있으면 답변 토큰 스트림과 출처를 반환한다."""
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

    # 토큰 단위 스트림 모의
    app = chat_client._transport.app  # type: ignore[attr-defined]
    app.state.llm_client.stream = _stream_returning("테스트 ", "답변", "입니다.")

    resp = await chat_client.post(
        "/api/chat",
        json={"query": "테스트 질문"},
    )
    assert resp.status_code == 200
    events = _parse_ndjson(resp.text)
    assert _collect_answer(events) == "테스트 답변입니다."
    sources = _collect_sources(events)
    assert len(sources) >= 1
    assert sources[0]["title"] == "테스트 문서"
    assert sources[0]["document_id"] == doc_id
    # 마지막 이벤트는 done
    assert events[-1]["type"] == "done"

    # reasoning_mode="high" 가 LLM 스트리밍 호출에 전달되었는지 확인
    call_kwargs = app.state.llm_client.stream.call_args.kwargs
    assert call_kwargs.get("reasoning_mode") == "high"


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

    # 그래프 탐색 계획용 complete + 최종 답변용 stream 분리
    app = chat_client._transport.app  # type: ignore[attr-defined]
    plan_response = json.dumps({
        "should_search": True,
        "reasoning": "Gateway 구조 파악 필요",
        "search_steps": [{"entity_name": "Gateway", "depth": 1, "focus_relations": []}],
    })
    app.state.llm_client.complete = AsyncMock(side_effect=[plan_response])
    app.state.llm_client.stream = _stream_returning("테스트 답변입니다.")

    resp = await chat_client.post(
        "/api/chat",
        json={"query": "Gateway"},
    )
    assert resp.status_code == 200
    events = _parse_ndjson(resp.text)
    assert _collect_answer(events) == "테스트 답변입니다."
    # 그래프 탐색에서 출처가 추출되어야 한다
    sources = _collect_sources(events)
    assert len(sources) >= 1
    assert sources[0]["title"] == "아키텍처 문서"
    assert sources[0]["document_id"] == doc_id
