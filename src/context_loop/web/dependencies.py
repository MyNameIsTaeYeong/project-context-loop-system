"""FastAPI 의존성 주입 함수."""

from __future__ import annotations

from fastapi import Request
from fastapi.templating import Jinja2Templates

from context_loop.config import Config
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
