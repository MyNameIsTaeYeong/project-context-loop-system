"""MCP Server 메인 모듈.

FastMCP 기반으로 stdio/SSE 전송을 지원하는 MCP 서버를 구성한다.
사내 지식 검색·조회 도구를 제공한다.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from context_loop.config import Config
from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore

logger = logging.getLogger(__name__)

mcp = FastMCP("context-loop", instructions="사내 지식 컨텍스트를 검색·조회하는 MCP 서버입니다.")

# 런타임에 초기화되는 전역 의존성
_meta_store: MetadataStore | None = None
_vector_store: VectorStore | None = None
_graph_store: GraphStore | None = None
_embedding_client: object | None = None
_llm_client: object | None = None
_reranker_client: object | None = None
_config: Config | None = None


async def _initialize() -> None:
    """저장소와 설정을 초기화한다."""
    global _meta_store, _vector_store, _graph_store, _embedding_client, _llm_client, _reranker_client, _config  # noqa: PLW0603

    _config = Config()
    data_dir = Path(_config.get("app.data_dir", "~/.context-loop/data")).expanduser()

    _meta_store = MetadataStore(data_dir / "metadata.db")
    await _meta_store.initialize()

    _vector_store = VectorStore(data_dir)
    _vector_store.initialize()

    _graph_store = GraphStore(_meta_store)
    await _graph_store.load_from_db()

    # 임베딩 클라이언트 초기화
    from context_loop.processor.embedder import EndpointEmbeddingClient, LocalEmbeddingClient

    provider = _config.get("processor.embedding_provider", "endpoint")
    if provider == "endpoint":
        _embedding_client = EndpointEmbeddingClient(
            endpoint=_config.get("processor.embedding_endpoint", ""),
            model=_config.get("processor.embedding_model", "text-embedding-3-small"),
            api_key=_config.get("processor.embedding_api_key", ""),
            headers=_config.get("processor.embedding_headers") or None,
        )
    else:
        _embedding_client = LocalEmbeddingClient(
            model=_config.get("processor.embedding_model", "all-MiniLM-L6-v2"),
        )

    # LLM 클라이언트 초기화 (그래프 탐색 플래너용)
    from context_loop.web.app import _build_llm_client, _build_reranker_client

    try:
        _llm_client = _build_llm_client(_config)
    except Exception:
        logger.warning("LLM 클라이언트 초기화 실패 (그래프 탐색 비활성화)", exc_info=True)
        _llm_client = None

    # 전용 리랭커 클라이언트 초기화 (config 미설정 시 None — 리랭킹 스킵)
    try:
        _reranker_client = _build_reranker_client(_config)
    except Exception:
        logger.warning("리랭커 클라이언트 초기화 실패 (리랭킹 비활성화)", exc_info=True)
        _reranker_client = None

    logger.info("MCP Server 저장소 초기화 완료")


def _get_stores() -> tuple[MetadataStore, VectorStore, GraphStore]:
    """초기화된 저장소를 반환한다."""
    assert _meta_store is not None and _vector_store is not None and _graph_store is not None
    return _meta_store, _vector_store, _graph_store


# --- MCP Tools 등록 (tools.py에서 정의) ---

from context_loop.mcp.tools import register_tools  # noqa: E402

register_tools(mcp)


def run_stdio() -> None:
    """stdio 전송으로 MCP 서버를 실행한다."""

    async def _run() -> None:
        await _initialize()
        await mcp.run_async(transport="stdio")

    asyncio.run(_run())


def run_sse(port: int = 3001) -> None:
    """SSE 전송으로 MCP 서버를 실행한다."""

    async def _run() -> None:
        await _initialize()
        await mcp.run_async(transport="sse", sse_params={"port": port})

    asyncio.run(_run())
