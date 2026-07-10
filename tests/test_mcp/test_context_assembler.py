"""context_assembler LLM 기반 그래프 탐색 + 리랭킹/threshold 테스트."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from context_loop.mcp.context_assembler import (
    _apply_parent_documents,
    _fetch_and_format_source_code,
    _format_graph_chunk_results,
    _search_chunks,
    _search_graph_sourced_chunks,
    _search_graph_with_llm,
    assemble_context,
    assemble_context_with_sources,
)
from context_loop.processor.chunker import count_tokens
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
    """threshold=0이면 모든 (문서 단위) 결과를 반환한다.

    R3: dedup 키가 document_id 이므로 서로 다른 두 문서의 청크가 결과에
    유지되는지 검증.
    """
    meta_store, vector_store, _ = stores

    doc_a = await meta_store.create_document(
        source_type="manual", title="DocA", original_content="ca", content_hash="hnta",
    )
    doc_b = await meta_store.create_document(
        source_type="manual", title="DocB", original_content="cb", content_hash="hntb",
    )
    vector_store.add_chunks(
        chunk_ids=[f"chunk_{doc_a}_0", f"chunk_{doc_b}_0"],
        embeddings=[[0.9, 0.1], [0.1, 0.9]],
        documents=["내용 A", "내용 B"],
        metadatas=[
            {"document_id": doc_a, "chunk_index": 0},
            {"document_id": doc_b, "chunk_index": 0},
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
async def test_search_chunks_dedupes_by_document(stores) -> None:
    """R3: 같은 document_id 의 여러 view/청크가 매칭되면 가장 가까운 1건만 반환.

    한 문서가 body/meta/question 등 여러 view 로 인덱싱되어도 결과는 문서
    단위로 dedup 되어야 한다. 가장 가까운(distance 최소) view 의 metadata
    가 보존되어 출처 라벨에 활용된다.
    """
    meta_store, vector_store, _ = stores

    doc_a = await meta_store.create_document(
        source_type="manual", title="DocA", original_content="c", content_hash="hduba",
    )
    doc_b = await meta_store.create_document(
        source_type="manual", title="DocB", original_content="c", content_hash="hdubb",
    )
    # DocA 는 body/meta/question 3 view 모두 등록 — query 와 가장 가까운 건 question.
    # DocB 는 body 1 view 만 등록.
    vector_store.add_chunks(
        chunk_ids=["a#body", "a#meta", "a#q0", "b#body"],
        embeddings=[[0.5, 0.5], [0.7, 0.3], [0.9, 0.1], [0.1, 0.9]],
        documents=["본문 A", "본문 A", "본문 A", "본문 B"],
        metadatas=[
            {"document_id": doc_a, "logical_chunk_id": "a", "view": "body"},
            {"document_id": doc_a, "logical_chunk_id": "a", "view": "meta"},
            {"document_id": doc_a, "logical_chunk_id": "a",
             "view": "question", "question_text": "DocA 의 동작은?"},
            {"document_id": doc_b, "logical_chunk_id": "b", "view": "body"},
        ],
    )

    embed_client = _make_embedding_client([0.9, 0.1])
    query_embedding = await embed_client.aembed_query("test")

    results = await _search_chunks(
        query_embedding, vector_store, max_chunks=10,
        similarity_threshold=0.0,
    )

    doc_ids = [r["metadata"]["document_id"] for r in results]
    assert len(doc_ids) == 2
    assert doc_ids.count(doc_a) == 1
    assert doc_ids.count(doc_b) == 1
    # DocA 의 question view 가 가장 가까웠으므로 보존
    a_result = next(r for r in results if r["metadata"]["document_id"] == doc_a)
    assert a_result["metadata"]["view"] == "question"
    assert a_result["metadata"]["question_text"] == "DocA 의 동작은?"


@pytest.mark.asyncio
async def test_assemble_context_with_reranking(stores) -> None:
    """리랭킹이 활성화되면 LLM 기반으로 결과가 재정렬된다.

    R3: dedup 키가 document_id 이므로 리랭킹 후보는 서로 다른 두 문서로 둔다.
    """
    meta_store, vector_store, graph_store = stores

    doc_a = await meta_store.create_document(
        source_type="manual", title="DocA", original_content="c", content_hash="hrra",
    )
    doc_b = await meta_store.create_document(
        source_type="manual", title="DocB", original_content="c", content_hash="hrrb",
    )
    vector_store.add_chunks(
        chunk_ids=[f"chunk_{doc_a}_0", f"chunk_{doc_b}_0"],
        embeddings=[[0.9, 0.1], [0.85, 0.15]],
        documents=["일반 내용", "핵심 답변"],
        metadatas=[
            {"document_id": doc_a, "chunk_index": 0},
            {"document_id": doc_b, "chunk_index": 0},
        ],
    )

    embed_client = _make_embedding_client([0.9, 0.1])

    # 리랭커가 DocB(핵심 답변)에 높은 점수를 부여
    reranker = AsyncMock()
    reranker.rerank = AsyncMock(return_value=[0.3, 0.9])

    result = await assemble_context(
        query="핵심 정보",
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=embed_client,
        reranker_client=reranker,
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
    reranker = AsyncMock()
    # threshold(0.4) 미만인 0.2 는 제외 대상
    reranker.rerank = AsyncMock(return_value=[0.8, 0.2])

    assembled = await assemble_context_with_sources(
        query="관련 질의",
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=embed_client,
        reranker_client=reranker,
        include_graph=False,
        rerank_enabled=True,
        rerank_score_threshold=0.4,
    )
    # 점수 0.2인 "무관한 잡음"은 제외되어야 함
    assert "관련 내용" in assembled.context_text
    assert "무관한 잡음" not in assembled.context_text


# --- HyDE 통합 테스트 ---


@pytest.mark.asyncio
async def test_assemble_context_with_hyde(stores) -> None:
    """HyDE 활성화 시 LLM이 가상 문서를 생성하고 임베딩에 반영된다."""
    meta_store, vector_store, graph_store = stores

    doc_id = await meta_store.create_document(
        source_type="manual", title="HydeDoc", original_content="c", content_hash="hhy",
    )
    vector_store.add_chunks(
        chunk_ids=[f"chunk_{doc_id}_0"],
        embeddings=[[0.9, 0.1]],
        documents=["배포 자동화 CI/CD 파이프라인 설명"],
        metadatas=[{"document_id": doc_id, "chunk_index": 0}],
    )

    embed_client = _make_embedding_client([0.9, 0.1])
    llm = AsyncMock()
    # HyDE 가상 문서 생성 호출
    llm.complete = AsyncMock(return_value="배포 프로세스는 CI/CD를 통해 릴리즈됩니다.")

    result = await assemble_context(
        query="배포 절차",
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=embed_client,
        llm_client=llm,
        include_graph=False,
        hyde_enabled=True,
    )
    # HyDE가 활성화되었으므로 LLM이 호출됨
    llm.complete.assert_called()
    # 검색 결과가 포함되어야 함
    assert "배포 자동화" in result


@pytest.mark.asyncio
async def test_assemble_context_hyde_disabled_no_llm_call(stores) -> None:
    """HyDE 비활성화 시 가상 문서 생성 LLM 호출이 없다."""
    meta_store, vector_store, graph_store = stores

    doc_id = await meta_store.create_document(
        source_type="manual", title="NoHyde", original_content="c", content_hash="hnh",
    )
    vector_store.add_chunks(
        chunk_ids=[f"chunk_{doc_id}_0"],
        embeddings=[[0.9, 0.1]],
        documents=["내용"],
        metadatas=[{"document_id": doc_id, "chunk_index": 0}],
    )

    embed_client = _make_embedding_client([0.9, 0.1])
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value="unused")

    await assemble_context(
        query="질의",
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=embed_client,
        llm_client=llm,
        include_graph=False,
        hyde_enabled=False,
    )
    # HyDE 비활성 → LLM 호출 없음 (그래프도 비활성)
    llm.complete.assert_not_called()


# --- Phase 9.7: 원본 소스 코드 첨부 테스트 ---


@pytest.mark.asyncio
async def test_fetch_and_format_source_code(stores) -> None:
    """code_doc의 document_sources에서 git_code 원본을 포맷팅한다."""
    meta_store, _, _ = stores

    # git_code 문서 생성
    git_id = await meta_store.create_document(
        source_type="git_code",
        source_id="src/main.py",
        title="main.py",
        original_content="print('hello')",
        content_hash="h_gc1",
    )
    # code_doc 문서 생성 + document_sources 연결
    doc_id = await meta_store.create_document(
        source_type="code_doc",
        source_id="product:architecture",
        title="아키텍처 문서",
        original_content="# 아키텍처\n설명...",
        content_hash="h_cd1",
    )
    await meta_store.add_document_source(doc_id, git_id, "src/main.py")

    result = await _fetch_and_format_source_code({doc_id}, meta_store)
    assert result is not None
    assert "원본 소스 코드" in result
    assert "main.py" in result
    assert "print('hello')" in result
    assert "```py" in result  # 확장자 기반 언어 힌트


@pytest.mark.asyncio
async def test_fetch_and_format_source_code_no_sources(stores) -> None:
    """document_sources가 없으면 None을 반환한다."""
    meta_store, _, _ = stores

    doc_id = await meta_store.create_document(
        source_type="code_doc",
        source_id="product:dev",
        title="개발 가이드",
        original_content="# 개발\n...",
        content_hash="h_cd2",
    )

    result = await _fetch_and_format_source_code({doc_id}, meta_store)
    assert result is None


@pytest.mark.asyncio
async def test_fetch_and_format_source_code_non_code_doc(stores) -> None:
    """code_doc/code_summary가 아닌 문서는 소스 코드를 조회하지 않는다."""
    meta_store, _, _ = stores

    doc_id = await meta_store.create_document(
        source_type="manual",
        title="일반 문서",
        original_content="내용",
        content_hash="h_mn1",
    )

    result = await _fetch_and_format_source_code({doc_id}, meta_store)
    assert result is None


@pytest.mark.asyncio
async def test_fetch_and_format_source_code_deduplicates(stores) -> None:
    """여러 code_doc이 같은 git_code를 참조해도 중복 없이 한 번만 포함된다."""
    meta_store, _, _ = stores

    git_id = await meta_store.create_document(
        source_type="git_code",
        source_id="src/shared.go",
        title="shared.go",
        original_content="package shared",
        content_hash="h_gc2",
    )
    doc_id1 = await meta_store.create_document(
        source_type="code_doc", source_id="p:arch",
        title="아키텍처", original_content="doc1", content_hash="h_cd3",
    )
    doc_id2 = await meta_store.create_document(
        source_type="code_doc", source_id="p:dev",
        title="개발", original_content="doc2", content_hash="h_cd4",
    )
    await meta_store.add_document_source(doc_id1, git_id, "src/shared.go")
    await meta_store.add_document_source(doc_id2, git_id, "src/shared.go")

    result = await _fetch_and_format_source_code({doc_id1, doc_id2}, meta_store)
    assert result is not None
    # shared.go가 한 번만 나와야 함
    assert result.count("shared.go") == 2  # 제목 + file_path 각 1번


@pytest.mark.asyncio
async def test_assemble_context_include_source_code(stores) -> None:
    """include_source_code=True일 때 원본 코드가 컨텍스트에 포함된다."""
    meta_store, vector_store, graph_store = stores

    # git_code
    git_id = await meta_store.create_document(
        source_type="git_code",
        source_id="services/vpc/main.go",
        title="main.go",
        original_content="package main\nfunc main() {}",
        content_hash="h_gc_ac",
    )
    # code_doc
    doc_id = await meta_store.create_document(
        source_type="code_doc",
        source_id="vpc:architecture",
        title="[VPC] 아키텍처",
        original_content="# VPC 아키텍처 분석",
        content_hash="h_cd_ac",
    )
    await meta_store.add_document_source(doc_id, git_id, "services/vpc/main.go")

    # 벡터 저장소에 code_doc 청크 추가
    vector_store.add_chunks(
        chunk_ids=[f"chunk_{doc_id}_0"],
        embeddings=[[0.9, 0.1]],
        documents=["VPC 아키텍처 분석 내용"],
        metadatas=[{"document_id": doc_id, "chunk_index": 0}],
    )

    embed_client = _make_embedding_client([0.9, 0.1])

    result = await assemble_context(
        query="VPC 아키텍처",
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=embed_client,
        include_graph=False,
        include_source_code=True,
    )
    assert "VPC 아키텍처 분석" in result  # 청크 내용
    assert "원본 소스 코드" in result  # 소스 코드 섹션
    assert "package main" in result  # 원본 코드 내용


@pytest.mark.asyncio
async def test_assemble_context_with_sources_include_source_code(stores) -> None:
    """assemble_context_with_sources에서도 include_source_code가 동작한다."""
    meta_store, vector_store, graph_store = stores

    git_id = await meta_store.create_document(
        source_type="git_code",
        source_id="lib/util.py",
        title="util.py",
        original_content="def helper(): pass",
        content_hash="h_gc_aws",
    )
    doc_id = await meta_store.create_document(
        source_type="code_summary",
        source_id="product:lib",
        title="[product] lib 요약",
        original_content="# lib 요약",
        content_hash="h_cs_aws",
    )
    await meta_store.add_document_source(doc_id, git_id, "lib/util.py")

    vector_store.add_chunks(
        chunk_ids=[f"chunk_{doc_id}_0"],
        embeddings=[[0.9, 0.1]],
        documents=["유틸 함수 요약"],
        metadatas=[{"document_id": doc_id, "chunk_index": 0}],
    )

    embed_client = _make_embedding_client([0.9, 0.1])

    assembled = await assemble_context_with_sources(
        query="유틸 함수",
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=embed_client,
        include_graph=False,
        include_source_code=True,
    )
    assert "원본 소스 코드" in assembled.context_text
    assert "def helper(): pass" in assembled.context_text


@pytest.mark.asyncio
async def test_assemble_context_source_code_disabled_by_default(stores) -> None:
    """include_source_code=False(기본값)일 때 원본 코드가 포함되지 않는다."""
    meta_store, vector_store, graph_store = stores

    git_id = await meta_store.create_document(
        source_type="git_code",
        source_id="src/a.py",
        title="a.py",
        original_content="SECRET_CODE = 42",
        content_hash="h_gc_def",
    )
    doc_id = await meta_store.create_document(
        source_type="code_doc",
        source_id="p:arch",
        title="문서",
        original_content="# 문서",
        content_hash="h_cd_def",
    )
    await meta_store.add_document_source(doc_id, git_id, "src/a.py")

    vector_store.add_chunks(
        chunk_ids=[f"chunk_{doc_id}_0"],
        embeddings=[[0.9, 0.1]],
        documents=["문서 내용"],
        metadatas=[{"document_id": doc_id, "chunk_index": 0}],
    )

    embed_client = _make_embedding_client([0.9, 0.1])

    result = await assemble_context(
        query="검색",
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=embed_client,
        include_graph=False,
        # include_source_code 기본값 = False
    )
    assert "SECRET_CODE" not in result
    assert "원본 소스 코드" not in result


@pytest.mark.asyncio
async def test_assemble_context_shows_matched_question_in_source_label(stores) -> None:
    """R3: 매칭된 view='question' 의 question_text 가 출처 라벨에 노출된다."""
    meta_store, vector_store, graph_store = stores

    doc_id = await meta_store.create_document(
        source_type="manual", title="QDoc", original_content="c", content_hash="hq",
    )
    vector_store.add_chunks(
        chunk_ids=["q-body", "q-question"],
        embeddings=[[0.5, 0.5], [0.9, 0.1]],
        documents=["문서 본문 내용", "문서 본문 내용"],
        metadatas=[
            {"document_id": doc_id, "logical_chunk_id": "c1",
             "section_path": "A", "view": "body"},
            {"document_id": doc_id, "logical_chunk_id": "c1",
             "section_path": "A", "view": "question",
             "question_text": "QDoc 의 핵심 동작은?"},
        ],
    )

    embed_client = _make_embedding_client([0.9, 0.1])

    result = await assemble_context_with_sources(
        query="핵심 동작",
        meta_store=meta_store,
        vector_store=vector_store,
        graph_store=graph_store,
        embedding_client=embed_client,
        include_graph=False,
    )

    assert "QDoc" in result.context_text
    assert "섹션: A" in result.context_text
    # 매칭된 질문 텍스트가 출처 라벨에 노출되어야 함
    assert "QDoc 의 핵심 동작은?" in result.context_text
    assert "매칭 질문" in result.context_text


# ---------------------------------------------------------------------------
# 설계 A — 그래프 연결 문서 첨부
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_graph_sourced_chunks_excludes_vector_docs(stores) -> None:
    """벡터가 이미 찾은 문서는 그래프 첨부에서 제외된다 (순수 추가분만)."""
    meta_store, vector_store, _ = stores
    doc_a = await meta_store.create_document(
        source_type="manual", title="A", original_content="c", content_hash="gsca",
    )
    doc_b = await meta_store.create_document(
        source_type="manual", title="B", original_content="c", content_hash="gscb",
    )
    vector_store.add_chunks(
        chunk_ids=["a#body", "b#body"],
        embeddings=[[0.9, 0.1], [0.8, 0.2]],
        documents=["본문 A", "본문 B"],
        metadatas=[
            {"document_id": doc_a, "logical_chunk_id": "a", "view": "body"},
            {"document_id": doc_b, "logical_chunk_id": "b", "view": "body"},
        ],
    )

    results = await _search_graph_sourced_chunks(
        [0.9, 0.1], vector_store,
        graph_doc_ids={doc_a, doc_b}, existing_doc_ids={doc_a},
        max_graph_docs=10, max_graph_tokens=100000,
    )
    assert [r["metadata"]["document_id"] for r in results] == [doc_b]


@pytest.mark.asyncio
async def test_graph_sourced_chunks_dedupes_and_caps(stores) -> None:
    """문서당 1청크 dedup + max_graph_docs 개수 상한."""
    meta_store, vector_store, _ = stores
    doc_b = await meta_store.create_document(
        source_type="manual", title="B", original_content="c", content_hash="gscdb",
    )
    doc_c = await meta_store.create_document(
        source_type="manual", title="C", original_content="c", content_hash="gscdc",
    )
    vector_store.add_chunks(
        chunk_ids=["b#body", "b#q0", "c#body"],
        embeddings=[[0.9, 0.1], [0.85, 0.15], [0.7, 0.3]],
        documents=["본문 B", "본문 B", "본문 C"],
        metadatas=[
            {"document_id": doc_b, "logical_chunk_id": "b", "view": "body"},
            {"document_id": doc_b, "logical_chunk_id": "b",
             "view": "question", "question_text": "q"},
            {"document_id": doc_c, "logical_chunk_id": "c", "view": "body"},
        ],
    )

    res = await _search_graph_sourced_chunks(
        [0.9, 0.1], vector_store, {doc_b, doc_c}, set(),
        max_graph_docs=10, max_graph_tokens=100000,
    )
    ids = [r["metadata"]["document_id"] for r in res]
    assert sorted(ids) == sorted([doc_b, doc_c])
    assert ids.count(doc_b) == 1  # 두 view 가 1건으로 dedup

    res_cap = await _search_graph_sourced_chunks(
        [0.9, 0.1], vector_store, {doc_b, doc_c}, set(),
        max_graph_docs=1, max_graph_tokens=100000,
    )
    assert [r["metadata"]["document_id"] for r in res_cap] == [doc_b]


@pytest.mark.asyncio
async def test_graph_sourced_chunks_token_budget(stores) -> None:
    """토큰 상한을 넘으면 첫 문서만 첨부된다 (doc-level 청크는 무거움)."""
    meta_store, vector_store, _ = stores
    doc_b = await meta_store.create_document(
        source_type="manual", title="B", original_content="c", content_hash="gsctb",
    )
    doc_c = await meta_store.create_document(
        source_type="manual", title="C", original_content="c", content_hash="gsctc",
    )
    vector_store.add_chunks(
        chunk_ids=["b#body", "c#body"],
        embeddings=[[0.9, 0.1], [0.7, 0.3]],
        documents=["B" * 400, "C" * 400],
        metadatas=[
            {"document_id": doc_b, "logical_chunk_id": "b", "view": "body"},
            {"document_id": doc_c, "logical_chunk_id": "c", "view": "body"},
        ],
    )

    res = await _search_graph_sourced_chunks(
        [0.9, 0.1], vector_store, {doc_b, doc_c}, set(),
        max_graph_docs=10, max_graph_tokens=10,
    )
    # 첫 문서는 항상 포함, 둘째는 예산 초과로 제외
    assert [r["metadata"]["document_id"] for r in res] == [doc_b]


@pytest.mark.asyncio
async def test_graph_sourced_chunks_empty_conditions(stores) -> None:
    """query_embedding None / max_graph_docs=0 / 전부 겹침 → 빈 리스트."""
    meta_store, vector_store, _ = stores
    doc_b = await meta_store.create_document(
        source_type="manual", title="B", original_content="c", content_hash="gsce",
    )
    vector_store.add_chunks(
        chunk_ids=["b#body"], embeddings=[[0.9, 0.1]], documents=["본문 B"],
        metadatas=[{"document_id": doc_b, "logical_chunk_id": "b", "view": "body"}],
    )

    assert await _search_graph_sourced_chunks(
        None, vector_store, {doc_b}, set(),
        max_graph_docs=3, max_graph_tokens=6000,
    ) == []
    assert await _search_graph_sourced_chunks(
        [0.9, 0.1], vector_store, {doc_b}, set(),
        max_graph_docs=0, max_graph_tokens=6000,
    ) == []
    # 그래프 문서가 벡터 결과와 전부 겹침 → 추가분 없음
    assert await _search_graph_sourced_chunks(
        [0.9, 0.1], vector_store, {doc_b}, {doc_b},
        max_graph_docs=3, max_graph_tokens=6000,
    ) == []


@pytest.mark.asyncio
async def test_graph_sourced_chunks_ignores_doc_without_vector(stores) -> None:
    """벡터 엔트리가 없는 그래프 노드(예: 미임포트 페이지)는 본문 첨부에서 제외."""
    meta_store, vector_store, _ = stores
    doc_b = await meta_store.create_document(
        source_type="manual", title="B", original_content="c", content_hash="gscnv",
    )
    doc_x = await meta_store.create_document(
        source_type="manual", title="X", original_content="c", content_hash="gscnvx",
    )
    vector_store.add_chunks(
        chunk_ids=["b#body"], embeddings=[[0.9, 0.1]], documents=["본문 B"],
        metadatas=[{"document_id": doc_b, "logical_chunk_id": "b", "view": "body"}],
    )

    res = await _search_graph_sourced_chunks(
        [0.9, 0.1], vector_store, {doc_b, doc_x}, set(),
        max_graph_docs=3, max_graph_tokens=6000,
    )
    assert [r["metadata"]["document_id"] for r in res] == [doc_b]


@pytest.mark.asyncio
async def test_format_graph_chunk_results_header(stores) -> None:
    """그래프 연결 문서 섹션은 전용 헤더와 도달 라벨을 갖는다."""
    meta_store, _, _ = stores
    doc = await meta_store.create_document(
        source_type="manual", title="MyDoc", original_content="c", content_hash="hfmt",
    )
    results = [
        {"metadata": {"document_id": doc, "section_path": "A > B"},
         "document": "본문 내용"},
    ]
    text = await _format_graph_chunk_results(results, meta_store)
    assert "## 그래프 연결 문서" in text
    assert "MyDoc" in text
    assert "본문 내용" in text
    assert "그래프 경로로 도달" in text


@pytest.mark.asyncio
async def test_assemble_context_attaches_graph_sourced_document(stores) -> None:
    """벡터가 못 찾고 그래프만 도달한 문서의 본문이 컨텍스트에 첨부된다."""
    meta_store, vector_store, graph_store = stores
    doc_vec = await meta_store.create_document(
        source_type="manual", title="VecDoc", original_content="c", content_hash="hgv1",
    )
    doc_graph = await meta_store.create_document(
        source_type="manual", title="GraphDoc", original_content="c", content_hash="hgv2",
    )
    # doc_vec 은 query 와 가까워 threshold 통과, doc_graph 는 멀어 본 검색에서 제외
    vector_store.add_chunks(
        chunk_ids=["v#body", "g#body"],
        embeddings=[[0.9, 0.1], [0.1, 0.9]],
        documents=["벡터로 찾은 본문", "그래프 전용 문서 본문"],
        metadatas=[
            {"document_id": doc_vec, "logical_chunk_id": "v", "view": "body"},
            {"document_id": doc_graph, "logical_chunk_id": "g", "view": "body"},
        ],
    )
    # 그래프 엔티티를 doc_graph 에 연결
    await graph_store.save_graph_data(doc_graph, GraphData(
        entities=[Entity(name="GraphEntity", entity_type="service")],
        relations=[],
    ))

    embed_client = _make_embedding_client([0.9, 0.1])
    llm = _make_llm_client({
        "should_search": True, "reasoning": "x",
        "search_steps": [
            {"entity_name": "GraphEntity", "depth": 1, "focus_relations": []},
        ],
    })

    result = await assemble_context(
        query="GraphEntity 관련",
        meta_store=meta_store, vector_store=vector_store, graph_store=graph_store,
        embedding_client=embed_client, llm_client=llm,
        include_graph=True, similarity_threshold=0.5, max_graph_docs=3,
    )
    assert "## 그래프 연결 문서" in result
    assert "그래프 전용 문서 본문" in result


@pytest.mark.asyncio
async def test_assemble_context_graph_doc_overlaps_vector_no_section(stores) -> None:
    """그래프 문서가 벡터 결과와 겹치면 그래프 연결 문서 섹션이 생기지 않는다."""
    meta_store, vector_store, graph_store = stores
    doc = await meta_store.create_document(
        source_type="manual", title="D", original_content="c", content_hash="hovl",
    )
    vector_store.add_chunks(
        chunk_ids=["d#body"], embeddings=[[0.9, 0.1]], documents=["본문 D"],
        metadatas=[{"document_id": doc, "logical_chunk_id": "d", "view": "body"}],
    )
    await graph_store.save_graph_data(doc, GraphData(
        entities=[Entity(name="EntD", entity_type="service")], relations=[],
    ))

    embed_client = _make_embedding_client([0.9, 0.1])
    llm = _make_llm_client({
        "should_search": True, "reasoning": "x",
        "search_steps": [{"entity_name": "EntD", "depth": 1, "focus_relations": []}],
    })

    result = await assemble_context(
        query="EntD", meta_store=meta_store, vector_store=vector_store,
        graph_store=graph_store, embedding_client=embed_client, llm_client=llm,
        include_graph=True, max_graph_docs=3,
    )
    assert "## 그래프 연결 문서" not in result


@pytest.mark.asyncio
async def test_with_sources_graph_doc_gets_real_similarity(stores) -> None:
    """본문이 인출된 그래프 문서의 Source.similarity 가 0.0 이 아닌 실제 값이다."""
    meta_store, vector_store, graph_store = stores
    doc_vec = await meta_store.create_document(
        source_type="manual", title="VecDoc", original_content="c", content_hash="hws1",
    )
    doc_graph = await meta_store.create_document(
        source_type="manual", title="GraphDoc", original_content="c", content_hash="hws2",
    )
    vector_store.add_chunks(
        chunk_ids=["v#body", "g#body"],
        embeddings=[[0.9, 0.1], [0.1, 0.9]],
        documents=["벡터 본문", "그래프 본문"],
        metadatas=[
            {"document_id": doc_vec, "logical_chunk_id": "v", "view": "body"},
            {"document_id": doc_graph, "logical_chunk_id": "g", "view": "body"},
        ],
    )
    await graph_store.save_graph_data(doc_graph, GraphData(
        entities=[Entity(name="GEnt", entity_type="service")], relations=[],
    ))

    embed_client = _make_embedding_client([0.9, 0.1])
    llm = _make_llm_client({
        "should_search": True, "reasoning": "x",
        "search_steps": [{"entity_name": "GEnt", "depth": 1, "focus_relations": []}],
    })

    ctx = await assemble_context_with_sources(
        query="GEnt", meta_store=meta_store, vector_store=vector_store,
        graph_store=graph_store, embedding_client=embed_client, llm_client=llm,
        include_graph=True, similarity_threshold=0.5, max_graph_docs=3,
    )
    graph_source = next(s for s in ctx.sources if s.document_id == doc_graph)
    assert graph_source.similarity > 0.0


# ---------------------------------------------------------------------------
# Parent-document retrieval (섹션 폴백 청크 → 문서 전문 치환)
# ---------------------------------------------------------------------------


async def _create_fallback_doc(
    meta_store,
    vector_store,
    *,
    title: str,
    content_hash: str,
    embedding: list[float],
    source_type: str = "confluence_mcp",
    original_content: str | None = None,
    view: str = "body",
    question_text: str = "",
) -> int:
    """섹션 폴백(다청크) 문서 + 적중용 벡터 엔트리 1건을 생성한다."""
    original = original_content if original_content is not None else (
        f"# {title}\n\n섹션1 본문\n\n# 두번째 섹션\n\n섹션2 본문 전체 맥락"
    )
    doc_id = await meta_store.create_document(
        source_type=source_type, title=title,
        original_content=original, content_hash=content_hash,
    )
    await meta_store.create_chunk(
        chunk_id=f"{content_hash}-0", document_id=doc_id, chunk_index=0,
        content="섹션1 본문", token_count=10, section_path=title,
    )
    await meta_store.create_chunk(
        chunk_id=f"{content_hash}-1", document_id=doc_id, chunk_index=1,
        content="섹션2 본문 전체 맥락", token_count=10, section_path="두번째 섹션",
    )
    meta = {
        "document_id": doc_id, "logical_chunk_id": f"{content_hash}-0",
        "section_path": title, "view": view,
    }
    if question_text:
        meta["question_text"] = question_text
    vector_store.add_chunks(
        chunk_ids=[f"{content_hash}-0#{view}"],
        embeddings=[embedding],
        documents=["섹션1 본문"],
        metadatas=[meta],
    )
    return doc_id


@pytest.mark.asyncio
async def test_apply_parent_documents_substitutes_multichunk_doc(stores) -> None:
    """다청크(섹션 폴백) 문서의 적중 청크가 원문 전문으로 치환된다."""
    meta_store, vector_store, _ = stores
    doc_id = await _create_fallback_doc(
        meta_store, vector_store, title="PDoc", content_hash="pd1",
        embedding=[0.9, 0.1],
    )
    results = [{"metadata": {"document_id": doc_id, "section_path": "PDoc"},
                "document": "섹션1 본문"}]
    substituted: set[int] = set()

    consumed = await _apply_parent_documents(
        results, meta_store,
        max_doc_tokens=32000, remaining_budget=96000,
        substituted_doc_ids=substituted,
    )
    assert results[0]["parent_document"] is True
    assert "섹션2 본문 전체 맥락" in results[0]["document"]
    assert consumed > 0
    assert substituted == {doc_id}


@pytest.mark.asyncio
async def test_apply_parent_documents_skips_single_chunk_doc(stores) -> None:
    """1청크 문서(섹션 폴백 없음)는 이미 전문이므로 치환하지 않는다."""
    meta_store, vector_store, _ = stores
    doc_id = await meta_store.create_document(
        source_type="confluence_mcp", title="Small",
        original_content="작은 문서 전문", content_hash="pd2",
    )
    await meta_store.create_chunk(
        chunk_id="pd2-0", document_id=doc_id, chunk_index=0,
        content="작은 문서 전문", token_count=5,
    )
    results = [{"metadata": {"document_id": doc_id}, "document": "작은 문서 전문"}]

    consumed = await _apply_parent_documents(
        results, meta_store,
        max_doc_tokens=32000, remaining_budget=96000,
        substituted_doc_ids=set(),
    )
    assert consumed == 0
    assert "parent_document" not in results[0]


@pytest.mark.asyncio
async def test_apply_parent_documents_respects_doc_limit(stores) -> None:
    """전문이 문서당 한도를 넘으면 기존 섹션 청크를 유지한다."""
    meta_store, vector_store, _ = stores
    doc_id = await _create_fallback_doc(
        meta_store, vector_store, title="BigDoc", content_hash="pd3",
        embedding=[0.9, 0.1],
    )
    results = [{"metadata": {"document_id": doc_id}, "document": "섹션1 본문"}]

    consumed = await _apply_parent_documents(
        results, meta_store,
        max_doc_tokens=1, remaining_budget=96000,
        substituted_doc_ids=set(),
    )
    assert consumed == 0
    assert results[0]["document"] == "섹션1 본문"
    assert "parent_document" not in results[0]


@pytest.mark.asyncio
async def test_apply_parent_documents_respects_total_budget(stores) -> None:
    """총합 예산이 소진되면 후순위 문서는 치환되지 않는다."""
    meta_store, vector_store, _ = stores
    doc_a = await _create_fallback_doc(
        meta_store, vector_store, title="DocA", content_hash="pd4a",
        embedding=[0.9, 0.1],
    )
    doc_b = await _create_fallback_doc(
        meta_store, vector_store, title="DocB", content_hash="pd4b",
        embedding=[0.8, 0.2],
    )
    doc_a_row = await meta_store.get_document(doc_a)
    budget = count_tokens(doc_a_row["original_content"])  # 첫 문서만큼만

    results = [
        {"metadata": {"document_id": doc_a}, "document": "섹션1 본문"},
        {"metadata": {"document_id": doc_b}, "document": "섹션1 본문"},
    ]
    consumed = await _apply_parent_documents(
        results, meta_store,
        max_doc_tokens=32000, remaining_budget=budget,
        substituted_doc_ids=set(),
    )
    assert consumed == budget
    assert results[0].get("parent_document") is True
    assert "parent_document" not in results[1]


@pytest.mark.asyncio
async def test_apply_parent_documents_skips_git_code(stores) -> None:
    """git_code 소스는 치환 대상에서 제외된다 (소스 첨부 경로와 중복 방지)."""
    meta_store, vector_store, _ = stores
    doc_id = await _create_fallback_doc(
        meta_store, vector_store, title="code.py", content_hash="pd5",
        embedding=[0.9, 0.1], source_type="git_code",
    )
    results = [{"metadata": {"document_id": doc_id}, "document": "섹션1 본문"}]

    consumed = await _apply_parent_documents(
        results, meta_store,
        max_doc_tokens=32000, remaining_budget=96000,
        substituted_doc_ids=set(),
    )
    assert consumed == 0
    assert "parent_document" not in results[0]


@pytest.mark.asyncio
async def test_apply_parent_documents_skips_empty_original(stores) -> None:
    """original_content 가 비어 있으면 치환하지 않는다."""
    meta_store, vector_store, _ = stores
    doc_id = await _create_fallback_doc(
        meta_store, vector_store, title="Empty", content_hash="pd6",
        embedding=[0.9, 0.1], original_content="",
    )
    results = [{"metadata": {"document_id": doc_id}, "document": "섹션1 본문"}]

    consumed = await _apply_parent_documents(
        results, meta_store,
        max_doc_tokens=32000, remaining_budget=96000,
        substituted_doc_ids=set(),
    )
    assert consumed == 0
    assert "parent_document" not in results[0]


@pytest.mark.asyncio
async def test_assemble_context_parent_doc_enabled(stores) -> None:
    """parent_doc_enabled=True 면 섹션 청크 대신 전문 + '전문 첨부' 라벨이 출력된다."""
    meta_store, vector_store, graph_store = stores
    await _create_fallback_doc(
        meta_store, vector_store, title="PDoc", content_hash="pd7",
        embedding=[0.9, 0.1],
    )
    embed_client = _make_embedding_client([0.9, 0.1])

    result = await assemble_context(
        query="섹션1", meta_store=meta_store, vector_store=vector_store,
        graph_store=graph_store, embedding_client=embed_client,
        include_graph=False, parent_doc_enabled=True,
    )
    assert "전문 첨부" in result
    assert "매칭 섹션: PDoc" in result
    assert "섹션2 본문 전체 맥락" in result  # 전문에만 있는 텍스트


@pytest.mark.asyncio
async def test_assemble_context_parent_doc_default_off(stores) -> None:
    """파라미터 미전달(기본 off) 시 기존 섹션 청크 출력이 유지된다."""
    meta_store, vector_store, graph_store = stores
    await _create_fallback_doc(
        meta_store, vector_store, title="PDoc", content_hash="pd8",
        embedding=[0.9, 0.1],
    )
    embed_client = _make_embedding_client([0.9, 0.1])

    result = await assemble_context(
        query="섹션1", meta_store=meta_store, vector_store=vector_store,
        graph_store=graph_store, embedding_client=embed_client,
        include_graph=False,
    )
    assert "전문 첨부" not in result
    assert "섹션2 본문 전체 맥락" not in result
    assert "섹션1 본문" in result


@pytest.mark.asyncio
async def test_with_sources_parent_doc_flag(stores) -> None:
    """with_sources 경로에서 Source.full_document 와 전문 라벨이 노출된다."""
    meta_store, vector_store, graph_store = stores
    doc_id = await _create_fallback_doc(
        meta_store, vector_store, title="PDoc", content_hash="pd9",
        embedding=[0.9, 0.1],
    )
    embed_client = _make_embedding_client([0.9, 0.1])

    ctx = await assemble_context_with_sources(
        query="섹션1", meta_store=meta_store, vector_store=vector_store,
        graph_store=graph_store, embedding_client=embed_client,
        include_graph=False, parent_doc_enabled=True,
    )
    assert "(전문 첨부, 매칭 섹션: PDoc)" in ctx.context_text
    assert "섹션2 본문 전체 맥락" in ctx.context_text
    source = next(s for s in ctx.sources if s.document_id == doc_id)
    assert source.full_document is True


@pytest.mark.asyncio
async def test_assemble_context_parent_doc_on_graph_sourced(stores) -> None:
    """그래프 경로로 도달한 다청크 문서도 전문으로 치환된다."""
    meta_store, vector_store, graph_store = stores
    doc_vec = await meta_store.create_document(
        source_type="manual", title="VecDoc", original_content="c", content_hash="pd10v",
    )
    vector_store.add_chunks(
        chunk_ids=["pd10v#body"], embeddings=[[0.9, 0.1]], documents=["벡터 본문"],
        metadatas=[{"document_id": doc_vec, "logical_chunk_id": "pd10v", "view": "body"}],
    )
    doc_graph = await _create_fallback_doc(
        meta_store, vector_store, title="GraphDoc", content_hash="pd10g",
        embedding=[0.1, 0.9],
    )
    await graph_store.save_graph_data(doc_graph, GraphData(
        entities=[Entity(name="PEnt", entity_type="service")], relations=[],
    ))

    embed_client = _make_embedding_client([0.9, 0.1])
    llm = _make_llm_client({
        "should_search": True, "reasoning": "x",
        "search_steps": [{"entity_name": "PEnt", "depth": 1, "focus_relations": []}],
    })

    result = await assemble_context(
        query="PEnt", meta_store=meta_store, vector_store=vector_store,
        graph_store=graph_store, embedding_client=embed_client, llm_client=llm,
        include_graph=True, similarity_threshold=0.5, max_graph_docs=3,
        parent_doc_enabled=True,
    )
    assert "## 그래프 연결 문서" in result
    assert "전문 첨부" in result
    assert "섹션2 본문 전체 맥락" in result


@pytest.mark.asyncio
async def test_parent_doc_question_view_hit(stores) -> None:
    """question view 적중도 동일하게 치환되며 매칭 질문 라벨은 유지된다."""
    meta_store, vector_store, graph_store = stores
    await _create_fallback_doc(
        meta_store, vector_store, title="QDoc", content_hash="pd11",
        embedding=[0.9, 0.1], view="question", question_text="QDoc 의 동작은?",
    )
    embed_client = _make_embedding_client([0.9, 0.1])

    result = await assemble_context(
        query="동작", meta_store=meta_store, vector_store=vector_store,
        graph_store=graph_store, embedding_client=embed_client,
        include_graph=False, parent_doc_enabled=True,
    )
    assert "전문 첨부" in result
    assert "매칭 질문: QDoc 의 동작은?" in result
    assert "섹션2 본문 전체 맥락" in result
