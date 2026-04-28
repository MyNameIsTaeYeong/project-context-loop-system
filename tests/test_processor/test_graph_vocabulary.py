"""graph_vocabulary 단일 출처 어휘 테스트.

추출기들의 어휘가 graph_vocabulary 에 반드시 포함되도록 강제한다 — 추출기에
새 type 을 추가하고 graph_vocabulary 갱신을 잊으면 LLM 플래너가 그 어휘를
모르게 되어 검색 시 활용 못한다.
"""

from __future__ import annotations

from context_loop.processor.body_extractor import (
    BodyExtractionConfig,  # noqa: F401  (어휘 검증용 import 유지)
)
from context_loop.processor.graph_vocabulary import (
    ENTITY_TYPES,
    INTENT_TO_RELATIONS,
    RELATION_TYPES,
    all_entity_type_names,
    all_relation_type_names,
    format_entity_types_for_prompt,
    format_intent_mapping_for_prompt,
    format_relation_types_for_prompt,
)
from context_loop.processor.link_graph_builder import (
    _KIND_TO_ENTITY_TYPE,
    _KIND_TO_RELATION_TYPE,
)
from context_loop.processor.llm_body_extractor import (
    _DEFAULT_ENTITY_TYPES,
    _DEFAULT_RELATION_TYPES,
)


def test_link_graph_vocab_subset_of_vocabulary() -> None:
    """link_graph_builder 의 어휘는 모두 graph_vocabulary 에 정의되어 있어야 한다."""
    entity_names = all_entity_type_names()
    relation_names = all_relation_type_names()
    assert set(_KIND_TO_ENTITY_TYPE.values()) <= entity_names
    assert set(_KIND_TO_RELATION_TYPE.values()) <= relation_names
    # link_graph_builder 의 self entity 도 포함
    assert "document" in entity_names


def test_llm_body_vocab_subset_of_vocabulary() -> None:
    """llm_body_extractor 의 기본 어휘는 graph_vocabulary 에 정의되어 있어야 한다."""
    assert set(_DEFAULT_ENTITY_TYPES) <= all_entity_type_names()
    assert set(_DEFAULT_RELATION_TYPES) <= all_relation_type_names()


def test_body_extractor_vocab_subset_of_vocabulary() -> None:
    """body_extractor 가 emit 하는 entity_type / relation_type 도 포함."""
    # body_extractor 는 mentions, documents, has_attribute, mentions_ticket 4종.
    # entity 측은 concept, api, ticket, document.
    expected_etypes = {"concept", "api", "ticket", "document"}
    expected_rtypes = {"mentions", "documents", "has_attribute", "mentions_ticket"}
    assert expected_etypes <= all_entity_type_names()
    assert expected_rtypes <= all_relation_type_names()


def test_no_duplicate_vocab_entries() -> None:
    """동일 이름이 두 번 정의되면 안 된다 (오타/병합 누락 방지)."""
    e_names = [e.name for e in ENTITY_TYPES]
    r_names = [r.name for r in RELATION_TYPES]
    assert len(e_names) == len(set(e_names))
    assert len(r_names) == len(set(r_names))


def test_intent_mapping_uses_known_relations() -> None:
    """INTENT_TO_RELATIONS 의 관계 이름은 모두 RELATION_TYPES 에 정의돼 있어야 한다."""
    relation_names = all_relation_type_names()
    for _intent, rels in INTENT_TO_RELATIONS:
        for r in rels:
            assert r in relation_names, f"의도 매핑의 관계 '{r}' 가 어휘에 없음"


def test_format_helpers_render_all_entries() -> None:
    """프롬프트 포맷터가 모든 어휘 항목을 텍스트에 포함한다."""
    et = format_entity_types_for_prompt()
    for entry in ENTITY_TYPES:
        assert entry.name in et

    rt = format_relation_types_for_prompt()
    for entry in RELATION_TYPES:
        assert entry.name in rt


def test_format_intent_mapping_renders_all_intents() -> None:
    text = format_intent_mapping_for_prompt()
    for intent, rels in INTENT_TO_RELATIONS:
        assert intent in text
        for r in rels:
            assert r in text
