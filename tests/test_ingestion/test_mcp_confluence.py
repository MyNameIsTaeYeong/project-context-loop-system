"""Confluence MCP Client 임포트 모듈 테스트."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from context_loop.ingestion.mcp_confluence import (
    SearchEnvelope,
    _extract_page_content,
    _extract_page_title,
    _extract_text,
    _is_cql,
    _parse_json_result,
    build_cql,
    convert_html_to_markdown,
    format_breadcrumb,
    get_all_spaces,
    get_child_pages,
    get_page,
    get_page_with_ancestors,
    get_user_contributed_pages,
    import_page_via_mcp,
    list_available_tools,
    search_content,
    search_content_envelope,
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


@pytest.mark.asyncio
async def test_list_available_tools_logs_tools(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    session = AsyncMock()
    tool1 = MagicMock()
    tool1.name = "searchContent"
    tool1.description = "Search Confluence content"
    tool2 = MagicMock()
    tool2.name = "getPageByID"
    tool2.description = "Get page by id"
    session.list_tools.return_value = MagicMock(tools=[tool1, tool2])

    with caplog.at_level(logging.INFO, logger="context_loop.ingestion.mcp_confluence"):
        await list_available_tools(session)

    messages = [rec.getMessage() for rec in caplog.records]
    assert any("도구 2개 조회됨" in msg for msg in messages)
    assert any("searchContent" in msg for msg in messages)
    assert any("getPageByID" in msg for msg in messages)


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


# --- search_content_envelope 테스트 ---


@pytest.mark.asyncio
async def test_search_content_envelope_full_envelope() -> None:
    """totalSize/size/start/limit 메타가 전부 담긴 envelope을 파싱한다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"results": [{"id": "1"}, {"id": "2"}],'
        ' "start": 0, "limit": 25, "size": 2, "totalSize": 342}'
    )

    env = await search_content_envelope(session, "test", limit=25, start=0)

    assert isinstance(env, SearchEnvelope)
    assert len(env.results) == 2
    assert env.total_size == 342
    assert env.size == 2
    assert env.start == 0
    assert env.limit == 25


@pytest.mark.asyncio
async def test_search_content_envelope_passes_cql_and_paging() -> None:
    """CQL 변환과 start/limit 파라미터가 MCP 호출에 그대로 전달된다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"results": [], "size": 0, "totalSize": 0,'
        ' "start": 100, "limit": 50}'
    )

    await search_content_envelope(session, "결제", limit=50, start=100)

    session.call_tool.assert_called_once_with(
        "searchContent",
        {"cql": 'type = "page" AND text ~ "결제"', "limit": 50, "start": 100},
    )


@pytest.mark.asyncio
async def test_search_content_envelope_list_only_response() -> None:
    """envelope 없이 결과 배열만 오면 total_size=None, size=len(results)."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result('[{"id": "1"}, {"id": "2"}]')

    env = await search_content_envelope(session, "q", limit=25, start=0)

    assert env.total_size is None
    assert env.size == 2
    assert len(env.results) == 2
    assert env.start == 0
    assert env.limit == 25


@pytest.mark.asyncio
async def test_search_content_envelope_total_field_variants() -> None:
    """서버 변종 — totalSize 대신 total 필드를 사용하는 경우도 허용한다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"results": [{"id": "1"}], "size": 1, "total": 999}'
    )

    env = await search_content_envelope(session, "q")

    assert env.total_size == 999


@pytest.mark.asyncio
async def test_search_content_envelope_missing_total_size() -> None:
    """envelope은 있지만 totalSize가 빠진 응답은 total_size=None."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"results": [{"id": "1"}], "size": 1, "start": 0, "limit": 25}'
    )

    env = await search_content_envelope(session, "q")

    assert env.total_size is None
    assert env.size == 1


@pytest.mark.asyncio
async def test_search_content_envelope_empty_response() -> None:
    """결과가 전혀 없거나 텍스트 응답이면 빈 envelope을 반환한다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result("no matches")

    env = await search_content_envelope(session, "q", limit=25, start=0)

    assert env.results == []
    assert env.size == 0
    assert env.total_size is None
    assert env.start == 0
    assert env.limit == 25


@pytest.mark.asyncio
async def test_search_content_envelope_size_derived_when_missing() -> None:
    """envelope에 size 필드가 없으면 results 길이로 대체한다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"results": [{"id": "1"}, {"id": "2"}, {"id": "3"}]}'
    )

    env = await search_content_envelope(session, "q")

    assert env.size == 3


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


# --- get_page_with_ancestors 테스트 ---


@pytest.mark.asyncio
async def test_get_page_with_ancestors_calls_tool_with_ancestors_expand() -> None:
    """expand 파라미터에 ancestors/space가 포함된다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"id": "100", "title": "Overview",'
        ' "space": {"key": "ENG", "name": "Engineering"},'
        ' "ancestors": [{"id": "10", "title": "Docs"}]}'
    )

    page = await get_page_with_ancestors(session, "100")

    session.call_tool.assert_called_once_with(
        "getPageByID",
        {"pageId": "100", "expand": "ancestors,space,version"},
    )
    assert page["title"] == "Overview"
    assert page["space"]["name"] == "Engineering"
    assert page["ancestors"][0]["title"] == "Docs"


@pytest.mark.asyncio
async def test_get_page_with_ancestors_text_fallback() -> None:
    """응답이 dict가 아니면 최소한 id만 포함한 dict를 반환한다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result("plain text")

    page = await get_page_with_ancestors(session, "999")

    assert page == {"id": "999"}


# --- format_breadcrumb 테스트 ---


def test_format_breadcrumb_full_path() -> None:
    """space + ancestors + title 이 모두 있는 일반 케이스."""
    page = {
        "id": "100",
        "title": "Overview",
        "space": {"key": "ENG", "name": "Engineering"},
        "ancestors": [
            {"id": "1", "title": "Docs"},
            {"id": "2", "title": "Architecture"},
        ],
    }
    assert format_breadcrumb(page) == "Engineering / Docs / Architecture / Overview"


def test_format_breadcrumb_no_ancestors() -> None:
    """root 페이지 — ancestors가 비어 있으면 space + title만 결합."""
    page = {
        "id": "100",
        "title": "Overview",
        "space": {"name": "Engineering"},
        "ancestors": [],
    }
    assert format_breadcrumb(page) == "Engineering / Overview"


def test_format_breadcrumb_without_space() -> None:
    """space 정보가 없으면 생략한다."""
    page = {
        "id": "100",
        "title": "Overview",
        "ancestors": [{"title": "Docs"}],
    }
    assert format_breadcrumb(page) == "Docs / Overview"


def test_format_breadcrumb_title_only() -> None:
    """space/ancestors가 전혀 없으면 title만 반환."""
    page = {"id": "100", "title": "Overview"}
    assert format_breadcrumb(page) == "Overview"


def test_format_breadcrumb_space_key_fallback() -> None:
    """space.name 이 없으면 space.key로 폴백한다."""
    page = {
        "id": "100",
        "title": "Overview",
        "space": {"key": "ENG"},
    }
    assert format_breadcrumb(page) == "ENG / Overview"


def test_format_breadcrumb_ancestor_name_fallback() -> None:
    """ancestor에 title이 없으면 name으로 폴백한다."""
    page = {
        "id": "100",
        "title": "Overview",
        "ancestors": [{"name": "Docs"}],
    }
    assert format_breadcrumb(page) == "Docs / Overview"


def test_format_breadcrumb_skips_empty_items() -> None:
    """빈 제목의 ancestor는 건너뛴다."""
    page = {
        "id": "100",
        "title": "Overview",
        "space": {"name": "Engineering"},
        "ancestors": [
            {"title": ""},
            {"title": "Docs"},
            {},
        ],
    }
    assert format_breadcrumb(page) == "Engineering / Docs / Overview"


def test_format_breadcrumb_id_fallback() -> None:
    """title/space/ancestors가 모두 없으면 id 문자열을 반환한다."""
    assert format_breadcrumb({"id": "123456"}) == "123456"


def test_format_breadcrumb_empty_dict_returns_empty_string() -> None:
    """완전 빈 dict는 빈 문자열."""
    assert format_breadcrumb({}) == ""


def test_format_breadcrumb_malformed_ancestors() -> None:
    """ancestors가 list가 아닌 경우에도 예외 없이 처리한다."""
    page = {
        "id": "100",
        "title": "Overview",
        "space": {"name": "Engineering"},
        "ancestors": "not-a-list",
    }
    assert format_breadcrumb(page) == "Engineering / Overview"


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


# --- convert_html_to_markdown 테스트 ---


def test_convert_html_to_markdown() -> None:
    """HTML이 마크다운으로 변환된다."""
    result = convert_html_to_markdown("<h1>Hello</h1><p>World</p>")
    assert "# Hello" in result
    assert "World" in result


def test_convert_html_to_markdown_empty() -> None:
    """빈 HTML은 빈 문자열을 반환한다."""
    assert convert_html_to_markdown("") == ""
    assert convert_html_to_markdown("   ") == ""


def test_convert_html_to_markdown_table() -> None:
    """HTML 테이블이 마크다운 테이블로 변환된다."""
    html = "<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"
    result = convert_html_to_markdown(html)
    assert "A" in result
    assert "1" in result


def test_convert_html_to_markdown_list() -> None:
    """HTML 리스트가 마크다운 리스트로 변환된다."""
    html = "<ul><li>one</li><li>two</li></ul>"
    result = convert_html_to_markdown(html)
    assert "one" in result
    assert "two" in result


@pytest.mark.asyncio
async def test_import_page_via_mcp_converts_html(meta_store: MetadataStore) -> None:
    """임포트 시 HTML 콘텐츠가 마크다운으로 변환되어 저장된다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"id": "p10", "title": "HTML Page", "content": "<h1>Title</h1><p>Body</p>"}'
    )

    result = await import_page_via_mcp(session, meta_store, "p10")
    assert result["created"] is True

    doc = await meta_store.get_document(result["id"])
    assert doc is not None
    # HTML 태그가 아닌 마크다운 형식으로 저장됨
    assert "<h1>" not in doc["original_content"]
    assert "Title" in doc["original_content"]


@pytest.mark.asyncio
async def test_import_page_via_mcp_persists_raw_html(meta_store: MetadataStore) -> None:
    """임포트 시 원본 HTML이 ``raw_content``에 보존된다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"id": "p11", "title": "HTML Page", "content": "<h1>Title</h1><p>Body</p>"}'
    )

    result = await import_page_via_mcp(session, meta_store, "p11")
    doc = await meta_store.get_document(result["id"])
    assert doc is not None
    assert doc["raw_content"] == "<h1>Title</h1><p>Body</p>"


@pytest.mark.asyncio
async def test_import_page_via_mcp_update_refreshes_raw_html(
    meta_store: MetadataStore,
) -> None:
    """내용이 바뀌면 ``raw_content``도 새 HTML로 갱신된다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"id": "p12", "title": "Page", "content": "<p>v1</p>"}'
    )
    await import_page_via_mcp(session, meta_store, "p12")

    session.call_tool.return_value = _make_result(
        '{"id": "p12", "title": "Page", "content": "<p>v2</p>"}'
    )
    result2 = await import_page_via_mcp(session, meta_store, "p12")
    assert result2["changed"] is True

    doc = await meta_store.get_document(result2["id"])
    assert doc is not None
    assert doc["raw_content"] == "<p>v2</p>"
