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


def test_ast_code_vocab_subset_of_vocabulary() -> None:
    """ast_code_extractor 가 emit 하는 entity/relation 타입도 vocab 에 정의돼야 한다.

    실제 ast_code_extractor.to_graph_data 의 출력에 등장하는 타입과 graph_vocabulary
    가 어긋나면 graph_search_planner 의 LLM 가이드에 빈 항목이 생겨 검색 활용도가
    떨어진다. ast_code 가 사용하는 entity_type: module/function/class/method/
    struct/interface, relation_type: imports/contains.
    """
    expected_etypes = {
        "module", "function", "class", "method", "struct", "interface",
    }
    expected_rtypes = {"imports", "contains"}
    missing_e = expected_etypes - all_entity_type_names()
    missing_r = expected_rtypes - all_relation_type_names()
    assert not missing_e, f"ast_code entity_type 누락: {missing_e}"
    assert not missing_r, f"ast_code relation_type 누락: {missing_r}"


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


def test_llm_body_subset_helpers_are_consistent() -> None:
    """graph_vocabulary 의 llm_body subset 헬퍼와 llm_body_extractor 가 사용하는
    어휘가 정확히 일치한다 — 인덱싱-검색 LLM 어휘 정렬의 단일 출처 보증."""
    from context_loop.processor.graph_vocabulary import (
        llm_body_entity_type_names,
        llm_body_entity_types_vocab,
        llm_body_relation_type_names,
        llm_body_relation_types_vocab,
    )

    # subset 헬퍼가 ENTITY_TYPES / RELATION_TYPES 의 진짜 subset
    assert set(llm_body_entity_type_names()) <= all_entity_type_names()
    assert set(llm_body_relation_type_names()) <= all_relation_type_names()

    # llm_body_extractor 가 graph_vocabulary 의 subset 을 그대로 사용
    assert set(_DEFAULT_ENTITY_TYPES) == set(llm_body_entity_type_names()), (
        "llm_body_extractor _DEFAULT_ENTITY_TYPES 와 graph_vocabulary 의 "
        "llm_body_entity_type_names() 가 다릅니다 — 어휘 정렬 깨짐"
    )
    assert set(_DEFAULT_RELATION_TYPES) == set(llm_body_relation_type_names()), (
        "llm_body_extractor _DEFAULT_RELATION_TYPES 와 graph_vocabulary 의 "
        "llm_body_relation_type_names() 가 다릅니다 — 어휘 정렬 깨짐"
    )

    # subset vocab 의 source 필드에 "llm_body" 가 모두 포함
    for entry in llm_body_entity_types_vocab():
        assert "llm_body" in entry.source
    for entry in llm_body_relation_types_vocab():
        assert "llm_body" in entry.source


def test_format_vocab_entries_for_prompt_includes_descriptions() -> None:
    """범용 포매터가 이름 + 설명을 모두 노출한다 — 인덱싱·검색 LLM 모두
    같은 포맷으로 어휘 가이드를 받도록 한다."""
    from context_loop.processor.graph_vocabulary import (
        format_vocab_entries_for_prompt,
        llm_body_entity_types_vocab,
    )

    vocab = llm_body_entity_types_vocab()
    assert vocab, "llm_body entity subset 이 비어있으면 안 됨"
    text = format_vocab_entries_for_prompt(vocab)
    for entry in vocab:
        assert f"**{entry.name}**" in text
        assert entry.description in text
