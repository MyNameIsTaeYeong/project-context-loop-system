"""골드셋 직렬화/역직렬화 테스트."""

from __future__ import annotations

from pathlib import Path

from context_loop.eval.gold_set import (
    MEASUREMENT_UNITS,
    GoldItem,
    GoldSet,
    GraphEntityRef,
    GraphRelationRef,
    SupportingFact,
    load_gold_set,
    save_gold_set,
)


def test_gold_item_to_from_dict_roundtrip() -> None:
    item = GoldItem(
        id="q001",
        query="VPC quota 검증 로직?",
        relevant_doc_ids=[42, 89],
        source_chunk_id="abc-123",
        source_document_id=42,
        source_section_path="limits.py > QuotaChecker",
        difficulty="medium",
        synthesized=True,
        notes="합성",
    )
    d = item.to_dict()
    rehydrated = GoldItem.from_dict(d)
    assert rehydrated == item


def test_gold_item_to_dict_omits_empty() -> None:
    """비어 있는 선택 필드는 출력 dict 에서 빠진다 (YAML 가독성)."""
    item = GoldItem(id="q1", query="?", relevant_doc_ids=[1])
    d = item.to_dict()
    assert "source_chunk_id" not in d
    assert "source_type" not in d
    assert "source_text_anchor" not in d
    assert "relevant_graph_entities" not in d
    assert "difficulty" not in d
    assert "synthesized" not in d
    assert "notes" not in d
    assert d == {"id": "q1", "query": "?", "relevant_doc_ids": [1]}


def test_gold_item_from_dict_handles_missing() -> None:
    """필수 키만 있어도 파싱된다."""
    item = GoldItem.from_dict({"id": "q1", "query": "?", "relevant_doc_ids": [1]})
    assert item.id == "q1"
    assert item.relevant_doc_ids == [1]
    assert item.relevant_graph_entities == []
    assert item.source_type == ""
    assert item.source_text_anchor is None
    assert item.source_chunk_id is None
    assert item.synthesized is False


def test_gold_item_from_dict_coerces_int_ids() -> None:
    """relevant_doc_ids 가 문자열로 와도 int 로 변환된다."""
    item = GoldItem.from_dict({"id": "q1", "query": "?", "relevant_doc_ids": ["42", "89"]})
    assert item.relevant_doc_ids == [42, 89]


def test_save_load_roundtrip(tmp_path: Path) -> None:
    gold = GoldSet(
        version=1,
        items=[
            GoldItem(id="q1", query="첫 질의", relevant_doc_ids=[1, 2], synthesized=True),
            GoldItem(id="q2", query="두 번째", relevant_doc_ids=[3]),
        ],
        metadata={"seed": 42, "n_chunks_sampled": 30},
    )
    path = tmp_path / "gold.yaml"
    save_gold_set(gold, path)
    assert path.exists()

    loaded = load_gold_set(path)
    assert loaded.version == 1
    assert len(loaded.items) == 2
    assert loaded.items[0].query == "첫 질의"
    assert loaded.items[0].synthesized is True
    assert loaded.metadata["seed"] == 42


def test_save_creates_parent_dir(tmp_path: Path) -> None:
    """저장 경로의 상위 디렉토리가 없으면 생성된다."""
    path = tmp_path / "deep" / "nested" / "gold.yaml"
    save_gold_set(GoldSet(items=[GoldItem(id="q1", query="?", relevant_doc_ids=[1])]), path)
    assert path.exists()


def test_load_empty_yaml(tmp_path: Path) -> None:
    """빈 YAML 도 안전하게 로드된다 (빈 골드셋)."""
    path = tmp_path / "empty.yaml"
    path.write_text("", encoding="utf-8")
    loaded = load_gold_set(path)
    assert loaded.items == []
    assert loaded.version == 1


# ---------------------------------------------------------------------------
# 신규 필드 round-trip (R1, R2)
# ---------------------------------------------------------------------------


def test_graph_entity_ref_roundtrip() -> None:
    ref = GraphEntityRef(name="인증 서비스", type="system")
    d = ref.to_dict()
    assert d == {"name": "인증 서비스", "type": "system"}
    rehydrated = GraphEntityRef.from_dict(d)
    assert rehydrated == ref


def test_graph_entity_ref_from_dict_tolerates_missing_type() -> None:
    """type 누락 시 빈 문자열로 보정 (느슨한 입력 허용)."""
    ref = GraphEntityRef.from_dict({"name": "결제 팀"})
    assert ref.name == "결제 팀"
    assert ref.type == ""


def test_gold_item_with_graph_entities_roundtrip() -> None:
    """relevant_graph_entities + source_type + source_text_anchor round-trip."""
    item = GoldItem(
        id="q042",
        query="인증 서비스는 어느 팀이 운영?",
        relevant_doc_ids=[89],
        relevant_graph_entities=[
            GraphEntityRef(name="인증 서비스", type="system"),
            GraphEntityRef(name="플랫폼 팀", type="team"),
        ],
        source_type="confluence",
        source_document_id=89,
        source_text_anchor="인증 서비스는 플랫폼 팀이 운영하는...",
        difficulty="easy",
        synthesized=True,
    )
    d = item.to_dict()
    assert d["relevant_graph_entities"] == [
        {"name": "인증 서비스", "type": "system"},
        {"name": "플랫폼 팀", "type": "team"},
    ]
    assert d["source_type"] == "confluence"
    assert d["source_text_anchor"] == "인증 서비스는 플랫폼 팀이 운영하는..."
    rehydrated = GoldItem.from_dict(d)
    assert rehydrated == item


def test_gold_item_backward_compat_old_yaml() -> None:
    """graph 필드 없는 옛 YAML 로드 시 기본값 채워진다."""
    old_yaml = {
        "id": "q001",
        "query": "옛 골드셋 질의",
        "relevant_doc_ids": [10],
        "source_chunk_id": "uuid-1234",
        "source_document_id": 10,
        "source_section_path": "old/path",
        "difficulty": "easy",
        "synthesized": True,
    }
    item = GoldItem.from_dict(old_yaml)
    assert item.id == "q001"
    assert item.source_chunk_id == "uuid-1234"
    assert item.relevant_graph_entities == []
    assert item.source_type == ""
    assert item.source_text_anchor is None


def test_gold_item_source_chunk_id_preserved_on_emit() -> None:
    """기존 source_chunk_id 가 있는 GoldItem 은 to_dict 에서 그대로 emit 된다.

    (신규 생성 흐름에서는 set 하지 않지만, 옛 YAML 의 deserialize→serialize
    round-trip 시 보존되어야 한다.)
    """
    item = GoldItem(
        id="q1",
        query="?",
        relevant_doc_ids=[1],
        source_chunk_id="legacy-uuid",
    )
    d = item.to_dict()
    assert d["source_chunk_id"] == "legacy-uuid"


def test_gold_item_with_graph_only_no_doc() -> None:
    """graph-only 질문 — relevant_doc_ids 가 비어 있어도 round-trip OK."""
    item = GoldItem(
        id="qg1",
        query="결제 한도는 누가 소유?",
        relevant_doc_ids=[],
        relevant_graph_entities=[GraphEntityRef(name="결제 한도", type="concept")],
        source_type="confluence",
    )
    d = item.to_dict()
    assert d["relevant_doc_ids"] == []
    rehydrated = GoldItem.from_dict(d)
    assert rehydrated == item


def test_gold_item_from_dict_skips_invalid_entity_entries() -> None:
    """relevant_graph_entities 안에 dict 아닌 항목이 있으면 건너뛴다."""
    item = GoldItem.from_dict({
        "id": "q1",
        "query": "?",
        "relevant_doc_ids": [1],
        "relevant_graph_entities": [
            {"name": "정상", "type": "system"},
            "잘못된 문자열",
            None,
        ],
    })
    assert len(item.relevant_graph_entities) == 1
    assert item.relevant_graph_entities[0].name == "정상"


# ---------------------------------------------------------------------------
# 2차 — GraphEntityRef 확장 필드 (aliases / description / description_embedding)
# ---------------------------------------------------------------------------


def test_graph_entity_ref_roundtrip_with_aliases_and_description() -> None:
    """aliases / description / description_embedding 모두 round-trip."""
    ref = GraphEntityRef(
        name="결제 서비스",
        type="system",
        aliases=["Payment Service", "결제서비스"],
        description="결제 처리 시스템. 주문에 의존.",
        description_embedding=[0.1, -0.2, 0.3],
    )
    d = ref.to_dict()
    assert d["aliases"] == ["Payment Service", "결제서비스"]
    assert d["description"] == "결제 처리 시스템. 주문에 의존."
    assert d["description_embedding"] == [0.1, -0.2, 0.3]
    rehydrated = GraphEntityRef.from_dict(d)
    assert rehydrated == ref


def test_graph_entity_ref_to_dict_omits_empty_extension_fields() -> None:
    """확장 필드가 비어 있으면 dict 에 emit 안 됨 — 옛 YAML 과 동일."""
    ref = GraphEntityRef(name="X", type="t")
    d = ref.to_dict()
    assert "aliases" not in d
    assert "description" not in d
    assert "description_embedding" not in d
    assert d == {"name": "X", "type": "t"}


def test_graph_entity_ref_backward_compat_minimal() -> None:
    """1차 YAML 의 `{name, type}` 만 가진 dict 도 정상 로드."""
    ref = GraphEntityRef.from_dict({"name": "X", "type": "t"})
    assert ref.aliases == []
    assert ref.description == ""
    assert ref.description_embedding is None


def test_graph_entity_ref_from_dict_filters_non_string_aliases() -> None:
    ref = GraphEntityRef.from_dict({
        "name": "X", "type": "t",
        "aliases": ["valid", 123, None, "another"],
    })
    assert ref.aliases == ["valid", "another"]


def test_graph_entity_ref_empty_embedding_treated_as_none() -> None:
    """빈 리스트 description_embedding 은 None 으로."""
    ref = GraphEntityRef.from_dict({
        "name": "X", "type": "t",
        "description_embedding": [],
    })
    assert ref.description_embedding is None


# ---------------------------------------------------------------------------
# 2차 — GraphRelationRef
# ---------------------------------------------------------------------------


def test_graph_relation_ref_roundtrip() -> None:
    rel = GraphRelationRef(
        source_name="결제 서비스",
        target_name="주문 서비스",
        relation_type="depends_on",
        description="결제는 주문에 의존.",
        description_embedding=[0.5, 0.5],
    )
    d = rel.to_dict()
    assert d["source_name"] == "결제 서비스"
    assert d["description"] == "결제는 주문에 의존."
    assert d["description_embedding"] == [0.5, 0.5]
    rehydrated = GraphRelationRef.from_dict(d)
    assert rehydrated == rel


def test_graph_relation_ref_minimal_roundtrip() -> None:
    rel = GraphRelationRef(source_name="A", target_name="B", relation_type="x")
    d = rel.to_dict()
    assert "description" not in d
    assert "description_embedding" not in d
    rehydrated = GraphRelationRef.from_dict(d)
    assert rehydrated == rel


def test_graph_relation_ref_empty_embedding_treated_as_none() -> None:
    rel = GraphRelationRef.from_dict({
        "source_name": "A", "target_name": "B", "relation_type": "x",
        "description_embedding": [],
    })
    assert rel.description_embedding is None


def test_gold_item_with_graph_relations_roundtrip() -> None:
    """relevant_graph_relations round-trip."""
    item = GoldItem(
        id="qr1",
        query="결제 서비스가 의존하는 곳은?",
        relevant_doc_ids=[5],
        relevant_graph_entities=[
            GraphEntityRef(name="결제 서비스", type="system"),
        ],
        relevant_graph_relations=[
            GraphRelationRef(
                source_name="결제 서비스",
                target_name="주문 서비스",
                relation_type="depends_on",
                description="결제는 주문에 의존.",
            ),
        ],
    )
    d = item.to_dict()
    assert "relevant_graph_relations" in d
    assert d["relevant_graph_relations"][0]["relation_type"] == "depends_on"
    rehydrated = GoldItem.from_dict(d)
    assert rehydrated == item


def test_gold_item_no_relations_field_omitted_on_emit() -> None:
    item = GoldItem(id="q1", query="?", relevant_doc_ids=[1])
    d = item.to_dict()
    assert "relevant_graph_relations" not in d


# ---------------------------------------------------------------------------
# R3/R2 — relevant_doc_groups (동치 집합) + cross_document 플래그
# ---------------------------------------------------------------------------


def test_roundtrip_equivalence_groups() -> None:
    """relevant_doc_groups + cross_document round-trip 무손실."""
    item = GoldItem(
        id="qc1",
        query="cross-doc 질의",
        relevant_doc_ids=[3, 5, 9],
        relevant_doc_groups=[[3, 5], [9]],
        cross_document=True,
    )
    d = item.to_dict()
    assert d["relevant_doc_groups"] == [[3, 5], [9]]
    assert d["cross_document"] is True
    rehydrated = GoldItem.from_dict(d)
    assert rehydrated == item


def test_legacy_yaml_no_groups_loads() -> None:
    """그룹 키 없는 옛 YAML → groups=[], cross_document=False."""
    item = GoldItem.from_dict({
        "id": "q1", "query": "?", "relevant_doc_ids": [1, 2],
    })
    assert item.relevant_doc_groups == []
    assert item.cross_document is False


def test_groups_dedup_and_drop_empty() -> None:
    """그룹 내 중복 제거 + 빈 그룹 드롭."""
    item = GoldItem.from_dict({
        "id": "q1", "query": "?", "relevant_doc_ids": [3, 5],
        "relevant_doc_groups": [[3, 3, 5], []],
    })
    assert item.relevant_doc_groups == [[3, 5]]


def test_groups_omitted_when_empty_on_emit() -> None:
    """빈 relevant_doc_groups / cross_document=False 는 to_dict 에서 빠진다."""
    item = GoldItem(id="q1", query="?", relevant_doc_ids=[1])
    d = item.to_dict()
    assert "relevant_doc_groups" not in d
    assert "cross_document" not in d


def test_gold_item_backward_compat_with_extended_yaml(tmp_path: Path) -> None:
    """2차 골드셋 YAML 을 저장→로드 round-trip 후 모든 확장 필드 보존."""
    gold = GoldSet(
        version=1,
        metadata={"embedding_model": "text-embedding-3-small",
                  "graph_match_threshold_default": 0.78},
        items=[GoldItem(
            id="qg1",
            query="결제 서비스는 무엇에 의존?",
            relevant_doc_ids=[10],
            relevant_graph_entities=[
                GraphEntityRef(
                    name="결제 서비스",
                    type="system",
                    aliases=["Payment Service"],
                    description="결제 처리 시스템",
                    description_embedding=[0.1, 0.2, 0.3],
                ),
            ],
            relevant_graph_relations=[
                GraphRelationRef(
                    source_name="결제 서비스",
                    target_name="주문 서비스",
                    relation_type="depends_on",
                    description="결제는 주문에 의존.",
                    description_embedding=[0.4, 0.5],
                ),
            ],
            source_type="confluence",
        )],
    )
    path = tmp_path / "ext.yaml"
    save_gold_set(gold, path)
    loaded = save_loaded = load_gold_set(path)
    assert loaded.items[0].relevant_graph_entities[0].aliases == ["Payment Service"]
    assert loaded.items[0].relevant_graph_entities[0].description_embedding == [
        0.1, 0.2, 0.3,
    ]
    assert loaded.items[0].relevant_graph_relations[0].relation_type == "depends_on"
    assert loaded.metadata["embedding_model"] == "text-embedding-3-small"
    # 변수 의도 유지
    _ = save_loaded


# ---------------------------------------------------------------------------
# source-grounded (PR #79 P0) — SupportingFact + 신규 GoldItem 필드
# ---------------------------------------------------------------------------


def test_supporting_fact_to_from_dict_roundtrip() -> None:
    fact = SupportingFact(
        entity="Auth Service",
        entity_type="system",
        relation="depends_on",
        target="Token Validator",
        evidence_span="Auth Service가 결제 인증을 처리하며 Token Validator에 의존한다.",
        source_doc_id=12,
        acceptable_surface_forms=["인증 서비스", "AuthService"],
    )
    restored = SupportingFact.from_dict(fact.to_dict())
    assert restored == fact


def test_supporting_fact_minimal_omits_empty_keys() -> None:
    fact = SupportingFact(entity="X")
    d = fact.to_dict()
    assert d == {"entity": "X"}
    assert SupportingFact.from_dict(d) == fact


def test_gold_item_source_grounded_roundtrip() -> None:
    item = GoldItem(
        id="sg1",
        query="Auth Service는 무엇에 의존하나요?",
        relevant_doc_ids=[12],
        reference_answer="Auth Service는 Token Validator에 의존한다.",
        supporting_facts=[
            SupportingFact(
                entity="Auth Service",
                entity_type="system",
                relation="depends_on",
                target="Token Validator",
                evidence_span="Auth Service ... Token Validator에 의존한다.",
                source_doc_id=12,
            ),
        ],
        answerable=True,
        measurement_units=["doc", "answer", "graph"],
        provenance={"extraction_model": "model-a", "judge_model": "model-b", "seed": 7},
    )
    restored = GoldItem.from_dict(item.to_dict())
    assert restored.reference_answer == item.reference_answer
    assert restored.supporting_facts == item.supporting_facts
    assert restored.answerable is True
    assert restored.measurement_units == ["doc", "answer", "graph"]
    assert restored.provenance["seed"] == 7


def test_gold_item_answerable_false_emitted_and_loaded(tmp_path: Path) -> None:
    """answerable=False (인덱싱 표적) 는 emit 되고 round-trip 된다."""
    item = GoldItem(id="x", query="q", relevant_doc_ids=[1], answerable=False)
    assert item.to_dict()["answerable"] is False
    restored = GoldItem.from_dict(item.to_dict())
    assert restored.answerable is False


def test_gold_item_answerable_default_true_not_emitted() -> None:
    """기본 answerable=True 는 emit 되지 않는다 (스키마 잡음 억제)."""
    item = GoldItem(id="x", query="q", relevant_doc_ids=[1])
    assert "answerable" not in item.to_dict()


def test_gold_item_unknown_measurement_units_dropped() -> None:
    """알 수 없는 측정 단위는 조용히 버린다 (스키마 위생)."""
    item = GoldItem.from_dict({
        "id": "x",
        "query": "q",
        "relevant_doc_ids": [1],
        "measurement_units": ["doc", "bogus", "graph", "doc"],
    })
    assert item.measurement_units == ["doc", "graph"]  # bogus 제거 + 중복 제거
    assert set(item.measurement_units) <= MEASUREMENT_UNITS


def test_gold_item_legacy_without_source_grounded_fields_loads() -> None:
    """source-grounded 필드 없는 옛 YAML 도 기본값으로 로드된다 (하위호환)."""
    item = GoldItem.from_dict({"id": "old", "query": "q", "relevant_doc_ids": [1]})
    assert item.reference_answer == ""
    assert item.supporting_facts == []
    assert item.answerable is True
    assert item.measurement_units == []
    assert item.provenance == {}
