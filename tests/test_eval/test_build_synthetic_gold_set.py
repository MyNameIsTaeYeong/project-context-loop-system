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
# load_candidate_documents (R1 — chunks 테이블 비의존 문서 로더)
# ---------------------------------------------------------------------------


async def test_load_candidate_documents_uses_original_content(
    meta_store: MetadataStore,
) -> None:
    """original_content 기반 후보 로드 — chunks 테이블 미조회로도 동작."""
    doc_id = await meta_store.create_document(
        source_type="confluence",
        title="문서A",
        original_content="문서 원문 본문. " * 30,
        content_hash="h1",
        url="https://wiki/A",
    )
    # 청크를 만들지 않아도 문서 후보가 로드되어야 한다 (chunks 비의존).
    out = await builder.load_candidate_documents(
        meta_store,
        source_types=["confluence"],
        min_chars=100,
        max_chars=10000,
    )
    assert len(out) == 1
    item = out[0]
    assert item["document_id"] == doc_id
    assert item["source_type"] == "confluence"
    assert item["content"].startswith("문서 원문 본문.")
    assert item["title"] == "문서A"
    assert item["url"] == "https://wiki/A"
    # chunk 전용 키는 더 이상 존재하지 않는다.
    assert "chunk_id" not in item
    assert "chunk_index" not in item
    assert "section_path" not in item


async def test_load_candidate_documents_char_filter(
    meta_store: MetadataStore,
) -> None:
    """min/max_chars 가 문서 original_content 길이 기준으로 동작."""
    short_doc = await meta_store.create_document(
        source_type="confluence", title="짧음",
        original_content="짧다", content_hash="hs",
    )
    ok_doc = await meta_store.create_document(
        source_type="confluence", title="적당",
        original_content="가" * 500, content_hash="hk",
    )
    big_doc = await meta_store.create_document(
        source_type="confluence", title="큼",
        original_content="나" * 5000, content_hash="hb",
    )
    out = await builder.load_candidate_documents(
        meta_store, source_types=["confluence"],
        min_chars=100, max_chars=2000,
    )
    ids = {o["document_id"] for o in out}
    assert ok_doc in ids
    assert short_doc not in ids  # min_chars 미만
    assert big_doc not in ids    # max_chars 초과


async def test_load_candidate_documents_skips_empty_content(
    meta_store: MetadataStore,
) -> None:
    """original_content 가 NULL/빈/공백뿐인 문서는 제외."""
    empty_doc = await meta_store.create_document(
        source_type="confluence", title="빈본문",
        original_content="   \n  ", content_hash="he",
    )
    ok_doc = await meta_store.create_document(
        source_type="confluence", title="정상",
        original_content="정상 본문. " * 30, content_hash="ho",
    )
    out = await builder.load_candidate_documents(
        meta_store, source_types=None, min_chars=10, max_chars=10000,
    )
    ids = {o["document_id"] for o in out}
    assert ok_doc in ids
    assert empty_doc not in ids


async def test_load_candidate_documents_filters_source_types(
    meta_store: MetadataStore,
) -> None:
    """source_type 화이트리스트로 필터링."""
    conf_doc = await meta_store.create_document(
        source_type="confluence", title="conf",
        original_content="conf 본문 " * 50, content_hash="h1",
    )
    await meta_store.create_document(
        source_type="git_code", title="git",
        original_content="git 본문 " * 50, content_hash="h2",
    )
    out = await builder.load_candidate_documents(
        meta_store, source_types=["confluence"],
        min_chars=100, max_chars=10000,
    )
    assert len(out) == 1
    assert out[0]["document_id"] == conf_doc
    assert out[0]["source_type"] == "confluence"


async def test_load_candidate_documents_sorted_by_document_id(
    meta_store: MetadataStore,
) -> None:
    """후보가 document_id 오름차순으로 정렬됨 (결정론)."""
    d1 = await meta_store.create_document(
        source_type="confluence", title="1",
        original_content="첫 문서 " * 40, content_hash="h1",
    )
    d2 = await meta_store.create_document(
        source_type="confluence", title="2",
        original_content="둘째 문서 " * 40, content_hash="h2",
    )
    out = await builder.load_candidate_documents(
        meta_store, source_types=["confluence"],
        min_chars=10, max_chars=10000,
    )
    assert [o["document_id"] for o in out] == sorted([d1, d2])


# ---------------------------------------------------------------------------
# _distractor_excerpt (R1/R2 — distractor 본문 prefix 절단)
# ---------------------------------------------------------------------------


def test_distractor_excerpt_truncates_to_constant() -> None:
    """긴 distractor 본문은 DISTRACTOR_EXCERPT_CHARS 로 잘린다."""
    long_text = "가" * 5000
    out = builder._distractor_excerpt(long_text)
    assert len(out) == builder.DISTRACTOR_EXCERPT_CHARS
    # 짧은 본문은 그대로
    short = "짧은 본문"
    assert builder._distractor_excerpt(short) == short


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
    # R3 — sg dict 에 소유 문서 원문 키가 있다 (추가 DB 호출 없이 doc_by_id 재활용).
    assert "primary_document_content" in auth


async def test_load_candidate_subgraphs_includes_primary_document_content(
    meta_store: MetadataStore,
) -> None:
    """R3 — primary 소유 문서의 original_content 가 sg dict 에 적재된다."""
    doc_id = await meta_store.create_document(
        source_type="confluence",
        title="아키텍처 문서",
        original_content="인증 서비스는 사내 인증 게이트웨이 원문 전체 본문.",
        content_hash="h-arch-doc",
    )
    graph_store = GraphStore(meta_store)
    await graph_store.load_from_db()
    await graph_store.save_graph_data(
        doc_id,
        GraphData(
            entities=[
                Entity(name="인증 서비스", entity_type="system"),
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
        meta_store, graph_store, source_types=["confluence"], min_neighbors=1,
    )
    auth = next(sg for sg in out if sg["entity_name"] == "인증 서비스")
    assert auth["primary_document_content"] == (
        "인증 서비스는 사내 인증 게이트웨이 원문 전체 본문."
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
        subgraph, gq, score_relations=True,
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


def test_make_graph_gold_item_filters_alias_and_evidence_leakage() -> None:
    """S1-2 — source_text 가 주어지면 누설 alias 드롭·누설 evidence 비움."""
    from context_loop.eval.synth import GeneratedGraphQuestion

    subgraph = {
        "entity_name": "AuthService",
        "entity_type": "system",
        "entity_description": "",
        "document_ids": [1],
        "primary_document_id": 1,
        "source_type": "git_code",
        "subgraph_snippet": "class AuthService implements TokenValidator { ... }",
    }
    gq = GeneratedGraphQuestion(
        query="?",
        # 'TokenValidator' 는 청크의 다른 식별자 복붙 → 드롭.
        # 'auth-service' 는 엔티티 이름 표기 변형 → 유지.
        entity_aliases=["auth-service", "TokenValidator"],
        # evidence 가 청크 식별자를 복붙 → T4 skip 위해 비워짐.
        evidence_description="이 노드는 TokenValidator 를 직접 호출한다",
    )
    leak_stats: dict[str, int] = {}
    item = builder._make_graph_gold_item(
        subgraph, gq,
        source_text=subgraph["subgraph_snippet"],
        leak_stats=leak_stats,
    )
    entity = item.relevant_graph_entities[0]
    assert "auth-service" in entity.aliases
    assert "TokenValidator" not in entity.aliases
    assert entity.description == ""  # 누설 evidence → 비움 → T4 skip
    assert leak_stats["alias_leakage_filtered"] == 1
    assert leak_stats["evidence_leakage_filtered"] == 1


def test_make_graph_gold_item_no_source_text_skips_sanitation() -> None:
    """S1-2 — source_text 미지정 시 기존 동작 보존(누설 검사 없음)."""
    from context_loop.eval.synth import GeneratedGraphQuestion

    subgraph = {
        "entity_name": "AuthService",
        "entity_type": "system",
        "entity_description": "",
        "document_ids": [1],
        "primary_document_id": 1,
        "source_type": "git_code",
        "subgraph_snippet": "class AuthService implements TokenValidator { ... }",
    }
    gq = GeneratedGraphQuestion(
        query="?",
        entity_aliases=["TokenValidator"],
        evidence_description="TokenValidator 호출",
    )
    item = builder._make_graph_gold_item(subgraph, gq)
    entity = item.relevant_graph_entities[0]
    # 후방 호환: source_text 없으면 generator 출력 그대로.
    assert entity.aliases == ["TokenValidator"]
    assert entity.description == "TokenValidator 호출"


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
        subgraph, gq, score_relations=False,
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
        subgraph, gq, score_relations=False,
    )
    assert item.relevant_graph_entities[0].description == "노드 자체 설명"


# ---------------------------------------------------------------------------
# S1-3 (R6) — 그래프 소표본 경고 임계 + 카운트 메타
# ---------------------------------------------------------------------------


def test_graph_low_sample_threshold_constant() -> None:
    """S1-3 — 권고 임계 상수가 존재하고 N≥150 을 반영한다."""
    assert builder.GRAPH_LOW_SAMPLE_THRESHOLD == 150


def test_build_graph_metadata_low_sample_and_provenance() -> None:
    """S1-2/S1-3/S1-4 — graph metadata 헬퍼가 필수 키를 모두 채운다."""
    from context_loop.eval.gold_set import GoldItem, GraphEntityRef

    items = [
        GoldItem(
            id="q1", query="?", relevant_doc_ids=[1],
            relevant_graph_entities=[GraphEntityRef(name="x", type="t")],
        ),
        GoldItem(
            id="q2", query="?", relevant_doc_ids=[1, 2],
            relevant_graph_entities=[GraphEntityRef(name="y", type="t")],
            cross_document=True,  # 카운트 제외
        ),
    ]
    stats = {"alias_leakage_filtered": 3, "evidence_leakage_filtered": 1}
    meta = builder._build_graph_metadata(
        items=items,
        stats=stats,
        embedding_model_id="bge-m3",
        graph_match_threshold=0.65,
        score_relations=False,
        embed_graph_evidence=True,
    )
    # S1-2 — 누설 필터 카운트 노출
    assert meta["alias_leakage_filtered"] == 3
    assert meta["evidence_leakage_filtered"] == 1
    # S1-3 — 소표본 경고 (1 < 150)
    assert meta["graph_question_count"] == 1
    assert meta["graph_low_sample_warning"] is True
    assert meta["graph_low_sample_threshold"] == 150
    # S1-4 — 임베딩 출처 기록 + 추출 LLM 미기록 표식
    assert meta["graph_evidence_embedding_model"] == "bge-m3"
    assert meta["embedding_model"] == "bge-m3"
    assert meta["extraction_llm_provenance"] == "unrecorded"


def test_build_graph_metadata_no_low_sample_when_enough() -> None:
    """S1-3 — 항목 수가 임계 이상이면 경고 플래그가 False."""
    from context_loop.eval.gold_set import GoldItem, GraphEntityRef

    items = [
        GoldItem(
            id=f"q{i}", query="?", relevant_doc_ids=[1],
            relevant_graph_entities=[GraphEntityRef(name=f"e{i}", type="t")],
        )
        for i in range(builder.GRAPH_LOW_SAMPLE_THRESHOLD)
    ]
    meta = builder._build_graph_metadata(
        items=items,
        stats={},
        embedding_model_id="",
        graph_match_threshold=0.65,
        score_relations=False,
        embed_graph_evidence=True,
    )
    assert meta["graph_question_count"] == builder.GRAPH_LOW_SAMPLE_THRESHOLD
    assert meta["graph_low_sample_warning"] is False
    # 누설 카운트 누락 시 0 기본값
    assert meta["alias_leakage_filtered"] == 0
    assert meta["evidence_leakage_filtered"] == 0


def test_graph_question_count_predicate_excludes_cross_doc() -> None:
    """S1-3 — build() 가 쓰는 그래프 항목 카운트 술어 검증.

    순수 그래프(단일 엔티티) 항목만 세고, chunk-only / cross-doc 은 제외한다.
    """
    from context_loop.eval.gold_set import GoldItem, GraphEntityRef

    chunk_only = GoldItem(id="q1", query="?", relevant_doc_ids=[1])
    graph_a = GoldItem(
        id="q2", query="?", relevant_doc_ids=[1],
        relevant_graph_entities=[GraphEntityRef(name="x", type="t")],
    )
    graph_b = GoldItem(
        id="q3", query="?", relevant_doc_ids=[2],
        relevant_graph_entities=[GraphEntityRef(name="y", type="t")],
    )
    cross_doc = GoldItem(
        id="q4", query="?", relevant_doc_ids=[1, 2],
        relevant_graph_entities=[GraphEntityRef(name="z", type="t")],
        cross_document=True,
    )
    items = [chunk_only, graph_a, graph_b, cross_doc]
    # build() 내부 술어와 동일.
    count = sum(
        1 for it in items
        if it.relevant_graph_entities and not it.cross_document
    )
    assert count == 2
    assert (count < builder.GRAPH_LOW_SAMPLE_THRESHOLD) is True


# ---------------------------------------------------------------------------
# R3 — graph 다중-doc → OR 그룹 자동 변환
# ---------------------------------------------------------------------------


def test_graph_multi_doc_becomes_or_group() -> None:
    """sg.document_ids=[A,B,C] → relevant_doc_groups=[[A,B,C]] (단일 OR 그룹)."""
    from context_loop.eval.synth import GeneratedGraphQuestion

    subgraph = {
        "entity_name": "공유 엔티티",
        "entity_type": "system",
        "entity_description": "여러 문서에 등장",
        "document_ids": [7, 3, 5],
        "primary_document_id": 7,
        "source_type": "confluence",
    }
    gq = GeneratedGraphQuestion(query="?", evidence_description="설명")
    item = builder._make_graph_gold_item(subgraph, gq)
    assert item.relevant_doc_ids == [3, 5, 7]
    assert item.relevant_doc_groups == [[3, 5, 7]]
    assert item.cross_document is False


def test_graph_single_doc_no_group() -> None:
    """sg.document_ids=[A] → relevant_doc_groups=[] (그룹 불필요)."""
    from context_loop.eval.synth import GeneratedGraphQuestion

    subgraph = {
        "entity_name": "단일 엔티티",
        "entity_type": "system",
        "entity_description": "",
        "document_ids": [42],
        "primary_document_id": 42,
        "source_type": "confluence",
    }
    gq = GeneratedGraphQuestion(query="?", evidence_description="설명")
    item = builder._make_graph_gold_item(subgraph, gq)
    assert item.relevant_doc_ids == [42]
    assert item.relevant_doc_groups == []


# ---------------------------------------------------------------------------
# R2 — cross-document 씨앗 추출 + emit
# ---------------------------------------------------------------------------


async def test_load_cross_doc_seeds_disjoint(meta_store: MetadataStore) -> None:
    """노드 소유 문서가 서로소인 엣지만 cross-doc 씨앗으로 추출.

    같은 엔티티가 두 문서에 모두 등장하면 노드가 병합되어 소유 문서가
    겹치므로 cross-doc 아님. 서로 다른 엔티티가 다른 문서를 잇는 엣지만 통과.
    """
    doc_a = await meta_store.create_document(
        source_type="confluence", title="A",
        original_content="", content_hash="ha",
    )
    doc_b = await meta_store.create_document(
        source_type="confluence", title="B",
        original_content="", content_hash="hb",
    )
    graph_store = GraphStore(meta_store)
    await graph_store.load_from_db()
    # 문서 A: 결제 서비스 -> 인증 서비스 (둘 다 A 소유)
    await graph_store.save_graph_data(
        doc_a,
        GraphData(
            entities=[
                Entity(name="결제 서비스", entity_type="system"),
                Entity(name="인증 서비스", entity_type="system"),
            ],
            relations=[Relation(
                source="결제 서비스", target="인증 서비스",
                relation_type="depends_on",
            )],
        ),
    )
    # 문서 B: 인증 서비스(A 와 병합) -> 보안 팀 (보안 팀은 B 만 소유)
    await graph_store.save_graph_data(
        doc_b,
        GraphData(
            entities=[
                Entity(name="인증 서비스", entity_type="system"),
                Entity(name="보안 팀", entity_type="team"),
            ],
            relations=[Relation(
                source="인증 서비스", target="보안 팀",
                relation_type="owned_by",
            )],
        ),
    )

    seeds = await builder.load_cross_doc_seeds(
        meta_store, graph_store,
        source_types=["confluence"],
        max_seeds=None,
    )
    # 결제 서비스(A) -> 인증 서비스(A,B): 소유 문서 {A} vs {A,B} → 겹침 → 제외
    # 인증 서비스(A,B) -> 보안 팀(B): {A,B} vs {B} → 겹침 → 제외
    # 즉 서로소 엣지가 없으므로 씨앗 0개여야 한다.
    assert seeds == []


async def test_load_cross_doc_seeds_true_disjoint(
    meta_store: MetadataStore,
) -> None:
    """완전히 서로 다른 엔티티가 두 문서를 잇는 엣지는 cross-doc 씨앗."""
    doc_a = await meta_store.create_document(
        source_type="confluence", title="A",
        original_content="", content_hash="ha",
    )
    doc_b = await meta_store.create_document(
        source_type="confluence", title="B",
        original_content="", content_hash="hb",
    )
    graph_store = GraphStore(meta_store)
    await graph_store.load_from_db()
    # A 만 소유하는 엔티티 X, B 만 소유하는 엔티티 Y. 엣지는 한 문서가 추가.
    await graph_store.save_graph_data(
        doc_a,
        GraphData(
            entities=[Entity(name="엔티티X", entity_type="system")],
            relations=[],
        ),
    )
    await graph_store.save_graph_data(
        doc_b,
        GraphData(
            entities=[Entity(name="엔티티Y", entity_type="team")],
            relations=[],
        ),
    )
    # X@A -> Y@B 엣지를 직접 추가 (소유 문서 서로소: {A} vs {B}).
    x_id = next(
        n["id"] for n in await meta_store.get_all_graph_nodes()
        if n["entity_name"] == "엔티티X"
    )
    y_id = next(
        n["id"] for n in await meta_store.get_all_graph_nodes()
        if n["entity_name"] == "엔티티Y"
    )
    graph_store.graph.add_edge(
        x_id, y_id, id=999, relation_type="references", document_id=doc_a,
        properties={},
    )

    seeds = await builder.load_cross_doc_seeds(
        meta_store, graph_store,
        source_types=["confluence"],
        max_seeds=None,
    )
    assert len(seeds) == 1
    seed = seeds[0]
    assert seed["source_entity"]["name"] == "엔티티X"
    assert seed["target_entity"]["name"] == "엔티티Y"
    assert seed["source_entity"]["doc_id"] == doc_a
    assert seed["target_entity"]["doc_id"] == doc_b
    assert sorted(seed["document_ids"]) == sorted([doc_a, doc_b])
    assert seed["relation_type"] == "references"


async def test_cross_doc_seed_deterministic(meta_store: MetadataStore) -> None:
    """같은 입력 → 같은 정렬·동일 씨앗 리스트 (결정론)."""
    doc_a = await meta_store.create_document(
        source_type="confluence", title="A",
        original_content="", content_hash="ha",
    )
    doc_b = await meta_store.create_document(
        source_type="confluence", title="B",
        original_content="", content_hash="hb",
    )
    graph_store = GraphStore(meta_store)
    await graph_store.load_from_db()
    await graph_store.save_graph_data(
        doc_a, GraphData(entities=[Entity(name="X", entity_type="s")], relations=[]),
    )
    await graph_store.save_graph_data(
        doc_b, GraphData(entities=[Entity(name="Y", entity_type="t")], relations=[]),
    )
    nodes = await meta_store.get_all_graph_nodes()
    x_id = next(n["id"] for n in nodes if n["entity_name"] == "X")
    y_id = next(n["id"] for n in nodes if n["entity_name"] == "Y")
    graph_store.graph.add_edge(
        x_id, y_id, id=1, relation_type="r", document_id=doc_a, properties={},
    )

    s1 = await builder.load_cross_doc_seeds(
        meta_store, graph_store, source_types=None, max_seeds=None,
    )
    s2 = await builder.load_cross_doc_seeds(
        meta_store, graph_store, source_types=None, max_seeds=None,
    )
    assert s1 == s2


def test_make_cross_doc_item_and_groups() -> None:
    """씨앗 → relevant_doc_groups=[[A],[B]], cross_document=True."""
    from context_loop.eval.synth import GeneratedGraphQuestion

    seed = {
        "source_entity": {"name": "A", "type": "system", "doc_id": 3},
        "target_entity": {"name": "B", "type": "team", "doc_id": 7},
        "relation_type": "owned_by",
        "document_ids": [3, 7],
        "source_type": "confluence",
    }
    gq = GeneratedGraphQuestion(
        query="A 를 운영하는 팀은?", difficulty="medium",
        entity_aliases=["Alias A"],
    )
    item = builder._make_cross_doc_gold_item(seed, gq)
    assert item.cross_document is True
    assert item.relevant_doc_groups == [[3], [7]]
    assert item.relevant_doc_ids == [3, 7]
    assert {e.name for e in item.relevant_graph_entities} == {"A", "B"}
    assert item.notes == "cross_document"


def test_classify_mode_cross_doc_priority() -> None:
    """cross_document=True 면 mode 가 'cross_doc' 우선."""
    import eval_search  # type: ignore[import-not-found]

    from context_loop.eval.gold_set import GoldItem, GraphEntityRef

    item = GoldItem(
        id="q1", query="?",
        relevant_doc_ids=[3, 7],
        relevant_doc_groups=[[3], [7]],
        cross_document=True,
        relevant_graph_entities=[GraphEntityRef(name="A", type="t")],
    )
    assert eval_search._classify_mode(item) == "cross_doc"


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
    # 3차 — 항목 단위 병렬 처리 CLI.
    assert "--concurrency" in source
    # R2 — cross-document 생성 CLI.
    assert "--enable-cross-doc" in source
    assert "--cross-doc-max-seeds" in source


def test_eval_search_cli_exposes_new_options() -> None:
    """eval_search.py 가 새 옵션을 --help 에 노출."""
    import inspect

    import eval_search  # type: ignore[import-not-found]

    source = inspect.getsource(eval_search.main)
    assert "--graph-match-threshold" in source
    assert "--graph-match-strict" in source
    assert "--score-relations" in source
    # 3차 — 항목 단위 병렬 처리 CLI.
    assert "--concurrency" in source


# ---------------------------------------------------------------------------
# 3차 — _merge_stats 단위 테스트
# ---------------------------------------------------------------------------


def test_merge_stats_adds_known_and_dynamic_keys() -> None:
    """_merge_stats 가 알려진 키 + 동적 키 모두를 합산한다."""
    target: dict[str, int] = {"generated": 1, "passed": 2}
    local: dict[str, int] = {
        "generated": 3, "passed": 1, "fail_demonstrative": 1, "fail_runtime": 0,
    }
    builder._merge_stats(target, local)
    assert target["generated"] == 4
    assert target["passed"] == 3
    # 동적 키도 신규 생성.
    assert target["fail_demonstrative"] == 1
    assert target["fail_runtime"] == 0


def test_merge_stats_empty_local_noop() -> None:
    """빈 local 은 target 을 변경하지 않는다."""
    target: dict[str, int] = {"generated": 5}
    builder._merge_stats(target, {})
    assert target == {"generated": 5}
