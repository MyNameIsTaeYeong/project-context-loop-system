"""LLM 기반 그래프 탐색 플래너.

사용자 질의와 그래프 스키마 요약을 LLM에게 제공하여
그래프의 어떤 영역을 어떻게 탐색할지 계획을 세우고 실행한다.

플로우:
1. GraphStore.format_schema_for_llm()로 그래프 구조 요약 생성
2. LLM에게 질의 + 스키마 요약 전달 → 탐색 계획(JSON) 수신
3. 계획에 따라 GraphStore에서 실제 탐색 수행
4. 탐색 결과를 컨텍스트 텍스트로 포맷팅
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from context_loop.processor.graph_vocabulary import (
    format_entity_types_for_prompt,
    format_intent_mapping_for_prompt,
    format_relation_types_for_prompt,
)
from context_loop.processor.llm_client import LLMClient, extract_json
from context_loop.storage.graph_store import GraphStore

logger = logging.getLogger(__name__)

_PLAN_SYSTEM_PROMPT = """\
당신은 지식 그래프 탐색 전문가입니다.
사용자의 질의와 그래프 구조 정보를 분석하여 어떤 엔티티를 중심으로 탐색할지
계획합니다. 그래프에는 여러 추출기(링크 / 결정론 본문 / LLM 의미)가 만든
다양한 의미 관계가 들어 있으며, ``focus_relations`` 로 의도에 맞는 관계를
좁혀 탐색하면 더 정확한 컨텍스트를 얻을 수 있습니다.

# Entity types (그래프에 등장 가능)
{entity_types}

# Relation types (그래프에 등장 가능)
{relation_types}

# 질의 의도 → 주목할 관계 (focus_relations 힌트)
{intent_mapping}

# 규칙
- 그래프에 실제 존재하는 엔티티 이름만 사용하세요 (스키마 요약에 등장한 이름).
- 질의와 관련 없는 엔티티는 포함하지 마세요.
- 관련 엔티티가 없으면 ``should_search=false`` 와 빈 ``search_steps`` 를 반환하세요.
- 탐색 깊이(``depth``) 는 1~2 사이로 설정하세요 (단순 직접 관계는 1, 2-홉
  추론이 필요하면 2).
- ``focus_relations`` 는 위 매핑 가이드를 참고해 채우세요. 비어 있으면 모든
  관계가 표시됩니다 — 의도가 명확하면 반드시 좁히세요.
- ``focus_relations`` 에는 위 Relation types 목록의 정확한 이름만 사용하세요.

# 응답 형식 (JSON 만, 다른 텍스트 금지)
```json
{{
  "should_search": true,
  "reasoning": "탐색 필요 여부에 대한 간단한 이유",
  "search_steps": [
    {{
      "entity_name": "탐색 시작 엔티티 이름",
      "depth": 1,
      "focus_relations": ["depends_on"]
    }}
  ]
}}
```

- ``search_steps`` 는 최대 3개.
- ``should_search=false`` 인 경우 ``search_steps`` 는 빈 배열.
"""

_PLAN_USER_TEMPLATE = """\
## 사용자 질의
{query}

## 그래프 구조
{schema}
"""


def _render_system_prompt() -> str:
    """어휘 가이드를 템플릿에 끼워 시스템 프롬프트를 만든다.

    어휘는 ``graph_vocabulary`` 단일 출처에서 가져온다 — 추출기가 새 entity/
    relation 타입을 도입하면 거기 추가만 하면 자동으로 플래너가 인식한다.
    """
    return _PLAN_SYSTEM_PROMPT.format(
        entity_types=format_entity_types_for_prompt(),
        relation_types=format_relation_types_for_prompt(),
        intent_mapping=format_intent_mapping_for_prompt(),
    )


@dataclass
class SearchStep:
    """단일 탐색 단계."""

    entity_name: str
    depth: int = 1
    focus_relations: list[str] = field(default_factory=list)


@dataclass
class GraphSearchPlan:
    """LLM이 생성한 그래프 탐색 계획."""

    should_search: bool
    reasoning: str = ""
    search_steps: list[SearchStep] = field(default_factory=list)


@dataclass
class GraphSearchResult:
    """그래프 탐색 결과."""

    text: str
    document_ids: set[int] = field(default_factory=set)


async def plan_graph_search(
    query: str,
    graph_store: GraphStore,
    llm_client: LLMClient,
    *,
    query_embedding: list[float] | None = None,
) -> GraphSearchPlan:
    """LLM을 사용하여 그래프 탐색 계획을 생성한다.

    query_embedding이 제공되고 엔티티 임베딩이 구축되어 있으면
    쿼리와 관련된 서브그래프 스키마만 LLM에게 제공한다.

    Args:
        query: 사용자 질의.
        graph_store: 그래프 저장소.
        llm_client: LLM 클라이언트.
        query_embedding: 쿼리 텍스트의 임베딩 벡터. None이면 전체 스키마 사용.

    Returns:
        그래프 탐색 계획.
    """
    if graph_store.stats()["nodes"] == 0:
        return GraphSearchPlan(should_search=False, reasoning="그래프가 비어 있음")

    if query_embedding is not None and graph_store.entity_embedding_count > 0:
        schema_text = graph_store.format_query_relevant_schema_for_llm(query_embedding)
    else:
        schema_text = graph_store.format_schema_for_llm()

    prompt = _PLAN_USER_TEMPLATE.format(query=query, schema=schema_text)

    try:
        response = await llm_client.complete(
            prompt,
            system=_render_system_prompt(),
            max_tokens=512,
            temperature=0.0,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
        plan_data = extract_json(response)
    except Exception:
        logger.warning("그래프 탐색 계획 생성 실패", exc_info=True)
        return GraphSearchPlan(should_search=False, reasoning="계획 생성 실패")

    return _parse_plan(plan_data)


def _parse_plan(data: Any) -> GraphSearchPlan:
    """LLM 응답 JSON을 GraphSearchPlan으로 파싱한다."""
    if not isinstance(data, dict):
        return GraphSearchPlan(should_search=False, reasoning="응답 파싱 실패")

    should_search = bool(data.get("should_search", False))
    reasoning = str(data.get("reasoning", ""))

    steps: list[SearchStep] = []
    for step_data in data.get("search_steps", [])[:3]:
        if not isinstance(step_data, dict):
            continue
        entity_name = step_data.get("entity_name", "")
        if not entity_name:
            continue
        depth = min(max(int(step_data.get("depth", 1)), 1), 2)
        focus = step_data.get("focus_relations", [])
        if not isinstance(focus, list):
            focus = []
        steps.append(SearchStep(
            entity_name=entity_name,
            depth=depth,
            focus_relations=[str(r) for r in focus],
        ))

    return GraphSearchPlan(
        should_search=should_search,
        reasoning=reasoning,
        search_steps=steps,
    )


async def execute_graph_search(
    plan: GraphSearchPlan,
    graph_store: GraphStore,
) -> GraphSearchResult | None:
    """탐색 계획에 따라 그래프를 탐색하고 결과를 포맷팅한다.

    Args:
        plan: LLM이 생성한 탐색 계획.
        graph_store: 그래프 저장소.

    Returns:
        그래프 탐색 결과(텍스트 + 관련 document_id). 결과가 없으면 None.
    """
    if not plan.should_search or not plan.search_steps:
        return None

    all_nodes: list[dict[str, Any]] = []
    all_node_ids: set[int] = set()
    searched_entities: list[str] = []

    for step in plan.search_steps:
        neighbors = graph_store.get_neighbors(step.entity_name, depth=step.depth)
        if not neighbors:
            continue
        searched_entities.append(step.entity_name)

        for n in neighbors:
            nid = n.get("id")
            if nid and nid not in all_node_ids:
                all_node_ids.add(nid)
                all_nodes.append(n)

    if not all_nodes:
        return None

    edges = graph_store.get_edges_between(list(all_node_ids))

    # focus_relations 필터링: 계획에 명시된 관계 유형이 있으면 해당 관계만 표시
    all_focus = set()
    for step in plan.search_steps:
        all_focus.update(step.focus_relations)

    if all_focus:
        filtered_edges = [e for e in edges if e.get("relation_type", "") in all_focus]
        # focus 관계가 하나도 없으면 전체 표시
        if filtered_edges:
            edges = filtered_edges

    # 관련 document_id 수집 (정규 노드는 document_ids set을 가짐)
    document_ids: set[int] = set()
    for node in all_nodes:
        node_doc_ids = node.get("document_ids")
        if isinstance(node_doc_ids, set):
            document_ids.update(node_doc_ids)
        else:
            # 레거시 호환: 단일 document_id
            doc_id = node.get("document_id")
            if doc_id is not None:
                document_ids.add(doc_id)

    # 포맷팅
    searched_set = {name.lower() for name in searched_entities}
    lines = ["## 관련 그래프 컨텍스트"]
    if plan.reasoning:
        lines.append(f"_탐색 근거: {plan.reasoning}_")

    lines.append("\n**엔티티:**")
    for node in all_nodes:
        name = node.get("entity_name", "")
        etype = node.get("entity_type", "")
        marker = " *" if name.lower() in searched_set else ""
        desc = node.get("properties", {}).get("description", "")
        desc_text = f" — {desc}" if desc else ""
        lines.append(f"- {name} ({etype}){marker}{desc_text}")

    if edges:
        lines.append("\n**관계:**")
        id_to_name = {n["id"]: n.get("entity_name", "") for n in all_nodes}
        for edge in edges:
            src = id_to_name.get(edge.get("source"), "?")
            tgt = id_to_name.get(edge.get("target"), "?")
            rel = edge.get("relation_type", "관련")
            label = edge.get("properties", {}).get("label", "")
            label_text = f" ({label})" if label else ""
            lines.append(f"- {src} --[{rel}]--> {tgt}{label_text}")

    return GraphSearchResult(text="\n".join(lines), document_ids=document_ids)
