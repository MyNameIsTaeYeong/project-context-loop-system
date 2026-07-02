"""소개(About) 페이지 라우트·템플릿 테스트.

웹 앱의 풀페이지 Jinja 렌더는 테스트 하니스에서 검증하지 않으므로
(기존 스위트도 동일), 여기서는 라우트 등록과 템플릿/네비 정합성만 확인한다.
"""

from __future__ import annotations

from pathlib import Path

from context_loop.web.api.documents import router as documents_router

_TEMPLATES = Path(__file__).resolve().parents[2] / "src" / "context_loop" / "web" / "templates"


def test_about_route_registered():
    """/about GET 라우트가 문서 라우터에 등록되어 있다."""
    paths = {getattr(r, "path", None) for r in documents_router.routes}
    assert "/about" in paths


def test_about_template_extends_base():
    """about.html 이 base.html 을 상속하고 핵심 소개 콘텐츠를 담는다."""
    body = (_TEMPLATES / "about.html").read_text(encoding="utf-8")
    assert '{% extends "base.html" %}' in body
    assert "Context Loop" in body
    assert "MCP" in body


def test_about_link_in_nav():
    """base.html 네비게이션에 /about 링크가 추가되어 있다."""
    body = (_TEMPLATES / "base.html").read_text(encoding="utf-8")
    assert 'href="/about"' in body
