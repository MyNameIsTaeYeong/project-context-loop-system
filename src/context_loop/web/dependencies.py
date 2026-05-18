"""FastAPI 의존성 주입 함수."""

from __future__ import annotations

from fastapi import Request
from fastapi.templating import Jinja2Templates
from langchain_core.embeddings import Embeddings

from context_loop.config import Config
from context_loop.processor.llm_client import LLMClient
from context_loop.processor.reranker_client import RerankerClient
from context_loop.storage.graph_store import GraphStore
from context_loop.storage.metadata_store import MetadataStore
from context_loop.storage.vector_store import VectorStore


def get_config(request: Request) -> Config:
    return request.app.state.config


def get_meta_store(request: Request) -> MetadataStore:
    return request.app.state.meta_store


def get_vector_store(request: Request) -> VectorStore:
    return request.app.state.vector_store


def get_graph_store(request: Request) -> GraphStore:
    return request.app.state.graph_store


def get_templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def get_llm_client(request: Request) -> LLMClient:
    """앱 시작 시 생성된 LLM 클라이언트를 반환한다."""
    return request.app.state.llm_client


def get_embedding_client(request: Request) -> Embeddings:
    """앱 시작 시 생성된 임베딩 클라이언트를 반환한다."""
    return request.app.state.embedding_client


def get_reranker_client(request: Request) -> RerankerClient | None:
    """앱 시작 시 생성된 리랭커 클라이언트를 반환한다 (미설정 시 None)."""
    return request.app.state.reranker_client
