"""GraphStore 테스트."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from context_loop.processor.graph_extractor import Entity, GraphData, Relation
from context_loop.storage.graph_store import (
    GraphStore,
    _cosine_similarity,
    _extract_scoped_name,
    _extract_short_name,
)
from context_loop.storage.metadata_store import MetadataStore


def test_extract_short_name() -> None:
    """FQN에서 짧은 이름 추출."""
    assert _extract_short_name("user_service.py::UserService.create") == "create"
    assert _extract_short_name("user_service.py::main") == "main"
    assert _extract_short_name("UserService") == "UserService"
    # ::가 없으면 그대로 반환 (파일/import 모듈 엔티티)
    assert _extract_short_name("handler.go") == "handler.go"
    assert _extract_short_name("logging") == "logging"


def test_extract_scoped_name() -> None:
    """FQN에서 파일 범위를 제거한 부분 반환."""
    assert _extract_scoped_name("user_service.py::UserService.create") == "UserService.create"
    assert _extract_scoped_name("user_service.py::main") == "main"
    assert _extract_scoped_name("UserService") == "UserService"
    assert _extract_scoped_name("handler.go") == "handler.go"


@pytest.fixture
async def meta_store(tmp_path: Path) -> MetadataStore:  # type: ignore[misc]
    s = MetadataStore(tmp_path / "test.db")
    await s.initialize()
    yield s
    await s.close()


@pytest.fixture
async def graph_store(meta_store: MetadataStore) -> GraphStore:  # type: ignore[misc]
    return GraphStore(meta_store)


async def _create_doc(store: MetadataStore) -> int:
    return await store.create_document(
        source_type="manual",
        title="Test",
        original_content="content",
        content_hash="abc",
    )


@pytest.mark.asyncio
async def test_save_graph_data(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """엔티티와 관계를 저장한다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[
            Entity(name="Auth Service", entity_type="service"),
            Entity(name="User DB", entity_type="system"),
        ],
        relations=[
            Relation(source="Auth Service", target="User DB", relation_type="uses"),
        ],
    )
    result = await graph_store.save_graph_data(doc_id, data)
    assert result["nodes"] == 2
    assert result["edges"] == 1

    nodes = await meta_store.get_graph_nodes_by_document(doc_id)
    assert len(nodes) == 2
    edges = await meta_store.get_graph_edges_by_document(doc_id)
    assert len(edges) == 1


@pytest.mark.asyncio
async def test_save_graph_data_skips_missing_entity_in_relation(
    graph_store: GraphStore, meta_store: MetadataStore
) -> None:
    """relation의 source/target 엔티티가 없으면 해당 edge는 저장하지 않는다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[Entity(name="A", entity_type="system")],
        relations=[
            Relation(source="A", target="NonExistent", relation_type="depends_on"),
        ],
    )
    result = await graph_store.save_graph_data(doc_id, data)
    assert result["nodes"] == 1
    assert result["edges"] == 0  # B가 없어서 스킵


@pytest.mark.asyncio
async def test_save_graph_data_normalizes_vocab_aliases(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """저장 시 어휘 alias 가 canonical 로 정규화된다.

    - entity_type: struct → class (다른 문서의 class 노드와 병합 가능)
    - relation_type: has_part → contains, uses → depends_on
    - documented_in 은 방향까지 뒤집어 documents 로 저장
    """
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[
            Entity(name="Server", entity_type="struct"),
            Entity(name="Handler", entity_type="class"),
            Entity(name="설계문서", entity_type="document"),
        ],
        relations=[
            Relation(source="Server", target="Handler", relation_type="has_part"),
            Relation(source="Handler", target="설계문서",
                     relation_type="documented_in"),
        ],
    )
    await graph_store.save_graph_data(doc_id, data)

    types = {
        d["entity_name"]: d["entity_type"]
        for _, d in graph_store.graph.nodes(data=True)
    }
    assert types["Server"] == "class"  # struct → class

    edges = {
        (
            graph_store.graph.nodes[u]["entity_name"],
            graph_store.graph.nodes[v]["entity_name"],
        ): d["relation_type"]
        for u, v, d in graph_store.graph.edges(data=True)
    }
    assert edges[("Server", "Handler")] == "contains"  # has_part → contains
    # documented_in(Handler, 설계문서) → documents(설계문서, Handler)
    assert edges[("설계문서", "Handler")] == "documents"
    assert ("Handler", "설계문서") not in edges

    # SQLite 영속본도 canonical 로 저장된다.
    db_nodes = await meta_store.get_graph_nodes_by_document(doc_id)
    db_types = {n["entity_name"]: n["entity_type"] for n in db_nodes}
    assert db_types["Server"] == "class"
    db_edges = await meta_store.get_graph_edges_by_document(doc_id)
    assert {e["relation_type"] for e in db_edges} == {"contains", "documents"}


@pytest.mark.asyncio
async def test_alias_entity_type_merges_with_canonical_node(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """struct 로 추출된 엔티티가 기존 class 노드와 같은 정규 노드로 병합된다."""
    doc1 = await _create_doc(meta_store)
    doc2 = await meta_store.create_document(
        source_type="manual", title="Doc2",
        original_content="content", content_hash="abc2",
    )
    await graph_store.save_graph_data(doc1, GraphData(
        entities=[Entity(name="Server", entity_type="class")],
    ))
    result = await graph_store.save_graph_data(doc2, GraphData(
        entities=[Entity(name="Server", entity_type="struct")],
    ))
    assert result["merged"] == 1
    assert result["nodes"] == 0
    assert graph_store.stats()["nodes"] == 1


@pytest.mark.asyncio
async def test_delete_document_graph(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """문서 그래프 삭제 후 노드/엣지가 사라진다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[Entity(name="X"), Entity(name="Y")],
        relations=[Relation(source="X", target="Y", relation_type="related_to")],
    )
    await graph_store.save_graph_data(doc_id, data)
    await graph_store.delete_document_graph(doc_id)

    nodes = await meta_store.get_graph_nodes_by_document(doc_id)
    edges = await meta_store.get_graph_edges_by_document(doc_id)
    assert nodes == []
    assert edges == []
    assert graph_store.stats()["nodes"] == 0


@pytest.mark.asyncio
async def test_get_neighbors(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """엔티티 이름으로 주변 노드를 탐색한다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[
            Entity(name="API Gateway"),
            Entity(name="Auth Service"),
            Entity(name="User DB"),
        ],
        relations=[
            Relation(source="API Gateway", target="Auth Service", relation_type="depends_on"),
            Relation(source="Auth Service", target="User DB", relation_type="uses"),
        ],
    )
    await graph_store.save_graph_data(doc_id, data)

    neighbors = graph_store.get_neighbors("API Gateway", depth=1)
    names = [n["entity_name"] for n in neighbors]
    assert "API Gateway" in names
    assert "Auth Service" in names

    # depth=2면 User DB도 포함
    neighbors2 = graph_store.get_neighbors("API Gateway", depth=2)
    names2 = [n["entity_name"] for n in neighbors2]
    assert "User DB" in names2


@pytest.mark.asyncio
async def test_get_neighbors_nonexistent(graph_store: GraphStore) -> None:
    """존재하지 않는 엔티티는 빈 목록을 반환한다."""
    result = graph_store.get_neighbors("Nonexistent Entity")
    assert result == []


@pytest.mark.asyncio
async def test_get_neighbors_short_name_fallback(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """FQN으로 등록된 코드 심볼은 짧은 이름으로도 매칭된다.

    AST 코드 추출기는 `file.py::Class.method` 형태의 FQN으로 엔티티를 등록하므로,
    LLM/클라이언트가 짧은 이름으로 질의해도 fallback 매칭이 동작해야 한다.
    """
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[
            Entity(name="user_service.py", entity_type="module"),
            Entity(name="user_service.py::UserService", entity_type="class"),
            Entity(name="user_service.py::UserService.create_user", entity_type="method"),
        ],
        relations=[
            Relation(
                source="user_service.py::UserService",
                target="user_service.py::UserService.create_user",
                relation_type="contains",
            ),
        ],
    )
    await graph_store.save_graph_data(doc_id, data)

    # 1. FQN 완전 일치 (기본 경로)
    by_fqn = graph_store.get_neighbors(
        "user_service.py::UserService.create_user", depth=1,
    )
    assert any(
        n["entity_name"] == "user_service.py::UserService.create_user"
        for n in by_fqn
    )

    # 2. 파일 범위를 벗긴 부분 매칭 ("Class.method")
    by_scoped = graph_store.get_neighbors("UserService.create_user", depth=1)
    assert any(
        n["entity_name"] == "user_service.py::UserService.create_user"
        for n in by_scoped
    )

    # 3. 짧은 이름 매칭 ("create_user" 단일 토큰)
    by_short = graph_store.get_neighbors("create_user", depth=1)
    assert any(
        n["entity_name"] == "user_service.py::UserService.create_user"
        for n in by_short
    )

    # 4. 클래스 짧은 이름 매칭
    by_class = graph_store.get_neighbors("UserService", depth=1)
    assert any(
        n["entity_name"] == "user_service.py::UserService" for n in by_class
    )


@pytest.mark.asyncio
async def test_get_neighbors_bridges_bare_and_fqn_nodes(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """한 질의 이름이 bare 노드(문서)와 FQN 노드(코드)를 함께 시드로 잡는다.

    표면 tier 를 union 하므로, confluence 가 만든 bare ``UserService`` 노드와
    git_code 가 만든 FQN ``other.py::UserService`` 노드가 별개로 저장돼 있어도
    질의 "UserService" 하나로 **둘 다** 중심 노드가 되어 코드↔지식 그래프를
    검색 시점에 잇는다 (과거에는 완전 일치에서 멈춰 bare 노드만 반환했다).
    """
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[
            Entity(name="UserService", entity_type="class"),
            Entity(name="other.py::UserService", entity_type="class"),
        ],
    )
    await graph_store.save_graph_data(doc_id, data)

    result = graph_store.get_neighbors("UserService", depth=1)
    names = {n["entity_name"] for n in result}
    assert names == {"UserService", "other.py::UserService"}


@pytest.mark.asyncio
async def test_get_neighbors_normalizes_spacing_and_case(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """표면 매칭이 storage 와 동일한 normalize 키로 정렬돼 공백/케이스/하이픈을 흡수한다.

    "Auth Service" / "auth-service" 가 코드 FQN ``auth_service.py::AuthService``
    의 scoped/short 이름(``AuthService``)과 정규화 후 같은 키로 매칭된다 —
    임베딩 fallback 없이 표면 tier 에서 코드 노드에 닿는다.
    """
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[Entity(name="auth_service.py::AuthService", entity_type="class")],
        relations=[],
    ))

    for query in ("Auth Service", "auth-service", "AUTHSERVICE"):
        result = graph_store.get_neighbors(query, depth=1)
        names = {n["entity_name"] for n in result}
        assert "auth_service.py::AuthService" in names, query


@pytest.mark.asyncio
async def test_get_neighbors_seed_count_is_capped(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """흔한 짧은 이름이 과도한 시드를 끌어오지 않도록 max_seeds 로 컷된다."""
    doc_id = await _create_doc(meta_store)
    # 15개 파일이 모두 같은 짧은 이름 `run` 을 가진 FQN 심볼을 정의.
    entities = [
        Entity(name=f"mod{i}.py::run", entity_type="function") for i in range(15)
    ]
    await graph_store.save_graph_data(doc_id, GraphData(entities=entities, relations=[]))

    # depth=1, 엣지 없음 → 도달 노드 = 시드. 기본 상한 10 으로 컷.
    result = graph_store.get_neighbors("run", depth=1)
    assert len(result) == 10


@pytest.mark.asyncio
async def test_load_from_db(meta_store: MetadataStore, tmp_path: Path) -> None:
    """DB에서 그래프를 로드하여 재구성한다."""
    store1 = GraphStore(meta_store)
    doc_id = await _create_doc(meta_store)
    data = GraphData(entities=[Entity(name="Node A"), Entity(name="Node B")])
    await store1.save_graph_data(doc_id, data)

    # 새 GraphStore 인스턴스로 로드
    store2 = GraphStore(meta_store)
    await store2.load_from_db()
    assert store2.stats()["nodes"] == 2


# --- 엔티티 임베딩 캐시 테스트 ---


def test_cosine_similarity_identical() -> None:
    """동일 벡터의 코사인 유사도는 1.0이다."""
    v = [1.0, 0.0, 0.0]
    assert abs(_cosine_similarity(v, v) - 1.0) < 1e-6


def test_cosine_similarity_orthogonal() -> None:
    """직교 벡터의 코사인 유사도는 0.0이다."""
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    assert abs(_cosine_similarity(a, b)) < 1e-6


def test_cosine_similarity_zero_vector() -> None:
    """영벡터와의 유사도는 0.0이다."""
    assert _cosine_similarity([0.0, 0.0], [1.0, 1.0]) == 0.0


@pytest.mark.asyncio
async def test_build_entity_embeddings(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """엔티티 임베딩을 빌드하면 캐시에 저장된다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[Entity(name="Gateway"), Entity(name="AuthService")],
        relations=[],
    )
    await graph_store.save_graph_data(doc_id, data)

    mock_embed = AsyncMock()
    mock_embed.aembed_documents = AsyncMock(return_value=[[1.0, 0.0], [0.0, 1.0]])

    count = await graph_store.build_entity_embeddings(mock_embed)
    assert count == 2
    assert graph_store.entity_embedding_count == 2


@pytest.mark.asyncio
async def test_build_entity_embeddings_uses_display_name_for_fqn(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """코드 FQN 노드는 파일 범위를 벗긴 표시명으로 임베딩된다 (자연어 질의와 정렬).

    임베딩 입력 텍스트는 ``UserService.create`` 같은 표시명이지만, 캐시에 저장되는
    엔티티 이름은 원본 FQN 을 유지해 다운스트림 표기를 보존한다.
    """
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name="user_service.py::UserService.create", entity_type="method"),
            Entity(name="Auth Service", entity_type="system"),
        ],
        relations=[],
    ))

    mock_embed = AsyncMock()
    mock_embed.aembed_documents = AsyncMock(return_value=[[1.0, 0.0], [0.0, 1.0]])
    await graph_store.build_entity_embeddings(mock_embed)

    embedded_inputs = set(mock_embed.aembed_documents.call_args.args[0])
    # FQN 은 표시명으로, bare 이름은 그대로.
    assert "UserService.create" in embedded_inputs
    assert "user_service.py::UserService.create" not in embedded_inputs
    assert "Auth Service" in embedded_inputs

    # 이미 캐시됨 → 다시 빌드해도 0
    count2 = await graph_store.build_entity_embeddings(mock_embed)
    assert count2 == 0


@pytest.mark.asyncio
async def test_build_entity_embeddings_batches_large_node_set(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """엔티티 수가 배치 크기를 넘으면 batch_size 단위로 나눠 호출한다."""
    doc_id = await _create_doc(meta_store)
    entities = [Entity(name=f"Entity{i}") for i in range(250)]
    await graph_store.save_graph_data(doc_id, GraphData(entities=entities, relations=[]))

    async def fake_embed(texts: list[str]) -> list[list[float]]:
        return [[float(len(t)), 0.0] for t in texts]

    mock_embed = AsyncMock()
    mock_embed.aembed_documents = AsyncMock(side_effect=fake_embed)

    count = await graph_store.build_entity_embeddings(mock_embed, batch_size=100)

    assert count == 250
    assert graph_store.entity_embedding_count == 250
    # 250개를 100개 단위로 → 3청크 (100, 100, 50). 병렬이라 순서 비결정적.
    assert mock_embed.aembed_documents.await_count == 3
    batch_sizes = sorted(len(call.args[0]) for call in mock_embed.aembed_documents.await_args_list)
    assert batch_sizes == [50, 100, 100]


@pytest.mark.asyncio
async def test_build_entity_embeddings_respects_concurrency_limit(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """concurrency 파라미터로 동시 호출 수를 제한한다."""
    doc_id = await _create_doc(meta_store)
    entities = [Entity(name=f"Entity{i}") for i in range(500)]
    await graph_store.save_graph_data(doc_id, GraphData(entities=entities, relations=[]))

    state = {"in_flight": 0, "max": 0}

    async def fake_embed(texts: list[str]) -> list[list[float]]:
        state["in_flight"] += 1
        state["max"] = max(state["max"], state["in_flight"])
        await asyncio.sleep(0.01)  # 겹침을 강제해 동시 실행 수를 측정
        state["in_flight"] -= 1
        return [[1.0, 0.0] for _ in texts]

    mock_embed = AsyncMock()
    mock_embed.aembed_documents = AsyncMock(side_effect=fake_embed)

    count = await graph_store.build_entity_embeddings(
        mock_embed, batch_size=100, concurrency=2,
    )

    assert count == 500
    # 500개를 100개씩 → 5청크, 동시 실행은 최대 2.
    assert mock_embed.aembed_documents.await_count == 5
    assert state["max"] == 2


@pytest.mark.asyncio
async def test_build_entity_embeddings_partial_failure_keeps_successful_chunks(
    graph_store: GraphStore, meta_store: MetadataStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """한 청크가 실패해도 성공한 청크는 보존되고, 누락분은 재호출로 복구된다."""
    from context_loop.storage import graph_store as gs_mod

    monkeypatch.setattr(gs_mod, "_ENTITY_EMBED_MAX_RETRIES", 1)  # 재시도 없이 즉시 실패

    doc_id = await _create_doc(meta_store)
    entities = [Entity(name=f"Entity{i}") for i in range(250)]
    await graph_store.save_graph_data(doc_id, GraphData(entities=entities, relations=[]))

    async def flaky_embed(texts: list[str]) -> list[list[float]]:
        # 두 번째 청크(Entity100~199)만 실패 — 실행 순서와 무관하게 내용으로 판별.
        if "Entity150" in texts:
            raise RuntimeError("rate limited")
        return [[1.0, 0.0] for _ in texts]

    mock_embed = AsyncMock()
    mock_embed.aembed_documents = AsyncMock(side_effect=flaky_embed)

    count = await graph_store.build_entity_embeddings(mock_embed, batch_size=100)
    # 성공한 2개 청크(100+50)만 반영, 실패한 1개 청크(100)는 누락.
    assert count == 150
    assert graph_store.entity_embedding_count == 150

    # 재호출 시 누락된 100개만 다시 시도하여 복구.
    mock_embed.aembed_documents = AsyncMock(side_effect=lambda texts: [[1.0, 0.0] for _ in texts])
    count2 = await graph_store.build_entity_embeddings(mock_embed, batch_size=100)
    assert count2 == 100
    assert graph_store.entity_embedding_count == 250
    assert mock_embed.aembed_documents.await_count == 1


@pytest.mark.asyncio
async def test_build_entity_embeddings_retries_transient_failure(
    graph_store: GraphStore, meta_store: MetadataStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """청크 호출이 일시적으로 실패하면 백오프 후 재시도하여 성공한다."""
    from context_loop.storage import graph_store as gs_mod

    monkeypatch.setattr(gs_mod, "_ENTITY_EMBED_MAX_RETRIES", 3)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(gs_mod.asyncio, "sleep", fake_sleep)

    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(
        doc_id, GraphData(entities=[Entity(name="Gateway")], relations=[]),
    )

    attempts = {"n": 0}

    async def flaky_embed(texts: list[str]) -> list[list[float]]:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("transient")
        return [[1.0, 0.0] for _ in texts]

    mock_embed = AsyncMock()
    mock_embed.aembed_documents = AsyncMock(side_effect=flaky_embed)

    count = await graph_store.build_entity_embeddings(mock_embed)
    assert count == 1
    assert attempts["n"] == 3
    # 2번 실패 → 2번 백오프(2^0, 2^1).
    assert sleeps == [1.0, 2.0]


@pytest.mark.asyncio
async def test_unembedded_entity_count_tracks_pending(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """unembedded_entity_count 는 이름이 있는 미임베딩 노드만 센다."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[Entity(name="Gateway"), Entity(name="AuthService")],
        relations=[],
    ))

    # 빌드 전: 둘 다 미임베딩.
    assert graph_store.unembedded_entity_count == 2

    mock_embed = AsyncMock()
    mock_embed.aembed_documents = AsyncMock(return_value=[[1.0, 0.0], [0.0, 1.0]])
    await graph_store.build_entity_embeddings(mock_embed)

    # 빌드 후: 보완할 노드 없음.
    assert graph_store.unembedded_entity_count == 0


@pytest.mark.asyncio
async def test_unembedded_entity_count_drops_after_partial_heal(
    graph_store: GraphStore, meta_store: MetadataStore, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """부분 실패 후 남은 미임베딩 수가 재호출(다음 검색 lazy 보완)로 0이 된다."""
    from context_loop.storage import graph_store as gs_mod

    monkeypatch.setattr(gs_mod, "_ENTITY_EMBED_MAX_RETRIES", 1)

    doc_id = await _create_doc(meta_store)
    entities = [Entity(name=f"Entity{i}") for i in range(250)]
    await graph_store.save_graph_data(doc_id, GraphData(entities=entities, relations=[]))

    async def flaky_embed(texts: list[str]) -> list[list[float]]:
        if "Entity150" in texts:  # 두 번째 청크(100~199)만 실패
            raise RuntimeError("rate limited")
        return [[1.0, 0.0] for _ in texts]

    mock_embed = AsyncMock()
    mock_embed.aembed_documents = AsyncMock(side_effect=flaky_embed)

    await graph_store.build_entity_embeddings(mock_embed, batch_size=100)
    # 100개 청크가 실패로 누락 → 보완 대상 100개.
    assert graph_store.unembedded_entity_count == 100

    # 다음 검색이 lazy 보완을 트리거하는 상황: 다시 호출하면 0이 된다.
    mock_embed.aembed_documents = AsyncMock(side_effect=lambda texts: [[1.0, 0.0] for _ in texts])
    await graph_store.build_entity_embeddings(mock_embed, batch_size=100)
    assert graph_store.unembedded_entity_count == 0


@pytest.mark.asyncio
async def test_build_entity_embeddings_serializes_concurrent_calls(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """동시 호출은 lock 으로 직렬화되어 같은 노드를 중복 임베딩하지 않는다."""
    doc_id = await _create_doc(meta_store)
    entities = [Entity(name=f"Entity{i}") for i in range(300)]
    await graph_store.save_graph_data(doc_id, GraphData(entities=entities, relations=[]))

    async def slow_embed(texts: list[str]) -> list[list[float]]:
        await asyncio.sleep(0.01)
        return [[1.0, 0.0] for _ in texts]

    mock_embed = AsyncMock()
    mock_embed.aembed_documents = AsyncMock(side_effect=slow_embed)

    # 두 빌드를 동시에 시작. 두 번째는 lock 대기 후 missing 이 비어 빈 작업.
    c1, c2 = await asyncio.gather(
        graph_store.build_entity_embeddings(mock_embed, batch_size=100),
        graph_store.build_entity_embeddings(mock_embed, batch_size=100),
    )

    assert c1 + c2 == 300  # 한쪽이 300, 다른 쪽은 0 (중복 없음)
    assert graph_store.entity_embedding_count == 300
    # 300개를 100개씩 → 정확히 3청크만 호출 (중복 임베딩 없음).
    assert mock_embed.aembed_documents.await_count == 3


@pytest.mark.asyncio
async def test_search_entities_by_embedding(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """임베딩 유사도로 엔티티를 검색한다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[
            Entity(name="Gateway", entity_type="component"),
            Entity(name="AuthService", entity_type="service"),
            Entity(name="Database", entity_type="system"),
        ],
        relations=[],
    )
    await graph_store.save_graph_data(doc_id, data)

    mock_embed = AsyncMock()
    mock_embed.aembed_documents = AsyncMock(return_value=[
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
    ])
    await graph_store.build_entity_embeddings(mock_embed)

    # Gateway와 유사한 벡터로 검색
    results = graph_store.search_entities_by_embedding(
        [0.9, 0.1, 0.0], threshold=0.7, top_k=2,
    )
    assert len(results) >= 1
    assert results[0]["entity_name"] == "Gateway"

    # 아무것도 매칭 안 되는 벡터
    results_empty = graph_store.search_entities_by_embedding(
        [0.0, 0.0, 0.0], threshold=0.7,
    )
    assert results_empty == []


@pytest.mark.asyncio
async def test_save_graph_data_concurrent_orphan_cleanup_safe(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """save_graph_data 가 신규 노드 INSERT 와 link INSERT 를 한 트랜잭션으로
    처리하여, 동시 진행 중인 다른 문서의 delete_graph_data_by_document 가
    호출되어도 FK 위반이 발생하지 않는다 — 재인덱싱 산발 실패의 핵심 회귀 가드.

    asyncio 동시 실행으로 두 코루틴을 함께 돌리는 시뮬레이션:
    - A: save_graph_data 로 신규 노드 N 개 생성
    - B: 비어있는 다른 문서의 delete_graph_data_by_document 호출 (orphan SQL 발동)
    """
    doc_a = await _create_doc(meta_store)
    doc_b = await _create_doc(meta_store)

    # B 의 사전 정리 호출은 B 가 link 한 노드만 정리하므로 안전 — 이 테스트는
    # save 도중 다른 문서의 delete 가 발생하더라도 A 의 신규 노드가 살아남는지
    # 검증한다.
    import asyncio
    async def run_save() -> None:
        data = GraphData(
            entities=[
                Entity(name=f"Node{i}", entity_type="system") for i in range(5)
            ],
            relations=[],
        )
        await graph_store.save_graph_data(doc_a, data)

    async def run_delete_other() -> None:
        # B 의 정리 — A 와 무관한 작업
        await meta_store.delete_graph_data_by_document(doc_b)

    await asyncio.gather(run_save(), run_delete_other())

    # A 의 모든 노드가 보존되어야 함 (FK 위반 / 누락 없음)
    nodes_after = await meta_store.get_graph_nodes_by_document(doc_a)
    assert len(nodes_after) == 5


@pytest.mark.asyncio
async def test_search_entities_default_threshold_lowered_to_0_5(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """R1: ``search_entities_by_embedding`` 기본 threshold 가 0.5 로 낮춰졌다.

    이전 0.7 은 표기 변형(공백/케이스/하이픈)이 있는 검색에서 의미 임베딩 통과를
    어렵게 만들었다 — 그래프 검색 funnel 손실의 한 축. 0.5 가 default 임을
    회귀 가드로 확정.
    """
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[Entity(name="X", entity_type="t")], relations=[],
    ))
    mock_embed = AsyncMock()
    mock_embed.aembed_documents = AsyncMock(return_value=[[1.0, 0.0]])
    await graph_store.build_entity_embeddings(mock_embed)

    # cosine 0.6 (sqrt(0.36+0.64)=1.0, 0.6 dot 1.0 = 0.6) → 0.5 통과, 0.7 미통과.
    results = graph_store.search_entities_by_embedding([0.6, 0.8])  # default
    assert len(results) == 1, "default threshold 가 0.5 미만으로 낮춰진 것에 의존"


@pytest.mark.asyncio
async def test_get_neighbors_falls_back_to_embedding_when_name_unknown(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """R1 F-SRCH-01: 표면 매칭(exact/scoped/short)이 모두 실패해도 임베딩
    fallback 으로 시드 노드를 찾는다.

    정규화로도 잇지 못하는 진짜 의미 차이(예: 한국어 "인증 서비스" ↔ 영어
    "AuthService")일 때, LLM 추측 이름이 빈 결과가 되어 그래프 메트릭이 0 이
    되는 funnel 손실을 임베딩 fallback 으로 완화한다.
    """
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[Entity(name="AuthService", entity_type="system")],
        relations=[],
    ))
    mock_embed = AsyncMock()
    mock_embed.aembed_documents = AsyncMock(return_value=[[1.0, 0.0, 0.0]])
    await graph_store.build_entity_embeddings(mock_embed)

    # 표면 매칭으로는 "인증 서비스" (정규화 "인증서비스") ≠ "authservice" → 빈 결과.
    no_fallback = graph_store.get_neighbors("인증 서비스")
    assert no_fallback == []

    # 같은 의미의 임베딩 fallback 을 주면 시드 노드가 잡혀 결과가 비어있지 않다.
    with_fallback = graph_store.get_neighbors(
        "인증 서비스",
        embedding_fallback=[0.95, 0.1, 0.0],  # cosine ~0.96 > 0.5
    )
    assert len(with_fallback) == 1
    assert with_fallback[0]["entity_name"] == "AuthService"


@pytest.mark.asyncio
async def test_get_neighbors_from_node_id_returns_subgraph(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """R1: ``get_neighbors_from_node_id`` 가 node_id 시드로 직접 서브그래프를
    반환한다 — execute_graph_search 의 query-embedding fallback 경로에서 사용."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name="A", entity_type="t"),
            Entity(name="B", entity_type="t"),
        ],
        relations=[Relation(source="A", target="B", relation_type="related")],
    ))
    # NetworkX 그래프에서 A 의 node_id 찾기
    a_id = next(
        n for n, d in graph_store.graph.nodes(data=True)
        if d.get("entity_name") == "A"
    )
    result = graph_store.get_neighbors_from_node_id(a_id, depth=1)
    names = {n["entity_name"] for n in result}
    assert "A" in names
    assert "B" in names


@pytest.mark.asyncio
async def test_get_neighbors_follows_both_directions(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """R2 (F-SRCH-R2-01): get_neighbors 가 양방향(successor + predecessor) 으로
    1-hop 이웃을 모두 반환한다.

    DiGraph 의 자연 동작인 successor-only 는 sink 노드(예: 데이터베이스, 외부
    시스템)가 시드로 선택되면 retrieved 에 sink 자신만 담겨 gold seed 누락의
    가장 큰 원인이 되었다. 양방향 BFS 로 이를 회복한다.
    """
    doc_id = await _create_doc(meta_store)
    # A → B → C  (A: 소스, B: 중간, C: sink)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name="A", entity_type="t"),
            Entity(name="B", entity_type="t"),
            Entity(name="C", entity_type="t"),
        ],
        relations=[
            Relation(source="A", target="B", relation_type="uses"),
            Relation(source="B", target="C", relation_type="uses"),
        ],
    ))
    # sink 노드 C 에서 양방향 이웃 → C, B 가 포함되어야 한다.
    result = graph_store.get_neighbors("C", depth=1)
    names = {n["entity_name"] for n in result}
    assert "C" in names, "자기 자신 포함"
    assert "B" in names, "양방향이면 incoming neighbor B 도 포함"


@pytest.mark.asyncio
async def test_get_neighbors_from_node_id_bidirectional(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """R2: get_neighbors_from_node_id 도 양방향."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name="X", entity_type="t"),
            Entity(name="Y", entity_type="t"),
        ],
        relations=[Relation(source="X", target="Y", relation_type="uses")],
    ))
    y_id = next(
        n for n, d in graph_store.graph.nodes(data=True)
        if d.get("entity_name") == "Y"
    )
    # sink 노드 Y 에서 — incoming 으로 X 가 와야 한다.
    result = graph_store.get_neighbors_from_node_id(y_id, depth=1)
    names = {n["entity_name"] for n in result}
    assert "Y" in names
    assert "X" in names


@pytest.mark.asyncio
async def test_delete_clears_embedding_cache(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """문서 삭제 시 임베딩 캐시도 삭제된다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(entities=[Entity(name="NodeA")], relations=[])
    await graph_store.save_graph_data(doc_id, data)

    mock_embed = AsyncMock()
    mock_embed.aembed_documents = AsyncMock(return_value=[[1.0, 0.0]])
    await graph_store.build_entity_embeddings(mock_embed)
    assert graph_store.entity_embedding_count == 1

    await graph_store.delete_document_graph(doc_id)
    assert graph_store.entity_embedding_count == 0


# --- 그래프 스키마 요약 테스트 ---


@pytest.mark.asyncio
async def test_get_schema_summary_empty(graph_store: GraphStore) -> None:
    """빈 그래프의 스키마 요약은 모든 값이 0/빈 값이다."""
    summary = graph_store.get_schema_summary()
    assert summary["total_nodes"] == 0
    assert summary["total_edges"] == 0
    assert summary["entity_types"] == {}
    assert summary["relation_types"] == {}


@pytest.mark.asyncio
async def test_get_schema_summary_with_data(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """그래프 데이터가 있으면 유형별 집계를 반환한다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[
            Entity(name="Gateway", entity_type="component"),
            Entity(name="AuthService", entity_type="service"),
            Entity(name="UserDB", entity_type="system"),
        ],
        relations=[
            Relation(source="Gateway", target="AuthService", relation_type="depends_on"),
            Relation(source="AuthService", target="UserDB", relation_type="uses"),
        ],
    )
    await graph_store.save_graph_data(doc_id, data)

    summary = graph_store.get_schema_summary()
    assert summary["total_nodes"] == 3
    assert summary["total_edges"] == 2
    assert "component" in summary["entity_types"]
    assert "service" in summary["entity_types"]
    # uses 는 alias — 저장 시 depends_on 으로 정규화된다.
    assert "depends_on" in summary["relation_types"]
    assert "uses" not in summary["relation_types"]
    assert "Gateway" in summary["entities_by_type"]["component"]
    assert len(summary["sample_relations"]) == 2


@pytest.mark.asyncio
async def test_format_schema_for_llm_empty(graph_store: GraphStore) -> None:
    """빈 그래프의 LLM 포맷은 비어있음을 알린다."""
    text = graph_store.format_schema_for_llm()
    assert "비어 있습니다" in text


@pytest.mark.asyncio
async def test_format_schema_for_llm_with_data(graph_store: GraphStore, meta_store: MetadataStore) -> None:
    """그래프 데이터가 있으면 LLM이 읽을 수 있는 텍스트를 생성한다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[
            Entity(name="Gateway", entity_type="component"),
            Entity(name="AuthService", entity_type="service"),
        ],
        relations=[
            Relation(source="Gateway", target="AuthService", relation_type="depends_on"),
        ],
    )
    await graph_store.save_graph_data(doc_id, data)

    text = graph_store.format_schema_for_llm()
    assert "지식 그래프 구조" in text
    assert "Gateway" in text
    assert "AuthService" in text
    assert "depends_on" in text
    assert "component" in text
    assert "service" in text


# --- 쿼리 기반 스키마 테스트 ---


@pytest.mark.asyncio
async def test_get_query_relevant_schema_no_embeddings(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """임베딩 캐시가 없으면 전체 스키마로 폴백한다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[Entity(name="A", entity_type="service")],
        relations=[],
    )
    await graph_store.save_graph_data(doc_id, data)

    # 임베딩 구축 없이 호출
    summary = graph_store.get_query_relevant_schema([1.0, 0.0])
    # 전체 스키마와 동일해야 함
    assert summary["total_nodes"] == 1
    assert "seed_entities" not in summary


@pytest.mark.asyncio
async def test_get_query_relevant_schema_filters_by_query(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """쿼리 임베딩과 유사한 엔티티 중심으로 축소된 스키마를 생성한다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[
            Entity(name="Gateway", entity_type="component"),
            Entity(name="AuthService", entity_type="service"),
            Entity(name="PaymentService", entity_type="service"),
            Entity(name="Database", entity_type="system"),
        ],
        relations=[
            Relation(source="Gateway", target="AuthService", relation_type="depends_on"),
            Relation(source="Gateway", target="PaymentService", relation_type="depends_on"),
            Relation(source="AuthService", target="Database", relation_type="uses"),
            Relation(source="PaymentService", target="Database", relation_type="uses"),
        ],
    )
    await graph_store.save_graph_data(doc_id, data)

    # 임베딩 구축: Gateway=[1,0,0,0], Auth=[0,1,0,0], Payment=[0,0,1,0], DB=[0,0,0,1]
    mock_embed = AsyncMock()
    mock_embed.aembed_documents = AsyncMock(return_value=[
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ])
    await graph_store.build_entity_embeddings(mock_embed)

    # Gateway와 유사한 쿼리 → Gateway + 이웃(Auth, Payment)만 포함, DB는 depth=1이면 제외 안 됨
    summary = graph_store.get_query_relevant_schema(
        [0.9, 0.1, 0.0, 0.0],  # Gateway와 가장 유사
        similarity_threshold=0.5,
        top_k=1,
        neighbor_depth=1,
    )
    assert "seed_entities" in summary
    assert summary["seed_entities"][0]["name"] == "Gateway"
    # Gateway(시드) + AuthService, PaymentService(이웃) = 3개
    assert summary["total_nodes"] == 3
    entity_names = []
    for names in summary["entities_by_type"].values():
        entity_names.extend(names)
    assert "Gateway" in entity_names
    assert "AuthService" in entity_names
    assert "PaymentService" in entity_names


@pytest.mark.asyncio
async def test_get_query_relevant_schema_no_similar_entities(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """유사한 엔티티가 없으면 전체 스키마로 폴백한다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[Entity(name="X", entity_type="service")],
        relations=[],
    )
    await graph_store.save_graph_data(doc_id, data)

    mock_embed = AsyncMock()
    mock_embed.aembed_documents = AsyncMock(return_value=[[1.0, 0.0]])
    await graph_store.build_entity_embeddings(mock_embed)

    # 완전히 반대 방향 벡터 → 유사도 낮음
    summary = graph_store.get_query_relevant_schema(
        [0.0, 1.0], similarity_threshold=0.9,
    )
    # 폴백: 전체 스키마
    assert "seed_entities" not in summary
    assert summary["total_nodes"] == 1


@pytest.mark.asyncio
async def test_format_query_relevant_schema_for_llm(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """쿼리 관련 스키마의 LLM 포맷에 핵심 엔티티 섹션이 포함된다."""
    doc_id = await _create_doc(meta_store)
    data = GraphData(
        entities=[
            Entity(name="Gateway", entity_type="component"),
            Entity(name="Auth", entity_type="service"),
        ],
        relations=[
            Relation(source="Gateway", target="Auth", relation_type="depends_on"),
        ],
    )
    await graph_store.save_graph_data(doc_id, data)

    mock_embed = AsyncMock()
    mock_embed.aembed_documents = AsyncMock(return_value=[[1.0, 0.0], [0.0, 1.0]])
    await graph_store.build_entity_embeddings(mock_embed)

    text = graph_store.format_query_relevant_schema_for_llm(
        [0.9, 0.1], similarity_threshold=0.5,
    )
    assert "쿼리 관련 핵심 엔티티" in text
    assert "Gateway" in text
    assert "관련 노드" in text


# --- 크로스-문서 엔티티 병합 테스트 ---


async def _create_doc_with_title(store: MetadataStore, title: str) -> int:
    return await store.create_document(
        source_type="manual",
        title=title,
        original_content="content",
        content_hash=f"hash_{title}",
    )


@pytest.mark.asyncio
async def test_cross_doc_entity_merge_same_entity(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """동일 엔티티(이름+타입)가 다른 문서에서 등장하면 같은 노드로 병합된다."""
    doc1 = await _create_doc_with_title(meta_store, "Doc A")
    doc2 = await _create_doc_with_title(meta_store, "Doc B")

    data1 = GraphData(
        entities=[
            Entity(name="쿠버네티스", entity_type="technology"),
            Entity(name="도커", entity_type="technology"),
        ],
        relations=[
            Relation(source="쿠버네티스", target="도커", relation_type="uses"),
        ],
    )
    result1 = await graph_store.save_graph_data(doc1, data1)
    assert result1["nodes"] == 2
    assert result1["merged"] == 0

    data2 = GraphData(
        entities=[
            Entity(name="쿠버네티스", entity_type="technology"),
            Entity(name="AWS", entity_type="platform"),
        ],
        relations=[
            Relation(source="쿠버네티스", target="AWS", relation_type="runs_on"),
        ],
    )
    result2 = await graph_store.save_graph_data(doc2, data2)
    assert result2["nodes"] == 1  # AWS만 신규
    assert result2["merged"] == 1  # 쿠버네티스 병합

    # 전체 노드 수: 쿠버네티스(1) + 도커(1) + AWS(1) = 3
    assert graph_store.stats()["nodes"] == 3
    assert graph_store.stats()["edges"] == 2


@pytest.mark.asyncio
async def test_merged_node_visible_in_second_doc_graph_query(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """canonical 병합된 노드가 두 번째 문서의 그래프 조회에도 포함된다.

    회귀 방지: 과거에는 `graph_nodes.document_id` 컬럼만 조회해 두 번째 import한
    문서의 그래프 탭에서 공유 모듈 노드가 보이지 않는 버그가 있었다.
    """
    doc1 = await _create_doc_with_title(meta_store, "caller_a.py")
    doc2 = await _create_doc_with_title(meta_store, "caller_b.py")

    shared_module = "project.api.api_service.services"

    data1 = GraphData(
        entities=[
            Entity(name="caller_a.py", entity_type="module"),
            Entity(name=shared_module, entity_type="module"),
        ],
        relations=[
            Relation(source="caller_a.py", target=shared_module, relation_type="imports"),
        ],
    )
    await graph_store.save_graph_data(doc1, data1)

    data2 = GraphData(
        entities=[
            Entity(name="caller_b.py", entity_type="module"),
            Entity(name=shared_module, entity_type="module"),
        ],
        relations=[
            Relation(source="caller_b.py", target=shared_module, relation_type="imports"),
        ],
    )
    await graph_store.save_graph_data(doc2, data2)

    # doc2 그래프 탭에서 공유 모듈 노드도 함께 반환되어야 한다
    nodes_doc2 = await meta_store.get_graph_nodes_by_document(doc2)
    names_doc2 = {n["entity_name"] for n in nodes_doc2}
    assert "caller_b.py" in names_doc2
    assert shared_module in names_doc2


@pytest.mark.asyncio
async def test_cross_doc_entity_merge_preserves_document_ids(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """병합된 노드는 양쪽 문서의 document_ids를 가진다."""
    doc1 = await _create_doc_with_title(meta_store, "Doc A")
    doc2 = await _create_doc_with_title(meta_store, "Doc B")

    for doc_id in [doc1, doc2]:
        data = GraphData(
            entities=[Entity(name="Kubernetes", entity_type="technology")],
            relations=[],
        )
        await graph_store.save_graph_data(doc_id, data)

    # NetworkX에서 document_ids 확인
    k8s_nodes = [
        (n, d) for n, d in graph_store.graph.nodes(data=True)
        if d.get("entity_name") == "Kubernetes"
    ]
    assert len(k8s_nodes) == 1  # 하나의 정규 노드
    _, node_data = k8s_nodes[0]
    assert node_data["document_ids"] == {doc1, doc2}


@pytest.mark.asyncio
async def test_cross_doc_traversal_via_merged_node(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """병합된 노드를 통해 크로스-문서 관계 탐색이 가능하다."""
    doc1 = await _create_doc_with_title(meta_store, "Doc A")
    doc2 = await _create_doc_with_title(meta_store, "Doc B")

    # 문서 A: 쿠버네티스 → 도커
    data1 = GraphData(
        entities=[
            Entity(name="쿠버네티스", entity_type="technology"),
            Entity(name="도커", entity_type="technology"),
        ],
        relations=[
            Relation(source="쿠버네티스", target="도커", relation_type="uses"),
        ],
    )
    await graph_store.save_graph_data(doc1, data1)

    # 문서 B: 쿠버네티스 → AWS
    data2 = GraphData(
        entities=[
            Entity(name="쿠버네티스", entity_type="technology"),
            Entity(name="AWS", entity_type="platform"),
        ],
        relations=[
            Relation(source="쿠버네티스", target="AWS", relation_type="runs_on"),
        ],
    )
    await graph_store.save_graph_data(doc2, data2)

    # 쿠버네티스(병합 노드)에서 depth=1로 탐색 → 도커(doc1) + AWS(doc2) 모두 도달
    neighbors = graph_store.get_neighbors("쿠버네티스", depth=1)
    names = {n["entity_name"] for n in neighbors}
    assert "쿠버네티스" in names
    assert "도커" in names  # doc1의 관계
    assert "AWS" in names  # doc2의 관계 → 크로스-문서 탐색 성공


@pytest.mark.asyncio
async def test_cross_doc_delete_partial_keeps_shared_node(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """공유 노드가 있는 문서를 삭제해도 다른 문서가 참조하면 노드가 유지된다."""
    doc1 = await _create_doc_with_title(meta_store, "Doc A")
    doc2 = await _create_doc_with_title(meta_store, "Doc B")

    # 양쪽 문서에 쿠버네티스 등장
    for doc_id in [doc1, doc2]:
        data = GraphData(
            entities=[Entity(name="쿠버네티스", entity_type="technology")],
            relations=[],
        )
        await graph_store.save_graph_data(doc_id, data)

    assert graph_store.stats()["nodes"] == 1  # 병합된 1개 노드

    # 문서 A 삭제
    await graph_store.delete_document_graph(doc1)

    # 쿠버네티스 노드는 문서 B가 참조하므로 살아있음
    assert graph_store.stats()["nodes"] == 1
    k8s = [
        d for _, d in graph_store.graph.nodes(data=True)
        if d.get("entity_name") == "쿠버네티스"
    ]
    assert len(k8s) == 1
    assert k8s[0]["document_ids"] == {doc2}


@pytest.mark.asyncio
async def test_cross_doc_delete_all_removes_node(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """모든 문서가 삭제되면 고아 노드도 정리된다."""
    doc1 = await _create_doc_with_title(meta_store, "Doc A")
    doc2 = await _create_doc_with_title(meta_store, "Doc B")

    for doc_id in [doc1, doc2]:
        data = GraphData(
            entities=[Entity(name="쿠버네티스", entity_type="technology")],
            relations=[],
        )
        await graph_store.save_graph_data(doc_id, data)

    await graph_store.delete_document_graph(doc1)
    await graph_store.delete_document_graph(doc2)

    assert graph_store.stats()["nodes"] == 0


@pytest.mark.asyncio
async def test_cross_doc_description_enrichment(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """기존 노드에 description이 없으면 새 문서에서 보강한다."""
    doc1 = await _create_doc_with_title(meta_store, "Doc A")
    doc2 = await _create_doc_with_title(meta_store, "Doc B")

    # 문서 A: description 없이 저장
    data1 = GraphData(
        entities=[Entity(name="K8s", entity_type="technology", description="")],
        relations=[],
    )
    await graph_store.save_graph_data(doc1, data1)

    # 문서 B: description 포함
    data2 = GraphData(
        entities=[
            Entity(name="K8s", entity_type="technology", description="컨테이너 오케스트레이션 플랫폼"),
        ],
        relations=[],
    )
    await graph_store.save_graph_data(doc2, data2)

    k8s = [
        d for _, d in graph_store.graph.nodes(data=True)
        if d.get("entity_name") == "K8s"
    ]
    assert len(k8s) == 1
    assert k8s[0]["properties"]["description"] == "컨테이너 오케스트레이션 플랫폼"


@pytest.mark.asyncio
async def test_cross_doc_case_insensitive_merge(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """엔티티 이름 대소문자가 달라도 병합된다."""
    doc1 = await _create_doc_with_title(meta_store, "Doc A")
    doc2 = await _create_doc_with_title(meta_store, "Doc B")

    data1 = GraphData(
        entities=[Entity(name="Kubernetes", entity_type="technology")],
        relations=[],
    )
    await graph_store.save_graph_data(doc1, data1)

    data2 = GraphData(
        entities=[Entity(name="kubernetes", entity_type="technology")],
        relations=[],
    )
    await graph_store.save_graph_data(doc2, data2)

    assert graph_store.stats()["nodes"] == 1  # 대소문자 무시 병합


@pytest.mark.asyncio
async def test_cross_doc_different_type_not_merged(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """같은 이름이지만 entity_type이 다르면 병합하지 않는다."""
    doc1 = await _create_doc_with_title(meta_store, "Doc A")
    doc2 = await _create_doc_with_title(meta_store, "Doc B")

    data1 = GraphData(
        entities=[Entity(name="Gateway", entity_type="component")],
        relations=[],
    )
    await graph_store.save_graph_data(doc1, data1)

    data2 = GraphData(
        entities=[Entity(name="Gateway", entity_type="service")],
        relations=[],
    )
    await graph_store.save_graph_data(doc2, data2)

    assert graph_store.stats()["nodes"] == 2  # 타입 다르면 별도 노드


@pytest.mark.asyncio
async def test_cross_doc_load_from_db_preserves_document_ids(
    meta_store: MetadataStore,
) -> None:
    """DB에서 로드 시 document_ids가 올바르게 복원된다."""
    store1 = GraphStore(meta_store)
    doc1 = await _create_doc_with_title(meta_store, "Doc A")
    doc2 = await _create_doc_with_title(meta_store, "Doc B")

    for doc_id in [doc1, doc2]:
        data = GraphData(
            entities=[Entity(name="SharedEntity", entity_type="system")],
            relations=[],
        )
        await store1.save_graph_data(doc_id, data)

    # 새 GraphStore로 DB에서 로드
    store2 = GraphStore(meta_store)
    await store2.load_from_db()

    assert store2.stats()["nodes"] == 1
    node_data = list(store2.graph.nodes(data=True))
    assert len(node_data) == 1
    _, data = node_data[0]
    assert data["document_ids"] == {doc1, doc2}


@pytest.mark.asyncio
async def test_cross_doc_embedding_no_duplicates(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """병합된 노드는 임베딩이 1개만 생성된다."""
    doc1 = await _create_doc_with_title(meta_store, "Doc A")
    doc2 = await _create_doc_with_title(meta_store, "Doc B")

    for doc_id in [doc1, doc2]:
        data = GraphData(
            entities=[Entity(name="Kubernetes", entity_type="technology")],
            relations=[],
        )
        await graph_store.save_graph_data(doc_id, data)

    mock_embed = AsyncMock()
    mock_embed.aembed_documents = AsyncMock(return_value=[[1.0, 0.0]])
    count = await graph_store.build_entity_embeddings(mock_embed)

    assert count == 1  # 1개 노드 → 1개 임베딩
    assert graph_store.entity_embedding_count == 1


@pytest.mark.asyncio
async def test_get_connected_component_returns_full_component(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """시드에서 양방향으로 연결된 모든 노드를 반환한다 (depth 무제한)."""
    doc_id = await _create_doc(meta_store)
    # A -> B -> C (체인). depth=1 이웃은 B 까지지만, 컴포넌트는 C 까지.
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[
            Entity(name="A", entity_type="component"),
            Entity(name="B", entity_type="component"),
            Entity(name="C", entity_type="component"),
        ],
        relations=[
            Relation(source="A", target="B", relation_type="calls"),
            Relation(source="B", target="C", relation_type="calls"),
        ],
    ))

    component = graph_store.get_connected_component("A")
    names = {n["entity_name"] for n in component}
    assert names == {"A", "B", "C"}
    # 시드 표시
    seeds = [n for n in component if n.get("is_seed")]
    assert len(seeds) == 1
    assert seeds[0]["entity_name"] == "A"


@pytest.mark.asyncio
async def test_get_connected_component_no_match(graph_store: GraphStore) -> None:
    """일치하는 시드가 없으면 빈 목록을 반환한다."""
    assert graph_store.get_connected_component("없는엔티티") == []


@pytest.mark.asyncio
async def test_get_connected_component_respects_max_nodes(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """max_nodes 상한을 넘지 않는다."""
    doc_id = await _create_doc(meta_store)
    rels = [
        Relation(source="hub", target=f"n{i}", relation_type="rel")
        for i in range(10)
    ]
    ents = [Entity(name="hub", entity_type="component")] + [
        Entity(name=f"n{i}", entity_type="component") for i in range(10)
    ]
    await graph_store.save_graph_data(doc_id, GraphData(entities=ents, relations=rels))

    component = graph_store.get_connected_component("hub", max_nodes=5)
    assert len(component) <= 5


@pytest.mark.asyncio
async def test_get_connected_component_depth_limit(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """depth 를 주면 그 hop 까지만 반환하고, None 이면 전체를 반환한다."""
    doc_id = await _create_doc(meta_store)
    # A -> B -> C -> D 체인
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[Entity(name=n, entity_type="component") for n in ["A", "B", "C", "D"]],
        relations=[
            Relation(source="A", target="B", relation_type="r"),
            Relation(source="B", target="C", relation_type="r"),
            Relation(source="C", target="D", relation_type="r"),
        ],
    ))

    # depth=1 → A, B 만
    d1 = graph_store.get_connected_component("A", depth=1)
    assert {n["entity_name"] for n in d1} == {"A", "B"}
    # depth=2 → A, B, C
    d2 = graph_store.get_connected_component("A", depth=2)
    assert {n["entity_name"] for n in d2} == {"A", "B", "C"}
    # depth=None → 전체
    full = graph_store.get_connected_component("A", depth=None)
    assert {n["entity_name"] for n in full} == {"A", "B", "C", "D"}


@pytest.mark.asyncio
async def test_get_connected_component_reports_hop(
    graph_store: GraphStore, meta_store: MetadataStore,
) -> None:
    """각 노드에 시드로부터의 hop 거리가 부여된다 (시드=0)."""
    doc_id = await _create_doc(meta_store)
    await graph_store.save_graph_data(doc_id, GraphData(
        entities=[Entity(name=n, entity_type="component") for n in ["A", "B", "C"]],
        relations=[
            Relation(source="A", target="B", relation_type="r"),
            Relation(source="B", target="C", relation_type="r"),
        ],
    ))
    comp = graph_store.get_connected_component("A")
    hop = {n["entity_name"]: n["hop"] for n in comp}
    assert hop["A"] == 0
    assert hop["B"] == 1
    assert hop["C"] == 2
