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

from context_loop.eval.gold_set import GraphEntityRef, GraphRelationRef
from context_loop.processor.graph_vocabulary import (
    format_entity_types_for_prompt,
    format_intent_mapping_for_prompt,
    format_relation_types_for_prompt,
)
from context_loop.processor.llm_client import LLMClient, extract_json
from context_loop.storage.graph_store import GraphStore

logger = logging.getLogger(__name__)

_PLAN_SYSTEM_PROMPT = """\
당신은 사용자 질문을 지식 그래프 쿼리로 변환하는 전문가입니다.
인덱싱 단계에서 문서에서 추출한 엔티티와 관계를 **같은 어휘·같은 방향성**
으로 찾아야 합니다. 즉, 인덱싱 LLM 이 ``{{source, target, relation_type}}``
형태로 방향성을 가진 관계를 추출했다면, 검색 LLM 도 같은 형태로 정답이 될
엔티티와 관계를 명시합니다.

# Entity types (인덱싱 어휘 — 이 목록만 사용)
{entity_types}

# Relation types (인덱싱 어휘 — 이 목록만 사용)
{relation_types}

# 의도 → 관계 매핑 가이드
{intent_mapping}

# 규칙
- 그래프 스키마 요약에 실제 존재하는 엔티티 이름만 사용. 표기를 **글자 단위로
  정확히 복사**합니다. 임의로 공백/케이스/하이픈/언더스코어를 바꾸지 마세요.
  (스키마 표기 그대로: 공백·하이픈·언더스코어·케이스·한자/영문 혼합 모두 보존)
- ``target_entities``: 질문의 **정답이 될 엔티티**들. 질문이 "X 는 무엇인가"
  / "X 의 속성" 류면 그 X 자체가 target. 질문이 "Y 와 관계가 있는 것은?" 류면
  Y 와 정답 후보 entity 모두 target 에 포함.
- ``target_relations``: 질문이 **관계 자체를 묻거나 관계로 정답을 찾는** 경우
  ``(source, target, relation_type)`` 으로 방향성을 명시. 인덱싱 시 추출된
  관계와 같은 형태.
  - 질문이 "A 가 의존하는 것?" 류면 source=A, relation_type=depends_on,
    target 은 빈 문자열 가능 (시스템이 그래프에서 보충).
  - 질문이 "X 를 사용하는 서비스는?" 류면 target=X, relation_type=uses,
    source 는 빈 문자열 가능 (incoming 방향 탐색).
- 관계의 방향: ``source --[type]--> target`` 이 인덱싱 측 어휘와 동일한 규약.
  "A 가 B 에 depends_on" 이면 source=A, target=B.
- 그래프와 무관한 질문이면 ``should_search=false`` + 빈 배열 반환.
- ``relation_type`` 은 위 Relation types 목록의 이름만 사용 (어휘 외 금지).

# 응답 형식 (JSON 만, 다른 텍스트 금지) — 예시는 형태만, 실제 엔티티는 스키마에서
```json
{{
  "should_search": true,
  "reasoning": "왜 그래프 검색이 필요한지 간단히",
  "target_entities": [
    {{"name": "Auth Service", "type": "system"}},
    {{"name": "Token Validator", "type": "module"}}
  ],
  "target_relations": [
    {{"source": "Auth Service", "target": "Token Validator", "relation_type": "depends_on"}}
  ]
}}
```

- ``target_entities`` 와 ``target_relations`` 합쳐 최대 5개 (간결성).
- 모르는 끝점은 빈 문자열 (``""``) 허용 — 시스템이 그래프에서 추론.
- ``should_search=false`` 인 경우 두 배열 모두 빈 배열.
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
    """단일 탐색 단계 (legacy — R3 이전 LLM 응답 호환).

    R3 에서 ``GraphSearchPlan`` 의 1차 표현은 ``target_entities`` +
    ``target_relations`` 로 변경됨. LLM 이 구식 응답 (search_steps) 을
    돌려주거나, 호출자가 직접 search_steps 를 만들어 넘기는 기존 코드 경로를
    유지하기 위해 클래스 자체는 보존한다.
    """

    entity_name: str
    depth: int = 1
    focus_relations: list[str] = field(default_factory=list)


@dataclass
class TargetEntity:
    """검색 LLM 이 식별한 정답 후보 엔티티.

    인덱싱 측 ``Entity`` (graph_extractor.Entity) 와 같은 형태 —
    ``(name, entity_type)``. 검색-인덱싱 정렬의 핵심.
    """

    name: str
    entity_type: str = ""


@dataclass
class TargetRelation:
    """검색 LLM 이 식별한 정답 후보 관계.

    인덱싱 측 ``Relation`` (graph_extractor.Relation) 과 같은 형태 —
    ``(source, target, relation_type)``. **방향성** 을 보존하여 인덱싱
    시점의 directed edge 와 정확히 비교 가능.

    끝점이 미상이면 빈 문자열 (``source=""`` 또는 ``target=""``) 허용 —
    실행 시 그래프에서 fuzzy 매칭으로 채움.
    """

    source: str
    target: str
    relation_type: str = ""


@dataclass
class GraphSearchPlan:
    """LLM이 생성한 그래프 탐색 계획.

    R3 1차 표현: ``target_entities`` + ``target_relations`` 로 인덱싱 측과
    같은 어휘/방향성을 사용한다. ``search_steps`` 는 R2 이하 호환을 위해
    유지되며, target_* 가 채워져 있으면 그것이 우선 사용된다.
    """

    should_search: bool
    reasoning: str = ""
    target_entities: list[TargetEntity] = field(default_factory=list)
    target_relations: list[TargetRelation] = field(default_factory=list)
    search_steps: list[SearchStep] = field(default_factory=list)

    @property
    def has_targets(self) -> bool:
        """R3 신규 target_* 가 채워졌는가."""
        return bool(self.target_entities or self.target_relations)


@dataclass
class GraphSearchResult:
    """그래프 탐색 결과.

    ``entities`` 의 각 ``GraphEntityRef`` 에는 노드 ``properties`` JSON 의
    ``description`` 필드가 채워진다 (2차 — 평가 측 tiered matching 의 T4
    embedding 단계가 자연어 evidence 를 비교할 수 있도록).

    ``relations`` 는 ``--score-relations`` 평가용으로 노출되는 1-hop
    엣지 정보 (2차 — 관계 채점 활성화 시).
    """

    text: str
    document_ids: set[int] = field(default_factory=set)
    entities: list[GraphEntityRef] = field(default_factory=list)
    relations: list[GraphRelationRef] = field(default_factory=list)


async def plan_graph_search(
    query: str,
    graph_store: GraphStore,
    llm_client: LLMClient,
    *,
    query_embedding: list[float] | None = None,
    seed: int | None = None,
) -> GraphSearchPlan:
    """LLM을 사용하여 그래프 탐색 계획을 생성한다.

    query_embedding이 제공되고 엔티티 임베딩이 구축되어 있으면
    쿼리와 관련된 서브그래프 스키마만 LLM에게 제공한다.

    Args:
        query: 사용자 질의.
        graph_store: 그래프 저장소.
        llm_client: LLM 클라이언트.
        query_embedding: 쿼리 텍스트의 임베딩 벡터. None이면 전체 스키마 사용.
        seed: LLM 호출 seed. None이면 미전달(기존 동작). 평가 재현성을 위해
            호출부에서 쿼리 기반 결정적 seed 를 주입한다. seed 미지원 백엔드
            (예: Anthropic)에서는 무시된다.

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
        # 그래프 탐색 계획 JSON 은 일반적으로 짧지만, reasoning 모델은 thinking
        # 예산을 응답 토큰으로 잡아먹어 max_tokens 가 작으면 JSON 이 잘려 파싱
        # 실패한다. 모델 한도 범위 안에서 큰 값을 두어 잘림을 방지.
        complete_kwargs: dict[str, Any] = {
            "system": _render_system_prompt(),
            "max_tokens": 32768,
            "temperature": 0.0,
            "reasoning_mode": "off",
            "purpose": "graph_search_planner",
        }
        if seed is not None:
            complete_kwargs["seed"] = int(seed)
        response = await llm_client.complete(prompt, **complete_kwargs)
        plan_data = extract_json(response)
    except Exception:
        logger.warning("그래프 탐색 계획 생성 실패", exc_info=True)
        return GraphSearchPlan(should_search=False, reasoning="계획 생성 실패")

    return _parse_plan(plan_data)


def _parse_plan(data: Any) -> GraphSearchPlan:
    """LLM 응답 JSON을 GraphSearchPlan으로 파싱한다.

    R3 1차 신호: ``target_entities`` / ``target_relations``. 둘 중 하나라도
    채워져 있으면 R3 모드. 없으면 R2 이하 ``search_steps`` 경로로 fallback.
    """
    if not isinstance(data, dict):
        return GraphSearchPlan(should_search=False, reasoning="응답 파싱 실패")

    should_search = bool(data.get("should_search", False))
    reasoning = str(data.get("reasoning", ""))

    # R3 — target_entities / target_relations 파싱
    target_entities: list[TargetEntity] = []
    for te_data in data.get("target_entities", [])[:5]:
        if not isinstance(te_data, dict):
            continue
        name = str(te_data.get("name", "")).strip()
        if not name:
            continue
        etype = str(te_data.get("type", "")).strip()
        target_entities.append(TargetEntity(name=name, entity_type=etype))

    target_relations: list[TargetRelation] = []
    for tr_data in data.get("target_relations", [])[:5]:
        if not isinstance(tr_data, dict):
            continue
        src = str(tr_data.get("source", "")).strip()
        tgt = str(tr_data.get("target", "")).strip()
        rtype = str(
            tr_data.get("relation_type") or tr_data.get("type") or "",
        ).strip()
        # 적어도 한쪽 끝점과 relation_type 둘 중 하나는 있어야 의미 있는 쿼리
        if not (src or tgt) and not rtype:
            continue
        target_relations.append(TargetRelation(
            source=src, target=tgt, relation_type=rtype,
        ))

    # 후방 호환: 구식 search_steps
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
        target_entities=target_entities,
        target_relations=target_relations,
        search_steps=steps,
    )


async def _build_seed_embeddings(
    plan: GraphSearchPlan,
    embedding_client: Any,
) -> dict[str, list[float]]:
    """탐색 계획의 시드 이름들을 한 번의 배치 호출로 임베딩해 name→vector 맵을 만든다.

    ``target_entities`` / ``target_relations`` 끝점 / ``search_steps`` 의 이름을
    모아 중복 제거 후 ``aembed_documents`` 1회로 임베딩한다. 이전에는 시드마다
    개별 ``aembed_query`` 를 순차 await 하여 검색 1건이 임베딩 HTTP 를 10여 회
    발생시켰다(전역 동시성 세마포어를 그만큼 점유).

    임베딩 입력은 호출부 lookup 키와 동일하도록 원본 이름을 그대로 쓴다(strip
    하지 않음 — 기존 ``_maybe_embed`` 가 원본 텍스트를 임베딩하던 것과 동일).
    ``embedding_client`` 가 없거나 호출이 실패하면 빈 맵을 반환한다(= fallback
    없이 표면 매칭에만 의존하는 graceful 동작, 기존 예외 처리와 동일).
    """
    if embedding_client is None:
        return {}

    seen: set[str] = set()
    texts: list[str] = []

    def _queue(text: str) -> None:
        if text and text not in seen:
            seen.add(text)
            texts.append(text)

    for te in plan.target_entities:
        _queue(te.name)
    for tr in plan.target_relations:
        _queue(tr.source)
        _queue(tr.target)
    for step in plan.search_steps:
        _queue(step.entity_name)

    if not texts:
        return {}

    try:
        embeddings = await embedding_client.aembed_documents(texts)
    except Exception:
        logger.debug("그래프 시드 일괄 임베딩 실패 — fallback 없이 진행", exc_info=True)
        return {}
    return dict(zip(texts, embeddings))


async def execute_graph_search(
    plan: GraphSearchPlan,
    graph_store: GraphStore,
    *,
    query_embedding: list[float] | None = None,
    embedding_client: Any = None,
) -> GraphSearchResult | None:
    """탐색 계획에 따라 그래프를 탐색하고 결과를 포맷팅한다.

    LLM 추측 entity_name 이 인덱스의 표기와 다른 경우(공백/케이스/하이픈 등)
    표면 매칭이 모두 실패하면 시드 노드가 0개가 되어 retrieved 결과가
    빈다 — 그래프 메트릭 0% 의 주된 원인. 이를 완화하기 위해 세 단계
    fallback 을 도입한다 (R2):

    1. step 별: ``get_neighbors`` 가 표면 매칭 실패 시, step.entity_name 의
       임베딩(``embedding_client`` 가 있으면 즉시 계산)으로 가장 가까운 노드를
       시드로 사용.
    2. **R2 — always-on 시드 보강**: search_steps 이 일부 성공해도(LLM 이
       sink 이웃을 시드로 선택해 retrieved 가 sink 자신만 담는 케이스가 잦음)
       ``query_embedding`` 으로 top-k 유사 노드를 항상 union 보강. 임계값을
       다소 보수(0.6)로 잡아 noise 통제.
    3. 전체 step 이 모두 실패해도 ``query_embedding`` 이 제공되면 그것으로
       가장 가까운 노드들을 시드로 추가 — LLM 계획이 완전히 빗나가도 의미
       유사도로 회복.

    Args:
        plan: LLM이 생성한 탐색 계획.
        graph_store: 그래프 저장소.
        query_embedding: 쿼리 임베딩. 시드 보강(2)과 폴백(3)에 사용.
        embedding_client: step 별 임베딩 fallback 에 사용 (없으면 step 별
            fallback 만 skip; 전체 fallback 은 query_embedding 으로 가능).

    Returns:
        그래프 탐색 결과(텍스트 + 관련 document_id). 결과가 없으면 None.
    """
    if not plan.should_search:
        return None
    # R3 1차 신호 (target_*) 가 없고 R2 이하 search_steps 도 없으면 종료.
    if not plan.has_targets and not plan.search_steps:
        return None

    all_nodes: list[dict[str, Any]] = []
    all_node_ids: set[int] = set()
    # priority_order: target_entities 를 우선적으로 retrieved 의 앞순위에 배치
    # — MRR/NDCG 가 rank 민감하므로, LLM 이 식별한 정답 후보가 retrieved 의
    # rank-1 로 들어가도록 보장.
    priority_node_ids: list[int] = []
    searched_entities: list[str] = []

    # 시드 fallback 임베딩을 한 번의 배치 호출로 미리 계산 (name → vector).
    # get_neighbors 는 표면 매칭이 성공하면 이 fallback 을 쓰지 않으므로,
    # 미리 전부 임베딩해도 매칭 결과는 동일하고 임베딩 호출 수만 1회로 준다.
    emb_map = await _build_seed_embeddings(plan, embedding_client)

    priority_set: set[int] = set()

    def _add_node_to_result(nid: int, node_data: dict[str, Any], priority: bool) -> None:
        # 신규 추가
        if nid not in all_node_ids:
            all_node_ids.add(nid)
            all_nodes.append(node_data)
        # priority 상태는 한 번 True 가 되면 유지 — 같은 노드가 비-우선으로
        # 먼저 들어왔어도 후속 priority=True 호출로 승격될 수 있도록 한다.
        if priority and nid not in priority_set:
            priority_set.add(nid)
            priority_node_ids.append(nid)

    # R3 — target_entities / target_relations 우선 처리.
    if plan.has_targets:
        # 1) target_entities: 각 정답 후보를 그래프에서 찾고 우선 시드로 추가.
        for te in plan.target_entities:
            te_emb = emb_map.get(te.name)
            neighbors = graph_store.get_neighbors(
                te.name, depth=1, embedding_fallback=te_emb,
            )
            if not neighbors:
                continue
            searched_entities.append(te.name)
            # 시드 자기 자신을 priority — gold 의 relevant_graph_entities 가
            # 정확히 이 시드와 매칭되므로 rank-1 보장이 목표.
            for n in neighbors:
                nid = n.get("id")
                if nid is None:
                    continue
                is_seed = (
                    str(n.get("entity_name", "")).strip().lower()
                    == te.name.strip().lower()
                )
                _add_node_to_result(nid, n, priority=is_seed)

        # 2) target_relations: 양쪽 끝점을 시드로 추가 + 실제 edge 있으면 부스트.
        for tr in plan.target_relations:
            for endpoint in (tr.source, tr.target):
                if not endpoint:
                    continue
                ep_emb = emb_map.get(endpoint)
                neighbors = graph_store.get_neighbors(
                    endpoint, depth=1, embedding_fallback=ep_emb,
                )
                if not neighbors:
                    continue
                searched_entities.append(endpoint)
                for n in neighbors:
                    nid = n.get("id")
                    if nid is None:
                        continue
                    is_endpoint = (
                        str(n.get("entity_name", "")).strip().lower()
                        == endpoint.strip().lower()
                    )
                    # 관계 끝점은 priority — gold 가 관계를 채점하면 source/
                    # target 양쪽이 retrieved 의 앞순위에 있어야 한다.
                    _add_node_to_result(nid, n, priority=is_endpoint)

    # R2 호환 — search_steps 경로 (LLM 이 구식 응답을 돌려준 경우 / 호출자가
    # 직접 search_steps 만 채운 경우). R3 target_* 가 있어도 추가 시드 보강
    # 으로 활용 가능.
    for step in plan.search_steps:
        step_emb = emb_map.get(step.entity_name)
        neighbors = graph_store.get_neighbors(
            step.entity_name,
            depth=step.depth,
            embedding_fallback=step_emb,
        )
        if not neighbors:
            continue
        searched_entities.append(step.entity_name)
        for n in neighbors:
            nid = n.get("id")
            if nid is None:
                continue
            is_seed = (
                str(n.get("entity_name", "")).strip().lower()
                == step.entity_name.strip().lower()
            )
            _add_node_to_result(nid, n, priority=is_seed)

    # R2 — always-on query embedding 시드 보강.
    # LLM 이 질의에 명시된 sink 이웃(예: KakaoPay, Elasticsearch) 을 search step
    # entity_name 으로 선택하면 retrieved 가 sink 자신만 담겨 gold seed 누락
    # (양방향 traversal 도입 후에도 LLM 선택의 잡음을 보완). 임계값 0.6 으로
    # 보수 — 의미 무관 노드 유입 최소화.
    if query_embedding is not None:
        boost = graph_store.search_entities_by_embedding(
            query_embedding, threshold=0.6, top_k=3,
        )
        new_boost = 0
        for s in boost:
            nid = s.get("node_id")
            if nid is None or nid in all_node_ids:
                continue
            reachable = graph_store.get_neighbors_from_node_id(nid, depth=1)
            for n in reachable:
                n_nid = n.get("id")
                if n_nid is None:
                    continue
                if n_nid not in all_node_ids:
                    _add_node_to_result(n_nid, n, priority=False)
                    new_boost += 1
        if new_boost:
            logger.debug(
                "그래프 탐색 — query 임베딩 always-on 시드 보강 (+%d nodes)",
                new_boost,
            )
            if not searched_entities:
                searched_entities.append("(query-embedding fallback)")

    # 전체 step + 보강 모두 실패한 경우 임계값 더 낮춰 마지막 폴백.
    if not all_nodes and query_embedding is not None:
        similar = graph_store.search_entities_by_embedding(
            query_embedding, threshold=0.5, top_k=5,
        )
        if similar:
            logger.info(
                "그래프 탐색 — 모든 step 실패, query 임베딩 시드 폴백 (n=%d)",
                len(similar),
            )
            for s in similar:
                nid = s.get("node_id")
                if nid is None:
                    continue
                reachable = graph_store.get_neighbors_from_node_id(nid, depth=1)
                for n in reachable:
                    n_nid = n.get("id")
                    if n_nid is None:
                        continue
                    _add_node_to_result(n_nid, n, priority=False)
            if all_nodes:
                searched_entities.append("(query-embedding fallback)")

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

    # 평가용 (entity_name, entity_type) 페어 노출 — `GoldItem.relevant_graph_entities`
    # 와 동일 키로 비교 가능하도록 dataclass 외부에 채워준다.
    # 2차: description 도 채워서 tiered matching T4 (embedding) 이 자연어
    # evidence 를 비교할 수 있게 한다.
    # R2 (F-METRIC-R2-01): description 자연어 fallback 의 비특이성 문제를
    # 줄이기 위해, node 의 1-hop 관계를 자연어 문장으로 풀어쓴다 — 평가 측의
    # gold evidence_description 과 의미적으로 더 비교 가능.
    id_to_meta: dict[int, tuple[str, str]] = {
        n["id"]: (
            str(n.get("entity_name", "")),
            str(n.get("entity_type", "")),
        )
        for n in all_nodes
    }
    out_rels: dict[int, list[tuple[str, str]]] = {nid: [] for nid in id_to_meta}
    in_rels: dict[int, list[tuple[str, str]]] = {nid: [] for nid in id_to_meta}
    for edge in edges:
        src_id = edge.get("source")
        tgt_id = edge.get("target")
        rel = str(edge.get("relation_type", "관련"))
        if src_id in id_to_meta and tgt_id in id_to_meta:
            tgt_name = id_to_meta[tgt_id][0]
            src_name = id_to_meta[src_id][0]
            out_rels.setdefault(src_id, []).append((rel, tgt_name))
            in_rels.setdefault(tgt_id, []).append((rel, src_name))

    def _natural_description(
        node_id: int, name: str, etype: str,
    ) -> str:
        outs = out_rels.get(node_id, [])
        ins = in_rels.get(node_id, [])
        parts: list[str] = []
        # 최대 3개씩 — context 길이 통제.
        for rel, tgt in outs[:3]:
            parts.append(f"{tgt} 에 대해 {rel}")
        for rel, src in ins[:3]:
            parts.append(f"{src} 가(이) {rel}")
        if parts:
            type_hint = f"{etype} 유형의 " if etype else ""
            joined = ", ".join(parts)
            return f"{type_hint}'{name}' 은(는) {joined} 관계를 가진다."
        # 관계가 없는 단독 노드 — 기존 자연어 fallback 유지.
        return (
            f"이 entity 는 {etype} 유형의 '{name}' 이며 그래프 노드로 등록되어 있다."
            if etype
            else f"이 entity 는 '{name}' 이며 그래프 노드로 등록되어 있다."
        )

    # R3 — retrieved entity 순서를 priority 우선으로 정렬.
    # LLM 이 식별한 target_entities / target_relations 끝점이 retrieved 의
    # 앞순위에 오도록 — gold 의 relevant_graph_entities 가 정확히 이 노드와
    # 매칭되므로 rank-1 hit 으로 MRR/NDCG 가 회복된다.
    nodes_by_id: dict[int, dict[str, Any]] = {n["id"]: n for n in all_nodes if "id" in n}
    nodes_ordered: list[dict[str, Any]] = []
    # 1) priority 노드를 등록 순서대로
    for nid in priority_node_ids:
        node = nodes_by_id.get(nid)
        if node is not None:
            nodes_ordered.append(node)
    # 2) 나머지 노드를 all_nodes 의 발견 순서대로
    for node in all_nodes:
        nid = node.get("id")
        if nid in priority_set:
            continue
        nodes_ordered.append(node)

    entities: list[GraphEntityRef] = []
    seen_pairs: set[tuple[str, str]] = set()
    for node in nodes_ordered:
        name = str(node.get("entity_name", ""))
        etype = str(node.get("entity_type", ""))
        if not name:
            continue
        key = (name.lower(), etype)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        node_props = node.get("properties") or {}
        description = ""
        if isinstance(node_props, dict):
            description = str(node_props.get("description") or "")
        # description 이 비어 있으면 1-hop 관계 요약으로 채움 — T4 매칭의
        # 의미 임베딩이 짧은 이름끼리 비교 시 비특이적이 되는 funnel 손실을
        # 줄인다. R1 의 metadata-스타일 보일러플레이트보다 더 의미적.
        if not description:
            node_id = node.get("id")
            description = _natural_description(node_id, name, etype)
        entities.append(GraphEntityRef(
            name=name,
            type=etype,
            description=description,
        ))

    # 2차: 관계 채점 (`--score-relations`) 을 위해 검색된 edges 도 노출.
    id_to_name_map = {n["id"]: n.get("entity_name", "") for n in all_nodes}
    id_to_type_map = {n["id"]: n.get("entity_type", "") for n in all_nodes}
    relations: list[GraphRelationRef] = []
    seen_rel_keys: set[tuple[str, str, str]] = set()
    for edge in edges:
        src = str(id_to_name_map.get(edge.get("source"), ""))
        tgt = str(id_to_name_map.get(edge.get("target"), ""))
        rel = str(edge.get("relation_type", ""))
        if not (src and tgt):
            continue
        rel_key = (src.lower(), tgt.lower(), rel)
        if rel_key in seen_rel_keys:
            continue
        seen_rel_keys.add(rel_key)
        # 관계의 description 은 edge properties 의 label 또는 빈 문자열.
        edge_props = edge.get("properties") or {}
        label = ""
        if isinstance(edge_props, dict):
            label = str(edge_props.get("label") or "")
        # 자연어 evidence — type 명 변경 robust 매칭에 사용.
        _ = id_to_type_map  # 사용 의도 유지 (현재는 타입 필요 없음)
        rel_description = label or f"{src} {rel} {tgt}"
        relations.append(GraphRelationRef(
            source_name=src,
            target_name=tgt,
            relation_type=rel,
            description=rel_description,
        ))

    return GraphSearchResult(
        text="\n".join(lines),
        document_ids=document_ids,
        entities=entities,
        relations=relations,
    )
