"""Confluence MCP Client 임포트 모듈.

사내 Confluence MCP Server에 MCP Client로 연결하여
문서를 가져와 메타데이터 저장소에 등록한다.

MCP Server가 제공하는 도구:
  - searchContent: 콘텐츠 키워드 검색
  - getPageByID: 페이지 단건 조회 (본문 포함)
  - getChild: 하위 페이지 목록 조회
  - getSpaceInfoAll: 전체 스페이스 목록 조회
  - getSpaceInfo: 특정 스페이스 정보
  - getUserContributedPages: 사용자 기여 페이지 목록
"""

from __future__ import annotations

import json
import logging
import re
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client

from context_loop.ingestion.uploader import compute_content_hash
from context_loop.storage.metadata_store import MetadataStore

logger = logging.getLogger(__name__)

SOURCE_TYPE = "confluence_mcp"


class MCPConnectionError(Exception):
    """MCP 서버 연결 실패 시 발생한다."""


class MCPToolError(Exception):
    """MCP 도구 호출 실패 시 발생한다."""


def _extract_text(result: Any) -> str:
    """CallToolResult에서 텍스트 내용을 추출한다."""
    if hasattr(result, "content"):
        parts = []
        for block in result.content:
            if hasattr(block, "text"):
                parts.append(block.text)
        return "\n".join(parts)
    return str(result)


def _parse_json_result(result: Any) -> Any:
    """CallToolResult에서 JSON을 파싱한다.

    텍스트 형태의 결과를 JSON으로 파싱 시도하고,
    실패하면 원본 텍스트를 반환한다.
    """
    text = _extract_text(result)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


def connect_mcp(
    server_url: str,
    token: str | None = None,
    transport: str = "http",
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
):
    """MCP 서버에 연결한다.

    컨텍스트 매니저로 사용한다::

        async with connect_mcp("http://mcp-server:3001/mcp", token="xxx") as session:
            tools = await session.list_tools()

    Args:
        server_url: MCP 서버 엔드포인트 URL.
        token: 인증 토큰. ``x-auth`` 헤더로 전달된다.
        transport: 전송 방식. ``"http"`` (Streamable HTTP) 또는 ``"sse"``.
        headers: 추가 커스텀 헤더. token이 지정되면 ``x-auth`` 가 자동 추가된다.
        timeout: 연결 타임아웃(초).

    Yields:
        초기화된 ClientSession.
    """
    return _MCPConnection(
        server_url, token=token, transport=transport, headers=headers, timeout=timeout,
    )


class _MCPConnection:
    """MCP 연결을 관리하는 비동기 컨텍스트 매니저."""

    def __init__(
        self,
        server_url: str,
        *,
        token: str | None = None,
        transport: str = "http",
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._server_url = server_url
        self._token = token
        self._transport = transport
        self._extra_headers = headers or {}
        self._timeout = timeout
        self._transport_cm = None
        self._session_cm = None

    def _build_headers(self) -> dict[str, str]:
        h: dict[str, str] = {**self._extra_headers}
        if self._token:
            h["x-auth"] = self._token
        return h

    async def __aenter__(self) -> ClientSession:
        try:
            headers = self._build_headers()
            if self._transport == "sse":
                self._transport_cm = sse_client(
                    self._server_url, headers=headers, timeout=self._timeout,
                )
            else:
                self._transport_cm = streamablehttp_client(
                    self._server_url, headers=headers, timeout=self._timeout,
                )
            transport_result = await self._transport_cm.__aenter__()
            read_stream, write_stream = transport_result[0], transport_result[1]

            self._session_cm = ClientSession(read_stream, write_stream)
            session = await self._session_cm.__aenter__()
            await session.initialize()
            return session
        except Exception as exc:
            # 정리
            if self._session_cm:
                try:
                    await self._session_cm.__aexit__(None, None, None)
                except Exception:
                    pass
            if self._transport_cm:
                try:
                    await self._transport_cm.__aexit__(None, None, None)
                except Exception:
                    pass
            raise MCPConnectionError(f"MCP 서버 연결 실패: {exc}") from exc

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._session_cm:
            try:
                await self._session_cm.__aexit__(exc_type, exc_val, exc_tb)
            except Exception:
                pass
        if self._transport_cm:
            try:
                await self._transport_cm.__aexit__(exc_type, exc_val, exc_tb)
            except Exception:
                pass


async def list_available_tools(session: ClientSession) -> list[dict[str, Any]]:
    """MCP 서버에서 사용 가능한 도구 목록을 반환한다.

    조회된 도구 이름과 설명을 INFO 레벨 로그로 기록한다.
    """
    result = await session.list_tools()
    tools = [
        {
            "name": tool.name,
            "description": getattr(tool, "description", ""),
        }
        for tool in result.tools
    ]
    logger.info("Confluence MCP 서버에서 사용 가능한 도구 %d개 조회됨", len(tools))
    for tool in tools:
        logger.info("  - %s: %s", tool["name"], tool["description"])
    return tools


def _is_cql(query: str) -> bool:
    """주어진 문자열이 이미 CQL 형식인지 판별한다.

    CQL 연산자(=, ~, !=, >=, <=, >, <)나 키워드(AND, OR, NOT, IN, ORDER BY)가
    포함되어 있으면 CQL로 간주한다.
    """
    import re

    cql_operator_pattern = re.compile(r'[~=!<>]|!=|>=|<=')
    cql_keyword_pattern = re.compile(
        r'\b(AND|OR|NOT|IN|ORDER\s+BY)\b', re.IGNORECASE,
    )
    return bool(cql_operator_pattern.search(query) or cql_keyword_pattern.search(query))


def build_cql(query: str) -> str:
    """사용자 입력을 CQL 쿼리로 변환한다.

    이미 CQL 형식이면 그대로 반환하고,
    일반 키워드이면 ``type = "page" AND text ~ "검색어"`` 형식으로 감싼다.
    """
    query = query.strip()
    if not query:
        return query
    if _is_cql(query):
        return query
    escaped = query.replace('\\', '\\\\').replace('"', '\\"')
    return f'type = "page" AND text ~ "{escaped}"'


async def search_content(
    session: ClientSession,
    query: str,
    limit: int = 25,
    start: int = 0,
) -> list[dict[str, Any]]:
    """MCP 서버의 searchContent 도구로 콘텐츠를 검색한다.

    사용자가 일반 키워드를 입력하면 자동으로 CQL로 변환한다.
    이미 CQL 형식이면 그대로 사용한다.

    Args:
        session: 초기화된 ClientSession.
        query: 검색 키워드 또는 CQL 쿼리.
        limit: 최대 결과 수.
        start: 결과 시작 오프셋.

    Returns:
        검색 결과 목록. 각 항목에 id, title 등이 포함된다.
    """
    cql = build_cql(query)
    result = await session.call_tool(
        "searchContent", {"cql": cql, "limit": limit, "start": start},
    )
    parsed = _parse_json_result(result)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict) and "results" in parsed:
        return parsed["results"]
    # 텍스트 결과인 경우 그대로 반환
    return [{"content": parsed}] if parsed else []


@dataclass(frozen=True)
class SearchEnvelope:
    """``searchContent`` 응답의 envelope 메타를 보존하는 DTO.

    Confluence CQL 검색 응답은 ``{results, start, limit, size, totalSize}``
    형태로 내려오는데 기존 :func:`search_content` 는 ``results`` 만 반환하여
    전체 건수 등 메타가 유실된다. 공간 전체 싱크의 예상 페이지 수 표시,
    페이지네이션 커서 관리 등에 쓰려면 envelope 원형이 필요하므로 이 DTO를
    사용한다.

    Attributes:
        results: 이 응답 페이지에 포함된 결과 배열.
        total_size: CQL 매치 전체 건수. 서버가 내려주지 않으면 ``None``.
        size: 이 응답에 실제 담긴 건수. envelope이 없으면 ``len(results)``.
        start: 요청한 오프셋.
        limit: 요청한 페이지 크기.
    """

    results: list[dict[str, Any]]
    total_size: int | None
    size: int
    start: int
    limit: int


async def search_content_envelope(
    session: ClientSession,
    query: str,
    limit: int = 25,
    start: int = 0,
) -> SearchEnvelope:
    """:func:`search_content` 와 동일하게 검색하되 envelope 메타를 보존한다.

    응답 형태 3가지에 대응한다:

    1. envelope dict (``{results, totalSize, size, start, limit}``) — 기본 케이스.
    2. 결과만 배열로 — envelope 없이 ``[{id, title}, ...]`` 만 오는 경우.
       ``total_size`` 는 ``None`` 이 된다.
    3. 빈/텍스트 응답 — 빈 envelope을 반환한다.

    Args:
        session: 초기화된 ClientSession.
        query: 검색 키워드 또는 CQL 쿼리.
        limit: 요청 페이지 크기.
        start: 요청 오프셋.

    Returns:
        :class:`SearchEnvelope` 인스턴스.
    """
    cql = build_cql(query)
    result = await session.call_tool(
        "searchContent", {"cql": cql, "limit": limit, "start": start},
    )
    parsed = _parse_json_result(result)

    if isinstance(parsed, dict) and "results" in parsed:
        results = parsed["results"] or []
        # 서버 변종에 따라 total/totalSize/_totalSize 중 하나로 내려올 수 있다.
        total_size_val = (
            parsed.get("totalSize")
            if parsed.get("totalSize") is not None
            else parsed.get("total")
            if parsed.get("total") is not None
            else parsed.get("_totalSize")
        )
        total_size = int(total_size_val) if total_size_val is not None else None
        return SearchEnvelope(
            results=results,
            total_size=total_size,
            size=int(parsed.get("size", len(results))),
            start=int(parsed.get("start", start)),
            limit=int(parsed.get("limit", limit)),
        )

    if isinstance(parsed, list):
        return SearchEnvelope(
            results=parsed,
            total_size=None,
            size=len(parsed),
            start=start,
            limit=limit,
        )

    return SearchEnvelope(
        results=[], total_size=None, size=0, start=start, limit=limit,
    )


async def get_page(session: ClientSession, page_id: str) -> dict[str, Any]:
    """MCP 서버의 getPageByID 도구로 페이지를 가져온다.

    Args:
        session: 초기화된 ClientSession.
        page_id: 페이지 ID.

    Returns:
        페이지 정보 dict. title, content 등이 포함된다.
    """
    result = await session.call_tool(
        "getPageByID",
        {
            "pageId": page_id,
            "expand": "history,space,version,body.storage",
        },
    )
    parsed = _parse_json_result(result)
    if isinstance(parsed, dict):
        return parsed
    return {"content": _extract_text(result), "id": page_id}


async def get_page_with_ancestors(
    session: ClientSession, page_id: str,
) -> dict[str, Any]:
    """페이지를 조회하되 breadcrumb 구성에 필요한 ``ancestors``/``space`` 를 포함한다.

    싱크 대상 등록 시점에 사용자에게 보여줄 계층형 이름
    (예: ``"Engineering / Docs / Architecture / Overview"``)을 해석하기 위한
    경량 호출 경로. 본문(``body.storage``)은 포함하지 않아 :func:`get_page` 보다
    페이로드가 가볍다. 일반 임포트 흐름은 그대로 :func:`get_page` 를 사용한다.

    Args:
        session: 초기화된 ClientSession.
        page_id: 페이지 ID.

    Returns:
        페이지 정보 dict. ``ancestors`` (root→parent 순 배열) 와 ``space`` (dict)
        가 포함된다. 서버가 텍스트만 반환하면 ``{"id": page_id}`` 를 반환한다.
    """
    result = await session.call_tool(
        "getPageByID",
        {
            "pageId": page_id,
            "expand": "ancestors,space,version",
        },
    )
    parsed = _parse_json_result(result)
    if isinstance(parsed, dict):
        return parsed
    return {"id": page_id}


def format_breadcrumb(page: dict[str, Any]) -> str:
    """페이지 dict에서 ``"공간 / 조상 / ... / 제목"`` breadcrumb를 생성한다.

    :func:`get_page_with_ancestors` 응답을 그대로 받아 사용할 수 있다.

    구성 규칙:
      1. ``page["space"]["name"]`` 이 있으면 맨 앞에 붙임. 없으면
         ``page["space"]["key"]`` 로 폴백.
      2. ``page["ancestors"]`` 는 root→parent 순으로 전제, 각 항목의
         ``title`` (없으면 ``name``) 을 순서대로 이어 붙임.
      3. 마지막으로 ``page["title"]`` (없으면 ``name``) 을 붙임.
      4. 어느 것도 없으면 ``page["id"]`` 를 문자열로 반환. id마저 없으면 빈 문자열.

    구분자는 ``" / "``. 빈 제목 등 falsy 값은 건너뛴다.
    """
    parts: list[str] = []

    space = page.get("space")
    if isinstance(space, dict):
        space_label = space.get("name") or space.get("key")
        if space_label:
            parts.append(str(space_label))

    ancestors = page.get("ancestors")
    if isinstance(ancestors, list):
        for ancestor in ancestors:
            if isinstance(ancestor, dict):
                label = ancestor.get("title") or ancestor.get("name")
                if label:
                    parts.append(str(label))

    title = page.get("title") or page.get("name")
    if title:
        parts.append(str(title))

    if parts:
        return " / ".join(parts)

    page_id = page.get("id")
    return str(page_id) if page_id else ""


async def get_child_pages(
    session: ClientSession,
    page_id: str,
    *,
    page_size: int = 100,
    max_children: int = 5000,
    expand: str = "",
) -> list[dict[str, Any]]:
    """MCP 서버의 ``getChild`` 도구로 하위 페이지 목록을 가져온다.

    ``getChild`` 는 ``pageId`` 뿐 아니라 ``start``/``limit``/``expand`` 를
    필수 인자로 요구한다. envelope 응답(``{results, start, limit, size,
    totalSize}``) 인 경우 페이지네이션으로 전체를 수집하고, envelope 없이
    list 로 오는 변종은 한 번에 처리한다.

    Args:
        session: 초기화된 ClientSession.
        page_id: 부모 페이지 ID.
        page_size: 한 번에 요청할 결과 수.
        max_children: 수집 상한(안전장치).
        expand: ``getChild`` 의 필수 파라미터. 기본값 ``""`` 는 최소 필드만.
    """
    children: list[dict[str, Any]] = []
    start = 0
    total_size: int | None = None

    while len(children) < max_children:
        result = await session.call_tool(
            "getChild",
            {
                "pageId": page_id,
                "start": start,
                "limit": page_size,
                "expand": expand,
            },
        )
        parsed = _parse_json_result(result)

        # envelope 없이 list 로 오는 서버 변종 — 전부 반환 후 종료.
        if isinstance(parsed, list):
            children.extend(c for c in parsed if isinstance(c, dict))
            break

        if not isinstance(parsed, dict) or "results" not in parsed:
            break

        results = parsed.get("results") or []
        if not results:
            break
        children.extend(c for c in results if isinstance(c, dict))
        size = int(parsed.get("size", len(results)))

        if total_size is None:
            ts_val = (
                parsed.get("totalSize")
                if parsed.get("totalSize") is not None
                else parsed.get("total")
                if parsed.get("total") is not None
                else parsed.get("_totalSize")
            )
            if ts_val is not None:
                try:
                    total_size = int(ts_val)
                except (TypeError, ValueError):
                    total_size = None

        if total_size is not None and len(children) >= total_size:
            break
        if size < page_size:
            break
        start += page_size
    else:
        logger.warning(
            "get_child_pages max_children(%d) 도달 — page_id=%s",
            max_children, page_id,
        )

    return children[:max_children]


async def walk_subtree(
    session: ClientSession,
    root_page_id: str,
    *,
    max_depth: int = 20,
    max_pages: int = 5000,
) -> list[dict[str, Any]]:
    """서브트리의 모든 페이지를 BFS로 전개해 평탄화한 목록을 반환한다.

    루트부터 :func:`get_child_pages` 를 재귀 호출하여 하위 전체를 훑는다.
    본문은 페치하지 않고 ``{id, parent_id, depth, title}`` 만 수집한다.
    루트 자신도 첫 항목으로 포함된다(``parent_id=None, depth=0``).
    루트의 ``title`` 은 walker가 별도로 조회하지 않으므로 빈 문자열이 들어간다 —
    호출측이 이미 알고 있다면 결과의 첫 항목을 후처리할 수 있다.

    안전 장치:
      - ``type != "page"`` 인 자식은 건너뛴다(blogpost, attachment 등 무시).
        ``type`` 필드가 없으면 page로 간주한다.
      - 방문 집합으로 사이클과 중복 참조를 차단한다.
      - ``max_depth`` 를 초과하는 깊이로는 확장하지 않는다(루트는 깊이 0).
      - ``max_pages`` 에 도달하면 즉시 반환하고 경고 로그를 남긴다.
      - 특정 노드의 ``getChild`` 호출이 실패해도 warning만 남기고 다른 가지를 계속 탐색한다.

    Args:
        session: 초기화된 ClientSession.
        root_page_id: 루트 페이지 ID.
        max_depth: 허용할 최대 깊이(루트=0). 기본 20.
        max_pages: 수집할 최대 페이지 수. 기본 5000.

    Returns:
        각 항목은 ``{"id": str, "parent_id": str | None, "depth": int,
        "title": str}`` 형태. BFS 순서로 정렬되며 루트가 첫 항목.
    """
    root_id_str = str(root_page_id)
    visited: set[str] = {root_id_str}
    nodes: list[dict[str, Any]] = [
        {"id": root_id_str, "parent_id": None, "depth": 0, "title": ""},
    ]

    # BFS 큐: (page_id, depth)
    queue: deque[tuple[str, int]] = deque([(root_id_str, 0)])

    while queue:
        current_id, current_depth = queue.popleft()

        # max_depth에 도달했으면 더 내려가지 않는다 (이미 수집된 노드는 유지).
        if current_depth >= max_depth:
            continue

        try:
            children = await get_child_pages(session, current_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "하위 페이지 조회 실패 page_id=%s: %s", current_id, exc,
            )
            continue

        for child in children:
            if not isinstance(child, dict):
                continue

            # type 필터: 명시된 경우에만 page 이외를 스킵한다.
            child_type = child.get("type")
            if child_type is not None and child_type != "page":
                continue

            child_id_raw = child.get("id")
            if not child_id_raw:
                continue
            child_id = str(child_id_raw)

            if child_id in visited:
                continue

            if len(nodes) >= max_pages:
                logger.warning(
                    "walk_subtree max_pages(%d) 도달 — root=%s",
                    max_pages, root_id_str,
                )
                return nodes

            visited.add(child_id)
            nodes.append({
                "id": child_id,
                "parent_id": current_id,
                "depth": current_depth + 1,
                "title": str(child.get("title", "")),
            })
            queue.append((child_id, current_depth + 1))

    return nodes


def _space_cql(space_key: str) -> str:
    """공간 내 모든 페이지를 매치하는 CQL을 만든다.

    ``space_key`` 의 ``\\`` 와 ``"`` 는 CQL 문자열 리터럴 규칙에 따라 escape 한다.
    """
    escaped = space_key.replace("\\", "\\\\").replace('"', '\\"')
    return f'space = "{escaped}" AND type = "page"'


async def estimate_space_page_count(
    session: ClientSession, space_key: str,
) -> int | None:
    """공간에 속한 페이지 수를 추정한다.

    ``searchContent`` 를 ``limit=1`` 로 호출해 envelope의 ``totalSize`` 만
    확인한다. 공간 전체 싱크 확인 다이얼로그의 "예상 페이지 수" 표기에 쓴다.

    Args:
        session: 초기화된 ClientSession.
        space_key: 공간 키 (예: ``"ENG"``).

    Returns:
        ``totalSize`` 값. MCP 서버가 해당 메타를 내려주지 않으면 ``None``.
    """
    env = await search_content_envelope(
        session, _space_cql(space_key), limit=1, start=0,
    )
    return env.total_size


async def enumerate_space_pages(
    session: ClientSession,
    space_key: str,
    *,
    page_size: int = 100,
    max_pages: int = 10000,
) -> AsyncIterator[dict[str, Any]]:
    """공간의 모든 페이지를 CQL 페이지네이션으로 순회하며 하나씩 yield 한다.

    ``searchContent`` 를 ``space = "KEY" AND type = "page"`` 로 호출하고
    ``limit``/``start`` 를 증가시키며 반복한다. 결과 수가 많은 스페이스에
    대비해 메모리 부담을 줄이려 async generator로 구현한다.

    종료 조건:
      - envelope의 ``total_size`` 가 있으면 yield한 개수가 그 값에 도달했을 때.
      - ``total_size`` 가 없으면 응답의 ``size`` 가 ``page_size`` 보다 작을 때.
      - 응답의 ``results`` 가 비어 있으면 즉시 종료.
      - ``max_pages`` 에 도달하면 경고 로그를 남기고 조기 반환.

    Args:
        session: 초기화된 ClientSession.
        space_key: 공간 키.
        page_size: 한 번에 요청할 결과 수.
        max_pages: yield할 수 있는 최대 페이지 수 (안전 상한).

    Yields:
        ``searchContent`` 결과 dict. 일반적으로 ``id``, ``title``, ``space``,
        ``type`` 등을 포함한다.
    """
    cql = _space_cql(space_key)
    start = 0
    emitted = 0
    total_size: int | None = None

    while emitted < max_pages:
        env = await search_content_envelope(
            session, cql, limit=page_size, start=start,
        )

        if not env.results:
            break

        for page in env.results:
            if not isinstance(page, dict):
                continue
            yield page
            emitted += 1
            if emitted >= max_pages:
                logger.warning(
                    "enumerate_space_pages max_pages(%d) 도달 — space=%s",
                    max_pages, space_key,
                )
                return

        # 첫 응답에서 totalSize를 고정한다 (서버가 값을 바꾸지는 않지만 방어적).
        if total_size is None and env.total_size is not None:
            total_size = env.total_size

        if total_size is not None and emitted >= total_size:
            break
        if env.size < page_size:
            # 마지막 페이지 (envelope의 size가 요청 limit보다 작음).
            break

        start += page_size


async def get_space_info(
    session: ClientSession, space_key: str,
) -> dict[str, Any]:
    """MCP 서버의 getSpaceInfo 도구로 단일 공간 정보를 가져온다.

    Args:
        session: 초기화된 ClientSession.
        space_key: 공간 키 (예: ``"ENG"``).

    Returns:
        공간 정보 dict. 일반적으로 ``key``, ``name``, ``id`` 등이 포함된다.
        MCP 서버가 dict가 아닌 응답을 주면 ``{"key": space_key}`` 를 반환한다.
    """
    result = await session.call_tool("getSpaceInfo", {"spaceKey": space_key})
    parsed = _parse_json_result(result)
    if isinstance(parsed, dict):
        return parsed
    return {"key": space_key}


async def get_all_spaces(
    session: ClientSession,
    *,
    page_size: int = 100,
    max_spaces: int = 10000,
) -> list[dict[str, Any]]:
    """MCP 서버의 ``getSpaceInfoAll`` 도구로 모든 스페이스를 가져온다.

    서버가 ``start``/``limit`` 을 필수 인자로 요구하므로 envelope 응답
    (``{results, start, limit, size, totalSize}``) 에 대해 페이지네이션으로
    전체를 수집한다. 일부 서버 변종이 envelope 없이 list 만 돌려주는 경우엔
    그 한 번의 응답을 그대로 반환하고 종료한다.

    종료 조건(envelope 경로):
      - ``totalSize`` 가 있으면 누적이 그 값에 도달했을 때
      - ``size`` 가 ``page_size`` 보다 작으면 마지막 페이지로 간주
      - ``results`` 가 비면 즉시 종료
      - ``max_spaces`` 안전 상한 초과 시 경고 후 조기 반환

    Args:
        session: 초기화된 ClientSession.
        page_size: 한 번에 요청할 개수.
        max_spaces: 수집 상한.
    """
    spaces: list[dict[str, Any]] = []
    start = 0
    total_size: int | None = None

    while len(spaces) < max_spaces:
        result = await session.call_tool(
            "getSpaceInfoAll", {"start": start, "limit": page_size},
        )
        parsed = _parse_json_result(result)

        # envelope 없이 list만 오는 서버 변종 — 한 번에 전부이므로 종료.
        if isinstance(parsed, list):
            spaces.extend(s for s in parsed if isinstance(s, dict))
            break

        if not isinstance(parsed, dict) or "results" not in parsed:
            break

        results = parsed.get("results") or []
        if not results:
            break
        spaces.extend(s for s in results if isinstance(s, dict))
        size = int(parsed.get("size", len(results)))

        if total_size is None:
            ts_val = (
                parsed.get("totalSize")
                if parsed.get("totalSize") is not None
                else parsed.get("total")
                if parsed.get("total") is not None
                else parsed.get("_totalSize")
            )
            if ts_val is not None:
                try:
                    total_size = int(ts_val)
                except (TypeError, ValueError):
                    total_size = None

        if total_size is not None and len(spaces) >= total_size:
            break
        if size < page_size:
            break
        start += page_size
    else:
        logger.warning("get_all_spaces max_spaces(%d) 도달", max_spaces)

    return spaces[:max_spaces]


async def get_user_contributed_pages(
    session: ClientSession, user_id: str,
) -> list[dict[str, Any]]:
    """MCP 서버의 getUserContributedPages 도구로 사용자 기여 페이지를 가져온다."""
    result = await session.call_tool("getUserContributedPages", {"userId": user_id})
    parsed = _parse_json_result(result)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict) and "results" in parsed:
        return parsed["results"]
    return []


def _extract_page_content(page_data: dict[str, Any]) -> str:
    """페이지 데이터에서 본문 내용을 추출한다.

    MCP 서버가 반환하는 형식에 따라 여러 필드를 시도한다.
    """
    # 마크다운 본문이 있으면 우선
    for key in ("markdown", "content", "body", "value", "text"):
        val = page_data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    # body가 dict인 경우 (Confluence 표준 응답 형식)
    body = page_data.get("body")
    if isinstance(body, dict):
        for sub_key in ("storage", "view", "export_view"):
            sub = body.get(sub_key)
            if isinstance(sub, dict) and sub.get("value"):
                return sub["value"]
            if isinstance(sub, str) and sub.strip():
                return sub
    return ""


def _extract_page_title(page_data: dict[str, Any], page_id: str) -> str:
    """페이지 데이터에서 제목을 추출한다."""
    return page_data.get("title") or page_data.get("name") or f"Confluence Page {page_id}"


def convert_html_to_markdown(html: str) -> str:
    """HTML 콘텐츠를 마크다운으로 변환한다.

    Confluence 매크로(패널, 코드, 테이블 등)를 전처리한 뒤
    markdownify로 변환한다. 공유 모듈 ``html_converter``에 위임.

    Args:
        html: 변환할 HTML 문자열.

    Returns:
        마크다운 형식의 문자열.
    """
    from context_loop.ingestion.html_converter import html_to_markdown  # noqa: PLC0415

    return html_to_markdown(html)


async def import_page_via_mcp(
    session: ClientSession,
    store: MetadataStore,
    page_id: str,
) -> dict[str, Any]:
    """MCP를 통해 Confluence 페이지를 가져와 메타데이터 저장소에 등록한다.

    기존 import_page()와 동일한 패턴을 따른다.

    Args:
        session: 초기화된 MCP ClientSession.
        store: 초기화된 MetadataStore 인스턴스.
        page_id: 임포트할 Confluence 페이지 ID.

    Returns:
        생성 또는 업데이트된 문서 dict.
        추가 키:
          - "created" (bool): True면 새로 생성됨.
          - "changed" (bool): True면 내용이 변경됨.
    """
    page_data = await get_page(session, page_id)
    html_body = _extract_page_content(page_data)
    title = _extract_page_title(page_data, page_id)

    # HTML → 마크다운 변환
    content = convert_html_to_markdown(html_body) if html_body else html_body

    content_hash = compute_content_hash(content)

    # 기존 문서 확인
    existing_docs = await store.list_documents(source_type=SOURCE_TYPE)
    existing = next((d for d in existing_docs if d.get("source_id") == page_id), None)

    if existing is None:
        doc_id = await store.create_document(
            source_type=SOURCE_TYPE,
            source_id=page_id,
            title=title,
            original_content=content,
            content_hash=content_hash,
            raw_content=html_body or None,
        )
        await store.add_processing_history(
            document_id=doc_id,
            action="created",
            status="started",
        )
        doc = await store.get_document(doc_id)
        assert doc is not None
        return {**doc, "created": True, "changed": True}

    if existing["content_hash"] == content_hash:
        return {**existing, "created": False, "changed": False}

    # 내용 변경
    await store.update_document_content(
        existing["id"],
        original_content=content,
        content_hash=content_hash,
        raw_content=html_body or None,
    )
    await store.update_document_status(existing["id"], status="changed")
    await store.add_processing_history(
        document_id=existing["id"],
        action="updated",
        prev_storage_method=existing.get("storage_method"),
        status="started",
    )
    doc = await store.get_document(existing["id"])
    assert doc is not None
    return {**doc, "created": False, "changed": True}
