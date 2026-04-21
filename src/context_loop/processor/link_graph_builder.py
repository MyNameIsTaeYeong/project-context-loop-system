"""Confluence outbound_links → 결정론적 링크 그래프 빌더.

``confluence_extractor``가 HTML에서 뽑은 ``OutLink`` 목록을 기존 그래프
스키마(``Entity``/``Relation``/``GraphData``)로 변환한다. LLM 호출이 없으며
순수 함수이다.

self-entity
    현재 문서 자신을 ``Entity(name=doc_title, entity_type="document")``로
    함께 생성한다. ``GraphStore``의 ``(name, entity_type)`` 병합 규칙 덕분에
    다른 문서에서 들어오는 ``references`` 관계가 동일 노드로 수렴한다.

OutLink → Entity/Relation 매핑
    ======================  ======================  ================
    kind                    entity_type             relation_type
    ======================  ======================  ================
    page                    document                references
    user                    person                  mentions_user
    jira                    ticket                  mentions_ticket
    attachment              attachment              has_attachment
    url                     (SKIP)                  —
    ======================  ======================  ================

엔티티/관계 중복 제거
    한 문서에서 같은 타겟이 여러 번 등장해도 ``Entity``는 한 번만 생성하고
    ``(source, target, relation_type)`` 3-튜플이 같은 관계도 한 번만
    만든다. 같은 타겟이 서로 다른 섹션에 등장하면 가장 먼저 본 섹션을
    ``Relation.label``에 기록한다.
"""

from __future__ import annotations

from context_loop.ingestion.confluence_extractor import ExtractedDocument, OutLink
from context_loop.processor.graph_extractor import Entity, GraphData, Relation

_SELF_ENTITY_TYPE = "document"

_KIND_TO_ENTITY_TYPE: dict[str, str] = {
    "page": "document",
    "user": "person",
    "jira": "ticket",
    "attachment": "attachment",
}

_KIND_TO_RELATION_TYPE: dict[str, str] = {
    "page": "references",
    "user": "mentions_user",
    "jira": "mentions_ticket",
    "attachment": "has_attachment",
}


def build_link_graph(
    extracted: ExtractedDocument,
    *,
    doc_title: str,
) -> GraphData:
    """``ExtractedDocument``를 링크 기반 ``GraphData``로 변환한다.

    Args:
        extracted: Step 1 추출기 결과.
        doc_title: 현재 문서 제목. self-entity 이름으로 사용된다.

    Returns:
        ``GraphData``. 추출 링크가 하나도 없으면 빈 그래프를 반환한다.
    """
    if not doc_title:
        return GraphData()

    outbound = [lnk for lnk in extracted.outbound_links if _should_include(lnk)]
    if not outbound:
        return GraphData()

    self_entity = Entity(
        name=doc_title,
        entity_type=_SELF_ENTITY_TYPE,
        description="",
    )

    entities: dict[tuple[str, str], Entity] = {
        _entity_key(self_entity.name, self_entity.entity_type): self_entity,
    }
    relations: dict[tuple[str, str, str], Relation] = {}

    for link in outbound:
        target_name = _target_name(link)
        if not target_name:
            continue
        target_type = _KIND_TO_ENTITY_TYPE[link.kind]
        relation_type = _KIND_TO_RELATION_TYPE[link.kind]

        target_key = _entity_key(target_name, target_type)
        if target_key not in entities:
            entities[target_key] = Entity(
                name=target_name,
                entity_type=target_type,
                description=_entity_description(link),
            )

        rel_key = (
            _entity_key(doc_title, _SELF_ENTITY_TYPE),
            target_key,
            relation_type,
        )
        if rel_key not in relations:
            relations[rel_key] = Relation(
                source=doc_title,
                target=target_name,
                relation_type=relation_type,
                label=_relation_label(link),
            )

    return GraphData(
        entities=list(entities.values()),
        relations=list(relations.values()),
    )


def _should_include(link: OutLink) -> bool:
    """Link가 그래프 엣지로 변환 대상인지 판정한다."""
    return link.kind in _KIND_TO_ENTITY_TYPE


def _target_name(link: OutLink) -> str:
    """OutLink에서 타겟 엔티티 이름을 결정한다.

    page 링크는 표시용 제목이 있으면 우선 사용하고, 없으면
    ``page:{target_id}`` 포맷으로 폴백한다. 그 외 kind는 ``target_id``를
    그대로 쓴다.
    """
    if link.kind == "page":
        if link.target_title:
            return link.target_title
        if link.target_id:
            return f"page:{link.target_id}"
        return ""
    return link.target_id or ""


def _entity_key(name: str, entity_type: str) -> tuple[str, str]:
    """Entity 중복 제거용 키. ``GraphStore``의 병합 규칙과 동일 형식."""
    return (name.strip().lower(), entity_type)


def _entity_description(link: OutLink) -> str:
    """OutLink 종류별로 간단한 설명을 생성한다."""
    if link.kind == "page" and link.target_space:
        return f"Confluence page in space {link.target_space}"
    if link.kind == "jira":
        return "Jira issue"
    if link.kind == "user":
        return "Confluence user"
    if link.kind == "attachment":
        return "Attached file"
    return ""


def _relation_label(link: OutLink) -> str:
    """Relation에 붙일 라벨. 섹션 경로가 있으면 그 경로를 기록한다."""
    if link.in_section:
        return " / ".join(link.in_section)
    return ""
