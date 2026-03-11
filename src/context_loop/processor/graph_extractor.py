"""LLM 기반 그래프 엔티티/관계 추출 모듈.

문서 내용에서 엔티티(노드)와 관계(엣지)를 추출하여 그래프 구조로 반환한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from context_loop.processor.llm_client import LLMClient, extract_json

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You are a knowledge graph extractor.
Extract entities and relationships from the given document.

Rules:
- Entities: concrete or abstract things with clear names (people, systems, teams, concepts, services)
- Entity types: "person", "system", "team", "concept", "service", "component", "organization", "other"
- Relations: directional connections between entities
- Relation types: "belongs_to", "depends_on", "manages", "uses", "contains", "connects_to", "related_to"
- Extract only relationships explicitly stated or strongly implied in the text
- Keep entity names concise and consistent (same entity = same name)

Respond ONLY with a JSON object in this exact format:
{
  "entities": [
    {"name": "Entity Name", "type": "entity_type", "description": "brief description"}
  ],
  "relations": [
    {"source": "Entity A", "target": "Entity B", "type": "relation_type", "label": "optional label"}
  ]
}"""

_USER_PROMPT_TEMPLATE = """Extract entities and relationships from this document:

Title: {title}

Content:
{content}"""


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


async def extract_graph(
    client: LLMClient,
    title: str,
    content: str,
    *,
    max_content_chars: int = 4000,
) -> GraphData:
    """LLM을 사용하여 문서에서 엔티티와 관계를 추출한다.

    Args:
        client: LLMClient 인스턴스.
        title: 문서 제목.
        content: 문서 원본 내용.
        max_content_chars: LLM에 전달할 최대 문자 수.

    Returns:
        추출된 GraphData (entities, relations).
    """
    prompt = _USER_PROMPT_TEMPLATE.format(
        title=title,
        content=content[:max_content_chars],
    )

    response = await client.complete(
        prompt,
        system=_SYSTEM_PROMPT,
        max_tokens=2048,
        temperature=0.0,
    )

    try:
        data = extract_json(response)
        entities = [
            Entity(
                name=e.get("name", "Unknown"),
                entity_type=e.get("type", "other"),
                description=e.get("description", ""),
            )
            for e in data.get("entities", [])
            if e.get("name")
        ]
        relations = [
            Relation(
                source=r.get("source", ""),
                target=r.get("target", ""),
                relation_type=r.get("type", "related_to"),
                label=r.get("label", ""),
            )
            for r in data.get("relations", [])
            if r.get("source") and r.get("target")
        ]
        logger.info(
            "그래프 추출 완료 — 엔티티: %d개, 관계: %d개",
            len(entities),
            len(relations),
        )
        return GraphData(entities=entities, relations=relations)
    except ValueError:
        logger.warning("그래프 추출 응답 파싱 실패. 빈 그래프 반환.")
        return GraphData()
