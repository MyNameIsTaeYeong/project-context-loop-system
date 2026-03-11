"""Confluence API 임포트 모듈.

Confluence REST API를 통해 스페이스/페이지를 조회하고,
HTML 콘텐츠를 마크다운으로 변환하여 메타데이터 저장소에 등록한다.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import httpx

from context_loop.auth import get_token
from context_loop.config import Config
from context_loop.processor.parser import compute_content_hash, html_to_markdown
from context_loop.storage.metadata_store import MetadataStore


@dataclass
class ConfluencePage:
    """Confluence 페이지 정보."""

    page_id: str
    title: str
    space_key: str
    version: int
    url: str
    body_html: str


class ConfluenceClient:
    """Confluence REST API 클라이언트.

    Args:
        base_url: Confluence 인스턴스 URL (예: https://company.atlassian.net).
        email: 사용자 이메일.
        auth_type: 인증 타입 ("cloud" 또는 "datacenter").
    """

    def __init__(
        self,
        base_url: str,
        email: str,
        auth_type: str = "cloud",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._email = email
        self._auth_type = auth_type
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """인증 헤더가 설정된 HTTP 클라이언트를 반환한다."""
        if self._client is None:
            token = get_token("confluence", self._email)
            if token is None:
                raise RuntimeError(
                    f"Confluence 토큰이 설정되지 않았습니다. "
                    f"email={self._email}에 대한 토큰을 먼저 저장하세요."
                )

            if self._auth_type == "cloud":
                credentials = base64.b64encode(
                    f"{self._email}:{token}".encode()
                ).decode()
                headers = {"Authorization": f"Basic {credentials}"}
            else:
                headers = {"Authorization": f"Bearer {token}"}

            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=headers,
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        """HTTP 클라이언트를 닫는다."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def list_spaces(self) -> list[dict[str, Any]]:
        """스페이스 목록을 조회한다."""
        client = await self._get_client()
        spaces: list[dict[str, Any]] = []
        url = "/wiki/api/v2/spaces"

        while url:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
            spaces.extend(data.get("results", []))
            # 페이지네이션
            next_link = data.get("_links", {}).get("next")
            url = next_link if next_link else ""

        return spaces

    async def list_pages(
        self,
        space_id: str,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """스페이스의 페이지 목록을 조회한다."""
        client = await self._get_client()
        pages: list[dict[str, Any]] = []
        url = f"/wiki/api/v2/spaces/{space_id}/pages?limit={limit}"

        while url:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
            pages.extend(data.get("results", []))
            next_link = data.get("_links", {}).get("next")
            url = next_link if next_link else ""

        return pages

    async def get_page(self, page_id: str) -> ConfluencePage:
        """페이지 상세 정보(본문 포함)를 조회한다."""
        client = await self._get_client()
        response = await client.get(
            f"/wiki/api/v2/pages/{page_id}",
            params={"body-format": "storage"},
        )
        response.raise_for_status()
        data = response.json()

        return ConfluencePage(
            page_id=data["id"],
            title=data["title"],
            space_key=data.get("spaceId", ""),
            version=data.get("version", {}).get("number", 1),
            url=f"{self._base_url}/wiki{data.get('_links', {}).get('webui', '')}",
            body_html=data.get("body", {}).get("storage", {}).get("value", ""),
        )

    async def get_recently_modified(
        self,
        space_key: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """최근 변경된 페이지를 조회한다 (증분 동기화용, v1 API 사용)."""
        client = await self._get_client()
        params: dict[str, Any] = {
            "expand": "version",
            "orderby": "lastmodified desc",
            "limit": limit,
        }
        if space_key:
            params["spaceKey"] = space_key

        response = await client.get(
            "/wiki/rest/api/content",
            params=params,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("results", [])


class ConfluenceImporter:
    """Confluence 페이지를 임포트하여 메타데이터 저장소에 등록한다.

    Args:
        client: Confluence API 클라이언트.
        store: 메타데이터 저장소.
    """

    def __init__(self, client: ConfluenceClient, store: MetadataStore) -> None:
        self._client = client
        self._store = store

    @classmethod
    def from_config(cls, config: Config, store: MetadataStore) -> ConfluenceImporter:
        """Config에서 설정을 읽어 인스턴스를 생성한다."""
        base_url = config.get("sources.confluence.base_url", "")
        email = config.get("sources.confluence.email", "")
        if not base_url or not email:
            raise ValueError("Confluence base_url과 email이 설정되어야 합니다.")

        client = ConfluenceClient(base_url=base_url, email=email)
        return cls(client=client, store=store)

    async def import_page(self, page_id: str) -> int:
        """단일 페이지를 임포트한다.

        Args:
            page_id: Confluence 페이지 ID.

        Returns:
            생성된 문서 ID.
        """
        page = await self._client.get_page(page_id)
        markdown_content = html_to_markdown(page.body_html)
        content_hash = compute_content_hash(markdown_content)

        # 이미 존재하는지 확인
        existing = await self._store.list_documents(source_type="confluence")
        for doc in existing:
            if doc["source_id"] == page.page_id:
                # 해시 비교로 변경 여부 확인
                if doc["content_hash"] == content_hash:
                    return doc["id"]
                # 변경 감지 → 업데이트
                await self._store.update_document_content(
                    doc["id"], markdown_content, content_hash
                )
                return doc["id"]

        doc_id = await self._store.create_document(
            source_type="confluence",
            source_id=page.page_id,
            title=page.title,
            original_content=markdown_content,
            content_hash=content_hash,
            url=page.url,
        )

        return doc_id

    async def import_space(self, space_id: str) -> list[int]:
        """스페이스의 모든 페이지를 임포트한다.

        Args:
            space_id: Confluence 스페이스 ID.

        Returns:
            생성된 문서 ID 목록.
        """
        pages = await self._client.list_pages(space_id)
        doc_ids: list[int] = []

        for page_info in pages:
            doc_id = await self.import_page(page_info["id"])
            doc_ids.append(doc_id)

        return doc_ids
