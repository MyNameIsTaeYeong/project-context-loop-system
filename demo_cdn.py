"""CDN 아키텍처 문서 처리 시뮬레이션 데모.

실제 LLM/임베딩 API 호출 없이 파이프라인 전체 흐름과
저장 데이터를 확인할 수 있습니다.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from context_loop.processor.chunker import chunk_text
from context_loop.processor.graph_extractor import Entity, GraphData, Relation
from context_loop.processor.pipeline import PipelineConfig, process_document
from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore

# ─────────────────────────────────────────────
# CDN 아키텍처 문서 (예시)
# ─────────────────────────────────────────────
CDN_TITLE = "CDN 아키텍처 설계 문서"
CDN_CONTENT = """
## 개요

CDN(Content Delivery Network)은 전 세계에 분산된 엣지 서버를 통해
사용자에게 콘텐츠를 빠르게 전달하는 인프라입니다.
Origin Server에서 콘텐츠를 캐싱하여 지연 시간을 줄이고 트래픽 부하를 분산합니다.

## 구성 요소

### Origin Server
모든 원본 콘텐츠가 저장되는 메인 서버입니다.
CDN이 캐싱하지 못한 요청을 처리하며, S3 또는 자체 서버로 구성됩니다.

### Edge Server (PoP)
전 세계 주요 도시에 위치한 캐시 서버입니다.
사용자와 가장 가까운 엣지 서버가 요청을 처리합니다.
서울, 도쿄, 싱가포르, 프랑크푸르트, 버지니아에 PoP을 운영합니다.

### Load Balancer
들어오는 트래픽을 여러 엣지 서버로 균등하게 분산합니다.
헬스 체크를 수행하여 장애 서버를 자동으로 제외합니다.

### DNS Resolver
사용자 위치 기반으로 가장 가까운 엣지 서버 IP를 반환합니다.
GeoDNS를 활용하여 지역별 라우팅을 수행합니다.

## 캐싱 전략

### Cache-Control 헤더
- 정적 파일(JS, CSS, 이미지): max-age=31536000 (1년)
- HTML 페이지: max-age=3600, must-revalidate
- API 응답: no-cache (캐싱 안 함)

### 캐시 무효화(Purge)
콘텐츠 업데이트 시 CDN 전체 캐시를 즉시 무효화할 수 있습니다.
배포 파이프라인(CI/CD)과 연동하여 자동으로 Purge를 트리거합니다.

## 트래픽 흐름

1. 사용자 → DNS Resolver → 가장 가까운 PoP IP 반환
2. 사용자 → Edge Server → 캐시 히트 시 즉시 응답
3. 캐시 미스 → Edge Server → Origin Server 요청 → 캐싱 후 응답
4. Load Balancer가 복수 Edge Server 간 부하 분산

## 보안

### TLS/SSL
모든 엣지 서버는 TLS 1.3을 사용합니다.
Let's Encrypt 인증서를 자동 갱신합니다.

### DDoS 방어
Rate Limiting을 통해 초당 요청 수를 제한합니다.
WAF(Web Application Firewall)를 엣지에 배포합니다.

## 모니터링

Prometheus와 Grafana를 사용하여 엣지 서버 메트릭을 수집합니다.
캐시 히트율, 응답 시간, 에러율을 대시보드로 시각화합니다.
"""


# ─────────────────────────────────────────────
# LLM 응답 Mock (실제 API 호출 없이 시뮬레이션)
# ─────────────────────────────────────────────
MOCK_CLASSIFIER_RESPONSE = json.dumps({
    "method": "hybrid",
    "reason": "CDN 아키텍처 문서는 구성 요소 간 의존 관계(그래프)와 캐싱 전략 설명(서술형)이 혼재한다.",
})

MOCK_GRAPH_RESPONSE = json.dumps({
    "entities": [
        {"name": "Origin Server", "type": "system", "description": "원본 콘텐츠가 저장되는 메인 서버"},
        {"name": "Edge Server", "type": "system", "description": "전 세계에 분산된 캐시 서버 (PoP)"},
        {"name": "Load Balancer", "type": "component", "description": "트래픽을 여러 엣지 서버로 분산"},
        {"name": "DNS Resolver", "type": "service", "description": "사용자 위치 기반으로 가장 가까운 PoP IP 반환"},
        {"name": "CDN", "type": "system", "description": "전체 콘텐츠 전송 네트워크"},
        {"name": "WAF", "type": "component", "description": "Web Application Firewall - 엣지에 배포"},
        {"name": "CI/CD", "type": "system", "description": "배포 파이프라인 - 캐시 Purge 트리거"},
        {"name": "Prometheus", "type": "service", "description": "엣지 서버 메트릭 수집"},
        {"name": "Grafana", "type": "service", "description": "메트릭 시각화 대시보드"},
        {"name": "GeoDNS", "type": "service", "description": "지역별 DNS 라우팅"},
    ],
    "relations": [
        {"source": "CDN", "target": "Origin Server", "type": "contains", "label": "원본 서버"},
        {"source": "CDN", "target": "Edge Server", "type": "contains", "label": "엣지 서버"},
        {"source": "Load Balancer", "target": "Edge Server", "type": "manages", "label": "부하 분산"},
        {"source": "DNS Resolver", "target": "Edge Server", "type": "connects_to", "label": "IP 라우팅"},
        {"source": "DNS Resolver", "target": "GeoDNS", "type": "uses", "label": "지역 라우팅"},
        {"source": "Edge Server", "target": "Origin Server", "type": "depends_on", "label": "캐시 미스 시 요청"},
        {"source": "WAF", "target": "Edge Server", "type": "belongs_to", "label": "엣지 배포"},
        {"source": "CI/CD", "target": "CDN", "type": "manages", "label": "캐시 Purge"},
        {"source": "Prometheus", "target": "Edge Server", "type": "uses", "label": "메트릭 수집"},
        {"source": "Grafana", "target": "Prometheus", "type": "depends_on", "label": "시각화"},
    ],
})


def build_mock_llm_client() -> MagicMock:
    """LLM 응답을 Mock하여 실제 API 없이 파이프라인을 실행한다."""
    client = MagicMock()
    call_count = {"n": 0}

    async def mock_complete(*args, **kwargs):
        call_count["n"] += 1
        # 1번째 호출 = classifier, 2번째 호출 = graph extractor
        if call_count["n"] == 1:
            return MOCK_CLASSIFIER_RESPONSE
        return MOCK_GRAPH_RESPONSE

    client.complete = mock_complete
    return client


def build_mock_embedding_client() -> MagicMock:
    """임베딩을 Mock하여 실제 OpenAI API 없이 파이프라인을 실행한다."""
    client = MagicMock()

    async def mock_embed(texts: list[str]) -> list[list[float]]:
        # 각 텍스트에 대해 1536차원 더미 벡터 반환
        return [[0.01 * (i + 1)] * 1536 for i in range(len(texts))]

    client.embed = mock_embed
    return client


# ─────────────────────────────────────────────
# 메인 데모
# ─────────────────────────────────────────────
async def main() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = Path(tmpdir)

        # 저장소 초기화
        meta_store = MetadataStore(data_dir / "metadata.db")
        vector_store = VectorStore(data_dir)
        await meta_store.initialize()
        vector_store.initialize()
        graph_store = GraphStore(meta_store)

        # ── 1. 문서 저장 ─────────────────────────────
        print("=" * 60)
        print("① 문서 저장 (save_document 역할)")
        print("=" * 60)
        import hashlib
        content_hash = hashlib.sha256(CDN_CONTENT.encode()).hexdigest()
        doc_id = await meta_store.create_document(
            source_type="manual",
            title=CDN_TITLE,
            original_content=CDN_CONTENT,
            content_hash=content_hash,
        )
        doc = await meta_store.get_document(doc_id)
        print(f"  document_id : {doc['id']}")
        print(f"  title       : {doc['title']}")
        print(f"  source_type : {doc['source_type']}")
        print(f"  status      : {doc['status']}")
        print(f"  content_hash: {doc['content_hash'][:16]}...")
        print()

        # ── 2. 파이프라인 처리 ───────────────────────
        print("=" * 60)
        print("② 파이프라인 처리 (process_document)")
        print("=" * 60)
        result = await process_document(
            doc_id,
            meta_store=meta_store,
            vector_store=vector_store,
            graph_store=graph_store,
            llm_client=build_mock_llm_client(),
            embedding_client=build_mock_embedding_client(),
            config=PipelineConfig(chunk_size=300, chunk_overlap=30),
        )
        print(f"  storage_method : {result['storage_method']}")
        print(f"  chunk_count    : {result['chunk_count']}")
        print(f"  node_count     : {result['node_count']}")
        print(f"  edge_count     : {result['edge_count']}")
        print()

        # ── 3. 저장된 청크 데이터 ────────────────────
        print("=" * 60)
        print("③ 저장된 청크 데이터 (SQLite: chunks 테이블)")
        print("=" * 60)
        chunks = await meta_store.get_chunks_by_document(doc_id)
        for c in chunks:
            preview = c['content'][:60].replace('\n', ' ')
            print(f"  [{c['chunk_index']}] tokens={c['token_count']:3d} | {preview}...")
        print()

        print("  ChromaDB(VectorStore) 총 청크 수:", vector_store.count())
        print()

        # ── 4. 저장된 그래프 데이터 ──────────────────
        print("=" * 60)
        print("④ 저장된 그래프 데이터 (SQLite: graph_nodes / graph_edges)")
        print("=" * 60)
        nodes = await meta_store.get_graph_nodes_by_document(doc_id)
        edges = await meta_store.get_graph_edges_by_document(doc_id)

        print(f"  [노드] 총 {len(nodes)}개")
        for n in nodes:
            props = json.loads(n['properties'] or '{}')
            desc = props.get('description', '')[:40]
            print(f"    id={n['id']:2d} | {n['entity_type']:12s} | {n['entity_name']} — {desc}")

        print()
        print(f"  [엣지] 총 {len(edges)}개")

        # 노드 ID → 이름 맵
        node_map = {n['id']: n['entity_name'] for n in nodes}
        for e in edges:
            src = node_map.get(e['source_node_id'], '?')
            tgt = node_map.get(e['target_node_id'], '?')
            props = json.loads(e['properties'] or '{}')
            label = props.get('label', '')
            print(f"    {src:20s} --[{e['relation_type']:12s}]--> {tgt}  ({label})")

        print()

        # NetworkX 통계
        stats = graph_store.stats()
        print(f"  NetworkX 인메모리 그래프: 노드={stats['nodes']}, 엣지={stats['edges']}")
        print()

        # ── 5. 처리 이력 ─────────────────────────────
        print("=" * 60)
        print("⑤ 처리 이력 (SQLite: processing_history 테이블)")
        print("=" * 60)
        history = await meta_store.get_processing_history(doc_id)
        for h in history:
            print(f"  action={h['action']:15s} | status={h['status']:10s} | {h['started_at']} → {h['completed_at']}")
        print()

        # ── 6. 최종 문서 상태 ────────────────────────
        print("=" * 60)
        print("⑥ 최종 문서 상태")
        print("=" * 60)
        doc = await meta_store.get_document(doc_id)
        print(f"  status         : {doc['status']}")
        print(f"  storage_method : {doc['storage_method']}")
        print(f"  version        : {doc['version']}")
        print()

        # ── 7. CDN 질의 시 검색 흐름 ─────────────────
        print("=" * 60)
        print("⑦ 사용자 질의: 'CDN 캐시 무효화는 어떻게 하나요?'")
        print("=" * 60)
        query = "CDN 캐시 무효화는 어떻게 하나요?"
        print(f"  질의: {query}")
        print()

        # storage_method=hybrid이므로 벡터 + 그래프 모두 검색
        print("  [벡터 검색] ChromaDB 유사도 검색")
        query_embedding = [0.01] * 1536  # 더미 쿼리 임베딩
        vector_results = vector_store.search(query_embedding, n_results=3)
        for i, r in enumerate(vector_results):
            preview = r['document'][:70].replace('\n', ' ')
            print(f"    [{i+1}] dist={r['distance']:.4f} | chunk_idx={r['metadata'].get('chunk_index')} | {preview}...")
        print()

        print("  [그래프 검색] 'CDN' 엔티티 주변 관계 탐색 (depth=1)")
        neighbors = graph_store.get_neighbors("CDN", depth=1)
        print(f"    CDN 관련 노드 {len(neighbors)}개:")
        for n in neighbors:
            print(f"      - {n['entity_name']} ({n['entity_type']})")

        neighbor_ids = [n['id'] for n in neighbors]
        related_edges = graph_store.get_edges_between(neighbor_ids)
        print(f"    연결 엣지 {len(related_edges)}개:")
        for e in related_edges:
            src = node_map.get(e['source'], '?')
            tgt = node_map.get(e['target'], '?')
            print(f"      {src} --[{e['relation_type']}]--> {tgt}")
        print()

        print("  [최종 컨텍스트 구성]")
        print("  → 벡터 검색 결과 (관련 청크) + 그래프 검색 결과 (엔티티 관계)")
        print("    두 결과를 합쳐 LLM 프롬프트에 컨텍스트로 제공")

        await meta_store.close()


if __name__ == "__main__":
    asyncio.run(main())
