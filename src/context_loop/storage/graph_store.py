"""그래프 저장소 — NetworkX + SQLite.

NetworkX로 인메모리 그래프를 관리하고,
SQLite(MetadataStore)에 영속 저장한다.
엔티티 병합 및 고아 엣지 정리 로직을 포함한다.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import networkx as nx

from context_loop.processor.graph_extractor import GraphData
from context_loop.storage.metadata_store import MetadataStore

logger = logging.getLogger(__name__)


class GraphStore:
    """NetworkX + SQLite 기반 그래프 저장소.

    SQLite(MetadataStore)가 진실의 원천(source of truth)이다.
    NetworkX 그래프는 검색/탐색용 인메모리 인덱스 역할을 한다.

    Args:
        store: 초기화된 MetadataStore 인스턴스.
    """

    def __init__(self, store: MetadataStore) -> None:
        self._store = store
        self._graph: nx.DiGraph = nx.DiGraph()

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
