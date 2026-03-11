"""직접 마크다운 작성 문서 처리 모듈.

대시보드 에디터에서 직접 작성한 마크다운 문서를 저장/수정한다.
"""

from __future__ import annotations

from context_loop.processor.parser import compute_content_hash, extract_title_from_content
from context_loop.storage.metadata_store import MetadataStore


class ManualEditor:
    """마크다운 직접 작성 문서를 관리하는 클래스.

    Args:
        store: 메타데이터 저장소.
    """

    def __init__(self, store: MetadataStore) -> None:
        self._store = store

    async def create_document(
        self,
        content: str,
        title: str | None = None,
        author: str | None = None,
    ) -> int:
        """새 마크다운 문서를 생성한다.

        Args:
            content: 마크다운 콘텐츠.
            title: 문서 제목. None이면 콘텐츠에서 자동 추출.
            author: 작성자.

        Returns:
            생성된 문서 ID.
        """
        content = content.strip()
        if not content:
            raise ValueError("콘텐츠가 비어있습니다.")

        content_hash = compute_content_hash(content)

        if title is None:
            title = extract_title_from_content(content)

        doc_id = await self._store.create_document(
            source_type="manual",
            title=title,
            original_content=content,
            content_hash=content_hash,
            author=author,
        )

        return doc_id

    async def update_document(
        self,
        document_id: int,
        content: str,
        title: str | None = None,
    ) -> bool:
        """기존 문서를 수정한다.

        콘텐츠가 변경된 경우에만 업데이트한다.

        Args:
            document_id: 수정할 문서 ID.
            content: 새 마크다운 콘텐츠.
            title: 새 제목. None이면 콘텐츠에서 자동 추출.

        Returns:
            변경이 있었으면 True, 동일 콘텐츠면 False.

        Raises:
            ValueError: 문서가 존재하지 않는 경우.
        """
        content = content.strip()
        if not content:
            raise ValueError("콘텐츠가 비어있습니다.")

        doc = await self._store.get_document(document_id)
        if doc is None:
            raise ValueError(f"문서를 찾을 수 없습니다: ID {document_id}")

        new_hash = compute_content_hash(content)
        if new_hash == doc["content_hash"]:
            return False

        await self._store.update_document_content(document_id, content, new_hash)
        return True
