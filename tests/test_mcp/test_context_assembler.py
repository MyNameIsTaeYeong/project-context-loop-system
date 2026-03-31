"""context_assembler LLM 기반 그래프 탐색 + 리랭킹/threshold 테스트."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from context_loop.mcp.context_assembler import (
    _search_chunks,
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


# --- 유사도 threshold 테스트 ---


@pytest.mark.asyncio
async def test_search_chunks_threshold_filters_low_similarity(stores) -> None:
    """similarity_threshold가 유사도 낮은 청크를 제외한다."""
    meta_store, vector_store, _ = stores

    doc_id = await meta_store.create_document(
        source_type="manual", title="Doc", original_content="c", content_hash="ht",
    )
    # distance=0.2 → similarity=0.8, distance=0.8 → similarity=0.2
    vector_store.add_chunks(
        chunk_ids=[f"chunk_{doc_id}_0", f"chunk_{doc_id}_1"],
        embeddings=[[0.9, 0.1], [0.1, 0.9]],
        documents=["관련 내용", "무관한 내용"],
        metadatas=[
            {"document_id": doc_id, "chunk_index": 0},
            {"document_id": doc_id, "chunk_index": 1},
        ],
    )

    embed_client = _make_embedding_client([0.9, 0.1])
    query_embedding = await embed_client.aembed_query("test")

    # threshold=0.5 → distance > 0.5인 청크 제외
    results = await _search_chunks(
        query_embedding, vector_store, max_chunks=10,
        similarity_threshold=0.5,
    )

    # 유사도 0.8인 청크만 남아야 함 (distance=0.2)
    assert len(results) >= 1
    for r in results:
        similarity = 1 - r["distance"]
        assert similarity >= 0.5


@pytest.mark.asyncio
async def test_search_chunks_no_threshold_returns_all(stores) -> None:
    """threshold=0이면 모든 결과를 반환한다."""
    meta_store, vector_store, _ = stores

    doc_id = await meta_store.create_document(
        source_type="manual", title="Doc", original_content="c", content_hash="hnt",
    )
    vector_store.add_chunks(
        chunk_ids=[f"chunk_{doc_id}_0", f"chunk_{doc_id}_1"],
        embeddings=[[0.9, 0.1], [0.1, 0.9]],
        documents=["내용 A", "내용 B"],
        metadatas=[
            {"document_id": doc_id, "chunk_index": 0},
            {"document_id": doc_id, "chunk_index": 1},
        ],
    )

    embed_client = _make_embedding_client([0.9, 0.1])
    query_embedding = await embed_client.aembed_query("test")

    results = await _search_chunks(
        query_embedding, vector_store, max_chunks=10,
        similarity_threshold=0.0,
    )
    assert len(results) == 2


@pytest.mark.asyncio
async def test_assemble_context_with_reranking(stores) -> None:
    """리랭킹이 활성화되면 LLM 기반으로 결과가 재정렬된다."""
    meta_store, vector_store, graph_store = stores

    doc_id = await meta_store.create_document(
        source_type="manual", title="RerankDoc", original_content="c", content_hash="hrr",
    )
    vector_store.add_chunks(
        chunk_ids=[f"chunk_{doc_id}_0", f"chunk_{doc_id}_1"],
        embeddings=[[0.9, 0.1], [0.85, 0.15]],
        documents=["일반 내용", "핵심 답변"],
        metadatas=[
            {"document_id": doc_id, "chunk_index": 0},
            {"document_id": doc_id, "chunk_index": 1},
        ],
    )

    embed_client = _make_embedding_client([0.9, 0.1])

    # LLM이 chunk_1(핵심 답변)에 높은 점수를 부여
    rerank_response = json.dumps([
        {"index": 0, "score": 3},
        {"index": 1, "score": 9},
    ])
    llm = AsyncMock()
    # LLM은 리랭킹 호출과 그래프 탐색 호출 모두 처리
    llm.complete = AsyncMock(return_value=rerank_response)

    result = await assemble_context(
        query="핵심 정보",
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=embed_client,
        llm_client=llm,
        include_graph=False,
        rerank_enabled=True,
        rerank_top_k=2,
    )
    # 리랭킹 후 "핵심 답변"이 먼저 나와야 함
    assert "핵심 답변" in result


@pytest.mark.asyncio
async def test_assemble_context_rerank_score_threshold(stores) -> None:
    """리랭크 점수 threshold가 낮은 점수의 청크를 제외한다."""
    meta_store, vector_store, graph_store = stores

    doc_id = await meta_store.create_document(
        source_type="manual", title="ThDoc", original_content="c", content_hash="hth",
    )
    vector_store.add_chunks(
        chunk_ids=[f"chunk_{doc_id}_0", f"chunk_{doc_id}_1"],
        embeddings=[[0.9, 0.1], [0.85, 0.15]],
        documents=["관련 내용", "무관한 잡음"],
        metadatas=[
            {"document_id": doc_id, "chunk_index": 0},
            {"document_id": doc_id, "chunk_index": 1},
        ],
    )

    embed_client = _make_embedding_client([0.9, 0.1])
    rerank_response = json.dumps([
        {"index": 0, "score": 8},
        {"index": 1, "score": 2},  # threshold(4.0) 미만
    ])
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value=rerank_response)

    assembled = await assemble_context_with_sources(
        query="관련 질의",
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=embed_client,
        llm_client=llm,
        include_graph=False,
        rerank_enabled=True,
        rerank_score_threshold=4.0,
    )
    # 점수 2인 "무관한 잡음"은 제외되어야 함
    assert "관련 내용" in assembled.context_text
    assert "무관한 잡음" not in assembled.context_text
