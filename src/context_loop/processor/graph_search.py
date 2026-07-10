"""임베딩 시딩 기반 그래프 검색.

쿼리 임베딩으로 그래프 엔티티를 직접 시딩하는 최소 구성의 그래프 검색:

    쿼리 임베딩 → search_entities_by_embedding(threshold, top_k)
    → 각 시드 1-hop 확장 → 노드 집합 내부 엣지 수집
    → 텍스트 포맷 + document_ids

LLM 호출 0회, fallback 0층으로 결정적으로 동작한다. 이전의 LLM 플래너
(plan_graph_search/execute_graph_search) 는 시드 이름 임베딩 fallback /
always-on 보강 / 최후 폴백의 3층 패치가 전부 "임베딩 검색으로 수렴"하는
구조였고, 그 수렴점 하나만 남긴 것이 이 모듈이다.

"그래프와 무관한 질문" 게이팅은 LLM should_search 판단 대신
"threshold 를 넘는 시드 엔티티가 없으면 그래프 섹션 생략"이 대체한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from context_loop.eval.gold_set import GraphEntityRef, GraphRelationRef
from context_loop.storage.graph_store import GraphStore

logger = logging.getLogger(__name__)

# 시드 엔티티 선정 기준. 짧은 엔티티 이름 vs 문장형 쿼리의 임베딩 비대칭을
# 고려해 보수적이지 않은 0.5 로 시작한다 (이전 최후 폴백과 동일 값).
DEFAULT_SEED_THRESHOLD = 0.5
DEFAULT_SEED_TOP_K = 5


@dataclass
class GraphSearchResult:
    """그래프 탐색 결과.

    ``entities`` 의 각 ``GraphEntityRef`` 에는 노드 ``properties`` JSON 의
    ``description`` 필드가 채워진다 (평가 측 tiered matching 의 T4
    embedding 단계가 자연어 evidence 를 비교할 수 있도록).

    ``relations`` 는 ``--score-relations`` 평가용으로 노출되는 1-hop
    엣지 정보.
    """

    text: str
    document_ids: set[int] = field(default_factory=set)
    entities: list[GraphEntityRef] = field(default_factory=list)
    relations: list[GraphRelationRef] = field(default_factory=list)


async def search_graph(
    query_embedding: list[float] | None,
    graph_store: GraphStore,
    *,
    embedding_client: Any = None,
    seed_threshold: float = DEFAULT_SEED_THRESHOLD,
    seed_top_k: int = DEFAULT_SEED_TOP_K,
) -> GraphSearchResult | None:
    """쿼리 임베딩으로 그래프를 시딩·탐색하고 결과를 포맷팅한다.

    Args:
        query_embedding: 쿼리 임베딩 벡터. None 이면 탐색 없이 None 반환.
        graph_store: 그래프 저장소.
        embedding_client: 엔티티 임베딩 lazy 보완용 (aembed_documents).
            None 이면 이미 캐시된 엔티티 임베딩만으로 시딩한다.
        seed_threshold: 시드 엔티티의 cosine 유사도 최소값. 이 값을 넘는
            엔티티가 없으면 그래프와 무관한 질문으로 보고 None 을 반환한다.
        seed_top_k: 시드 엔티티 최대 개수.

    Returns:
        그래프 탐색 결과(텍스트 + 관련 document_id + 평가용 entities/
        relations). 시드가 없으면 None.
    """
    if query_embedding is None:
        return None
    if graph_store.stats()["nodes"] == 0:
        return None

    # 엔티티 임베딩 자동 구축 — 시딩의 전제. 캐시가 비었을 때뿐 아니라 아직
    # 임베딩되지 않은 엔티티가 남아 있을 때도 호출해, 기동 사전 구축에서
    # 부분 실패한 노드가 다음 검색 때 점진적으로 보완되게 한다(누락분만 재시도).
    if embedding_client is not None and graph_store.unembedded_entity_count > 0:
        await graph_store.build_entity_embeddings(embedding_client)

    seeds = graph_store.search_entities_by_embedding(
        query_embedding, threshold=seed_threshold, top_k=seed_top_k,
    )
    if not seeds:
        logger.debug(
            "그래프 탐색 생략 — threshold %.2f 를 넘는 시드 엔티티 없음",
            seed_threshold,
        )
        return None

    # 시드(유사도 내림차순)를 먼저, 1-hop 이웃을 그 뒤에 배치한다.
    # entities 순서가 평가(MRR/NDCG)의 rank 로 쓰이므로 쿼리와 가장 유사한
    # 노드가 rank-1 이 되도록 한다.
    all_nodes: list[dict[str, Any]] = []
    all_node_ids: set[int] = set()
    seed_ids: set[int] = set()
    neighbors_by_seed: list[list[dict[str, Any]]] = []

    def _add_node(node: dict[str, Any]) -> None:
        nid = node.get("id")
        if nid is None or nid in all_node_ids:
            return
        all_node_ids.add(nid)
        all_nodes.append(node)

    for s in seeds:
        nid = s.get("node_id")
        if nid is None:
            continue
        reachable = graph_store.get_neighbors_from_node_id(nid, depth=1)
        if not reachable:
            continue
        seed_ids.add(nid)
        for n in reachable:
            if n.get("id") == nid:
                _add_node(n)
        neighbors_by_seed.append(reachable)
    for reachable in neighbors_by_seed:
        for n in reachable:
            _add_node(n)

    if not all_nodes:
        return None

    edges = graph_store.get_edges_between(list(all_node_ids))

    # 관련 document_id 수집 (정규 노드는 document_ids set 을 가짐)
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

    text = _format_text(all_nodes, edges, seed_ids)
    entities = _build_entity_refs(all_nodes, edges)
    relations = _build_relation_refs(all_nodes, edges)

    return GraphSearchResult(
        text=text,
        document_ids=document_ids,
        entities=entities,
        relations=relations,
    )


def _format_text(
    all_nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    seed_ids: set[int],
) -> str:
    """탐색된 노드/엣지를 컨텍스트 텍스트로 포맷팅한다."""
    lines = ["## 관련 그래프 컨텍스트"]

    lines.append("\n**엔티티:**")
    for node in all_nodes:
        name = node.get("entity_name", "")
        etype = node.get("entity_type", "")
        marker = " *" if node.get("id") in seed_ids else ""
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

    return "\n".join(lines)


def _build_entity_refs(
    all_nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[GraphEntityRef]:
    """평가용 (entity_name, entity_type, description) 페어를 만든다.

    ``GoldItem.relevant_graph_entities`` 와 동일 키로 비교 가능하도록
    노출한다. description 이 비어 있으면 1-hop 관계를 자연어 문장으로
    풀어쓴다 — 평가 측 tiered matching T4 (embedding) 가 짧은 이름끼리
    비교할 때의 비특이성을 줄이고, gold 의 evidence_description 과
    의미적으로 비교 가능하게 한다.
    """
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

    def _natural_description(node_id: int, name: str, etype: str) -> str:
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
        # 관계가 없는 단독 노드 — 자연어 fallback.
        return (
            f"이 entity 는 {etype} 유형의 '{name}' 이며 그래프 노드로 등록되어 있다."
            if etype
            else f"이 entity 는 '{name}' 이며 그래프 노드로 등록되어 있다."
        )

    entities: list[GraphEntityRef] = []
    seen_pairs: set[tuple[str, str]] = set()
    for node in all_nodes:
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
        if not description:
            description = _natural_description(node.get("id"), name, etype)
        entities.append(GraphEntityRef(
            name=name,
            type=etype,
            description=description,
        ))
    return entities


def _build_relation_refs(
    all_nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> list[GraphRelationRef]:
    """관계 채점 (``--score-relations``) 을 위해 검색된 edges 를 노출한다."""
    id_to_name_map = {n["id"]: n.get("entity_name", "") for n in all_nodes}
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
        # 관계의 description 은 edge properties 의 label 또는 자연어 조합 —
        # type 명 변경에 robust 한 매칭에 사용.
        edge_props = edge.get("properties") or {}
        label = ""
        if isinstance(edge_props, dict):
            label = str(edge_props.get("label") or "")
        rel_description = label or f"{src} {rel} {tgt}"
        relations.append(GraphRelationRef(
            source_name=src,
            target_name=tgt,
            relation_type=rel,
            description=rel_description,
        ))
    return relations
