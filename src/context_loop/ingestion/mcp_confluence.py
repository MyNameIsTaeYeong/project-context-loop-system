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


async def get_child_pages(session: ClientSession, page_id: str) -> list[dict[str, Any]]:
    """MCP 서버의 getChild 도구로 하위 페이지 목록을 가져온다."""
    result = await session.call_tool("getChild", {"pageId": page_id})
    parsed = _parse_json_result(result)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict) and "results" in parsed:
        return parsed["results"]
    return []


async def get_all_spaces(session: ClientSession) -> list[dict[str, Any]]:
    """MCP 서버의 getSpaceInfoAll 도구로 스페이스 목록을 가져온다."""
    result = await session.call_tool("getSpaceInfoAll", {})
    parsed = _parse_json_result(result)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict) and "results" in parsed:
        return parsed["results"]
    return []


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
