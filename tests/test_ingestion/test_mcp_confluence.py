"""Confluence MCP Client 임포트 모듈 테스트."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from context_loop.ingestion.mcp_confluence import (
    MCPToolError,
    SearchEnvelope,
    _extract_page_content,
    _extract_page_title,
    _extract_page_title_raw,
    _extract_text,
    _looks_like_truncated_json,
    _page_declares_body,
    _page_has_explicit_empty_body,
    fallback_page_title,
    _is_cql,
    _parse_json_result,
    _space_cql,
    build_cql,
    convert_html_to_markdown,
    enumerate_space_pages,
    estimate_space_page_count,
    format_breadcrumb,
    get_all_spaces,
    get_child_pages,
    get_page,
    get_page_with_ancestors,
    get_space_info,
    _subtree_cql,
    enumerate_subtree_pages,
    estimate_subtree_page_count,
    get_user_contributed_pages,
    import_page_via_mcp,
    list_available_tools,
    search_content,
    search_content_envelope,
    walk_subtree,
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
    isError: bool = False  # noqa: N815 — MCP CallToolResult 속성명 그대로


def _make_result(text: str) -> FakeCallToolResult:
    return FakeCallToolResult(content=[FakeTextContent(text=text)])


def _make_error_result(text: str) -> FakeCallToolResult:
    return FakeCallToolResult(
        content=[FakeTextContent(text=text)], isError=True,
    )


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


def test_parse_json_result_prefers_structured_content() -> None:
    """MCP 신규 스펙: structuredContent 가 있으면 content 파싱보다 우선한다."""

    @dataclass
    class ResultWithStructured:
        content: list[FakeTextContent]
        structuredContent: Any

    r = ResultWithStructured(
        content=[FakeTextContent(text='"ignored"')],
        structuredContent={"results": [{"id": "s1"}], "size": 1},
    )
    parsed = _parse_json_result(r)
    assert isinstance(parsed, dict)
    assert parsed["results"][0]["id"] == "s1"


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


def test_extract_page_title_uses_enumerated_fallback() -> None:
    """응답에 제목이 없으면 열거 단계 제목(fallback 인자)을 쓴다."""
    assert _extract_page_title({}, "789", fallback="열거 제목") == "열거 제목"
    assert _extract_page_title(
        {"title": "실제 제목"}, "789", fallback="열거 제목",
    ) == "실제 제목"


def test_extract_page_title_raw_returns_none_when_absent() -> None:
    assert _extract_page_title_raw({}) is None
    assert _extract_page_title_raw({"title": "  "}) is None
    assert _extract_page_title_raw({"title": " My Page "}) == "My Page"
    assert _extract_page_title_raw({"name": "Named"}) == "Named"


def test_fallback_page_title_format() -> None:
    assert fallback_page_title("2222222") == "Confluence Page 2222222"


# --- 본문 결손 판정 헬퍼 테스트 ---


def test_looks_like_truncated_json() -> None:
    assert _looks_like_truncated_json('{"id": "1", "body": "잘린') is True
    assert _looks_like_truncated_json('[{"id": "1"') is True
    assert _looks_like_truncated_json('{"id": "1"}') is False  # 유효 JSON
    assert _looks_like_truncated_json("그냥 평문 본문") is False
    assert _looks_like_truncated_json("") is False


def test_page_declares_body() -> None:
    # 구조화 body — 값이 비어도 선언으로 인정
    assert _page_declares_body({"body": {"storage": {"value": ""}}}) is True
    assert _page_declares_body({"body": {"storage": {}}}) is True
    assert _page_declares_body({"body": "text"}) is True
    assert _page_declares_body({"content": "본문"}) is True
    # 부재/퇴화 케이스
    assert _page_declares_body({"id": "1", "title": "T"}) is False
    assert _page_declares_body({"content": ""}) is False
    assert _page_declares_body({"body": {}}) is False


def test_page_has_explicit_empty_body() -> None:
    # 명시적 value 키가 있어야 빈 본문 덮어쓰기 근거로 인정
    assert _page_has_explicit_empty_body(
        {"body": {"storage": {"value": ""}}},
    ) is True
    assert _page_has_explicit_empty_body({"body": ""}) is True
    assert _page_has_explicit_empty_body({"body": {"storage": {}}}) is False
    assert _page_has_explicit_empty_body({"id": "1"}) is False


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


@pytest.mark.asyncio
async def test_get_page_raises_on_tool_error() -> None:
    """isError=True 응답은 본문/제목으로 오인 임포트하지 않고 예외로 승격한다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_error_result("permission denied")

    with pytest.raises(MCPToolError, match="permission denied"):
        await get_page(session, "123")


@pytest.mark.asyncio
async def test_get_page_raises_on_truncated_json() -> None:
    """길이 cap 으로 잘린 JSON 응답은 본문으로 임포트하지 않고 예외 처리."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"id": "123", "title": "Big Page", "body": {"storage": {"value": "잘린'
    )

    with pytest.raises(MCPToolError, match="잘림 의심"):
        await get_page(session, "123")


@pytest.mark.asyncio
async def test_get_page_unwraps_result_envelope() -> None:
    """{"result": {...페이지...}} 처럼 한 겹 감싼 서버 변종 응답을 풀어낸다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"result": {"id": "123", "title": "Wrapped", "content": "Body"}}'
    )

    page = await get_page(session, "123")
    assert page["title"] == "Wrapped"
    assert page["content"] == "Body"


@pytest.mark.asyncio
async def test_get_page_does_not_unwrap_page_like_payload() -> None:
    """최상위에 페이지 필드가 있으면 래핑 해제를 시도하지 않는다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"id": "123", "title": "Top", "page": {"id": "999", "title": "Inner"}}'
    )

    page = await get_page(session, "123")
    assert page["title"] == "Top"


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
async def test_get_child_pages_list_response() -> None:
    """envelope 없이 list 만 오는 서버 변종 — 한 번에 전부 반환."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result('[{"id": "10", "title": "Child"}]')

    children = await get_child_pages(session, "1")

    assert session.call_tool.call_count == 1
    # pageId 뿐 아니라 start/limit/expand 를 반드시 같이 넘겨야 한다.
    args, _ = session.call_tool.call_args
    assert args[0] == "getChild"
    assert args[1]["pageId"] == "1"
    assert args[1]["start"] == 0
    assert "limit" in args[1]
    assert "expand" in args[1]
    assert len(children) == 1


@pytest.mark.asyncio
async def test_get_child_pages_paginates_until_total_size() -> None:
    """envelope 응답이면 totalSize 에 도달할 때까지 페이지네이션."""
    session = AsyncMock()
    session.call_tool.side_effect = [
        _make_result(
            '{"results":[{"id":"1"},{"id":"2"}],'
            ' "start":0, "limit":2, "size":2, "totalSize":3}',
        ),
        _make_result(
            '{"results":[{"id":"3"}],'
            ' "start":2, "limit":2, "size":1, "totalSize":3}',
        ),
    ]

    children = await get_child_pages(session, "root", page_size=2)

    assert [c["id"] for c in children] == ["1", "2", "3"]
    assert session.call_tool.call_count == 2
    first_args = session.call_tool.call_args_list[0].args[1]
    second_args = session.call_tool.call_args_list[1].args[1]
    assert first_args["start"] == 0 and first_args["limit"] == 2
    assert second_args["start"] == 2 and second_args["limit"] == 2


@pytest.mark.asyncio
async def test_get_child_pages_stops_when_size_less_than_page_size() -> None:
    """totalSize 가 없으면 size < page_size 로 마지막 페이지 판정."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"results":[{"id":"1"}], "start":0, "limit":10, "size":1}',
    )

    children = await get_child_pages(session, "root", page_size=10)

    assert [c["id"] for c in children] == ["1"]
    assert session.call_tool.call_count == 1


@pytest.mark.asyncio
async def test_get_child_pages_accepts_children_envelope_key() -> None:
    """서버 변종: envelope 키가 ``children`` 인 경우도 파싱."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"children":[{"id":"1"},{"id":"2"}], "size":2, "totalSize":2}',
    )

    children = await get_child_pages(session, "root")

    assert [c["id"] for c in children] == ["1", "2"]


@pytest.mark.asyncio
async def test_get_child_pages_accepts_nested_page_results() -> None:
    """서버 변종: ``{page: {results: [...]}}`` 중첩 envelope 파싱."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"page":{"results":[{"id":"1"}]}, "size":1}',
    )

    children = await get_child_pages(session, "root")

    assert [c["id"] for c in children] == ["1"]


@pytest.mark.asyncio
async def test_get_child_pages_default_expand_is_nonempty() -> None:
    """빈 expand 를 거부하는 서버에 대비해 기본값은 비어있지 않아야 한다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result('[]')

    await get_child_pages(session, "1")

    args = session.call_tool.call_args.args[1]
    assert args["expand"] != ""


# --- walk_subtree 테스트 ---


def _make_tree_session(
    child_map: dict[str, list[dict[str, Any]]],
    *,
    raise_for: set[str] | None = None,
) -> Any:
    """``getChild`` 호출 인자에 따라 자식 목록을 돌려주는 가짜 세션.

    ``raise_for`` 에 포함된 ``pageId`` 로 호출되면 예외를 던진다 — 에러 격리
    동작 검증용.
    """
    raise_for = raise_for or set()

    class FakeSession:
        async def call_tool(self, tool_name: str, args: dict[str, Any]):
            if tool_name != "getChild":
                raise AssertionError(f"Unexpected tool: {tool_name}")
            pid = str(args.get("pageId"))
            if pid in raise_for:
                raise RuntimeError(f"simulated failure for {pid}")
            return _make_result(json.dumps(child_map.get(pid, [])))

    return FakeSession()


@pytest.mark.asyncio
async def test_walk_subtree_bfs_order_and_metadata() -> None:
    """BFS 순서로 전개되고 parent_id/depth/title이 정확히 채워진다."""
    tree = {
        "root": [{"id": "a", "title": "A"}, {"id": "b", "title": "B"}],
        "a": [{"id": "a1", "title": "A1"}],
        "b": [{"id": "b1", "title": "B1"}],
        "a1": [],
        "b1": [],
    }
    session = _make_tree_session(tree)

    nodes = await walk_subtree(session, "root")

    assert [n["id"] for n in nodes] == ["root", "a", "b", "a1", "b1"]
    assert nodes[0] == {"id": "root", "parent_id": None, "depth": 0, "title": ""}
    a = next(n for n in nodes if n["id"] == "a")
    assert a == {"id": "a", "parent_id": "root", "depth": 1, "title": "A"}
    a1 = next(n for n in nodes if n["id"] == "a1")
    assert a1 == {"id": "a1", "parent_id": "a", "depth": 2, "title": "A1"}


@pytest.mark.asyncio
async def test_walk_subtree_leaf_root_returns_only_root() -> None:
    """자식이 없는 루트는 본인만 반환."""
    session = _make_tree_session({"solo": []})
    nodes = await walk_subtree(session, "solo")
    assert nodes == [{"id": "solo", "parent_id": None, "depth": 0, "title": ""}]


@pytest.mark.asyncio
async def test_walk_subtree_cycle_is_broken() -> None:
    """A → B → A 사이클이 있어도 무한루프 없이 각 노드를 1회만 방문한다."""
    tree = {
        "a": [{"id": "b", "title": "B"}],
        "b": [{"id": "a", "title": "A-again"}],
    }
    session = _make_tree_session(tree)

    nodes = await walk_subtree(session, "a")

    ids = [n["id"] for n in nodes]
    assert ids == ["a", "b"]


@pytest.mark.asyncio
async def test_walk_subtree_respects_max_depth() -> None:
    """max_depth=1 이면 루트 + 직계 자식까지만 수집한다."""
    tree = {
        "r": [{"id": "c1"}, {"id": "c2"}],
        "c1": [{"id": "g1"}],
        "c2": [{"id": "g2"}],
    }
    session = _make_tree_session(tree)

    nodes = await walk_subtree(session, "r", max_depth=1)

    assert sorted(n["id"] for n in nodes) == ["c1", "c2", "r"]
    assert all(n["depth"] <= 1 for n in nodes)


@pytest.mark.asyncio
async def test_walk_subtree_respects_max_pages() -> None:
    """max_pages 도달 시 더 수집하지 않고 조기 반환한다."""
    tree = {
        "r": [{"id": f"c{i}"} for i in range(10)],
    }
    for i in range(10):
        tree[f"c{i}"] = []
    session = _make_tree_session(tree)

    nodes = await walk_subtree(session, "r", max_pages=3)

    assert len(nodes) == 3


@pytest.mark.asyncio
async def test_walk_subtree_filters_non_page_types() -> None:
    """type != 'page' 인 자식은 건너뛴다. type 필드 없으면 page로 간주."""
    tree = {
        "r": [
            {"id": "p1", "title": "Page", "type": "page"},
            {"id": "bp1", "title": "Blog", "type": "blogpost"},
            {"id": "at1", "title": "Attach", "type": "attachment"},
            {"id": "p2", "title": "No Type"},   # type 필드 없음 → 통과
        ],
        "p1": [],
        "p2": [],
    }
    session = _make_tree_session(tree)

    nodes = await walk_subtree(session, "r")

    ids = {n["id"] for n in nodes}
    assert ids == {"r", "p1", "p2"}
    assert "bp1" not in ids
    assert "at1" not in ids


@pytest.mark.asyncio
async def test_walk_subtree_skips_children_without_id() -> None:
    """id 필드가 없는 자식은 스킵한다."""
    tree = {
        "r": [
            {"id": "c1", "title": "A"},
            {"title": "orphan-no-id"},
            {"id": "", "title": "empty-id"},
        ],
        "c1": [],
    }
    session = _make_tree_session(tree)

    nodes = await walk_subtree(session, "r")
    ids = [n["id"] for n in nodes]
    assert ids == ["r", "c1"]


@pytest.mark.asyncio
async def test_walk_subtree_isolates_branch_errors() -> None:
    """특정 노드의 getChild 가 실패해도 다른 가지는 계속 진행된다."""
    tree = {
        "r": [{"id": "a"}, {"id": "b"}],
        "a": [{"id": "a1"}],   # 정상
        "b": [{"id": "b1"}],   # 호출 자체가 실패할 예정
        "a1": [],
        "b1": [],
    }
    session = _make_tree_session(tree, raise_for={"b"})

    nodes = await walk_subtree(session, "r")

    ids = {n["id"] for n in nodes}
    # b는 자식 탐색 실패했지만 본인은 수집됨, b1만 누락
    assert {"r", "a", "b", "a1"}.issubset(ids)
    assert "b1" not in ids


@pytest.mark.asyncio
async def test_walk_subtree_missing_title_defaults_to_empty() -> None:
    """자식에 title이 없으면 빈 문자열로 채워진다."""
    tree = {"r": [{"id": "c1"}], "c1": []}
    session = _make_tree_session(tree)

    nodes = await walk_subtree(session, "r")
    c1 = next(n for n in nodes if n["id"] == "c1")
    assert c1["title"] == ""


@pytest.mark.asyncio
async def test_walk_subtree_deduplicates_siblings_pointing_to_same_id() -> None:
    """동일 ID가 자식 목록에 중복 등장해도 한 번만 수집된다."""
    tree = {
        "r": [{"id": "x"}, {"id": "x"}, {"id": "y"}],
        "x": [],
        "y": [],
    }
    session = _make_tree_session(tree)

    nodes = await walk_subtree(session, "r")
    ids = [n["id"] for n in nodes]
    assert ids == ["r", "x", "y"]


# --- _space_cql 테스트 ---


def test_space_cql_basic() -> None:
    assert _space_cql("ENG") == 'space = "ENG" AND type = "page"'


def test_space_cql_escapes_double_quote() -> None:
    assert _space_cql('AB"C') == 'space = "AB\\"C" AND type = "page"'


def test_space_cql_escapes_backslash() -> None:
    assert _space_cql("AB\\C") == 'space = "AB\\\\C" AND type = "page"'


# --- estimate_space_page_count / enumerate_space_pages 테스트 ---


def _make_search_session(
    pages: list[list[dict[str, Any]]],
    *,
    total_size: int | None = None,
    record_calls: list[dict[str, Any]] | None = None,
) -> Any:
    """``searchContent`` 호출마다 연속 페이지를 돌려주는 가짜 세션.

    ``pages[0]`` 은 첫 호출, ``pages[1]`` 은 두 번째 호출… 범위를 넘어가면
    빈 results 를 반환한다. ``record_calls`` 가 주어지면 호출 인자를 누적 기록.
    """
    call_idx = {"i": 0}

    class FakeSession:
        async def call_tool(self, tool_name: str, args: dict[str, Any]):
            if tool_name != "searchContent":
                raise AssertionError(f"Unexpected tool: {tool_name}")
            if record_calls is not None:
                record_calls.append(args)
            i = call_idx["i"]
            call_idx["i"] += 1
            results = pages[i] if i < len(pages) else []
            envelope: dict[str, Any] = {
                "results": results,
                "size": len(results),
                "start": args.get("start", 0),
                "limit": args.get("limit", 25),
            }
            if total_size is not None:
                envelope["totalSize"] = total_size
            return _make_result(json.dumps(envelope))

    return FakeSession()


@pytest.mark.asyncio
async def test_estimate_space_page_count_returns_total_size() -> None:
    """totalSize가 있으면 그대로 반환한다."""
    calls: list[dict[str, Any]] = []
    session = _make_search_session(
        [[{"id": "1"}]], total_size=342, record_calls=calls,
    )

    count = await estimate_space_page_count(session, "ENG")

    assert count == 342
    assert calls[0] == {
        "cql": 'space = "ENG" AND type = "page"', "limit": 1, "start": 0,
    }


@pytest.mark.asyncio
async def test_estimate_space_page_count_returns_none_when_server_omits() -> None:
    """서버가 totalSize를 내려주지 않으면 None."""
    session = _make_search_session([[{"id": "1"}]], total_size=None)
    assert await estimate_space_page_count(session, "ENG") is None


@pytest.mark.asyncio
async def test_enumerate_space_pages_single_page_completion() -> None:
    """totalSize에 맞게 한 페이지만에 모두 소진."""
    session = _make_search_session(
        [[{"id": "1"}, {"id": "2"}, {"id": "3"}]],
        total_size=3,
    )

    collected = [p async for p in enumerate_space_pages(session, "ENG", page_size=100)]

    assert [p["id"] for p in collected] == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_enumerate_space_pages_multiple_pages_with_total_size() -> None:
    """여러 페이지에 걸친 요청이 올바른 start로 이어진다."""
    calls: list[dict[str, Any]] = []
    session = _make_search_session(
        [
            [{"id": "1"}, {"id": "2"}],
            [{"id": "3"}],
        ],
        total_size=3,
        record_calls=calls,
    )

    collected = [p async for p in enumerate_space_pages(session, "ENG", page_size=2)]

    assert [p["id"] for p in collected] == ["1", "2", "3"]
    # 두 번의 searchContent: start=0 → start=2
    assert [c["start"] for c in calls] == [0, 2]
    assert all(c["limit"] == 2 for c in calls)


@pytest.mark.asyncio
async def test_enumerate_space_pages_terminates_on_short_page_without_total_size() -> None:
    """totalSize가 없을 때 size < page_size 응답이 오면 종료한다."""
    session = _make_search_session(
        [
            [{"id": "1"}, {"id": "2"}],
            [{"id": "3"}],   # size=1 < page_size=2 → 종료
        ],
        total_size=None,
    )

    collected = [p async for p in enumerate_space_pages(session, "ENG", page_size=2)]
    assert [p["id"] for p in collected] == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_enumerate_space_pages_terminates_on_empty_response() -> None:
    """totalSize 없고 size가 page_size와 같을 때는 추가 호출로 빈 응답을 확인해 종료."""
    session = _make_search_session(
        [
            [{"id": "1"}, {"id": "2"}],
            [{"id": "3"}, {"id": "4"}],
            [],   # 빈 응답 → 종료
        ],
        total_size=None,
    )

    collected = [p async for p in enumerate_space_pages(session, "ENG", page_size=2)]
    assert [p["id"] for p in collected] == ["1", "2", "3", "4"]


@pytest.mark.asyncio
async def test_enumerate_space_pages_empty_space() -> None:
    """페이지가 0개인 공간은 아무것도 yield 하지 않는다."""
    session = _make_search_session([[]], total_size=0)
    collected = [p async for p in enumerate_space_pages(session, "ENG")]
    assert collected == []


@pytest.mark.asyncio
async def test_enumerate_space_pages_respects_max_pages_cap() -> None:
    """max_pages 상한에 도달하면 중단한다."""
    session = _make_search_session(
        [[{"id": str(i)} for i in range(10)]],
        total_size=100,   # 실제 100개라 주장
    )

    collected = [
        p async for p in enumerate_space_pages(
            session, "ENG", page_size=10, max_pages=3,
        )
    ]
    assert [p["id"] for p in collected] == ["0", "1", "2"]


@pytest.mark.asyncio
async def test_enumerate_space_pages_skips_non_dict_items() -> None:
    """results 배열에 dict가 아닌 항목이 섞이면 건너뛴다."""
    session = _make_search_session(
        [[{"id": "1"}, "unexpected string", {"id": "2"}]],
        total_size=3,   # 2개만 yield하므로 다음 페이지 시도할 수 있지만 빈 응답으로 종료
    )

    collected = [p async for p in enumerate_space_pages(session, "ENG", page_size=100)]
    assert [p["id"] for p in collected] == ["1", "2"]


def _make_capping_search_session(
    items: list[dict[str, Any]],
    *,
    server_cap: int,
    total_size: int | None = None,
    record_calls: list[dict[str, Any]] | None = None,
) -> Any:
    """``start`` 를 실제로 존중하고 응답당 ``server_cap`` 까지만 돌려주는 fake.

    요청 ``limit`` 와 무관하게 서버가 응답당 개수를 cap 하는 상황을 재현한다.
    totalSize 는 전체 건수로 고정 (보통 ``len(items)`` 와 같음).
    """
    effective_total = total_size if total_size is not None else len(items)

    class FakeSession:
        async def call_tool(self, tool_name: str, args: dict[str, Any]):
            if tool_name != "searchContent":
                raise AssertionError(f"Unexpected tool: {tool_name}")
            if record_calls is not None:
                record_calls.append(args)
            start = int(args.get("start", 0))
            # 서버가 요청한 limit 을 무시하고 server_cap 만큼만 돌려준다.
            chunk = items[start:start + server_cap]
            envelope = {
                "results": chunk,
                "size": len(chunk),
                "start": start,
                "limit": args.get("limit", 25),
                "totalSize": effective_total,
            }
            return _make_result(json.dumps(envelope))

    return FakeSession()


@pytest.mark.asyncio
async def test_enumerate_space_pages_handles_server_cap_smaller_than_page_size() -> None:
    """서버가 응답당 개수를 cap 해도 totalSize 까지 모두 열거한다.

    회귀 방지: 기존 로직은 ``size < page_size`` 에서 무조건 break 해버려 첫
    응답 이후 나머지를 잃었다. totalSize 가 알려진 경우 short-page 휴리스틱을
    건너뛰고 ``start`` 를 실제 반환 개수만큼 전진시켜야 한다.
    """
    calls: list[dict[str, Any]] = []
    items = [{"id": str(i)} for i in range(50)]
    session = _make_capping_search_session(
        items, server_cap=25, total_size=50, record_calls=calls,
    )

    collected = [
        p async for p in enumerate_space_pages(
            session, "ENG", page_size=100,
        )
    ]

    assert len(collected) == 50
    assert [p["id"] for p in collected] == [str(i) for i in range(50)]
    # start=0 → 25, start=25 → 25 로 두 번의 호출로 모두 열거.
    assert [c["start"] for c in calls] == [0, 25]


@pytest.mark.asyncio
async def test_enumerate_subtree_pages_handles_server_cap_smaller_than_page_size() -> None:
    """서브트리 경로도 동일한 cap 대응을 상속한다(공통 헬퍼 `_paginate_cql`)."""
    calls: list[dict[str, Any]] = []
    items = [{"id": f"d{i}"} for i in range(30)]
    session = _make_capping_search_session(
        items, server_cap=10, total_size=30, record_calls=calls,
    )

    collected = [
        p async for p in enumerate_subtree_pages(
            session, "100", page_size=100,
        )
    ]

    assert len(collected) == 30
    assert [p["id"] for p in collected] == [f"d{i}" for i in range(30)]
    assert [c["start"] for c in calls] == [0, 10, 20]


@pytest.mark.asyncio
async def test_enumerate_space_pages_cql_is_correct() -> None:
    """CQL에 space와 type 필터가 함께 들어간다."""
    calls: list[dict[str, Any]] = []
    session = _make_search_session(
        [[]], total_size=0, record_calls=calls,
    )

    _ = [p async for p in enumerate_space_pages(session, "OPS")]

    assert calls[0]["cql"] == 'space = "OPS" AND type = "page"'


# --- _subtree_cql / estimate_subtree_page_count / enumerate_subtree_pages 테스트 ---


def test_subtree_cql_escapes_quotes_and_backslashes() -> None:
    assert _subtree_cql("12345") == 'ancestor = "12345" AND type = "page"'
    assert _subtree_cql('a"b') == 'ancestor = "a\\"b" AND type = "page"'
    assert _subtree_cql("c\\d") == 'ancestor = "c\\\\d" AND type = "page"'


@pytest.mark.asyncio
async def test_estimate_subtree_page_count_returns_total_size() -> None:
    """totalSize 를 그대로 돌려주고, CQL 은 ancestor 기반."""
    calls: list[dict[str, Any]] = []
    session = _make_search_session(
        [[{"id": "1"}]], total_size=57, record_calls=calls,
    )

    count = await estimate_subtree_page_count(session, "100")

    assert count == 57
    assert calls[0] == {
        "cql": 'ancestor = "100" AND type = "page"', "limit": 1, "start": 0,
    }


@pytest.mark.asyncio
async def test_estimate_subtree_page_count_returns_none_when_server_omits() -> None:
    session = _make_search_session([[{"id": "1"}]], total_size=None)
    assert await estimate_subtree_page_count(session, "100") is None


@pytest.mark.asyncio
async def test_enumerate_subtree_pages_multiple_pages_with_total_size() -> None:
    """CQL 페이지네이션으로 모든 depth 후손을 한 번에 열거한다."""
    session = _make_search_session(
        [
            [{"id": "d1"}, {"id": "d2"}],
            [{"id": "d3"}],
        ],
        total_size=3,
    )

    collected = [
        p async for p in enumerate_subtree_pages(session, "100", page_size=2)
    ]

    assert [p["id"] for p in collected] == ["d1", "d2", "d3"]


@pytest.mark.asyncio
async def test_enumerate_subtree_pages_terminates_on_short_page_without_total_size() -> None:
    """totalSize 가 없을 때 size < page_size 를 마지막 페이지로 간주한다."""
    session = _make_search_session(
        [[{"id": "d1"}]],  # size=1 < page_size=10
        total_size=None,
    )

    collected = [
        p async for p in enumerate_subtree_pages(session, "100", page_size=10)
    ]

    assert [p["id"] for p in collected] == ["d1"]


@pytest.mark.asyncio
async def test_enumerate_subtree_pages_empty_subtree() -> None:
    session = _make_search_session([[]], total_size=0)
    collected = [p async for p in enumerate_subtree_pages(session, "100")]
    assert collected == []


@pytest.mark.asyncio
async def test_enumerate_subtree_pages_respects_max_pages_cap() -> None:
    """max_pages 상한 초과 시 조기 반환."""
    session = _make_search_session(
        [[{"id": str(i)} for i in range(200)]],
        total_size=200,
    )

    collected = [
        p async for p in enumerate_subtree_pages(
            session, "100", page_size=100, max_pages=5,
        )
    ]

    assert len(collected) == 5


@pytest.mark.asyncio
async def test_enumerate_subtree_pages_cql_is_correct() -> None:
    """CQL 에 ancestor 와 type 필터가 함께 들어간다."""
    calls: list[dict[str, Any]] = []
    session = _make_search_session(
        [[]], total_size=0, record_calls=calls,
    )

    _ = [p async for p in enumerate_subtree_pages(session, "42")]

    assert calls[0]["cql"] == 'ancestor = "42" AND type = "page"'


@pytest.mark.asyncio
async def test_get_all_spaces_list_response() -> None:
    """envelope 없이 list 만 돌려주는 서버 변종 — 한 번에 전부 반환."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '[{"id": "s1", "key": "DEV", "name": "Dev Team"}]',
    )

    spaces = await get_all_spaces(session)

    assert session.call_tool.call_count == 1
    # start/limit 은 필수 인자이므로 반드시 같이 전달되어야 한다.
    args, kwargs = session.call_tool.call_args
    assert args[0] == "getSpaceInfoAll"
    assert args[1]["start"] == 0
    assert "limit" in args[1]
    assert spaces[0]["key"] == "DEV"


@pytest.mark.asyncio
async def test_get_all_spaces_paginates_until_total_size() -> None:
    """envelope 응답에서 totalSize 에 도달할 때까지 페이지네이션 한다."""
    session = AsyncMock()
    responses = [
        _make_result(
            '{"results":[{"key":"A"},{"key":"B"}],'
            ' "start":0, "limit":2, "size":2, "totalSize":3}',
        ),
        _make_result(
            '{"results":[{"key":"C"}],'
            ' "start":2, "limit":2, "size":1, "totalSize":3}',
        ),
    ]
    session.call_tool.side_effect = responses

    spaces = await get_all_spaces(session, page_size=2)

    assert [s["key"] for s in spaces] == ["A", "B", "C"]
    assert session.call_tool.call_count == 2
    first_call_args = session.call_tool.call_args_list[0].args[1]
    second_call_args = session.call_tool.call_args_list[1].args[1]
    assert first_call_args == {"start": 0, "limit": 2}
    assert second_call_args == {"start": 2, "limit": 2}


@pytest.mark.asyncio
async def test_get_all_spaces_stops_when_size_less_than_page_size() -> None:
    """totalSize 가 없으면 size < page_size 로 마지막 페이지 판정."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"results":[{"key":"A"}], "start":0, "limit":10, "size":1}',
    )

    spaces = await get_all_spaces(session, page_size=10)

    assert [s["key"] for s in spaces] == ["A"]
    assert session.call_tool.call_count == 1


@pytest.mark.asyncio
async def test_get_all_spaces_stops_on_empty_results() -> None:
    """빈 results 이면 즉시 종료."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"results":[], "start":0, "limit":10, "size":0}',
    )

    spaces = await get_all_spaces(session)

    assert spaces == []
    assert session.call_tool.call_count == 1


@pytest.mark.asyncio
async def test_get_space_info_returns_dict() -> None:
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"key": "ENG", "name": "Engineering", "id": "42"}',
    )

    info = await get_space_info(session, "ENG")

    session.call_tool.assert_called_once_with("getSpaceInfo", {"spaceKey": "ENG"})
    assert info["name"] == "Engineering"


@pytest.mark.asyncio
async def test_get_space_info_falls_back_to_key_on_non_dict_response() -> None:
    session = AsyncMock()
    session.call_tool.return_value = _make_result("opaque text response")

    info = await get_space_info(session, "ENG")
    assert info == {"key": "ENG"}


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


@pytest.mark.asyncio
async def test_import_page_title_only_change_reindexes(
    meta_store: MetadataStore,
) -> None:
    """본문 해시가 동일해도 제목이 바뀌면 changed=True + 제목 갱신."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"id": "t1", "title": "Old Title", "content": "Same Body"}'
    )
    result1 = await import_page_via_mcp(session, meta_store, "t1")
    assert result1["created"] is True

    # 제목만 변경 (본문 동일)
    session.call_tool.return_value = _make_result(
        '{"id": "t1", "title": "New Title", "content": "Same Body"}'
    )
    result2 = await import_page_via_mcp(session, meta_store, "t1")
    assert result2["created"] is False
    assert result2["changed"] is True  # Phase 2 재인덱싱 대상으로 분류
    assert result2["title"] == "New Title"
    assert result2["status"] == "changed"

    # 제목·본문 모두 그대로면 unchanged 유지
    result3 = await import_page_via_mcp(session, meta_store, "t1")
    assert result3["changed"] is False


@pytest.mark.asyncio
async def test_import_page_heals_fallback_title(
    meta_store: MetadataStore,
) -> None:
    """폴백 제목으로 오염된 문서가 실제 제목 확보 시 치유된다."""
    session = AsyncMock()
    # 1차: 제목 없는 응답 → 폴백 제목으로 저장
    session.call_tool.return_value = _make_result(
        '{"id": "t2", "content": "Body"}'
    )
    result1 = await import_page_via_mcp(session, meta_store, "t2")
    assert result1["title"] == "Confluence Page t2"

    # 2차: 정상 응답 (본문 동일) → 제목 치유 + 재인덱싱 분류
    session.call_tool.return_value = _make_result(
        '{"id": "t2", "title": "Real Title", "content": "Body"}'
    )
    result2 = await import_page_via_mcp(session, meta_store, "t2")
    assert result2["changed"] is True
    assert result2["title"] == "Real Title"


@pytest.mark.asyncio
async def test_import_page_never_overwrites_title_with_fallback(
    meta_store: MetadataStore,
) -> None:
    """제목 추출 실패 시 기존의 정상 제목을 폴백으로 덮어쓰지 않는다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"id": "t3", "title": "Good Title", "content": "V1"}'
    )
    await import_page_via_mcp(session, meta_store, "t3")

    # 본문은 변경됐지만 제목이 누락된 응답
    session.call_tool.return_value = _make_result(
        '{"id": "t3", "content": "V2"}'
    )
    result2 = await import_page_via_mcp(session, meta_store, "t3")
    assert result2["changed"] is True
    assert result2["title"] == "Good Title"  # 폴백으로 덮어쓰지 않음


@pytest.mark.asyncio
async def test_import_page_uses_enumerated_title_fallback(
    meta_store: MetadataStore,
) -> None:
    """getPageByID 응답에 제목이 없으면 열거 단계 제목을 사용한다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"id": "t4", "content": "Body"}'
    )
    result = await import_page_via_mcp(
        session, meta_store, "t4", enumerated_title="열거된 제목",
    )
    assert result["title"] == "열거된 제목"


@pytest.mark.asyncio
async def test_import_page_raises_on_tool_error(
    meta_store: MetadataStore,
) -> None:
    """isError 응답은 문서를 생성하지 않고 예외로 전파된다 (재시도 목록행)."""
    session = AsyncMock()
    session.call_tool.return_value = _make_error_result("server exploded")

    with pytest.raises(MCPToolError):
        await import_page_via_mcp(session, meta_store, "t5")

    assert await meta_store.get_document_by_source("confluence_mcp", "t5") is None


@pytest.mark.asyncio
async def test_import_page_raises_when_body_fields_absent(
    meta_store: MetadataStore,
) -> None:
    """body 계열 필드가 아예 없는 응답은 본문 추출 실패로 예외 처리한다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"id": "b1", "title": "Meta Only"}'
    )

    with pytest.raises(MCPToolError, match="본문 추출 실패"):
        await import_page_via_mcp(session, meta_store, "b1")

    assert await meta_store.get_document_by_source("confluence_mcp", "b1") is None


@pytest.mark.asyncio
async def test_import_page_allows_genuinely_empty_page(
    meta_store: MetadataStore,
) -> None:
    """body 필드가 있고 값만 빈 진짜 빈 페이지는 정상 임포트된다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"id": "b2", "title": "Container", '
        '"body": {"storage": {"value": ""}}}'
    )

    result = await import_page_via_mcp(session, meta_store, "b2")
    assert result["created"] is True
    assert result["title"] == "Container"
    assert result["original_content"] == ""


@pytest.mark.asyncio
async def test_import_page_refuses_empty_overwrite_without_explicit_body(
    meta_store: MetadataStore,
) -> None:
    """명시적 빈 body 값 없이 기존 본문을 빈 값으로 덮어쓰지 않는다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"id": "b3", "title": "T", "content": "정상 본문"}'
    )
    result1 = await import_page_via_mcp(session, meta_store, "b3")

    # 결손 변종 — body 선언은 있으나 명시적 value 없음
    session.call_tool.return_value = _make_result(
        '{"id": "b3", "title": "T", "body": {"storage": {}}}'
    )
    with pytest.raises(MCPToolError, match="덮어쓰기 거부"):
        await import_page_via_mcp(session, meta_store, "b3")

    doc = await meta_store.get_document(result1["id"])
    assert doc is not None
    assert doc["original_content"] == "정상 본문"  # 본문 보존


@pytest.mark.asyncio
async def test_import_page_allows_explicit_empty_body_overwrite(
    meta_store: MetadataStore,
) -> None:
    """Confluence 에서 실제로 비워진 페이지(body.storage.value=="")는 반영된다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"id": "b4", "title": "T", "content": "지워질 본문"}'
    )
    result1 = await import_page_via_mcp(session, meta_store, "b4")

    session.call_tool.return_value = _make_result(
        '{"id": "b4", "title": "T", "body": {"storage": {"value": ""}}}'
    )
    result2 = await import_page_via_mcp(session, meta_store, "b4")
    assert result2["changed"] is True

    doc = await meta_store.get_document(result1["id"])
    assert doc is not None
    assert doc["original_content"] == ""


@pytest.mark.asyncio
async def test_import_page_via_mcp_stores_source_version(
    meta_store: MetadataStore,
) -> None:
    """getPageByID 응답의 version.number 가 documents.source_version 에 저장된다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"id": "pv1", "title": "P", "content": "Body", '
        '"version": {"number": 7}}'
    )

    result = await import_page_via_mcp(session, meta_store, "pv1")
    doc = await meta_store.get_document(result["id"])
    assert doc is not None
    assert doc["source_version"] == 7


@pytest.mark.asyncio
async def test_import_page_via_mcp_unchanged_hash_still_updates_version(
    meta_store: MetadataStore,
) -> None:
    """본문 해시는 동일한데 리비전만 오른 경우 source_version 만 따라간다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"id": "pv2", "title": "P", "content": "Same", '
        '"version": {"number": 3}}'
    )
    result1 = await import_page_via_mcp(session, meta_store, "pv2")

    # 메타데이터성 편집 — 본문 동일, version 만 4 로 증가
    session.call_tool.return_value = _make_result(
        '{"id": "pv2", "title": "P", "content": "Same", '
        '"version": {"number": 4}}'
    )
    result2 = await import_page_via_mcp(session, meta_store, "pv2")
    assert result2["changed"] is False

    doc = await meta_store.get_document(result1["id"])
    assert doc is not None
    assert doc["source_version"] == 4


@pytest.mark.asyncio
async def test_import_page_via_mcp_version_flat_int(
    meta_store: MetadataStore,
) -> None:
    """서버 변종 — version 이 int 로 평탄화된 응답도 처리한다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"id": "pv3", "title": "P", "content": "Body", "version": 12}'
    )
    result = await import_page_via_mcp(session, meta_store, "pv3")
    doc = await meta_store.get_document(result["id"])
    assert doc is not None
    assert doc["source_version"] == 12


@pytest.mark.asyncio
async def test_import_page_via_mcp_version_missing_is_null(
    meta_store: MetadataStore,
) -> None:
    """version 필드가 없으면 source_version 은 NULL."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"id": "pv4", "title": "P", "content": "Body"}'
    )
    result = await import_page_via_mcp(session, meta_store, "pv4")
    doc = await meta_store.get_document(result["id"])
    assert doc is not None
    assert doc["source_version"] is None


@pytest.mark.asyncio
async def test_get_document_by_source_lookup(meta_store: MetadataStore) -> None:
    """(source_type, source_id) 단건 lookup 이 전체 스캔을 대체한다."""
    doc_id = await meta_store.create_document(
        source_type="confluence_mcp", source_id="42",
        title="T", original_content="c", content_hash="h",
    )
    found = await meta_store.get_document_by_source("confluence_mcp", "42")
    assert found is not None
    assert found["id"] == doc_id
    assert await meta_store.get_document_by_source("confluence_mcp", "43") is None
    assert await meta_store.get_document_by_source("git_code", "42") is None


# --- modified_since CQL 테스트 ---


@pytest.mark.asyncio
async def test_enumerate_space_pages_with_modified_since_appends_cql() -> None:
    """modified_since 지정 시 lastModified 조건이 CQL 에 추가된다."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"results": [{"id": "1"}], "totalSize": 1, "size": 1}'
    )

    pages = [
        p async for p in enumerate_space_pages(
            session, "ENG", modified_since="2026-07-01 09:00",
        )
    ]
    assert [p["id"] for p in pages] == ["1"]
    cql = session.call_tool.call_args.args[1]["cql"]
    assert 'space = "ENG" AND type = "page"' in cql
    assert 'lastModified >= "2026-07-01 09:00"' in cql


@pytest.mark.asyncio
async def test_enumerate_space_pages_without_modified_since_keeps_cql() -> None:
    """modified_since 미지정 시 기존 CQL 그대로."""
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"results": [{"id": "1"}], "totalSize": 1, "size": 1}'
    )

    _ = [p async for p in enumerate_space_pages(session, "ENG")]
    cql = session.call_tool.call_args.args[1]["cql"]
    assert "lastModified" not in cql


@pytest.mark.asyncio
async def test_enumerate_subtree_pages_with_modified_since_appends_cql() -> None:
    session = AsyncMock()
    session.call_tool.return_value = _make_result(
        '{"results": [{"id": "2"}], "totalSize": 1, "size": 1}'
    )

    _ = [
        p async for p in enumerate_subtree_pages(
            session, "100", modified_since="2026-07-01 09:00",
        )
    ]
    cql = session.call_tool.call_args.args[1]["cql"]
    assert 'ancestor = "100" AND type = "page"' in cql
    assert 'lastModified >= "2026-07-01 09:00"' in cql


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
