"""LLM 기반 그래프 엔티티/관계 추출 모듈.

문서 내용에서 엔티티(노드)와 관계(엣지)를 추출하여 그래프 구조로 반환한다.
긴 문서는 map-reduce 방식으로 분할 추출 후 병합한다.
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

_USER_PROMPT_CHUNK_TEMPLATE = """Extract entities and relationships from this section of the document "{title}":

Section ({chunk_index}/{total_chunks}):
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

    문서가 max_content_chars보다 길면 map-reduce 방식으로 분할 추출 후 병합한다.
    짧은 문서는 기존과 동일하게 단일 호출로 처리한다.

    Args:
        client: LLMClient 인스턴스.
        title: 문서 제목.
        content: 문서 원본 내용.
        max_content_chars: LLM 1회 호출당 최대 문자 수.

    Returns:
        추출된 GraphData (entities, relations).
    """
    if len(content) <= max_content_chars:
        return await _extract_single(client, title, content)

    # Map-reduce: 긴 문서를 청크로 분할하여 각각 추출 후 병합
    return await _extract_map_reduce(client, title, content, max_content_chars)


async def _extract_single(
    client: LLMClient,
    title: str,
    content: str,
) -> GraphData:
    """단일 LLM 호출로 그래프를 추출한다."""
    prompt = _USER_PROMPT_TEMPLATE.format(title=title, content=content)
    response = await client.complete(
        prompt,
        system=_SYSTEM_PROMPT,
        max_tokens=4096,
        temperature=0.0,
    )
    return _parse_graph_response(response)


async def _extract_map_reduce(
    client: LLMClient,
    title: str,
    content: str,
    max_content_chars: int,
) -> GraphData:
    """긴 문서를 분할하여 그래프 추출 후 병합한다 (map-reduce).

    1. Map: 문서를 max_content_chars 크기 청크로 분할, 각 청크에서 그래프 추출
    2. Reduce: 전체 결과를 병합 (동일 엔티티 중복 제거, 관계 중복 제거)
    """
    chunks = _split_content(content, max_content_chars)
    total = len(chunks)
    logger.info(
        "Map-reduce 그래프 추출 시작 — 문서='%s', %d개 청크 (원문 %d자)",
        title, total, len(content),
    )

    all_graphs: list[GraphData] = []
    for i, chunk in enumerate(chunks):
        prompt = _USER_PROMPT_CHUNK_TEMPLATE.format(
            title=title,
            content=chunk,
            chunk_index=i + 1,
            total_chunks=total,
        )
        try:
            response = await client.complete(
                prompt,
                system=_SYSTEM_PROMPT,
                max_tokens=4096,
                temperature=0.0,
            )
            graph = _parse_graph_response(response)
            all_graphs.append(graph)
        except Exception:
            logger.warning("청크 %d/%d 그래프 추출 실패, 건너뜀", i + 1, total, exc_info=True)

    merged = _merge_graphs(all_graphs)
    logger.info(
        "Map-reduce 그래프 추출 완료 — 엔티티: %d개, 관계: %d개 (%d개 청크 처리)",
        len(merged.entities), len(merged.relations), total,
    )
    return merged


def _split_content(content: str, max_chars: int) -> list[str]:
    """문서를 max_chars 크기 청크로 분할한다.

    단락 경계(\n\n)를 우선 존중하고, 불가능하면 줄바꿈(\n) 기준으로 분할.
    """
    if len(content) <= max_chars:
        return [content]

    chunks: list[str] = []
    start = 0
    while start < len(content):
        end = start + max_chars
        if end >= len(content):
            chunks.append(content[start:])
            break

        # 단락 경계 탐색 (뒤에서부터)
        split_pos = content.rfind("\n\n", start, end)
        if split_pos <= start:
            # 단락 경계 없으면 줄바꿈 탐색
            split_pos = content.rfind("\n", start, end)
        if split_pos <= start:
            # 줄바꿈도 없으면 강제 분할
            split_pos = end

        chunks.append(content[start:split_pos])
        start = split_pos
        # 구분자(\n\n 또는 \n) 건너뛰기
        while start < len(content) and content[start] == "\n":
            start += 1

    return [c for c in chunks if c.strip()]


def _merge_graphs(graphs: list[GraphData]) -> GraphData:
    """여러 GraphData를 병합한다.

    - 엔티티: (name, entity_type) 기준 중복 제거. 먼저 나온 description 유지.
    - 관계: (source, target, relation_type) 기준 중복 제거.
    """
    entity_map: dict[tuple[str, str], Entity] = {}
    relation_set: set[tuple[str, str, str]] = set()
    merged_relations: list[Relation] = []

    for graph in graphs:
        for entity in graph.entities:
            key = (entity.name.strip().lower(), entity.entity_type)
            if key not in entity_map:
                entity_map[key] = entity
            elif not entity_map[key].description and entity.description:
                # 기존에 설명이 없으면 보충
                entity_map[key] = entity

        for relation in graph.relations:
            key = (
                relation.source.strip().lower(),
                relation.target.strip().lower(),
                relation.relation_type,
            )
            if key not in relation_set:
                relation_set.add(key)
                merged_relations.append(relation)

    return GraphData(
        entities=list(entity_map.values()),
        relations=merged_relations,
    )


def _parse_graph_response(response: str) -> GraphData:
    """LLM 응답을 파싱하여 GraphData를 반환한다."""
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
