"""FastAPI 웹 대시보드 애플리케이션."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from context_loop.config import Config
from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore

logger = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent


def _build_llm_client(config: Config):
    """설정에 따라 LLM 클라이언트를 생성한다."""
    from context_loop.auth import get_token
    from context_loop.processor.llm_client import AnthropicClient, EndpointLLMClient, OpenAIClient

    provider = config.get("llm.provider", "endpoint")
    if provider == "endpoint":
        return EndpointLLMClient(
            endpoint=config.get("llm.endpoint", ""),
            model=config.get("llm.model", ""),
            api_key=config.get("llm.api_key", ""),
            headers=config.get("llm.headers") or None,
        )
    if provider == "anthropic":
        api_key = get_token("anthropic", "api_key")
        return AnthropicClient(api_key=api_key or "")
    # openai
    api_key = get_token("openai", "api_key")
    return OpenAIClient(api_key=api_key or "")


def _build_embedding_client(config: Config):
    """설정에 따라 임베딩 클라이언트를 생성한다."""
    from context_loop.processor.embedder import EndpointEmbeddingClient, LocalEmbeddingClient

    embed_provider = config.get("processor.embedding_provider", "endpoint")
    if embed_provider == "endpoint":
        return EndpointEmbeddingClient(
            endpoint=config.get("processor.embedding_endpoint", ""),
            model=config.get("processor.embedding_model", ""),
            api_key=config.get("processor.embedding_api_key", ""),
            headers=config.get("processor.embedding_headers") or None,
        )
    if embed_provider == "local":
        return LocalEmbeddingClient(
            model=config.get("processor.embedding_model", "all-MiniLM-L6-v2"),
        )
    # openai (legacy): OpenAI 호환 엔드포인트로 라우팅
    from context_loop.auth import get_token
    api_key = get_token("openai", "api_key") or ""
    return EndpointEmbeddingClient(
        endpoint="https://api.openai.com/v1",
        model=config.get("processor.embedding_model", "text-embedding-3-small"),
        api_key=api_key,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """앱 시작/종료 시 스토어와 모델 클라이언트를 초기화/정리한다."""
    config = Config()
    data_dir = config.data_dir

    meta_store = MetadataStore(data_dir / "metadata.db")
    await meta_store.initialize()

    vector_store = VectorStore(data_dir)
    vector_store.initialize()

    graph_store = GraphStore(meta_store)
    await graph_store.load_from_db()

    llm_client = _build_llm_client(config)
    embedding_client = _build_embedding_client(config)

    app.state.config = config
    app.state.meta_store = meta_store
    app.state.vector_store = vector_store
    app.state.graph_store = graph_store
    app.state.llm_client = llm_client
    app.state.embedding_client = embedding_client

    logger.info("웹 대시보드 스토어 및 모델 클라이언트 초기화 완료.")
    yield

    await meta_store.close()
    logger.info("웹 대시보드 스토어 종료.")


def create_app() -> FastAPI:
    """FastAPI 애플리케이션을 생성하고 설정한다."""
    app = FastAPI(title="Context Loop Dashboard", lifespan=lifespan)

    app.mount("/static", StaticFiles(directory=_WEB_DIR / "static"), name="static")

    templates = Jinja2Templates(directory=_WEB_DIR / "templates")
    app.state.templates = templates

    from context_loop.web.api.chat import router as chat_router
    from context_loop.web.api.confluence import router as confluence_router
    from context_loop.web.api.confluence_mcp import router as confluence_mcp_router
    from context_loop.web.api.documents import router as documents_router
    from context_loop.web.api.git_sync import router as git_sync_router
    from context_loop.web.api.stats import router as stats_router
    from context_loop.web.api.upload import router as upload_router

    app.include_router(stats_router)
    app.include_router(upload_router)
    app.include_router(confluence_router)
    app.include_router(confluence_mcp_router)
    app.include_router(git_sync_router)
    app.include_router(chat_router)
    app.include_router(documents_router)

    return app
