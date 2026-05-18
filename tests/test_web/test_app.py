"""웹 앱 기본 테스트."""

from __future__ import annotations

import logging

import pytest

from context_loop.config import Config
from context_loop.web.app import _configure_logging


def test_configure_logging_sets_context_loop_level(tmp_path) -> None:
    """app.log_level이 context_loop 로거에 적용된다."""
    pkg_logger = logging.getLogger("context_loop")
    original_level = pkg_logger.level
    original_handlers = list(pkg_logger.handlers)
    original_propagate = pkg_logger.propagate
    try:
        user_cfg = tmp_path / "config.yaml"
        user_cfg.write_text("app:\n  log_level: DEBUG\n", encoding="utf-8")
        config = Config(config_path=user_cfg)

        _configure_logging(config)

        assert pkg_logger.level == logging.DEBUG
    finally:
        pkg_logger.setLevel(original_level)
        pkg_logger.handlers = original_handlers
        pkg_logger.propagate = original_propagate


def test_configure_logging_defaults_to_info_on_invalid(tmp_path) -> None:
    """잘못된 로그 레벨 문자열은 INFO로 폴백한다."""
    pkg_logger = logging.getLogger("context_loop")
    original_level = pkg_logger.level
    original_handlers = list(pkg_logger.handlers)
    original_propagate = pkg_logger.propagate
    try:
        user_cfg = tmp_path / "config.yaml"
        user_cfg.write_text("app:\n  log_level: NOPE\n", encoding="utf-8")
        config = Config(config_path=user_cfg)

        _configure_logging(config)

        assert pkg_logger.level == logging.INFO
    finally:
        pkg_logger.setLevel(original_level)
        pkg_logger.handlers = original_handlers
        pkg_logger.propagate = original_propagate


@pytest.mark.asyncio
async def test_dashboard_returns_html(client):
    """대시보드 페이지가 HTML로 렌더링된다."""
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "Context Loop" in resp.text


@pytest.mark.asyncio
async def test_stats_api_empty(client):
    """빈 DB에서 통계 API가 0값을 반환한다."""
    resp = await client.get("/api/stats")
    assert resp.status_code == 200
    data = resp.json()
    assert data["document_count"] == 0
    assert data["chunk_count"] == 0


@pytest.mark.asyncio
async def test_stats_partial_returns_html(client):
    """통계 파셜이 HTML을 반환한다."""
    resp = await client.get("/partials/stats")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_document_list_partial_empty(client):
    """빈 DB에서 문서 목록 파셜이 빈 상태 메시지를 반환한다."""
    resp = await client.get("/partials/document-list")
    assert resp.status_code == 200
    assert "No documents found" in resp.text


@pytest.mark.asyncio
async def test_document_not_found(client):
    """존재하지 않는 문서 상세 페이지는 404를 반환한다."""
    resp = await client.get("/documents/999")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_editor_new_page(client):
    """새 문서 에디터 페이지가 렌더링된다."""
    resp = await client.get("/editor")
    assert resp.status_code == 200
    assert "New Document" in resp.text
