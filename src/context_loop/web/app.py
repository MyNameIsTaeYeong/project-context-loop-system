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


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """앱 시작/종료 시 스토어를 초기화/정리한다."""
    config = Config()
    data_dir = config.data_dir

    meta_store = MetadataStore(data_dir / "metadata.db")
    await meta_store.initialize()

    vector_store = VectorStore(data_dir)
    vector_store.initialize()

    graph_store = GraphStore(meta_store)
    await graph_store.load_from_db()

    app.state.config = config
    app.state.meta_store = meta_store
    app.state.vector_store = vector_store
    app.state.graph_store = graph_store

    logger.info("웹 대시보드 스토어 초기화 완료.")
    yield

    await meta_store.close()
    logger.info("웹 대시보드 스토어 종료.")


def create_app() -> FastAPI:
    """FastAPI 애플리케이션을 생성하고 설정한다."""
    app = FastAPI(title="Context Loop Dashboard", lifespan=lifespan)

    app.mount("/static", StaticFiles(directory=_WEB_DIR / "static"), name="static")

    templates = Jinja2Templates(directory=_WEB_DIR / "templates")
    app.state.templates = templates

    from context_loop.web.api.confluence import router as confluence_router
    from context_loop.web.api.documents import router as documents_router
    from context_loop.web.api.stats import router as stats_router
    from context_loop.web.api.upload import router as upload_router

    app.include_router(stats_router)
    app.include_router(upload_router)
    app.include_router(confluence_router)
    app.include_router(documents_router)

    return app
