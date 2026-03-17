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
        # 스키마 요약 캐시: 그래프 변경 시 무효화
        self._schema_cache: dict[str, Any] | None = None

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
        self._schema_cache = None
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

        # 캐시 무효화
        for name, nid in name_to_node_id.items():
            self._entity_embeddings.pop(nid, None)
        self._schema_cache = None

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

        # 캐시 무효화
        for nid in nodes_to_remove:
            self._entity_embeddings.pop(nid, None)
        self._schema_cache = None

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

    def get_schema_summary(
        self,
        max_entities_per_type: int = 10,
        max_total_entities: int = 50,
    ) -> dict[str, Any]:
        """LLM에게 제공할 그래프 스키마 요약을 생성한다.

        그래프 변경이 없으면 캐시된 결과를 반환한다.
        전체 그래프를 순회하지만 결과는 요약되어 LLM 토큰을 절약한다.

        Args:
            max_entities_per_type: 유형별 최대 엔티티 표시 수.
            max_total_entities: LLM에 전달할 엔티티 이름 총 상한.

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
        if self._schema_cache is not None:
            return self._schema_cache

        if self._graph.number_of_nodes() == 0:
            result: dict[str, Any] = {
                "total_nodes": 0,
                "total_edges": 0,
                "entity_types": {},
                "relation_types": {},
                "entities_by_type": {},
                "sample_relations": [],
            }
            self._schema_cache = result
            return result

        # 엔티티 유형별 집계
        type_counter: Counter[str] = Counter()
        all_entities_by_type: dict[str, list[str]] = {}
        for _, data in self._graph.nodes(data=True):
            etype = data.get("entity_type", "other")
            ename = data.get("entity_name", "")
            type_counter[etype] += 1
            if etype not in all_entities_by_type:
                all_entities_by_type[etype] = []
            all_entities_by_type[etype].append(ename)

        # 유형별로 max_entities_per_type 이내로 자르되,
        # 전체 합이 max_total_entities를 넘지 않도록 유형별 쿼타 분배
        num_types = len(all_entities_by_type)
        per_type_quota = max(1, max_total_entities // max(num_types, 1))
        per_type_quota = min(per_type_quota, max_entities_per_type)

        entities_by_type: dict[str, list[str]] = {}
        total_shown = 0
        for etype in type_counter:  # most_common 순서로 순회하기 위해 아래에서 정렬
            pass
        for etype, _ in type_counter.most_common():
            remaining = max_total_entities - total_shown
            if remaining <= 0:
                break
            quota = min(per_type_quota, remaining, len(all_entities_by_type[etype]))
            entities_by_type[etype] = all_entities_by_type[etype][:quota]
            total_shown += quota

        # 관계 유형별 집계 + 유형별 균등 샘플링
        rel_counter: Counter[str] = Counter()
        relations_by_type: dict[str, list[dict[str, str]]] = {}
        for u, v, data in self._graph.edges(data=True):
            rtype = data.get("relation_type", "related_to")
            rel_counter[rtype] += 1
            if rtype not in relations_by_type:
                relations_by_type[rtype] = []
            if len(relations_by_type[rtype]) < 3:  # 유형별 최대 3개
                src_name = self._graph.nodes[u].get("entity_name", str(u))
                tgt_name = self._graph.nodes[v].get("entity_name", str(v))
                relations_by_type[rtype].append({
                    "source": src_name,
                    "target": tgt_name,
                    "type": rtype,
                })

        # 유형별 샘플을 합쳐서 최대 15개
        sample_relations: list[dict[str, str]] = []
        for rtype, _ in rel_counter.most_common():
            for rel in relations_by_type.get(rtype, []):
                if len(sample_relations) >= 15:
                    break
                sample_relations.append(rel)
            if len(sample_relations) >= 15:
                break

        result = {
            "total_nodes": self._graph.number_of_nodes(),
            "total_edges": self._graph.number_of_edges(),
            "entity_types": dict(type_counter.most_common()),
            "relation_types": dict(rel_counter.most_common()),
            "entities_by_type": entities_by_type,
            "sample_relations": sample_relations,
        }
        self._schema_cache = result
        return result

    def format_schema_for_llm(
        self,
        max_entities_per_type: int = 10,
        max_total_entities: int = 50,
    ) -> str:
        """LLM 프롬프트에 삽입할 그래프 스키마 요약 텍스트를 생성한다.

        그래프 변경이 없으면 캐시된 요약을 사용한다.

        Args:
            max_entities_per_type: 유형별 최대 엔티티 표시 수.
            max_total_entities: 전체 엔티티 이름 총 상한.

        Returns:
            사람이 읽기 쉬운 스키마 요약 텍스트.
        """
        summary = self.get_schema_summary(max_entities_per_type, max_total_entities)
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
