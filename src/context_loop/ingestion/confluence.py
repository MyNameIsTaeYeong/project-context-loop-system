"""Confluence API 임포트 모듈.

Confluence Cloud / Data Center에서 스페이스와 페이지를 가져와
원본 마크다운으로 변환하고 메타데이터 저장소에 등록한다.

인증 방식:
  - Cloud: Basic Auth (email + API token)
  - Data Center: Bearer Token (Personal Access Token)
"""

from __future__ import annotations

import base64
import re
from typing import Any

import httpx

from context_loop.ingestion.html_converter import html_to_markdown
from context_loop.ingestion.uploader import compute_content_hash
from context_loop.storage.metadata_store import MetadataStore


class ConfluenceAuthError(Exception):
    """인증 실패 시 발생한다."""


class ConfluenceAPIError(Exception):
    """Confluence API 오류 시 발생한다."""


def _basic_auth_header(email: str, token: str) -> str:
    """Cloud용 Basic Auth 헤더 값을 반환한다."""
    encoded = base64.b64encode(f"{email}:{token}".encode()).decode()
    return f"Basic {encoded}"


def _bearer_auth_header(pat_token: str) -> str:
    """Data Center용 Bearer Token 헤더 값을 반환한다."""
    return f"Bearer {pat_token}"


def _html_to_markdown(html: str) -> str:
    """HTML을 마크다운으로 변환한다.

    Confluence 매크로(패널, 코드, 테이블 등)를 전처리한 뒤
    markdownify로 변환한다. 공유 모듈 ``html_converter``에 위임.

    Args:
        html: 변환할 HTML 문자열.

    Returns:
        마크다운 문자열.
    """
    return html_to_markdown(html)


class ConfluenceClient:
    """Confluence REST API v2 클라이언트.

    Args:
        base_url: Confluence 인스턴스 URL (예: "https://company.atlassian.net").
        email: 사용자 이메일 (Cloud 인증에 사용).
        token: API 토큰 (Cloud) 또는 Personal Access Token (Data Center).
        is_cloud: True면 Cloud(Basic Auth), False면 Data Center(Bearer Token).
        timeout: HTTP 타임아웃(초).
    """

    def __init__(
        self,
        base_url: str,
        email: str,
        token: str,
        *,
        is_cloud: bool = True,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        auth_header = (
            _basic_auth_header(email, token) if is_cloud else _bearer_auth_header(token)
        )
        self._headers = {
            "Authorization": auth_header,
            "Accept": "application/json",
        }
        self._timeout = timeout

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET 요청을 수행하고 JSON을 반환한다."""
        url = f"{self._base_url}{path}"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(url, headers=self._headers, params=params)
        if response.status_code == 401:
            raise ConfluenceAuthError("인증에 실패했습니다. 이메일/토큰을 확인하세요.")
        if response.status_code == 403:
            raise ConfluenceAuthError(f"접근 권한이 없습니다: {path}")
        if not response.is_success:
            raise ConfluenceAPIError(
                f"API 오류 {response.status_code}: {path}\n{response.text[:500]}"
            )
        return response.json()

    async def list_spaces(self, limit: int = 50) -> list[dict[str, Any]]:
        """스페이스 목록을 반환한다.

        Args:
            limit: 한 번에 가져올 최대 스페이스 수.

        Returns:
            스페이스 dict 목록. 각 항목에 id, key, name이 포함된다.
        """
        spaces: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            data = await self._get("/wiki/api/v2/spaces", params=params)
            spaces.extend(data.get("results", []))
            next_link = data.get("_links", {}).get("next")
            if not next_link:
                break
            # cursor 값 추출
            match = re.search(r"cursor=([^&]+)", next_link)
            cursor = match.group(1) if match else None
            if not cursor:
                break
        return spaces

    async def list_pages(self, space_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """스페이스의 페이지 목록을 반환한다.

        Args:
            space_id: 스페이스 ID.
            limit: 한 번에 가져올 최대 페이지 수.

        Returns:
            페이지 dict 목록. 각 항목에 id, title, version 등이 포함된다.
        """
        pages: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": limit}
            if cursor:
                params["cursor"] = cursor
            data = await self._get(f"/wiki/api/v2/spaces/{space_id}/pages", params=params)
            pages.extend(data.get("results", []))
            next_link = data.get("_links", {}).get("next")
            if not next_link:
                break
            match = re.search(r"cursor=([^&]+)", next_link)
            cursor = match.group(1) if match else None
            if not cursor:
                break
        return pages

    async def get_page(self, page_id: str) -> dict[str, Any]:
        """페이지 상세 정보(본문 포함)를 반환한다.

        Args:
            page_id: 가져올 페이지 ID.

        Returns:
            페이지 dict. body.storage.value에 HTML 본문이 포함된다.
        """
        data = await self._get(
            f"/wiki/api/v2/pages/{page_id}",
            params={"body-format": "storage"},
        )
        return data  # type: ignore[return-value]

    async def get_page_content_as_markdown(self, page_id: str) -> tuple[str, dict[str, Any]]:
        """페이지 본문을 마크다운으로 변환하여 반환한다.

        Args:
            page_id: 가져올 페이지 ID.

        Returns:
            (마크다운 문자열, 페이지 메타데이터 dict) 튜플.
            메타데이터 dict에는 id, title, version, authorId, createdAt 등이 포함된다.
        """
        markdown, _html, page = await self.get_page_content_with_html(page_id)
        return markdown, page

    async def get_page_content_with_html(
        self, page_id: str,
    ) -> tuple[str, str, dict[str, Any]]:
        """페이지 본문을 마크다운과 원본 HTML 양쪽으로 반환한다.

        Returns:
            (마크다운 문자열, 원본 Storage Format HTML, 페이지 메타데이터 dict).
        """
        page = await self.get_page(page_id)
        html_body = page.get("body", {}).get("storage", {}).get("value", "")
        markdown = _html_to_markdown(html_body)
        return markdown, html_body, page


async def import_page(
    client: ConfluenceClient,
    store: MetadataStore,
    page_id: str,
    base_url: str,
) -> dict[str, Any]:
    """Confluence 페이지를 가져와 메타데이터 저장소에 등록한다.

    이미 존재하는 페이지면 version을 기반으로 변경 여부를 판단한다.
    변경된 경우 원본을 갱신하고 status를 'changed'로 마킹한다.

    Args:
        client: ConfluenceClient 인스턴스.
        store: 초기화된 MetadataStore 인스턴스.
        page_id: 임포트할 Confluence 페이지 ID.
        base_url: Confluence 인스턴스 URL (원본 링크 생성에 사용).

    Returns:
        생성 또는 업데이트된 문서 dict.
        추가 키:
          - "created" (bool): True면 새로 생성됨.
          - "changed" (bool): True면 내용이 변경됨.
    """
    markdown, html_body, page_meta = await client.get_page_content_with_html(page_id)
    content_hash = compute_content_hash(markdown)
    title = page_meta.get("title", f"Confluence Page {page_id}")
    author_id = page_meta.get("authorId") or page_meta.get("createdBy", {}).get("accountId")
    page_url = f"{base_url.rstrip('/')}/wiki/spaces/_/pages/{page_id}"

    # 기존 문서 확인
    existing_docs = await store.list_documents(source_type="confluence")
    existing = next((d for d in existing_docs if d.get("source_id") == page_id), None)

    if existing is None:
        doc_id = await store.create_document(
            source_type="confluence",
            source_id=page_id,
            title=title,
            original_content=markdown,
            content_hash=content_hash,
            url=page_url,
            author=author_id,
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
        original_content=markdown,
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


async def import_space(
    client: ConfluenceClient,
    store: MetadataStore,
    space_id: str,
    base_url: str,
) -> list[dict[str, Any]]:
    """Confluence 스페이스의 모든 페이지를 가져온다.

    Args:
        client: ConfluenceClient 인스턴스.
        store: 초기화된 MetadataStore 인스턴스.
        space_id: 임포트할 스페이스 ID.
        base_url: Confluence 인스턴스 URL.

    Returns:
        임포트된 문서 dict 목록 (각 항목에 created, changed 키 포함).
    """
    pages = await client.list_pages(space_id)
    results: list[dict[str, Any]] = []
    for page in pages:
        page_id = str(page["id"])
        doc = await import_page(client, store, page_id, base_url)
        results.append(doc)
    return results
