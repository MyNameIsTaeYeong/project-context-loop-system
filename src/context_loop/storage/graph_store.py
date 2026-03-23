"""그래프 저장소 — NetworkX + SQLite.

NetworkX로 인메모리 그래프를 관리하고,
SQLite(MetadataStore)에 영속 저장한다.
엔티티 병합 및 고아 엣지 정리 로직을 포함한다.
LLM 기반 탐색을 위한 그래프 스키마 요약을 제공한다.
"""

from __future__ import annotations

import json
import logging
import math
from collections import Counter
from typing import Any

import networkx as nx

from context_loop.processor.graph_extractor import GraphData
from context_loop.storage.metadata_store import MetadataStore

logger = logging.getLogger(__name__)


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """두 벡터의 코사인 유사도를 계산한다."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class GraphStore:
    """NetworkX + SQLite 기반 그래프 저장소.

    SQLite(MetadataStore)가 진실의 원천(source of truth)이다.
    NetworkX 그래프는 검색/탐색용 인메모리 인덱스 역할을 한다.
    LLM 기반 그래프 탐색을 위한 스키마 요약을 제공한다.

    Args:
        store: 초기화된 MetadataStore 인스턴스.
    """

    def __init__(self, store: MetadataStore) -> None:
        self._store = store
        self._graph: nx.DiGraph = nx.DiGraph()
        # 엔티티 임베딩 캐시: node_id → (entity_name, embedding)
        self._entity_embeddings: dict[int, tuple[str, list[float]]] = {}

    async def load_from_db(self) -> None:
        """SQLite에서 그래프 데이터를 로드하여 NetworkX 그래프를 재구성한다."""
        self._graph = nx.DiGraph()
        # 모든 문서의 노드/엣지 로드
        docs = await self._store.list_documents()
        for doc in docs:
            nodes = await self._store.get_graph_nodes_by_document(doc["id"])
            for node in nodes:
                self._graph.add_node(
                    node["id"],
                    entity_name=node["entity_name"],
                    entity_type=node.get("entity_type", "other"),
                    document_id=node["document_id"],
                    properties=json.loads(node["properties"] or "{}"),
                )
            edges = await self._store.get_graph_edges_by_document(doc["id"])
            for edge in edges:
                self._graph.add_edge(
                    edge["source_node_id"],
                    edge["target_node_id"],
                    id=edge["id"],
                    relation_type=edge.get("relation_type", "related_to"),
                    document_id=edge["document_id"],
                    properties=json.loads(edge["properties"] or "{}"),
                )
        self._entity_embeddings.clear()
        logger.debug(
            "그래프 로드 완료 — 노드: %d, 엣지: %d",
            self._graph.number_of_nodes(),
            self._graph.number_of_edges(),
        )

    async def save_graph_data(
        self,
        document_id: int,
        graph_data: GraphData,
    ) -> dict[str, int]:
        """GraphData를 SQLite에 저장하고 NetworkX 그래프에 추가한다.

        동일 엔티티 병합(entity_name + entity_type 기준)을 처리한다.
        이미 다른 문서에 같은 이름의 엔티티가 있으면 별도 노드로 생성하되
        entity_merges 테이블 없이 NetworkX 상에서 논리적으로 병합한다.

        Args:
            document_id: 저장할 문서 ID.
            graph_data: 추출된 그래프 데이터.

        Returns:
            {"nodes": 생성된 노드 수, "edges": 생성된 엣지 수} 딕셔너리.
        """
        # 엔티티 이름 → 노드 DB ID 매핑 (이 문서에서 생성된 노드)
        name_to_node_id: dict[str, int] = {}

        for entity in graph_data.entities:
            props = {"description": entity.description} if entity.description else {}
            node_id = await self._store.create_graph_node(
                document_id=document_id,
                entity_name=entity.name,
                entity_type=entity.entity_type,
                properties=json.dumps(props, ensure_ascii=False),
            )
            name_to_node_id[entity.name] = node_id
            self._graph.add_node(
                node_id,
                entity_name=entity.name,
                entity_type=entity.entity_type,
                document_id=document_id,
                properties=props,
            )

        edge_count = 0
        for relation in graph_data.relations:
            src_id = name_to_node_id.get(relation.source)
            tgt_id = name_to_node_id.get(relation.target)
            if src_id is None or tgt_id is None:
                logger.debug(
                    "관계 스킵 (엔티티 없음): %s → %s",
                    relation.source,
                    relation.target,
                )
                continue
            props = {"label": relation.label} if relation.label else {}
            edge_id = await self._store.create_graph_edge(
                document_id=document_id,
                source_node_id=src_id,
                target_node_id=tgt_id,
                relation_type=relation.relation_type,
                properties=json.dumps(props, ensure_ascii=False),
            )
            self._graph.add_edge(
                src_id,
                tgt_id,
                id=edge_id,
                relation_type=relation.relation_type,
                document_id=document_id,
                properties=props,
            )
            edge_count += 1

        # 새 노드의 임베딩 캐시 무효화 (다음 build_entity_embeddings 호출 시 재생성)
        for name, nid in name_to_node_id.items():
            self._entity_embeddings.pop(nid, None)

        logger.info(
            "그래프 저장 완료 — document_id=%d, 노드: %d, 엣지: %d",
            document_id,
            len(name_to_node_id),
            edge_count,
        )
        return {"nodes": len(name_to_node_id), "edges": edge_count}

    async def delete_document_graph(self, document_id: int) -> None:
        """문서의 그래프 데이터를 삭제하고 고아 엣지를 정리한다.

        Args:
            document_id: 삭제할 문서 ID.
        """
        # SQLite에서 삭제 (CASCADE로 엣지도 삭제됨)
        await self._store.delete_graph_data_by_document(document_id)

        # NetworkX에서 해당 문서 노드/엣지 제거
        nodes_to_remove = [
            n for n, d in self._graph.nodes(data=True)
            if d.get("document_id") == document_id
        ]
        self._graph.remove_nodes_from(nodes_to_remove)

        # 고아 엣지 정리 (양쪽 노드가 없는 엣지)
        orphan_edges = [
            (u, v) for u, v in self._graph.edges()
            if not self._graph.has_node(u) or not self._graph.has_node(v)
        ]
        self._graph.remove_edges_from(orphan_edges)

        # 삭제된 노드의 임베딩 캐시도 제거
        for nid in nodes_to_remove:
            self._entity_embeddings.pop(nid, None)

        logger.debug("그래프 삭제 완료: document_id=%d", document_id)

    def get_neighbors(
        self,
        entity_name: str,
        depth: int = 1,
    ) -> list[dict[str, Any]]:
        """엔티티 이름을 중심으로 주변 관계를 탐색한다.

        Args:
            entity_name: 탐색 중심 엔티티 이름.
            depth: 탐색 깊이 (1 = 직접 연결만).

        Returns:
            관련 노드 정보 목록.
        """
        # entity_name으로 노드 ID 찾기
        center_nodes = [
            n for n, d in self._graph.nodes(data=True)
            if d.get("entity_name", "").lower() == entity_name.lower()
        ]
        if not center_nodes:
            return []

        result_nodes: dict[int, dict[str, Any]] = {}
        for center in center_nodes:
            reachable = nx.single_source_shortest_path_length(
                self._graph, center, cutoff=depth
            )
            for node_id in reachable:
                if node_id not in result_nodes:
                    data = dict(self._graph.nodes[node_id])
                    data["id"] = node_id
                    result_nodes[node_id] = data

        return list(result_nodes.values())

    def get_edges_between(
        self,
        node_ids: list[int],
    ) -> list[dict[str, Any]]:
        """주어진 노드 집합 사이의 엣지를 반환한다.

        Args:
            node_ids: 노드 ID 목록.

        Returns:
            엣지 정보 목록.
        """
        node_set = set(node_ids)
        result = []
        for u, v, data in self._graph.edges(data=True):
            if u in node_set and v in node_set:
                result.append({
                    "source": u,
                    "target": v,
                    **data,
                })
        return result

    @property
    def graph(self) -> nx.DiGraph:
        """내부 NetworkX 그래프 (읽기 전용 접근)."""
        return self._graph

    def stats(self) -> dict[str, int]:
        """그래프 통계를 반환한다."""
        return {
            "nodes": self._graph.number_of_nodes(),
            "edges": self._graph.number_of_edges(),
        }

    # --- LLM 기반 그래프 탐색 지원 ---

    def get_schema_summary(self, max_entities_per_type: int = 10) -> dict[str, Any]:
        """LLM에게 제공할 그래프 스키마 요약을 생성한다.

        그래프의 구조(엔티티 유형, 관계 유형, 대표 엔티티 목록)를
        LLM이 탐색 계획을 세울 수 있는 형태로 요약한다.

        Args:
            max_entities_per_type: 유형별 최대 엔티티 표시 수.

        Returns:
            {
                "total_nodes": int,
                "total_edges": int,
                "entity_types": {"type": count, ...},
                "relation_types": {"type": count, ...},
                "entities_by_type": {"type": ["name1", "name2", ...], ...},
                "sample_relations": [{"source", "target", "type"}, ...]
            }
        """
        if self._graph.number_of_nodes() == 0:
            return {
                "total_nodes": 0,
                "total_edges": 0,
                "entity_types": {},
                "relation_types": {},
                "entities_by_type": {},
                "sample_relations": [],
            }

        # 엔티티 유형별 집계
        type_counter: Counter[str] = Counter()
        entities_by_type: dict[str, list[str]] = {}
        for _, data in self._graph.nodes(data=True):
            etype = data.get("entity_type", "other")
            ename = data.get("entity_name", "")
            type_counter[etype] += 1
            if etype not in entities_by_type:
                entities_by_type[etype] = []
            if len(entities_by_type[etype]) < max_entities_per_type:
                entities_by_type[etype].append(ename)

        # 관계 유형별 집계
        rel_counter: Counter[str] = Counter()
        sample_relations: list[dict[str, str]] = []
        for u, v, data in self._graph.edges(data=True):
            rtype = data.get("relation_type", "related_to")
            rel_counter[rtype] += 1
            if len(sample_relations) < 15:
                src_name = self._graph.nodes[u].get("entity_name", str(u))
                tgt_name = self._graph.nodes[v].get("entity_name", str(v))
                sample_relations.append({
                    "source": src_name,
                    "target": tgt_name,
                    "type": rtype,
                })

        return {
            "total_nodes": self._graph.number_of_nodes(),
            "total_edges": self._graph.number_of_edges(),
            "entity_types": dict(type_counter.most_common()),
            "relation_types": dict(rel_counter.most_common()),
            "entities_by_type": entities_by_type,
            "sample_relations": sample_relations,
        }

    def format_schema_for_llm(self, max_entities_per_type: int = 10) -> str:
        """LLM 프롬프트에 삽입할 그래프 스키마 요약 텍스트를 생성한다.

        Args:
            max_entities_per_type: 유형별 최대 엔티티 표시 수.

        Returns:
            사람이 읽기 쉬운 스키마 요약 텍스트.
        """
        summary = self.get_schema_summary(max_entities_per_type)
        if summary["total_nodes"] == 0:
            return "그래프가 비어 있습니다."

        lines = [
            f"# 지식 그래프 구조 (노드: {summary['total_nodes']}개, 엣지: {summary['total_edges']}개)",
            "",
            "## 엔티티 유형별 목록",
        ]
        for etype, names in summary["entities_by_type"].items():
            count = summary["entity_types"].get(etype, 0)
            truncated = f" (외 {count - len(names)}개)" if count > len(names) else ""
            lines.append(f"- **{etype}** ({count}개): {', '.join(names)}{truncated}")

        if summary["relation_types"]:
            lines.append("")
            lines.append("## 관계 유형")
            for rtype, count in summary["relation_types"].items():
                lines.append(f"- {rtype}: {count}건")

        if summary["sample_relations"]:
            lines.append("")
            lines.append("## 관계 예시")
            for rel in summary["sample_relations"][:10]:
                lines.append(f"- {rel['source']} --[{rel['type']}]--> {rel['target']}")

        return "\n".join(lines)

    # --- 쿼리 기반 스키마 생성 ---

    def get_query_relevant_schema(
        self,
        query_embedding: list[float],
        *,
        similarity_threshold: float = 0.5,
        top_k: int = 10,
        neighbor_depth: int = 1,
        max_sample_relations: int = 15,
    ) -> dict[str, Any]:
        """쿼리와 관련된 엔티티 중심으로 축소된 스키마를 생성한다.

        1. 쿼리 임베딩과 유사한 엔티티를 찾는다.
        2. 해당 엔티티의 이웃(neighbor_depth)을 포함하여 서브그래프를 구성한다.
        3. 서브그래프의 스키마 요약을 반환한다.

        임베딩 캐시가 비어 있으면 전체 스키마로 폴백한다.

        Args:
            query_embedding: 쿼리 텍스트의 임베딩 벡터.
            similarity_threshold: 최소 코사인 유사도 임계값.
            top_k: 유사 엔티티 최대 검색 수.
            neighbor_depth: 유사 엔티티로부터의 이웃 탐색 깊이.
            max_sample_relations: 샘플 관계 최대 수.

        Returns:
            전체 스키마와 동일한 형태의 딕셔너리 + "seed_entities" 키 추가.
        """
        if not self._entity_embeddings:
            return self.get_schema_summary()

        # 1. 쿼리와 유사한 엔티티 찾기
        similar = self.search_entities_by_embedding(
            query_embedding, threshold=similarity_threshold, top_k=top_k,
        )
        if not similar:
            return self.get_schema_summary()

        # 2. 유사 엔티티 + 이웃으로 서브그래프 노드 ID 수집
        relevant_node_ids: set[int] = set()
        seed_entities: list[dict[str, Any]] = []
        for entity in similar:
            nid = entity["node_id"]
            relevant_node_ids.add(nid)
            seed_entities.append({
                "name": entity["entity_name"],
                "type": entity["entity_type"],
                "similarity": round(entity["similarity"], 3),
            })
            # 이웃 탐색
            reachable = nx.single_source_shortest_path_length(
                self._graph, nid, cutoff=neighbor_depth,
            )
            relevant_node_ids.update(reachable.keys())

        if not relevant_node_ids:
            return self.get_schema_summary()

        # 3. 서브그래프 스키마 집계
        type_counter: Counter[str] = Counter()
        entities_by_type: dict[str, list[str]] = {}
        for nid in relevant_node_ids:
            if nid not in self._graph:
                continue
            data = self._graph.nodes[nid]
            etype = data.get("entity_type", "other")
            ename = data.get("entity_name", "")
            type_counter[etype] += 1
            entities_by_type.setdefault(etype, []).append(ename)

        rel_counter: Counter[str] = Counter()
        sample_relations: list[dict[str, str]] = []
        for u, v, data in self._graph.edges(data=True):
            if u in relevant_node_ids and v in relevant_node_ids:
                rtype = data.get("relation_type", "related_to")
                rel_counter[rtype] += 1
                if len(sample_relations) < max_sample_relations:
                    src_name = self._graph.nodes[u].get("entity_name", str(u))
                    tgt_name = self._graph.nodes[v].get("entity_name", str(v))
                    sample_relations.append({
                        "source": src_name,
                        "target": tgt_name,
                        "type": rtype,
                    })

        return {
            "total_nodes": len(relevant_node_ids),
            "total_edges": sum(rel_counter.values()),
            "entity_types": dict(type_counter.most_common()),
            "relation_types": dict(rel_counter.most_common()),
            "entities_by_type": entities_by_type,
            "sample_relations": sample_relations,
            "seed_entities": seed_entities,
        }

    def format_query_relevant_schema_for_llm(
        self,
        query_embedding: list[float],
        **kwargs: Any,
    ) -> str:
        """쿼리 관련 스키마를 LLM 프롬프트용 텍스트로 변환한다.

        Args:
            query_embedding: 쿼리 텍스트의 임베딩 벡터.
            **kwargs: get_query_relevant_schema에 전달할 추가 인자.

        Returns:
            사람이 읽기 쉬운 스키마 요약 텍스트.
        """
        summary = self.get_query_relevant_schema(query_embedding, **kwargs)
        if summary["total_nodes"] == 0:
            return "그래프가 비어 있습니다."

        lines = [
            f"# 지식 그래프 구조 (관련 노드: {summary['total_nodes']}개, "
            f"관련 엣지: {summary['total_edges']}개)",
        ]

        # 쿼리와 직접 관련된 시드 엔티티 표시
        seed = summary.get("seed_entities", [])
        if seed:
            lines.append("")
            lines.append("## 쿼리 관련 핵심 엔티티")
            for s in seed:
                lines.append(
                    f"- **{s['name']}** ({s['type']}, 유사도: {s['similarity']:.2f})"
                )

        lines.append("")
        lines.append("## 엔티티 유형별 목록")
        for etype, names in summary["entities_by_type"].items():
            count = summary["entity_types"].get(etype, 0)
            lines.append(f"- **{etype}** ({count}개): {', '.join(names)}")

        if summary["relation_types"]:
            lines.append("")
            lines.append("## 관계 유형")
            for rtype, count in summary["relation_types"].items():
                lines.append(f"- {rtype}: {count}건")

        if summary["sample_relations"]:
            lines.append("")
            lines.append("## 관계 예시")
            for rel in summary["sample_relations"][:10]:
                lines.append(f"- {rel['source']} --[{rel['type']}]--> {rel['target']}")

        return "\n".join(lines)

    # --- 엔티티 임베딩 기반 유사도 검색 ---

    async def build_entity_embeddings(self, embedding_client: Any) -> int:
        """모든 엔티티 이름의 임베딩을 생성하여 캐시한다.

        이미 캐시된 노드는 건너뛴다.

        Args:
            embedding_client: Embeddings 인터페이스 구현체.

        Returns:
            새로 임베딩된 엔티티 수.
        """
        # 캐시에 없는 노드만 수집
        missing: list[tuple[int, str]] = []
        for node_id, data in self._graph.nodes(data=True):
            if node_id not in self._entity_embeddings:
                name = data.get("entity_name", "")
                if name:
                    missing.append((node_id, name))

        if not missing:
            return 0

        names = [name for _, name in missing]
        try:
            embeddings = await embedding_client.aembed_documents(names)
        except Exception:
            logger.warning("엔티티 임베딩 생성 실패", exc_info=True)
            return 0

        for (node_id, name), emb in zip(missing, embeddings):
            self._entity_embeddings[node_id] = (name, emb)

        logger.debug("엔티티 임베딩 캐시 구축: %d개 추가 (총 %d개)", len(missing), len(self._entity_embeddings))
        return len(missing)

    def search_entities_by_embedding(
        self,
        query_embedding: list[float],
        threshold: float = 0.7,
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """질의 임베딩과 유사한 엔티티를 검색한다.

        Args:
            query_embedding: 질의 텍스트의 임베딩 벡터.
            threshold: 최소 코사인 유사도 임계값.
            top_k: 반환할 최대 엔티티 수.

        Returns:
            유사도 내림차순 정렬된 엔티티 목록.
            각 항목: {"node_id", "entity_name", "entity_type", "similarity"}
        """
        if not self._entity_embeddings:
            return []

        scored: list[tuple[float, int, str]] = []
        for node_id, (name, emb) in self._entity_embeddings.items():
            sim = _cosine_similarity(query_embedding, emb)
            if sim >= threshold:
                scored.append((sim, node_id, name))

        scored.sort(key=lambda x: x[0], reverse=True)

        results: list[dict[str, Any]] = []
        for sim, node_id, name in scored[:top_k]:
            data = dict(self._graph.nodes[node_id])
            results.append({
                "node_id": node_id,
                "entity_name": name,
                "entity_type": data.get("entity_type", ""),
                "similarity": sim,
            })
        return results

    @property
    def entity_embedding_count(self) -> int:
        """캐시된 엔티티 임베딩 수."""
        return len(self._entity_embeddings)
