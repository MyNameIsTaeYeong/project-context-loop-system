"""Confluence MCP Client 임포트 모듈.

사내 Confluence MCP Server에 MCP Client로 연결하여
문서를 가져와 메타데이터 저장소에 등록한다.

MCP Server가 제공하는 도구:
  - searchContent: 콘텐츠 키워드 검색
  - getPage: 페이지 단건 조회 (본문 포함)
  - getChild: 하위 페이지 목록 조회
  - getSpaceInfoAll: 전체 스페이스 목록 조회
  - getSpaceInfo: 특정 스페이스 정보
  - getUserContributedPages: 사용자 기여 페이지 목록
"""

from __future__ import annotations

import json
import logging
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
    """MCP 서버에서 사용 가능한 도구 목록을 반환한다."""
    result = await session.list_tools()
    return [
        {
            "name": tool.name,
            "description": getattr(tool, "description", ""),
        }
        for tool in result.tools
    ]


async def search_content(
    session: ClientSession,
    query: str,
    limit: int = 25,
    start: int = 0,
) -> list[dict[str, Any]]:
    """MCP 서버의 searchContent 도구로 콘텐츠를 검색한다.

    Args:
        session: 초기화된 ClientSession.
        query: CQL 검색 쿼리.
        limit: 최대 결과 수.
        start: 결과 시작 오프셋.

    Returns:
        검색 결과 목록. 각 항목에 id, title 등이 포함된다.
    """
    result = await session.call_tool(
        "searchContent", {"cql": query, "limit": limit, "start": start},
    )
    parsed = _parse_json_result(result)
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict) and "results" in parsed:
        return parsed["results"]
    # 텍스트 결과인 경우 그대로 반환
    return [{"content": parsed}] if parsed else []


async def get_page(session: ClientSession, page_id: str) -> dict[str, Any]:
    """MCP 서버의 getPage 도구로 페이지를 가져온다.

    Args:
        session: 초기화된 ClientSession.
        page_id: 페이지 ID.

    Returns:
        페이지 정보 dict. title, content 등이 포함된다.
    """
    result = await session.call_tool("getPage", {"pageId": page_id})
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
    content = _extract_page_content(page_data)
    title = _extract_page_title(page_data, page_id)
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
