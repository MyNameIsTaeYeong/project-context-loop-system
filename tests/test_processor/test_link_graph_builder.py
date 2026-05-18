"""link_graph_builder.build_link_graph 단위 테스트.

Confluence OutLink → Entity/Relation 변환 규칙을 검증한다. DB/LLM/네트워크
의존이 전혀 없는 순수 함수 테스트이다.
"""

from __future__ import annotations

from context_loop.ingestion.confluence_extractor import ExtractedDocument, OutLink
from context_loop.processor.link_graph_builder import build_link_graph


def _doc(*links: OutLink) -> ExtractedDocument:
    return ExtractedDocument(outbound_links=list(links))


def test_empty_document_returns_empty_graph() -> None:
    """outbound_links가 없으면 self-entity도 만들지 않는다."""
    result = build_link_graph(ExtractedDocument(), doc_title="결제 시스템")
    assert result.entities == []
    assert result.relations == []


def test_only_skipped_urls_returns_empty_graph() -> None:
    """url 링크만 있으면 SKIP되어 결과적으로 빈 그래프가 된다."""
    extracted = _doc(
        OutLink(kind="url", target_id="https://example.com", anchor_text="ex"),
        OutLink(kind="url", target_id="https://docs.x", anchor_text="docs"),
    )
    result = build_link_graph(extracted, doc_title="결제 시스템")
    assert result.entities == []
    assert result.relations == []


def test_page_link_creates_document_reference() -> None:
    """page 링크는 document/references 엣지를 생성하고 self-entity도 포함한다."""
    extracted = _doc(
        OutLink(
            kind="page",
            target_id="123",
            target_title="인증 서비스",
            target_space="ARCH",
            anchor_text="인증 서비스",
            in_section=["결제 시스템", "엔드포인트"],
        ),
    )
    result = build_link_graph(extracted, doc_title="결제 시스템")

    names_by_type = {(e.name, e.entity_type) for e in result.entities}
    assert ("결제 시스템", "document") in names_by_type
    assert ("인증 서비스", "document") in names_by_type

    assert len(result.relations) == 1
    rel = result.relations[0]
    assert rel.source == "결제 시스템"
    assert rel.target == "인증 서비스"
    assert rel.relation_type == "references"
    assert rel.label == "결제 시스템 / 엔드포인트"


def test_page_link_without_title_falls_back_to_id() -> None:
    """target_title이 비어 있으면 ``page:{id}`` 형태로 이름을 생성한다."""
    extracted = _doc(
        OutLink(kind="page", target_id="987654", target_title=None),
    )
    result = build_link_graph(extracted, doc_title="A")

    targets = {e.name for e in result.entities if e.entity_type == "document"}
    assert "page:987654" in targets


def test_user_link_creates_person_mention() -> None:
    """user 링크는 person/mentions_user 관계를 만든다."""
    extracted = _doc(
        OutLink(kind="user", target_id="557058:alice", anchor_text="Alice"),
    )
    result = build_link_graph(extracted, doc_title="문서")

    person_entities = [e for e in result.entities if e.entity_type == "person"]
    assert len(person_entities) == 1
    assert person_entities[0].name == "557058:alice"

    assert len(result.relations) == 1
    assert result.relations[0].relation_type == "mentions_user"
    assert result.relations[0].target == "557058:alice"


def test_jira_link_creates_ticket_mention() -> None:
    """jira 링크는 ticket/mentions_ticket 관계를 만든다."""
    extracted = _doc(OutLink(kind="jira", target_id="PROJ-123"))
    result = build_link_graph(extracted, doc_title="문서")

    ticket_entities = [e for e in result.entities if e.entity_type == "ticket"]
    assert len(ticket_entities) == 1
    assert ticket_entities[0].name == "PROJ-123"

    assert result.relations[0].relation_type == "mentions_ticket"
    assert result.relations[0].target == "PROJ-123"


def test_attachment_link_creates_attachment_relation() -> None:
    """attachment 링크는 attachment/has_attachment 관계를 만든다."""
    extracted = _doc(
        OutLink(kind="attachment", target_id="design.pdf", anchor_text="design"),
    )
    result = build_link_graph(extracted, doc_title="문서")

    atts = [e for e in result.entities if e.entity_type == "attachment"]
    assert len(atts) == 1
    assert atts[0].name == "design.pdf"

    assert result.relations[0].relation_type == "has_attachment"


def test_url_link_is_skipped_even_among_valid_links() -> None:
    """url 링크는 무시되고 나머지 종류만 그래프에 들어간다."""
    extracted = _doc(
        OutLink(kind="url", target_id="https://x.com"),
        OutLink(kind="page", target_id="1", target_title="타겟"),
    )
    result = build_link_graph(extracted, doc_title="A")

    names = {e.name for e in result.entities}
    assert "타겟" in names
    assert "https://x.com" not in names
    assert len(result.relations) == 1


def test_duplicate_target_deduped_at_entity_and_relation_level() -> None:
    """같은 페이지를 두 번 언급하면 엔티티/관계 모두 1개만 생성된다."""
    extracted = _doc(
        OutLink(
            kind="page", target_id="1", target_title="타겟",
            in_section=["A"],
        ),
        OutLink(
            kind="page", target_id="1", target_title="타겟",
            in_section=["B"],
        ),
    )
    result = build_link_graph(extracted, doc_title="원본")

    assert len([e for e in result.entities if e.name == "타겟"]) == 1
    refs = [r for r in result.relations if r.relation_type == "references"]
    assert len(refs) == 1
    # 첫 등장 섹션("A")이 라벨로 기록된다
    assert refs[0].label == "A"


def test_different_kinds_to_same_id_do_not_collide() -> None:
    """동일 문자열이라도 kind가 다르면 엔티티 타입이 달라 별도 노드가 된다."""
    extracted = _doc(
        OutLink(kind="jira", target_id="PROJ-1"),
        OutLink(kind="attachment", target_id="PROJ-1"),
    )
    result = build_link_graph(extracted, doc_title="문서")

    types = {(e.name, e.entity_type) for e in result.entities}
    assert ("PROJ-1", "ticket") in types
    assert ("PROJ-1", "attachment") in types
    assert len(result.relations) == 2


def test_self_entity_is_document_type() -> None:
    """self-entity는 항상 entity_type='document'로 생성된다."""
    extracted = _doc(OutLink(kind="jira", target_id="X-1"))
    result = build_link_graph(extracted, doc_title="어떤 문서")

    self_entity = next(e for e in result.entities if e.name == "어떤 문서")
    assert self_entity.entity_type == "document"


def test_empty_doc_title_returns_empty_graph() -> None:
    """doc_title이 비어 있으면 self-entity를 만들 수 없으므로 빈 그래프 반환."""
    extracted = _doc(OutLink(kind="page", target_id="1", target_title="t"))
    result = build_link_graph(extracted, doc_title="")
    assert result.entities == []
    assert result.relations == []


def test_mixed_links_produce_expected_entity_counts() -> None:
    """혼합 링크가 주어졌을 때 타입별 엔티티 개수를 검증한다."""
    extracted = _doc(
        OutLink(kind="page", target_id="1", target_title="A"),
        OutLink(kind="page", target_id="2", target_title="B"),
        OutLink(kind="user", target_id="u1"),
        OutLink(kind="jira", target_id="J-1"),
        OutLink(kind="attachment", target_id="f.pdf"),
        OutLink(kind="url", target_id="https://x"),
    )
    result = build_link_graph(extracted, doc_title="원본")

    type_counts: dict[str, int] = {}
    for e in result.entities:
        type_counts[e.entity_type] = type_counts.get(e.entity_type, 0) + 1
    # document: self + A + B = 3, person: 1, ticket: 1, attachment: 1
    assert type_counts["document"] == 3
    assert type_counts["person"] == 1
    assert type_counts["ticket"] == 1
    assert type_counts["attachment"] == 1
    # 관계: page 2 + user 1 + jira 1 + attachment 1 = 5 (url 제외)
    assert len(result.relations) == 5
