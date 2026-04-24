"""Confluence MCP Client 임포트 모듈 테스트."""

from __future__ import annotations

import json
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


@pytest.mark.asyncio
async def test_enumerate_space_pages_cql_is_correct() -> None:
    """CQL에 space와 type 필터가 함께 들어간다."""
    calls: list[dict[str, Any]] = []
    session = _make_search_session(
        [[]], total_size=0, record_calls=calls,
    )

    _ = [p async for p in enumerate_space_pages(session, "OPS")]

    assert calls[0]["cql"] == 'space = "OPS" AND type = "page"'


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
