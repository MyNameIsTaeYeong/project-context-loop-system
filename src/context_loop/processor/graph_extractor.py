"""그래프 데이터 공통 타입.

``Entity`` / ``Relation`` / ``GraphData``는 여러 모듈이 공유하는
스키마(:mod:`link_graph_builder`, :mod:`ast_code_extractor`,
:mod:`storage.graph_store`)이다. 과거에는 이 모듈이 LLM 기반 그래프 추출
까지 포함했지만, 결정론적 추출(Confluence 링크 그래프 + AST 코드 심볼)로
전환하면서 LLM 관련 코드는 제거되었다.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Entity:
    """그래프 엔티티(노드).

    Attributes:
        name: 엔티티 이름.
        entity_type: 엔티티 유형.
        description: 엔티티 설명.
    """

    name: str
    entity_type: str = "other"
    description: str = ""


@dataclass
class Relation:
    """그래프 관계(엣지).

    Attributes:
        source: 출발 엔티티 이름.
        target: 도착 엔티티 이름.
        relation_type: 관계 유형.
        label: 관계 레이블 (선택).
    """

    source: str
    target: str
    relation_type: str = "related_to"
    label: str = ""


@dataclass
class GraphData:
    """추출된 그래프 데이터.

    Attributes:
        entities: 추출된 엔티티 목록.
        relations: 추출된 관계 목록.
    """

    entities: list[Entity] = field(default_factory=list)
    relations: list[Relation] = field(default_factory=list)
