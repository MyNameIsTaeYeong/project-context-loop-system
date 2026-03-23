"""context_assembler LLM 기반 그래프 탐색 테스트."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from context_loop.mcp.context_assembler import (
    _search_graph_with_llm,
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


def _make_llm_client(plan_response: dict) -> AsyncMock:
    """탐색 계획 JSON을 반환하는 mock LLM 클라이언트를 생성한다."""
    mock = AsyncMock()
    mock.complete = AsyncMock(return_value=json.dumps(plan_response, ensure_ascii=False))
    return mock


def _make_embedding_client(query_embedding: list[float]) -> AsyncMock:
    """테스트용 임베딩 클라이언트를 생성한다."""
    mock = AsyncMock()
    mock.aembed_query = AsyncMock(return_value=query_embedding)
    return mock


@pytest.mark.asyncio
async def test_graph_search_skipped_when_llm_says_no(stores) -> None:
    """LLM이 should_search=false를 반환하면 그래프 탐색이 스킵된다."""
    meta_store, _, graph_store = stores

    doc_id = await meta_store.create_document(
        source_type="manual", title="T", original_content="c", content_hash="h",
    )
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[Entity(name="Gateway", entity_type="component")],
        relations=[],
    ))

    llm = _make_llm_client({
        "should_search": False,
        "reasoning": "질의가 그래프와 무관합니다",
        "search_steps": [],
    })

    result = await _search_graph_with_llm("오늘 날씨 어때?", graph_store, llm)
    assert result is None


@pytest.mark.asyncio
async def test_graph_search_finds_entities_via_llm_plan(stores) -> None:
    """LLM이 탐색 계획을 세우면 해당 엔티티를 중심으로 탐색한다."""
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

    llm = _make_llm_client({
        "should_search": True,
        "reasoning": "Gateway 관련 구조를 파악해야 합니다",
        "search_steps": [
            {"entity_name": "Gateway", "depth": 1, "focus_relations": ["depends_on"]},
        ],
    })

    result = await _search_graph_with_llm("게이트웨이 구조", graph_store, llm)
    assert result is not None
    assert "Gateway" in result.text
    assert "AuthService" in result.text
    assert "depends_on" in result.text
    assert doc_id in result.document_ids


@pytest.mark.asyncio
async def test_graph_search_with_multiple_steps(stores) -> None:
    """LLM이 여러 엔티티를 탐색 계획에 포함할 수 있다."""
    meta_store, _, graph_store = stores

    doc_id = await meta_store.create_document(
        source_type="manual", title="T", original_content="c", content_hash="hm",
    )
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name="ServiceA", entity_type="service"),
            Entity(name="ServiceB", entity_type="service"),
            Entity(name="Database", entity_type="system"),
        ],
        relations=[
            Relation(source="ServiceA", target="Database", relation_type="uses"),
            Relation(source="ServiceB", target="Database", relation_type="uses"),
        ],
    ))

    llm = _make_llm_client({
        "should_search": True,
        "reasoning": "ServiceA와 ServiceB의 DB 의존성 파악",
        "search_steps": [
            {"entity_name": "ServiceA", "depth": 1, "focus_relations": []},
            {"entity_name": "ServiceB", "depth": 1, "focus_relations": []},
        ],
    })

    result = await _search_graph_with_llm("서비스 구조", graph_store, llm)
    assert result is not None
    assert "ServiceA" in result.text
    assert "ServiceB" in result.text
    assert "Database" in result.text


@pytest.mark.asyncio
async def test_graph_search_empty_graph(stores) -> None:
    """빈 그래프에서는 LLM 호출 없이 None을 반환한다."""
    _, _, graph_store = stores

    llm = _make_llm_client({"should_search": True, "search_steps": []})

    result = await _search_graph_with_llm("어떤 질의", graph_store, llm)
    assert result is None
    # 그래프가 비어있으므로 LLM이 호출되지 않아야 함
    llm.complete.assert_not_called()


@pytest.mark.asyncio
async def test_assemble_context_with_llm_graph_search(stores) -> None:
    """assemble_context가 LLM 기반 그래프 탐색을 사용한다."""
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

    embed_client = _make_embedding_client([0.9, 0.1])
    llm = _make_llm_client({
        "should_search": True,
        "reasoning": "ServiceA 관련 정보 필요",
        "search_steps": [
            {"entity_name": "ServiceA", "depth": 1, "focus_relations": ["calls"]},
        ],
    })

    result = await assemble_context(
        query="ServiceA 관련 정보",
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=embed_client,
        llm_client=llm,
        include_graph=True,
    )
    assert "ServiceA" in result
    assert "calls" in result


@pytest.mark.asyncio
async def test_assemble_context_no_llm_skips_graph(stores) -> None:
    """llm_client가 None이면 그래프 탐색을 스킵한다."""
    meta_store, vector_store, graph_store = stores

    doc_id = await meta_store.create_document(
        source_type="manual", title="Doc1", original_content="c", content_hash="h2",
    )
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[Entity(name="NodeX", entity_type="component")],
        relations=[],
    ))

    embed_client = _make_embedding_client([0.0, 0.0])

    result = await assemble_context(
        query="test",
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=embed_client,
        llm_client=None,  # LLM 없음
        include_graph=True,
    )
    # 벡터 데이터도 없으므로 컨텍스트 없음 메시지
    assert "찾을 수 없습니다" in result


@pytest.mark.asyncio
async def test_assemble_context_with_sources_llm_graph(stores) -> None:
    """assemble_context_with_sources도 LLM 기반 그래프 탐색을 사용한다."""
    meta_store, vector_store, graph_store = stores

    doc_id = await meta_store.create_document(
        source_type="manual", title="ArchDoc", original_content="c", content_hash="h3",
    )
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

    embed_client = _make_embedding_client([0.9, 0.1])
    llm = _make_llm_client({
        "should_search": True,
        "reasoning": "API 구조 확인 필요",
        "search_steps": [{"entity_name": "API", "depth": 1, "focus_relations": []}],
    })

    assembled = await assemble_context_with_sources(
        query="API 구조",
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=embed_client,
        llm_client=llm,
        include_graph=True,
    )
    assert assembled.context_text != ""
    assert len(assembled.sources) >= 1
    assert assembled.sources[0].title == "ArchDoc"


@pytest.mark.asyncio
async def test_graph_search_llm_failure_graceful(stores) -> None:
    """LLM 호출 실패 시 그래프 탐색이 None을 반환한다."""
    meta_store, _, graph_store = stores

    doc_id = await meta_store.create_document(
        source_type="manual", title="T", original_content="c", content_hash="hf",
    )
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[Entity(name="X", entity_type="component")],
        relations=[],
    ))

    llm = AsyncMock()
    llm.complete = AsyncMock(side_effect=Exception("LLM 서버 다운"))

    result = await _search_graph_with_llm("질의", graph_store, llm)
    assert result is None


@pytest.mark.asyncio
async def test_graph_search_reasoning_in_output(stores) -> None:
    """탐색 근거(reasoning)가 출력에 포함된다."""
    meta_store, _, graph_store = stores

    doc_id = await meta_store.create_document(
        source_type="manual", title="T", original_content="c", content_hash="hr",
    )
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[Entity(name="Auth", entity_type="service")],
        relations=[],
    ))

    llm = _make_llm_client({
        "should_search": True,
        "reasoning": "인증 서비스 구조 파악 필요",
        "search_steps": [{"entity_name": "Auth", "depth": 1, "focus_relations": []}],
    })

    result = await _search_graph_with_llm("인증", graph_store, llm)
    assert result is not None
    assert "인증 서비스 구조 파악 필요" in result.text
