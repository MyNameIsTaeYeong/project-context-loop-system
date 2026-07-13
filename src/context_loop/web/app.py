"""FastAPI 웹 대시보드 애플리케이션."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from context_loop.config import Config
from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore

logger = logging.getLogger(__name__)

_WEB_DIR = Path(__file__).resolve().parent


def _compute_asset_version() -> str:
    """정적 자산(js/css)의 최신 수정 시각을 캐시버스팅 버전 문자열로 반환한다.

    템플릿이 ``?v={{ asset_version }}`` 로 정적 파일을 참조하면, 파일이 바뀔
    때마다 값이 바뀌어 브라우저가 캐시 대신 새 파일을 받는다. 산출 실패 시
    빈 값을 반환(쿼리 없이 동작 — 기존과 동일).
    """
    static_dir = _WEB_DIR / "static"
    try:
        mtimes = [
            p.stat().st_mtime
            for p in static_dir.rglob("*")
            if p.suffix in (".js", ".css")
        ]
        if mtimes:
            return str(int(max(mtimes)))
    except OSError:
        logger.debug("asset_version 산출 실패", exc_info=True)
    return ""


def _configure_logging(config: Config) -> None:
    """config의 app.log_level을 context_loop 로거에 적용한다.

    uvicorn은 자체 로거만 INFO로 설정하므로 context_loop.* 로거들은
    기본 WARNING을 상속한다. 이 함수가 호출되어야 INFO 로그가 출력된다.
    """
    level_name = str(config.get("app.log_level", "INFO")).upper()
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        level = logging.INFO
    pkg_logger = logging.getLogger("context_loop")
    pkg_logger.setLevel(level)
    if not pkg_logger.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s"),
        )
        pkg_logger.addHandler(handler)
        pkg_logger.propagate = False


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
            reasoning_profiles=config.get("llm.reasoning_profiles") or None,
        )
    if provider == "anthropic":
        api_key = get_token("anthropic", "api_key")
        return AnthropicClient(api_key=api_key or "")
    # openai
    api_key = get_token("openai", "api_key")
    return OpenAIClient(api_key=api_key or "")


def _build_reranker_client(config: Config):
    """설정에 따라 전용 리랭커 클라이언트를 생성한다.

    ``reranker.endpoint`` 가 비어 있으면 None 을 반환해 리랭킹 단계를 스킵한다.
    """
    from context_loop.processor.reranker_client import EndpointRerankerClient

    endpoint = config.get("reranker.endpoint", "") or ""
    model = config.get("reranker.model", "") or ""
    if not endpoint or not model:
        return None
    return EndpointRerankerClient(
        endpoint=endpoint,
        model=model,
        api_key=config.get("reranker.api_key", "") or "",
        headers=config.get("reranker.headers") or None,
    )


def _build_embedding_client(config: Config):
    """설정에 따라 임베딩 클라이언트를 생성한다."""
    from context_loop.processor.embedder import (
        _EMBED_BACKOFF_BASE,
        _EMBED_MAX_CONCURRENCY,
        _EMBED_MAX_RETRIES,
        EndpointEmbeddingClient,
        LocalEmbeddingClient,
    )

    rate_limit_kwargs = {
        "max_concurrency": config.get(
            "processor.embedding_max_concurrency", _EMBED_MAX_CONCURRENCY
        ),
        "max_retries": config.get("processor.embedding_max_retries", _EMBED_MAX_RETRIES),
        "backoff_base": config.get("processor.embedding_backoff_base", _EMBED_BACKOFF_BASE),
    }

    embed_provider = config.get("processor.embedding_provider", "endpoint")
    if embed_provider == "endpoint":
        return EndpointEmbeddingClient(
            endpoint=config.get("processor.embedding_endpoint", ""),
            model=config.get("processor.embedding_model", ""),
            api_key=config.get("processor.embedding_api_key", ""),
            headers=config.get("processor.embedding_headers") or None,
            **rate_limit_kwargs,
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
        **rate_limit_kwargs,
    )


async def _prebuild_entity_embeddings(
    graph_store: GraphStore, embedding_client: Any, config: Config,
) -> None:
    """그래프 엔티티 임베딩을 시작 시 미리 구축한다 (실패해도 기동을 막지 않음)."""
    if not embedding_client:
        return
    try:
        count = await graph_store.build_entity_embeddings(
            embedding_client,
            batch_size=config.get("processor.entity_embedding_batch_size", 100),
            concurrency=config.get("processor.entity_embedding_concurrency", 4),
        )
        logger.info("그래프 엔티티 임베딩 사전 구축 완료: %d개", count)
    except Exception:
        logger.warning("그래프 엔티티 임베딩 사전 구축 실패 (검색 시 lazy 재시도)", exc_info=True)


def _build_mcp_sync_engine(
    config: Config,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
    embedding_client: Any,
    llm_client: Any,
):
    """설정이 허용하면 MCP 자동 싱크 엔진을 만들어 반환한다 (아니면 None).

    ``sources.confluence_mcp`` 의 ``enabled`` + ``auto_sync_enabled`` 가 모두
    켜져 있고 ``server_url`` 이 설정된 경우에만 엔진을 만든다. 러너로는
    수동 버튼 싱크와 동일한 :func:`run_sync_in_background` 를 주입해
    target 단위 락/진행 상태를 두 경로가 공유하도록 한다.
    """
    from context_loop.sync.mcp_engine import MCPSyncEngine
    from context_loop.web.api.confluence_mcp import run_sync_in_background

    if not config.get("sources.confluence_mcp.enabled", False):
        return None
    if not config.get("sources.confluence_mcp.auto_sync_enabled", False):
        return None
    if not config.get("sources.confluence_mcp.server_url", ""):
        logger.warning(
            "confluence_mcp.auto_sync_enabled 이지만 server_url 미설정 — "
            "자동 싱크를 시작하지 않습니다.",
        )
        return None

    async def _run_target(target_id: int) -> None:
        await run_sync_in_background(
            target_id, config, meta_store, vector_store, graph_store,
            embedding_client, llm_client,
        )

    return MCPSyncEngine(
        meta_store,
        _run_target,
        interval_minutes=config.get(
            "sources.confluence_mcp.sync_interval_minutes", 30,
        ),
    )


def _build_git_sync_engine(
    config: Config,
    meta_store: MetadataStore,
    vector_store: VectorStore,
    graph_store: GraphStore,
    embedding_client: Any,
    llm_client: Any,
):
    """설정이 허용하면 git_code 자동 싱크 엔진을 만들어 반환한다 (아니면 None).

    ``sources.git`` 의 ``enabled`` + ``auto_sync_enabled`` 가 모두 켜져 있고
    ``repositories`` 가 설정된 경우에만 엔진을 만든다. 러너로는 수동 트리거
    (``POST /api/git-sync/start``)와 같은 전역 싱크 상태를 공유하는
    :func:`run_sync_in_background` 를 주입해 두 경로의 중복 실행을 막는다.
    """
    from context_loop.ingestion.git_config import load_git_source_config
    from context_loop.sync.periodic import PeriodicSyncEngine
    from context_loop.web.api.git_sync import run_sync_in_background

    git_config = load_git_source_config(config)
    if not git_config.enabled or not git_config.auto_sync_enabled:
        return None
    if not git_config.repositories:
        logger.warning(
            "git.auto_sync_enabled 이지만 repositories 미설정 — "
            "자동 싱크를 시작하지 않습니다.",
        )
        return None

    async def _run_cycle() -> bool:
        return await run_sync_in_background(
            config, meta_store, vector_store, graph_store,
            embedding_client, llm_client,
        )

    return PeriodicSyncEngine(
        _run_cycle,
        name="git",
        interval_minutes=git_config.sync_interval_minutes,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """앱 시작/종료 시 스토어와 모델 클라이언트를 초기화/정리한다."""
    config = Config()
    data_dir = config.data_dir

    _configure_logging(config)

    meta_store = MetadataStore(data_dir / "metadata.db")
    await meta_store.initialize()

    vector_store = VectorStore(data_dir)
    vector_store.initialize()

    graph_store = GraphStore(meta_store)
    await graph_store.load_from_db()

    llm_client = _build_llm_client(config)
    embedding_client = _build_embedding_client(config)
    reranker_client = _build_reranker_client(config)

    app.state.config = config
    app.state.meta_store = meta_store
    app.state.vector_store = vector_store
    app.state.graph_store = graph_store
    app.state.llm_client = llm_client
    app.state.embedding_client = embedding_client
    app.state.reranker_client = reranker_client

    # 그래프 엔티티 임베딩을 시작 시 미리 구축한다. 노드가 수천 개면
    # 청크 단위로 나눠 호출하며, 일부/전체 실패해도 서버 기동은 막지 않는다
    # (최초 그래프 검색 시 누락분이 lazy 하게 재시도된다).
    await _prebuild_entity_embeddings(graph_store, embedding_client, config)

    # 소스별 자동 주기 싱크 — 각 소스의 auto_sync_enabled 토글이 켜진 경우에만.
    mcp_sync_engine = _build_mcp_sync_engine(
        config, meta_store, vector_store, graph_store,
        embedding_client, llm_client,
    )
    app.state.mcp_sync_engine = mcp_sync_engine
    if mcp_sync_engine is not None:
        mcp_sync_engine.start()

    git_sync_engine = _build_git_sync_engine(
        config, meta_store, vector_store, graph_store,
        embedding_client, llm_client,
    )
    app.state.git_sync_engine = git_sync_engine
    if git_sync_engine is not None:
        git_sync_engine.start()

    logger.info("웹 대시보드 스토어 및 모델 클라이언트 초기화 완료.")
    yield

    if git_sync_engine is not None:
        await git_sync_engine.stop()
    if mcp_sync_engine is not None:
        await mcp_sync_engine.stop()
    await meta_store.close()
    # 공유 임베딩 HTTP 커넥션 풀 정리 (aclose 미구현 클라이언트는 무시).
    aclose = getattr(embedding_client, "aclose", None)
    if callable(aclose):
        try:
            await aclose()
        except Exception:
            logger.warning("임베딩 클라이언트 종료 실패", exc_info=True)
    logger.info("웹 대시보드 스토어 종료.")


def create_app() -> FastAPI:
    """FastAPI 애플리케이션을 생성하고 설정한다."""
    app = FastAPI(title="Context Loop Dashboard", lifespan=lifespan)

    app.mount("/static", StaticFiles(directory=_WEB_DIR / "static"), name="static")

    templates = Jinja2Templates(directory=_WEB_DIR / "templates")
    # 정적 자산 캐시버스팅용 버전 — 정적 파일(js/css)의 최신 mtime 으로 산출한다.
    # 코드 변경 시 값이 바뀌어 브라우저가 새 파일을 받는다(stale graph.js 방지).
    templates.env.globals["asset_version"] = _compute_asset_version()
    app.state.templates = templates

    from context_loop.web.api.chat import router as chat_router
    from context_loop.web.api.confluence import router as confluence_router
    from context_loop.web.api.confluence_mcp import router as confluence_mcp_router
    from context_loop.web.api.documents import router as documents_router
    from context_loop.web.api.git_sync import router as git_sync_router
    from context_loop.web.api.graph import router as graph_router
    from context_loop.web.api.stats import router as stats_router
    from context_loop.web.api.upload import router as upload_router

    app.include_router(stats_router)
    app.include_router(upload_router)
    app.include_router(confluence_router)
    app.include_router(confluence_mcp_router)
    app.include_router(git_sync_router)
    app.include_router(chat_router)
    app.include_router(graph_router)
    app.include_router(documents_router)

    return app
