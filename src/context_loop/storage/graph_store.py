"""그래프 저장소 — NetworkX + SQLite.

NetworkX로 인메모리 그래프를 관리하고,
SQLite(MetadataStore)에 영속 저장한다.
크로스-문서 엔티티 병합(정규 노드)을 지원한다.
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


def _extract_short_name(entity_name: str) -> str:
    """FQN 엔티티 이름에서 짧은 이름을 추출한다.

    코드 심볼 엔티티는 `file.py::Class.method` 형태의 FQN을 사용하므로
    LLM이나 MCP 클라이언트가 짧은 이름으로 질의해도 매칭되도록
    fallback 용으로 짧은 이름을 꺼낸다.

    - "user_service.py::UserService.create"  → "create"
    - "user_service.py::main"                 → "main"
    - "UserService"                           → "UserService"
    - "handler.go"                            → "handler.go"  (::가 없으면 그대로)
    """
    if "::" not in entity_name:
        return entity_name
    after_scope = entity_name.split("::", 1)[1]
    if "." in after_scope:
        return after_scope.rsplit(".", 1)[-1]
    return after_scope


def _extract_scoped_name(entity_name: str) -> str:
    """FQN에서 파일 범위를 제거한 부분을 반환한다.

    - "user_service.py::UserService.create" → "UserService.create"
    - "user_service.py::main"               → "main"
    - "UserService"                          → "UserService"
    """
    if "::" not in entity_name:
        return entity_name
    return entity_name.split("::", 1)[1]


class GraphStore:
    """NetworkX + SQLite 기반 그래프 저장소.

    SQLite(MetadataStore)가 진실의 원천(source of truth)이다.
    NetworkX 그래프는 검색/탐색용 인메모리 인덱스 역할을 한다.

    크로스-문서 엔티티 병합:
    - 동일 엔티티(entity_name + entity_type, 대소문자 무시)는 하나의 정규 노드로 병합
    - graph_node_documents 테이블로 노드-문서 다대다 관계 관리
    - NetworkX 노드의 document_ids 속성에 연결된 문서 ID 집합 저장

    Args:
        store: 초기화된 MetadataStore 인스턴스.
    """

    def __init__(self, store: MetadataStore) -> None:
        self._store = store
        self._graph: nx.DiGraph = nx.DiGraph()
        # 엔티티 임베딩 캐시: node_id → (entity_name, embedding)
        self._entity_embeddings: dict[int, tuple[str, list[float]]] = {}

    async def load_from_db(self) -> None:
        """SQLite에서 그래프 데이터를 로드하여 NetworkX 그래프를 재구성한다.

        graph_node_documents 테이블에서 노드-문서 연결 정보를 로드하여
        각 노드의 document_ids 속성을 설정한다.
        """
        self._graph = nx.DiGraph()

        # 모든 노드 로드
        nodes = await self._store.get_all_graph_nodes()
        # 노드-문서 연결 정보 로드
        node_doc_links = await self._store.get_all_node_document_links()

        for node in nodes:
            doc_ids = set(node_doc_links.get(node["id"], []))
            # 레거시: graph_node_documents에 데이터가 없으면 document_id 사용
            if not doc_ids and node.get("document_id"):
                doc_ids = {node["document_id"]}
            self._graph.add_node(
                node["id"],
                entity_name=node["entity_name"],
                entity_type=node.get("entity_type", "other"),
                document_ids=doc_ids,
                properties=json.loads(node["properties"] or "{}"),
            )

        # 모든 엣지 로드
        docs = await self._store.list_documents()
        for doc in docs:
            edges = await self._store.get_graph_edges_by_document(doc["id"])
            for edge in edges:
                if (self._graph.has_node(edge["source_node_id"])
                        and self._graph.has_node(edge["target_node_id"])):
                    self._graph.add_edge(
                        edge["source_node_id"],
                        edge["target_node_id"],
                        id=edge["id"],
                        relation_type=edge.get("relation_type", "related_to"),
                        document_id=edge["document_id"],
                        properties=json.loads(edge["properties"] or "{}"),
                    )

        self._entity_embeddings.clear()
        logger.info(
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

        정규 엔티티 병합: entity_name(대소문자 무시) + entity_type 기준으로
        기존 노드가 있으면 재사용하고, 없으면 새로 생성한다.
        재사용 시 description이 비어 있으면 보강한다.

        Args:
            document_id: 저장할 문서 ID.
            graph_data: 추출된 그래프 데이터.

        Returns:
            {"nodes": 생성된 노드 수, "edges": 생성된 엣지 수,
             "merged": 기존 노드에 병합된 수} 딕셔너리.
        """
        name_to_node_id: dict[str, int] = {}
        new_count = 0
        merged_count = 0

        for entity in graph_data.entities:
            props = {"description": entity.description} if entity.description else {}

            # 기존 정규 노드 검색
            existing = await self._store.find_graph_node_by_entity(
                entity.name, entity.entity_type,
            )

            if existing:
                node_id = existing["id"]
                merged_count += 1
                # description 보강 (기존에 없으면 채움)
                existing_props = json.loads(existing["properties"] or "{}")
                if entity.description and not existing_props.get("description"):
                    existing_props["description"] = entity.description
                    await self._store.update_graph_node_properties(
                        node_id, json.dumps(existing_props, ensure_ascii=False),
                    )
                # 문서 연결 추가
                await self._store.add_node_document_link(node_id, document_id)
                # NetworkX 업데이트
                if self._graph.has_node(node_id):
                    self._graph.nodes[node_id]["document_ids"].add(document_id)
                    if entity.description and not self._graph.nodes[node_id].get("properties", {}).get("description"):
                        self._graph.nodes[node_id]["properties"]["description"] = entity.description
                else:
                    self._graph.add_node(
                        node_id,
                        entity_name=entity.name,
                        entity_type=entity.entity_type,
                        document_ids={document_id},
                        properties=existing_props,
                    )
            else:
                # 새 노드 생성
                node_id = await self._store.create_graph_node(
                    document_id=document_id,
                    entity_name=entity.name,
                    entity_type=entity.entity_type,
                    properties=json.dumps(props, ensure_ascii=False),
                )
                await self._store.add_node_document_link(node_id, document_id)
                new_count += 1
                self._graph.add_node(
                    node_id,
                    entity_name=entity.name,
                    entity_type=entity.entity_type,
                    document_ids={document_id},
                    properties=props,
                )

            name_to_node_id[entity.name] = node_id

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
            # 동일 엣지 중복 방지 (같은 src, tgt, relation_type, document_id)
            existing_edge = False
            if self._graph.has_edge(src_id, tgt_id):
                edge_data = self._graph.edges[src_id, tgt_id]
                if (edge_data.get("relation_type") == relation.relation_type
                        and edge_data.get("document_id") == document_id):
                    existing_edge = True
            if existing_edge:
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

        # 새 노드의 임베딩 캐시 무효화
        for name, nid in name_to_node_id.items():
            self._entity_embeddings.pop(nid, None)

        logger.info(
            "그래프 저장 완료 — document_id=%d, 신규: %d, 병합: %d, 엣지: %d",
            document_id,
            new_count,
            merged_count,
            edge_count,
        )
        return {"nodes": new_count, "edges": edge_count, "merged": merged_count}

    async def delete_document_graph(self, document_id: int) -> None:
        """문서의 그래프 데이터를 삭제한다.

        1. 해당 문서의 엣지를 삭제한다.
        2. 노드-문서 연결을 해제한다.
        3. 고아 노드(어떤 문서에도 연결되지 않은 노드)를 삭제한다.
        4. NetworkX 그래프를 동기화한다.

        Args:
            document_id: 삭제할 문서 ID.
        """
        # SQLite에서 삭제 (엣지, 연결 해제, 고아 노드 정리)
        await self._store.delete_graph_data_by_document(document_id)

        # NetworkX 동기화: 해당 문서의 엣지 제거
        edges_to_remove = [
            (u, v) for u, v, d in self._graph.edges(data=True)
            if d.get("document_id") == document_id
        ]
        self._graph.remove_edges_from(edges_to_remove)

        # NetworkX 동기화: 노드의 document_ids에서 해당 문서 제거
        nodes_to_remove = []
        for n, d in self._graph.nodes(data=True):
            doc_ids = d.get("document_ids", set())
            if document_id in doc_ids:
                doc_ids.discard(document_id)
                if not doc_ids:
                    nodes_to_remove.append(n)

        # 고아 노드 제거
        self._graph.remove_nodes_from(nodes_to_remove)

        # 삭제된 노드의 임베딩 캐시 제거
        for nid in nodes_to_remove:
            self._entity_embeddings.pop(nid, None)

        logger.debug("그래프 삭제 완료: document_id=%d", document_id)

    def get_neighbors(
        self,
        entity_name: str,
        depth: int = 1,
    ) -> list[dict[str, Any]]:
        """엔티티 이름을 중심으로 주변 관계를 탐색한다.

        정규 노드 병합 덕분에 동명 엔티티는 하나의 노드이므로
        크로스-문서 관계가 자연스럽게 탐색된다.

        코드 심볼은 `file.py::Class.method` 형태의 FQN으로 등록되므로,
        LLM이 짧은 이름(예: "create_user")이나 부분 경로(예:
        "UserService.create_user")로 요청해도 매칭되도록 단계별로
        fallback 조회한다.

        Args:
            entity_name: 탐색 중심 엔티티 이름.
            depth: 탐색 깊이 (1 = 직접 연결만).

        Returns:
            관련 노드 정보 목록.
        """
        query_lower = entity_name.lower()

        # 1. 완전 일치 (기존 동작)
        center_nodes = [
            n for n, d in self._graph.nodes(data=True)
            if d.get("entity_name", "").lower() == query_lower
        ]

        # 2. 파일 범위를 벗긴 부분 일치 (예: "UserService.create_user")
        if not center_nodes:
            center_nodes = [
                n for n, d in self._graph.nodes(data=True)
                if _extract_scoped_name(d.get("entity_name", "")).lower() == query_lower
            ]

        # 3. 짧은 이름 일치 (예: "create_user", "UserService")
        if not center_nodes:
            center_nodes = [
                n for n, d in self._graph.nodes(data=True)
                if _extract_short_name(d.get("entity_name", "")).lower() == query_lower
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
        """주어진 노드 집합 사이의 엣지를 반환한다."""
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
        """LLM에게 제공할 그래프 스키마 요약을 생성한다."""
        if self._graph.number_of_nodes() == 0:
            return {
                "total_nodes": 0,
                "total_edges": 0,
                "entity_types": {},
                "relation_types": {},
                "entities_by_type": {},
                "sample_relations": [],
            }

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
        """LLM 프롬프트에 삽입할 그래프 스키마 요약 텍스트를 생성한다."""
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
        """쿼리와 관련된 엔티티 중심으로 축소된 스키마를 생성한다."""
        if not self._entity_embeddings:
            return self.get_schema_summary()

        similar = self.search_entities_by_embedding(
            query_embedding, threshold=similarity_threshold, top_k=top_k,
        )
        if not similar:
            return self.get_schema_summary()

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
            reachable = nx.single_source_shortest_path_length(
                self._graph, nid, cutoff=neighbor_depth,
            )
            relevant_node_ids.update(reachable.keys())

        if not relevant_node_ids:
            return self.get_schema_summary()

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
        """쿼리 관련 스키마를 LLM 프롬프트용 텍스트로 변환한다."""
        summary = self.get_query_relevant_schema(query_embedding, **kwargs)
        if summary["total_nodes"] == 0:
            return "그래프가 비어 있습니다."

        lines = [
            f"# 지식 그래프 구조 (관련 노드: {summary['total_nodes']}개, "
            f"관련 엣지: {summary['total_edges']}개)",
        ]

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
        """모든 엔티티 이름의 임베딩을 생성하여 캐시한다."""
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
        """질의 임베딩과 유사한 엔티티를 검색한다."""
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
