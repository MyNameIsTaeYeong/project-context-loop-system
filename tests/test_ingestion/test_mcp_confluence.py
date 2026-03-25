"""Confluence MCP Client 임포트 모듈 테스트."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from context_loop.ingestion.mcp_confluence import (
    _extract_page_content,
    _extract_page_title,
    _extract_text,
    _is_cql,
    _parse_json_result,
    build_cql,
    get_all_spaces,
    get_child_pages,
    get_page,
    get_user_contributed_pages,
    import_page_via_mcp,
    list_available_tools,
    search_content,
)
from context_loop.storage.metadata_store import MetadataStore


# --- Helper: CallToolResult 모의 객체 ---


@dataclass
class FakeTextContent:
    text: str
    type: str = "text"


@dataclass
class FakeCallToolResult:
    content: list[FakeTextContent]


def _make_result(text: str) -> FakeCallToolResult:
    return FakeCallToolResult(content=[FakeTextContent(text=text)])


# --- _extract_text 테스트 ---


def test_extract_text_from_result() -> None:
    result = _make_result("hello world")
    assert _extract_text(result) == "hello world"


def test_extract_text_multiple_blocks() -> None:
    result = FakeCallToolResult(
        content=[FakeTextContent(text="line1"), FakeTextContent(text="line2")]
    )
    assert _extract_text(result) == "line1\nline2"


def test_extract_text_fallback() -> None:
    assert _extract_text("plain string") == "plain string"


# --- _parse_json_result 테스트 ---


def test_parse_json_result_list() -> None:
    result = _make_result('[{"id": "1", "title": "Page 1"}]')
    parsed = _parse_json_result(result)
    assert isinstance(parsed, list)
    assert parsed[0]["id"] == "1"


def test_parse_json_result_dict() -> None:
    result = _make_result('{"results": [{"id": "2"}]}')
    parsed = _parse_json_result(result)
    assert isinstance(parsed, dict)
    assert parsed["results"][0]["id"] == "2"


def test_parse_json_result_plain_text() -> None:
    result = _make_result("not json")
    parsed = _parse_json_result(result)
    assert parsed == "not json"


# --- _extract_page_content 테스트 ---


def test_extract_page_content_markdown() -> None:
    page = {"markdown": "# Hello", "title": "Test"}
    assert _extract_page_content(page) == "# Hello"


def test_extract_page_content_body_string() -> None:
    page = {"body": "Some body text"}
    assert _extract_page_content(page) == "Some body text"


def test_extract_page_content_body_storage_dict() -> None:
    page = {"body": {"storage": {"value": "<h1>Title</h1>"}}}
    assert _extract_page_content(page) == "<h1>Title</h1>"


def test_extract_page_content_content_field() -> None:
    page = {"content": "Direct content"}
    assert _extract_page_content(page) == "Direct content"


def test_extract_page_content_empty() -> None:
    assert _extract_page_content({}) == ""
    assert _extract_page_content({"body": {}}) == ""


# --- _extract_page_title 테스트 ---


def test_extract_page_title() -> None:
    assert _extract_page_title({"title": "My Page"}, "123") == "My Page"


def test_extract_page_title_name() -> None:
    assert _extract_page_title({"name": "Named Page"}, "456") == "Named Page"


def test_extract_page_title_fallback() -> None:
    assert _extract_page_title({}, "789") == "Confluence Page 789"


# --- MCP 도구 호출 테스트 ---


@pytest.mark.asyncio
async def test_list_available_tools() -> None:
    session = AsyncMock()
    tool1 = MagicMock(name="searchContent", description="Search")
    tool1.name = "searchContent"
    tool1.description = "Search"
    session.list_tools.return_value = MagicMock(tools=[tool1])

    tools = await list_available_tools(session)
    assert len(tools) == 1
    assert tools[0]["name"] == "searchContent"


# --- build_cql / _is_cql 테스트 ---


def test_is_cql_with_operator() -> None:
    assert _is_cql('text ~ "hello"') is True
    assert _is_cql('title = "page"') is True
    assert _is_cql('space != "DEV"') is True


def test_is_cql_with_keyword() -> None:
    assert _is_cql('text ~ "a" AND type = "page"') is True
    assert _is_cql('text ~ "a" OR text ~ "b"') is True
    assert _is_cql('type = "page" ORDER BY created') is True


def test_is_cql_plain_keyword() -> None:
    assert _is_cql("프로젝트 설계 문서") is False
    assert _is_cql("hello world") is False


def test_build_cql_plain_keyword() -> None:
    assert build_cql("설계 문서") == 'type = "page" AND text ~ "설계 문서"'


def test_build_cql_already_cql() -> None:
    cql = 'text ~ "hello" AND type = "page"'
    assert build_cql(cql) == cql


def test_build_cql_empty() -> None:
    assert build_cql("") == ""
    assert build_cql("   ") == ""


def test_build_cql_escapes_quotes() -> None:
    assert build_cql('say "hello"') == 'type = "page" AND text ~ "say \\"hello\\""'


# --- search_content 테스트 ---


@pytest.mark.asyncio
async def test_search_content_list_result() -> None:
    session = AsyncMock()
    session.call_tool.return_value = _make_result('[{"id": "1", "title": "Result"}]')

    results = await search_content(session, "test query")
    session.call_tool.assert_called_once_with(
        "searchContent", {"cql": 'type = "page" AND text ~ "test query"', "limit": 25, "start": 0},
    )
    assert len(results) == 1
    assert results[0]["id"] == "1"


@pytest.mark.asyncio
async def test_search_content_with_cql() -> None:
    """이미 CQL인 경우 변환 없이 그대로 전달한다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result('[{"id": "1"}]')

    cql = 'text ~ "hello" AND space = "DEV"'
    await search_content(session, cql)
    session.call_tool.assert_called_once_with(
        "searchContent", {"cql": cql, "limit": 25, "start": 0},
    )


@pytest.mark.asyncio
async def test_search_content_dict_result() -> None:
    session = AsyncMock()
    session.call_tool.return_value = _make_result('{"results": [{"id": "2"}]}')

    results = await search_content(session, 'type = "page"')
    assert results[0]["id"] == "2"


@pytest.mark.asyncio
async def test_get_page_dict_result() -> None:
    session = AsyncMock()
    session.call_tool.return_value = _make_result('{"id": "123", "title": "Test Page", "content": "Hello"}')

    page = await get_page(session, "123")
    session.call_tool.assert_called_once_with(
        "getPageByID",
        {"pageId": "123", "expand": "history,space,version,body.storage"},
    )
    assert page["title"] == "Test Page"


@pytest.mark.asyncio
async def test_get_page_text_result() -> None:
    session = AsyncMock()
    session.call_tool.return_value = _make_result("Just plain text content")

    page = await get_page(session, "456")
    assert page["content"] == "Just plain text content"
    assert page["id"] == "456"


@pytest.mark.asyncio
async def test_get_child_pages() -> None:
    session = AsyncMock()
    session.call_tool.return_value = _make_result('[{"id": "10", "title": "Child"}]')

    children = await get_child_pages(session, "1")
    session.call_tool.assert_called_once_with("getChild", {"pageId": "1"})
    assert len(children) == 1


@pytest.mark.asyncio
async def test_get_all_spaces() -> None:
    session = AsyncMock()
    session.call_tool.return_value = _make_result('[{"id": "s1", "key": "DEV", "name": "Dev Team"}]')

    spaces = await get_all_spaces(session)
    session.call_tool.assert_called_once_with("getSpaceInfoAll", {})
    assert spaces[0]["key"] == "DEV"


@pytest.mark.asyncio
async def test_get_user_contributed_pages() -> None:
    session = AsyncMock()
    session.call_tool.return_value = _make_result('[{"id": "100", "title": "My Doc"}]')

    pages = await get_user_contributed_pages(session, "user123")
    session.call_tool.assert_called_once_with("getUserContributedPages", {"userId": "user123"})
    assert pages[0]["title"] == "My Doc"


# --- import_page_via_mcp 테스트 ---


@pytest.fixture
async def meta_store(tmp_path) -> MetadataStore:
    store = MetadataStore(tmp_path / "test.db")
    await store.initialize()
    yield store
    await store.close()


@pytest.mark.asyncio
async def test_import_page_via_mcp_new(meta_store: MetadataStore) -> None:
    """신규 페이지를 임포트하면 created=True를 반환한다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"id": "p1", "title": "New Page", "content": "Hello World"}'
    )

    result = await import_page_via_mcp(session, meta_store, "p1")
    assert result["created"] is True
    assert result["changed"] is True
    assert result["title"] == "New Page"
    assert result["source_type"] == "confluence_mcp"

    # DB에 저장되었는지 확인
    doc = await meta_store.get_document(result["id"])
    assert doc is not None
    assert doc["original_content"] == "Hello World"


@pytest.mark.asyncio
async def test_import_page_via_mcp_unchanged(meta_store: MetadataStore) -> None:
    """동일 내용의 페이지를 다시 임포트하면 changed=False를 반환한다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"id": "p2", "title": "Same Page", "content": "Same Content"}'
    )

    # 1차 임포트
    result1 = await import_page_via_mcp(session, meta_store, "p2")
    assert result1["created"] is True

    # 2차 임포트 (동일 내용)
    result2 = await import_page_via_mcp(session, meta_store, "p2")
    assert result2["created"] is False
    assert result2["changed"] is False


@pytest.mark.asyncio
async def test_import_page_via_mcp_changed(meta_store: MetadataStore) -> None:
    """내용이 변경된 페이지를 임포트하면 changed=True를 반환한다."""
    session = AsyncMock()

    # 1차 임포트
    session.call_tool.return_value = _make_result(
        '{"id": "p3", "title": "Page", "content": "Version 1"}'
    )
    result1 = await import_page_via_mcp(session, meta_store, "p3")
    assert result1["created"] is True

    # 2차 임포트 (변경된 내용)
    session.call_tool.return_value = _make_result(
        '{"id": "p3", "title": "Page", "content": "Version 2"}'
    )
    result2 = await import_page_via_mcp(session, meta_store, "p3")
    assert result2["created"] is False
    assert result2["changed"] is True
    assert result2["status"] == "changed"
