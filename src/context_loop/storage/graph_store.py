"""그래프 저장소 — NetworkX + SQLite.

NetworkX로 인메모리 그래프를 관리하고,
SQLite(MetadataStore)에 영속 저장한다.
크로스-문서 엔티티 병합(정규 노드)을 지원한다.
LLM 기반 탐색을 위한 그래프 스키마 요약을 제공한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections import Counter
from typing import Any

import networkx as nx

from context_loop.processor.graph_extractor import GraphData
from context_loop.storage.entity_normalizer import normalize_entity_name
from context_loop.storage.metadata_store import MetadataStore

logger = logging.getLogger(__name__)

# 엔티티 임베딩을 build 할 때 한 번의 embedding 호출에 묶는 최대 엔티티 수.
# 노드가 수천 개에 달하면 전부를 한 호출로 보낼 때 rate limit/타임아웃에
# 걸려 전체가 실패하므로, 이 크기로 잘라 청크 단위로 호출한다.
_ENTITY_EMBED_BATCH_SIZE = 200
# 청크 호출 실패 시 재시도 횟수와 지수 백오프 기준(초).
_ENTITY_EMBED_MAX_RETRIES = 3
_ENTITY_EMBED_BACKOFF_BASE = 2.0


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


def _embedding_display_name(entity_name: str) -> str:
    """엔티티 임베딩 입력용 표시명.

    코드 심볼 FQN(``file.py::Class.method``)은 파일 범위를 벗긴 부분
    (``Class.method``)을 임베딩해, 자연어/문서 질의 임베딩과의 의미 거리를
    줄인다 — query-embedding 시드 보강이 코드 노드에도 닿게 하여 코드↔지식
    브리지를 의미 차원에서 보강한다. FQN 이 아니면 이름 그대로.
    """
    if "::" not in entity_name:
        return entity_name
    return _extract_scoped_name(entity_name)


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

            # R3: 정규화 키 산출 — graph_store 측에서 정규화하여 metadata_store
            # 에 전달 (책임 분리: storage 레이어는 입력 키를 그대로 신뢰). 같은
            # 키를 신규 노드 INSERT 시에도 재사용해 머지/생성 양쪽이 일관된
            # 정규화 정책을 따른다.
            normalized = normalize_entity_name(entity.name)

            # 기존 정규 노드 검색 (정규화 키 기반)
            existing = await self._store.find_graph_node_by_entity(
                entity.name,
                entity.entity_type,
                normalized_name=normalized,
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
                # R3 머지 로그: 표기 변형 없이 그대로 일치한 경우 'exact',
                # 정규화 키만으로 일치한 경우 'normalized' 로 구분. 비교는
                # canonical 노드 원본 entity_name 과 입력 entity.name 사이의
                # 직접 동일성으로 판정 (대소문자 포함 정확히 동일).
                method = "exact" if existing.get("entity_name") == entity.name else "normalized"
                await self._record_merge_safely(
                    canonical_node_id=node_id,
                    raw_entity_name=entity.name,
                    raw_entity_type=entity.entity_type,
                    source_document_id=document_id,
                    merge_method=method,
                )
            else:
                # 새 노드 생성 — graph_nodes INSERT 와 graph_node_documents link
                # INSERT 를 같은 트랜잭션 / 한 번의 commit 으로 묶어, 두 INSERT
                # 사이의 await 양보 시점에 다른 코루틴의 고아 노드 정리 SQL 이
                # 방금 만든 노드를 삭제하여 후속 add_node_document_link 가 FK
                # 위반을 일으키던 race window 를 제거한다.
                node_id = await self._store.create_graph_node_with_link(
                    document_id=document_id,
                    entity_name=entity.name,
                    entity_type=entity.entity_type,
                    properties=json.dumps(props, ensure_ascii=False),
                    normalized_name=normalized,
                )
                new_count += 1
                self._graph.add_node(
                    node_id,
                    entity_name=entity.name,
                    entity_type=entity.entity_type,
                    document_ids={document_id},
                    properties=props,
                )
                # R3 머지 로그: 신규 노드 생성 케이스
                await self._record_merge_safely(
                    canonical_node_id=node_id,
                    raw_entity_name=entity.name,
                    raw_entity_type=entity.entity_type,
                    source_document_id=document_id,
                    merge_method="new",
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

    async def _record_merge_safely(
        self,
        *,
        canonical_node_id: int,
        raw_entity_name: str,
        raw_entity_type: str,
        source_document_id: int,
        merge_method: str,
        similarity_score: float | None = None,
    ) -> None:
        """``graph_merge_log`` INSERT — 실패 시 그래프 저장은 진행한다.

        R3: 관측성 로그는 그래프 저장의 critical path 에서 실패해도 본 작업이
        멈추면 안 된다 — exception 을 삼키고 경고만 남긴다. 머지 결정 자체는
        이미 commit 된 상태이므로 로그 누락이 데이터 정합성을 깨지 않는다.
        """
        try:
            await self._store.record_graph_merge(
                canonical_node_id=canonical_node_id,
                raw_entity_name=raw_entity_name,
                raw_entity_type=raw_entity_type,
                source_document_id=source_document_id,
                merge_method=merge_method,
                similarity_score=similarity_score,
            )
        except Exception:
            logger.warning(
                "graph_merge_log 기록 실패 — node_id=%d, method=%s",
                canonical_node_id,
                merge_method,
                exc_info=True,
            )

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

    def _bidirectional_bfs(
        self,
        sources: list[int],
        depth: int,
    ) -> set[int]:
        """sources 에서 양방향(successor + predecessor) 으로 depth-hop BFS.

        DiGraph 의 ``single_source_shortest_path_length`` 는 successors(outgoing)
        만 따라간다. 그러나 "X 를 누가 사용하나?" 류의 질의에서 LLM 이 sink
        노드(예: KakaoPay) 를 search step entity_name 으로 답하면 outgoing 0
        으로 retrieved 가 sink 자신만 담겨 gold seed 를 놓친다 — < 10% 메트릭의
        결정적 funnel 손실. 검색은 의미적으로 양방향이 자연스럽기 때문에 BFS
        에서 양방향을 모두 따라간다.
        """
        if depth < 1:
            return set(sources)
        visited: set[int] = set(sources)
        frontier: set[int] = set(sources)
        for _ in range(depth):
            next_frontier: set[int] = set()
            for n in frontier:
                if not self._graph.has_node(n):
                    continue
                next_frontier.update(self._graph.successors(n))
                next_frontier.update(self._graph.predecessors(n))
            next_frontier -= visited
            if not next_frontier:
                break
            visited.update(next_frontier)
            frontier = next_frontier
        return visited

    def _resolve_seed_nodes(
        self,
        entity_name: str,
        *,
        embedding_fallback: list[float] | None = None,
        embedding_fallback_threshold: float = 0.5,
        embedding_fallback_top_k: int = 3,
        max_seeds: int = 10,
    ) -> list[int]:
        """엔티티 이름을 시드 node_id 목록으로 해석한다 (표면 union + 임베딩 폴백).

        ``get_neighbors`` 와 ``get_connected_component`` 가 공유하는 이름→노드
        해석 로직. 완전 일치 → 파일 범위 제거 일치 → 짧은 이름 일치 순으로
        **누적 union** 한 뒤(첫 매칭에서 멈추지 않음), 모두 비면 임베딩 fallback.

        표면 tier 를 union 하므로 한 질의 이름이 confluence bare 노드(완전 일치)와
        코드 FQN 노드(scoped/short 일치)를 **동시에** 시드로 잡아, 별개로 저장된
        코드 그래프와 지식(문서) 그래프를 검색 시점에 잇는다. 비교 키는 storage
        병합과 동일한 ``normalize_entity_name`` 으로 정렬하여 "Auth Service" ↔
        "AuthService" ↔ "auth-service" 같은 공백/케이스/구분자 차이를 흡수한다.
        흔한 짧은 이름이 과도한 시드를 끌어오는 것을 막기 위해 우선순위
        (exact > scoped > short) 순으로 ``max_seeds`` 까지만 채택한다.
        """
        query_key = normalize_entity_name(entity_name)

        seen: set[int] = set()
        ordered: list[int] = []

        def _collect(key_fn: Any) -> None:
            for n, d in self._graph.nodes(data=True):
                if n in seen:
                    continue
                if normalize_entity_name(key_fn(d.get("entity_name", ""))) == query_key:
                    seen.add(n)
                    ordered.append(n)

        if query_key:
            _collect(lambda name: name)        # 1. 완전 일치
            _collect(_extract_scoped_name)     # 2. 파일 범위 제거 일치
            _collect(_extract_short_name)      # 3. 짧은 이름 일치

        center_nodes = ordered[:max_seeds]

        # 4. 임베딩 fallback — 표면 매칭이 모두 실패한 경우. 질의 이름이
        # 인덱스에 표기 차이로 존재할 때 (예: "인증 서비스" ↔ "인증서비스")
        # 의미 임베딩으로 가장 가까운 노드를 시드로 사용하여 funnel 손실을 줄인다.
        if not center_nodes and embedding_fallback is not None:
            similar = self.search_entities_by_embedding(
                embedding_fallback,
                threshold=embedding_fallback_threshold,
                top_k=embedding_fallback_top_k,
            )
            center_nodes = [s["node_id"] for s in similar if s.get("node_id") is not None]

        return center_nodes

    def get_connected_component(
        self,
        entity_name: str,
        *,
        depth: int | None = None,
        embedding_fallback: list[float] | None = None,
        embedding_fallback_threshold: float = 0.5,
        embedding_fallback_top_k: int = 3,
        max_nodes: int = 500,
    ) -> list[dict[str, Any]]:
        """엔티티에서 (방향 무시) 연결된 노드를 hop 거리와 함께 반환한다.

        ``get_neighbors`` 가 depth 제한 BFS 인 것과 달리, 기본적으로 시드
        노드가 속한 연결 컴포넌트(weakly connected component) 전체를 반환한다
        — "이 키워드와 연결된 모든 엔티티" 탐색용. 시드 이름 해석은
        ``_resolve_seed_nodes`` 와 동일한 4단계 폴백을 쓴다.

        각 결과 노드에는 시드로부터의 최단 hop 거리(``hop``)가 부여된다 —
        UI 가 depth 별로 노드를 구분 표시할 수 있도록.

        Args:
            entity_name: 시작 키워드/엔티티 이름.
            depth: 탐색 깊이(hop). ``None`` 이면 연결된 전체를 탐색한다.
                1 이면 직접 이웃까지, 2 면 2-hop 까지.
            embedding_fallback: 표면 매칭 실패 시 사용할 임베딩 벡터.
            max_nodes: 반환 노드 상한 (거대 컴포넌트로 인한 과도한 페이로드
                방지). BFS 가 이 수에 도달하면 탐색을 중단한다.

        Returns:
            연결된 노드 dict 목록 (시드 포함, ``hop`` 키 포함). 시드를 못 찾으면
            빈 목록.
        """
        seeds = self._resolve_seed_nodes(
            entity_name,
            embedding_fallback=embedding_fallback,
            embedding_fallback_threshold=embedding_fallback_threshold,
            embedding_fallback_top_k=embedding_fallback_top_k,
        )
        if not seeds:
            return []

        seed_set = set(seeds)
        # hop 거리 기록 BFS — 시드는 0. depth=None 이면 무제한.
        hop_by_id: dict[int, int] = {s: 0 for s in seeds}
        frontier: set[int] = set(seeds)
        current_hop = 0
        while frontier and len(hop_by_id) < max_nodes:
            if depth is not None and current_hop >= depth:
                break
            current_hop += 1
            nxt: set[int] = set()
            for n in frontier:
                if not self._graph.has_node(n):
                    continue
                nxt.update(self._graph.successors(n))
                nxt.update(self._graph.predecessors(n))
            nxt -= hop_by_id.keys()
            if not nxt:
                break
            for nid in nxt:
                hop_by_id[nid] = current_hop
            frontier = nxt

        result: list[dict[str, Any]] = []
        for node_id in list(hop_by_id.keys())[:max_nodes]:
            if not self._graph.has_node(node_id):
                continue
            data = dict(self._graph.nodes[node_id])
            data["id"] = node_id
            data["is_seed"] = node_id in seed_set
            data["hop"] = hop_by_id[node_id]
            result.append(data)
        return result

    def get_neighbors(
        self,
        entity_name: str,
        depth: int = 1,
        *,
        embedding_fallback: list[float] | None = None,
        embedding_fallback_threshold: float = 0.5,
        embedding_fallback_top_k: int = 3,
    ) -> list[dict[str, Any]]:
        """엔티티 이름을 중심으로 주변 관계를 탐색한다.

        정규 노드 병합 덕분에 동명 엔티티는 하나의 노드이므로
        크로스-문서 관계가 자연스럽게 탐색된다.

        코드 심볼은 `file.py::Class.method` 형태의 FQN으로 등록되므로,
        LLM이 짧은 이름(예: "create_user")이나 부분 경로(예:
        "UserService.create_user")로 요청해도 매칭되도록 단계별로
        fallback 조회한다. 표면 매칭이 모두 실패하면 ``embedding_fallback`` 이
        제공된 경우 임베딩 cosine 기반으로 가장 가까운 노드를 시드로 사용한다.

        탐색은 양방향(successor + predecessor). DiGraph 의 자연 동작인
        successor-only 는 sink 노드(예: 데이터베이스, 외부 시스템) 가 시드로
        선택되면 retrieved 가 자기 자신만 담겨 검색 funnel 의 가장 큰 손실
        지점이 된다 — F-SRCH-R2-01.

        Args:
            entity_name: 탐색 중심 엔티티 이름.
            depth: 탐색 깊이 (1 = 직접 연결만).
            embedding_fallback: 표면 매칭 모두 실패 시 사용할 임베딩 벡터.
                일반적으로 ``entity_name`` 자체의 임베딩. ``None`` 이면
                fallback 없이 빈 결과 반환 (기존 동작 호환).
            embedding_fallback_threshold: 임베딩 fallback 의 cosine 최소값.
            embedding_fallback_top_k: 임베딩 fallback 으로 가져올 시드 노드 수.

        Returns:
            관련 노드 정보 목록.
        """
        center_nodes = self._resolve_seed_nodes(
            entity_name,
            embedding_fallback=embedding_fallback,
            embedding_fallback_threshold=embedding_fallback_threshold,
            embedding_fallback_top_k=embedding_fallback_top_k,
        )

        if not center_nodes:
            return []

        reachable_ids = self._bidirectional_bfs(center_nodes, depth)
        result_nodes: list[dict[str, Any]] = []
        for node_id in reachable_ids:
            if not self._graph.has_node(node_id):
                continue
            data = dict(self._graph.nodes[node_id])
            data["id"] = node_id
            result_nodes.append(data)
        return result_nodes

    def get_neighbors_from_node_id(
        self,
        node_id: int,
        depth: int = 1,
    ) -> list[dict[str, Any]]:
        """주어진 node_id 를 중심으로 주변 노드를 반환한다.

        ``get_neighbors`` 는 이름 기반 표면 매칭부터 시작하지만, query
        임베딩 fallback 처럼 이미 시드 ``node_id`` 가 결정된 경우 직접
        탐색이 필요하다. 양방향(successor + predecessor) traversal —
        ``get_neighbors`` 와 동일한 정책. 결과 모양도 동일.
        """
        if not self._graph.has_node(node_id):
            return []
        reachable_ids = self._bidirectional_bfs([node_id], depth)
        result_nodes: list[dict[str, Any]] = []
        for nid in reachable_ids:
            if not self._graph.has_node(nid):
                continue
            data = dict(self._graph.nodes[nid])
            data["id"] = nid
            result_nodes.append(data)
        return result_nodes

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

    def content_fingerprint(self) -> dict[str, Any]:
        """그래프 내용의 결정적 지문을 반환한다.

        노드는 ``(entity_name, entity_type)``, 엣지는
        ``(source_entity_name, target_entity_name, relation_type)`` 의 안정 키만
        사용한다. node id 는 재빌드마다 달라질 수 있어 지문에 넣지 않고, 엔티티
        이름으로 환원한다. 평가 산출물(summary/manifest)에 코퍼스 정체성을
        앵커링하기 위한 용도로, 임베딩/properties 같은 비결정 직렬화 대상은
        제외한다.

        Returns:
            ``{"nodes": int, "edges": int, "sha256": str}``.
        """
        import hashlib

        node_keys = sorted(
            (
                str(d.get("entity_name", "")),
                str(d.get("entity_type", "")),
            )
            for _, d in self._graph.nodes(data=True)
        )
        edge_keys = sorted(
            (
                str(self._graph.nodes[u].get("entity_name", "")),
                str(self._graph.nodes[v].get("entity_name", "")),
                str(d.get("relation_type", "")),
            )
            for u, v, d in self._graph.edges(data=True)
            if self._graph.has_node(u) and self._graph.has_node(v)
        )
        payload = json.dumps(
            {"nodes": node_keys, "edges": edge_keys},
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return {
            "nodes": self._graph.number_of_nodes(),
            "edges": self._graph.number_of_edges(),
            "sha256": digest,
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

        # 노드 수가 수천 개에 달하면 한 호출로 전부 보낼 때 rate limit/타임아웃에
        # 걸려 전체가 실패한다. _ENTITY_EMBED_BATCH_SIZE 단위로 잘라 청크별로
        # 호출하고, 성공한 청크는 즉시 캐시에 반영해 일부 실패가 전체 손실로
        # 번지지 않게 한다(다음 호출에서 누락분만 재시도).
        added = 0
        for start in range(0, len(missing), _ENTITY_EMBED_BATCH_SIZE):
            batch = missing[start : start + _ENTITY_EMBED_BATCH_SIZE]
            embeddings = await self._embed_entity_batch(embedding_client, batch)
            if embeddings is None:
                logger.warning(
                    "엔티티 임베딩 청크 실패: %d~%d (총 %d개 중) — 이번 청크 건너뜀",
                    start, start + len(batch), len(missing),
                )
                continue
            for (node_id, name), emb in zip(batch, embeddings):
                self._entity_embeddings[node_id] = (name, emb)
            added += len(batch)

        logger.debug(
            "엔티티 임베딩 캐시 구축: %d개 추가 (요청 %d개, 총 %d개)",
            added, len(missing), len(self._entity_embeddings),
        )
        return added

    async def _embed_entity_batch(
        self,
        embedding_client: Any,
        batch: list[tuple[int, str]],
    ) -> list[list[float]] | None:
        """엔티티 한 청크의 임베딩을 재시도와 함께 생성한다.

        성공 시 입력 순서에 대응하는 임베딩 목록을, 모든 재시도가 실패하면
        None 을 반환한다.

        임베딩 입력은 표시명(코드 FQN 은 파일 범위 제거)을 쓰되, 캐시에 저장하는
        이름은 원본 entity_name 으로 유지해 다운스트림 표기는 그대로 보존한다.
        """
        embed_inputs = [_embedding_display_name(name) for _, name in batch]
        for attempt in range(_ENTITY_EMBED_MAX_RETRIES):
            try:
                return await embedding_client.aembed_documents(embed_inputs)
            except Exception:
                if attempt + 1 >= _ENTITY_EMBED_MAX_RETRIES:
                    logger.warning(
                        "엔티티 임베딩 생성 실패 (재시도 %d회 소진)",
                        _ENTITY_EMBED_MAX_RETRIES, exc_info=True,
                    )
                    return None
                delay = _ENTITY_EMBED_BACKOFF_BASE ** attempt
                logger.warning(
                    "엔티티 임베딩 청크 실패 (시도 %d/%d), %.1f초 후 재시도",
                    attempt + 1, _ENTITY_EMBED_MAX_RETRIES, delay, exc_info=True,
                )
                await asyncio.sleep(delay)
        return None

    def search_entities_by_embedding(
        self,
        query_embedding: list[float],
        threshold: float = 0.5,
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
