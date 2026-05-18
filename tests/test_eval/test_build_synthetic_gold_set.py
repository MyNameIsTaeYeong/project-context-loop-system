"""build_synthetic_gold_set 의 후보 로더 + 헬퍼 단위 테스트.

LLM 호출은 LLMClient stub 으로 대체하고, MetadataStore/GraphStore 는
in-memory 인스턴스(tmp_path 의 sqlite + NetworkX) 로 셋업한다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# 빌드 스크립트의 import 가 src/ 경로 추가에 의존하므로 sys.path 보정.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))
if str(_PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))

import build_synthetic_gold_set as builder  # type: ignore[import-not-found]  # noqa: E402

from context_loop.processor.graph_extractor import (  # noqa: E402
    Entity,
    GraphData,
    Relation,
)
from context_loop.storage.graph_store import GraphStore  # noqa: E402
from context_loop.storage.metadata_store import MetadataStore  # noqa: E402


@pytest.fixture
async def meta_store(tmp_path: Path) -> MetadataStore:
    s = MetadataStore(tmp_path / "test.db")
    await s.initialize()
    yield s  # type: ignore[misc]
    await s.close()


# ---------------------------------------------------------------------------
# load_candidate_chunks — 정렬 키 (D-7)
# ---------------------------------------------------------------------------


async def test_load_candidate_chunks_sorted_by_chunk_index(
    meta_store: MetadataStore,
) -> None:
    """후보 청크 정렬 키가 (document_id, chunk_index) 인지 확인."""
    doc_id = await meta_store.create_document(
        source_type="confluence",
        title="문서A",
        original_content="원본 본문",
        content_hash="h1",
    )
    # 청크를 역순으로 삽입 — 정렬이 동작하는지 검증
    await meta_store.create_chunk(
        chunk_id="uuid-2",
        document_id=doc_id,
        chunk_index=2,
        content="두 번째 청크 내용. " * 30,
        token_count=10,
        section_path="A/B",
    )
    await meta_store.create_chunk(
        chunk_id="uuid-1",
        document_id=doc_id,
        chunk_index=1,
        content="첫 번째 청크 내용. " * 30,
        token_count=10,
        section_path="A",
    )

    out = await builder.load_candidate_chunks(
        meta_store,
        source_types=["confluence"],
        min_chars=100,
        max_chars=10000,
    )
    assert len(out) == 2
    # chunk_index 가 더 작은 것이 먼저
    assert out[0]["chunk_index"] == 1
    assert out[1]["chunk_index"] == 2


async def test_load_candidate_chunks_filters_source_types(
    meta_store: MetadataStore,
) -> None:
    """source_type 화이트리스트로 필터링."""
    conf_doc = await meta_store.create_document(
        source_type="confluence",
        title="conf",
        original_content="x",
        content_hash="h1",
    )
    git_doc = await meta_store.create_document(
        source_type="git_code",
        title="git",
        original_content="x",
        content_hash="h2",
    )
    await meta_store.create_chunk(
        chunk_id="c1", document_id=conf_doc, chunk_index=0,
        content="conf 본문 " * 50, token_count=10,
    )
    await meta_store.create_chunk(
        chunk_id="g1", document_id=git_doc, chunk_index=0,
        content="git 본문 " * 50, token_count=10,
    )

    out = await builder.load_candidate_chunks(
        meta_store,
        source_types=["confluence"],
        min_chars=100,
        max_chars=10000,
    )
    assert len(out) == 1
    assert out[0]["source_type"] == "confluence"


# ---------------------------------------------------------------------------
# load_candidate_subgraphs (R1 — graph 후보 로더)
# ---------------------------------------------------------------------------


async def test_load_candidate_subgraphs_basic(meta_store: MetadataStore) -> None:
    """그래프 후보 dict 구조 검증."""
    doc_id = await meta_store.create_document(
        source_type="confluence",
        title="아키텍처 문서",
        original_content="",
        content_hash="h-arch",
    )
    graph_store = GraphStore(meta_store)
    await graph_store.load_from_db()
    await graph_store.save_graph_data(
        doc_id,
        GraphData(
            entities=[
                Entity(
                    name="인증 서비스",
                    entity_type="system",
                    description="사내 인증 게이트웨이",
                ),
                Entity(name="플랫폼 팀", entity_type="team"),
            ],
            relations=[
                Relation(
                    source="인증 서비스",
                    target="플랫폼 팀",
                    relation_type="owned_by",
                ),
            ],
        ),
    )

    out = await builder.load_candidate_subgraphs(
        meta_store, graph_store,
        source_types=["confluence"],
        min_neighbors=1,
    )
    # 인증 서비스만 1-hop outgoing 이웃을 가짐 (DiGraph). 플랫폼 팀은 incoming 만
    # 있어 outgoing depth=1 탐색에서 자기 자신만 보임 → min_neighbors 미만으로 제외.
    names = {sg["entity_name"] for sg in out}
    assert "인증 서비스" in names
    # 핵심 필드 검증
    auth = next(sg for sg in out if sg["entity_name"] == "인증 서비스")
    assert auth["entity_type"] == "system"
    assert auth["entity_description"] == "사내 인증 게이트웨이"
    assert auth["source_type"] == "confluence"
    assert auth["primary_document_id"] == doc_id
    assert doc_id in auth["document_ids"]
    # subgraph_snippet 에 관계 텍스트 포함
    assert "owned_by" in auth["subgraph_snippet"]
    # edges 리스트도 정상
    assert any(
        e["relation_type"] == "owned_by" for e in auth["edges"]
    )


async def test_load_candidate_subgraphs_skips_orphan_nodes(
    meta_store: MetadataStore,
) -> None:
    """1-hop 이웃 없는 노드는 min_neighbors 필터로 제외."""
    doc_id = await meta_store.create_document(
        source_type="confluence",
        title="lonely",
        original_content="",
        content_hash="h-lone",
    )
    graph_store = GraphStore(meta_store)
    await graph_store.load_from_db()
    await graph_store.save_graph_data(
        doc_id,
        GraphData(entities=[Entity(name="외톨이", entity_type="concept")], relations=[]),
    )

    out = await builder.load_candidate_subgraphs(
        meta_store, graph_store,
        source_types=None,
        min_neighbors=1,
    )
    assert out == []


async def test_load_candidate_subgraphs_source_type_filter(
    meta_store: MetadataStore,
) -> None:
    """source_type 화이트리스트 — 다른 type 의 노드는 제외."""
    conf_doc = await meta_store.create_document(
        source_type="confluence",
        title="conf",
        original_content="",
        content_hash="h-c",
    )
    git_doc = await meta_store.create_document(
        source_type="git_code",
        title="git",
        original_content="",
        content_hash="h-g",
    )
    graph_store = GraphStore(meta_store)
    await graph_store.load_from_db()
    await graph_store.save_graph_data(
        conf_doc,
        GraphData(
            entities=[
                Entity(name="A", entity_type="concept"),
                Entity(name="B", entity_type="concept"),
            ],
            relations=[Relation(source="A", target="B", relation_type="related")],
        ),
    )
    await graph_store.save_graph_data(
        git_doc,
        GraphData(
            entities=[
                Entity(name="func_x", entity_type="function"),
                Entity(name="func_y", entity_type="function"),
            ],
            relations=[
                Relation(source="func_x", target="func_y", relation_type="calls"),
            ],
        ),
    )

    out = await builder.load_candidate_subgraphs(
        meta_store, graph_store,
        source_types=["confluence"],
        min_neighbors=1,
    )
    names = {sg["entity_name"] for sg in out}
    # outgoing edge 를 가진 노드만 candidate — A 가 outgoing 보유 (related→B)
    assert "A" in names
    # git_code 의 func_x/func_y 는 source_type 화이트리스트로 제외
    assert "func_x" not in names
    assert "func_y" not in names
    for sg in out:
        assert sg["source_type"] == "confluence"


# ---------------------------------------------------------------------------
# 엔티티 description 파싱 — properties JSON
# ---------------------------------------------------------------------------


async def test_load_candidate_subgraphs_parses_properties_json(
    meta_store: MetadataStore,
) -> None:
    """그래프 노드 properties (JSON 문자열) 에서 description 추출."""
    doc_id = await meta_store.create_document(
        source_type="confluence",
        title="x",
        original_content="",
        content_hash="h",
    )
    graph_store = GraphStore(meta_store)
    await graph_store.load_from_db()
    await graph_store.save_graph_data(
        doc_id,
        GraphData(
            entities=[
                Entity(
                    name="결제",
                    entity_type="system",
                    description="결제 처리 모듈",
                ),
                Entity(name="결제 팀", entity_type="team"),
            ],
            relations=[
                Relation(source="결제", target="결제 팀", relation_type="owned_by"),
            ],
        ),
    )

    out = await builder.load_candidate_subgraphs(
        meta_store, graph_store,
        source_types=None,
        min_neighbors=1,
    )
    target = next(sg for sg in out if sg["entity_name"] == "결제")
    assert target["entity_description"] == "결제 처리 모듈"
    # 직렬화도 정상 (JSON 인 경우)
    assert json.loads(json.dumps(target["entity_description"])) == "결제 처리 모듈"


# ---------------------------------------------------------------------------
# _classify_mode (eval_search) — 부수 점검
# ---------------------------------------------------------------------------


def test_classify_mode_chunk_graph_hybrid() -> None:
    """eval_search 의 _classify_mode 가 chunk/graph/hybrid 를 정확히 분류."""
    # 스크립트도 sys.path 에 추가했으므로 import 가능
    import eval_search  # type: ignore[import-not-found]

    from context_loop.eval.gold_set import GoldItem, GraphEntityRef

    chunk_item = GoldItem(id="q1", query="?", relevant_doc_ids=[1])
    graph_item = GoldItem(
        id="q2", query="?", relevant_doc_ids=[],
        relevant_graph_entities=[GraphEntityRef(name="x", type="t")],
    )
    hybrid_item = GoldItem(
        id="q3", query="?", relevant_doc_ids=[1],
        relevant_graph_entities=[GraphEntityRef(name="x", type="t")],
    )

    assert eval_search._classify_mode(chunk_item) == "chunk"
    assert eval_search._classify_mode(graph_item) == "graph"
    assert eval_search._classify_mode(hybrid_item) == "hybrid"


async def test_fetch_source_text_anchor_match(meta_store: MetadataStore) -> None:
    """_fetch_source_text 가 source_text_anchor prefix 매칭으로 청크를 찾는다."""
    import eval_search  # type: ignore[import-not-found]

    from context_loop.eval.gold_set import GoldItem

    doc_id = await meta_store.create_document(
        source_type="confluence",
        title="t",
        original_content="",
        content_hash="h",
    )
    # 두 청크 — anchor 와 prefix 일치하는 것 선택해야 함
    await meta_store.create_chunk(
        chunk_id="c1", document_id=doc_id, chunk_index=0,
        content="다른 본문", token_count=1,
    )
    await meta_store.create_chunk(
        chunk_id="c2", document_id=doc_id, chunk_index=1,
        content="정답 본문 입니다 — 인증 서비스에 대한 설명", token_count=1,
    )

    item = GoldItem(
        id="q1",
        query="?",
        relevant_doc_ids=[doc_id],
        source_document_id=doc_id,
        source_text_anchor="정답 본문 입니다",
    )
    text = await eval_search._fetch_source_text(item, meta_store)
    assert "정답 본문" in text


async def test_fetch_source_text_legacy_chunk_id_fallback(
    meta_store: MetadataStore,
) -> None:
    """anchor 없을 때 deprecated source_chunk_id 가 fallback 으로 동작."""
    import eval_search  # type: ignore[import-not-found]

    from context_loop.eval.gold_set import GoldItem

    doc_id = await meta_store.create_document(
        source_type="confluence",
        title="t",
        original_content="",
        content_hash="h",
    )
    await meta_store.create_chunk(
        chunk_id="legacy-uuid", document_id=doc_id, chunk_index=0,
        content="레거시 청크 본문", token_count=1,
    )
    await meta_store.create_chunk(
        chunk_id="other-uuid", document_id=doc_id, chunk_index=1,
        content="다른 청크", token_count=1,
    )

    item = GoldItem(
        id="q1",
        query="?",
        relevant_doc_ids=[doc_id],
        source_document_id=doc_id,
        source_chunk_id="legacy-uuid",
        source_text_anchor=None,
    )
    text = await eval_search._fetch_source_text(item, meta_store)
    assert text == "레거시 청크 본문"


# ---------------------------------------------------------------------------
# 2차 — _make_graph_gold_item 의 확장 필드 emit + 배치 임베딩 통합
# ---------------------------------------------------------------------------


def test_make_graph_gold_item_emits_aliases_and_description() -> None:
    """generator 가 채운 evidence/aliases 가 GraphEntityRef 에 그대로 들어간다."""
    from context_loop.eval.synth import (
        GeneratedGraphQuestion,
        GeneratedGraphRelation,
    )

    subgraph = {
        "entity_name": "결제 서비스",
        "entity_type": "system",
        "entity_description": "결제 처리",
        "document_ids": [42],
        "primary_document_id": 42,
        "source_type": "confluence",
    }
    gq = GeneratedGraphQuestion(
        query="누가 운영?",
        difficulty="easy",
        evidence_description="결제 서비스는 결제 팀이 운영하는 결제 처리 시스템",
        entity_aliases=["Payment Service"],
        relation=GeneratedGraphRelation(
            source_name="결제 서비스",
            target_name="결제 팀",
            relation_type="owned_by",
            description="결제는 결제 팀이 운영",
        ),
    )

    item = builder._make_graph_gold_item(
        subgraph, gq, existing_items=[], score_relations=True,
    )
    entity = item.relevant_graph_entities[0]
    assert entity.name == "결제 서비스"
    assert entity.aliases == ["Payment Service"]
    assert entity.description.startswith("결제 서비스")
    # description_embedding 은 빌더 측 배치 호출 전이라 None.
    assert entity.description_embedding is None

    # score_relations=True 이므로 relation 노출
    assert len(item.relevant_graph_relations) == 1
    rel = item.relevant_graph_relations[0]
    assert rel.relation_type == "owned_by"
    assert rel.description == "결제는 결제 팀이 운영"


def test_make_graph_gold_item_skips_relation_when_disabled() -> None:
    """score_relations=False 면 relation 이 채워져 있어도 emit 안 함."""
    from context_loop.eval.synth import (
        GeneratedGraphQuestion,
        GeneratedGraphRelation,
    )

    subgraph = {
        "entity_name": "X",
        "entity_type": "t",
        "entity_description": "",
        "document_ids": [1],
        "primary_document_id": 1,
        "source_type": "confluence",
    }
    gq = GeneratedGraphQuestion(
        query="?",
        relation=GeneratedGraphRelation(
            source_name="A", target_name="B", relation_type="x",
        ),
    )
    item = builder._make_graph_gold_item(
        subgraph, gq, existing_items=[], score_relations=False,
    )
    assert item.relevant_graph_relations == []


def test_make_graph_gold_item_falls_back_to_node_description() -> None:
    """generator 의 evidence_description 이 빈 경우 subgraph 의 entity_description fallback."""
    from context_loop.eval.synth import GeneratedGraphQuestion

    subgraph = {
        "entity_name": "X",
        "entity_type": "t",
        "entity_description": "노드 자체 설명",
        "document_ids": [1],
        "primary_document_id": 1,
        "source_type": "confluence",
    }
    gq = GeneratedGraphQuestion(query="?", evidence_description="")
    item = builder._make_graph_gold_item(
        subgraph, gq, existing_items=[], score_relations=False,
    )
    assert item.relevant_graph_entities[0].description == "노드 자체 설명"


import pytest as _pytest  # noqa: E402


@_pytest.mark.asyncio
async def test_embed_graph_item_descriptions_fills_embeddings() -> None:
    """_embed_graph_item_descriptions 가 entity/relation description 모두 채운다."""
    from context_loop.eval.gold_set import (
        GoldItem,
        GraphEntityRef,
        GraphRelationRef,
    )

    items = [
        GoldItem(
            id="q1", query="?",
            relevant_graph_entities=[
                GraphEntityRef(
                    name="A", type="t", description="설명 A",
                ),
                # 빈 description 은 임베딩 안 함
                GraphEntityRef(name="B", type="t", description=""),
                # 이미 embedding 있는 항목 — 재계산 안 함
                GraphEntityRef(
                    name="C", type="t",
                    description="설명 C",
                    description_embedding=[9.9, 9.9],
                ),
            ],
            relevant_graph_relations=[
                GraphRelationRef(
                    source_name="A", target_name="B",
                    relation_type="x",
                    description="설명 관계",
                ),
            ],
        ),
    ]

    class FakeEmbedder:
        def __init__(self) -> None:
            self.calls: list[list[str]] = []

        async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(texts)
            # 입력 텍스트의 길이를 첫 차원에 넣어 결정론적으로 다른 벡터 생성
            return [[float(len(t)), 0.0] for t in texts]

    embedder = FakeEmbedder()
    await builder._embed_graph_item_descriptions(items, embedder)

    # A 와 관계 description 만 임베딩 호출 (B 빈, C 이미 있음)
    assert items[0].relevant_graph_entities[0].description_embedding is not None
    assert items[0].relevant_graph_entities[1].description_embedding is None
    assert items[0].relevant_graph_entities[2].description_embedding == [9.9, 9.9]
    assert items[0].relevant_graph_relations[0].description_embedding is not None
    # 한 번에 호출 (배치)
    assert len(embedder.calls) == 1
    assert "설명 A" in embedder.calls[0]
    assert "설명 관계" in embedder.calls[0]


@_pytest.mark.asyncio
async def test_embed_graph_item_descriptions_no_client_silent() -> None:
    """embedding_client=None 이면 변경 없이 통과."""
    from context_loop.eval.gold_set import GoldItem, GraphEntityRef

    items = [GoldItem(
        id="q1", query="?",
        relevant_graph_entities=[GraphEntityRef(
            name="A", type="t", description="설명",
        )],
    )]
    await builder._embed_graph_item_descriptions(items, None)
    assert items[0].relevant_graph_entities[0].description_embedding is None


# ---------------------------------------------------------------------------
# CLI 옵션 노출 — argparse parser 의 옵션 존재 확인
# ---------------------------------------------------------------------------


def test_cli_exposes_new_options() -> None:
    """build_synthetic_gold_set.py 가 새 옵션을 --help 에 노출.

    main() 의 argparse 호출 시 ``--help`` 가 SystemExit 를 일으키므로
    그것으로 CLI 가 완결됐는지 확인하고, 정적으로 옵션 이름을 한 번 더
    검증한다.
    """
    import inspect
    import sys as _sys

    saved = _sys.argv
    _sys.argv = ["build_synthetic_gold_set.py", "--help"]
    try:
        with _pytest.raises(SystemExit):
            builder.main()
    finally:
        _sys.argv = saved

    source = inspect.getsource(builder.main)
    assert "--embed-graph-evidence" in source
    assert "--score-relations" in source
    assert "--graph-match-threshold" in source


def test_eval_search_cli_exposes_new_options() -> None:
    """eval_search.py 가 새 옵션을 --help 에 노출."""
    import inspect

    import eval_search  # type: ignore[import-not-found]

    source = inspect.getsource(eval_search.main)
    assert "--graph-match-threshold" in source
    assert "--graph-match-strict" in source
    assert "--score-relations" in source
