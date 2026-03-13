"""마크다운 직접 작성 문서 처리 모듈.

대시보드 에디터에서 직접 작성한 마크다운 문서를 등록·수정한다.
"""

from __future__ import annotations

from typing import Any

from context_loop.ingestion.uploader import compute_content_hash
from context_loop.storage.metadata_store import MetadataStore


async def save_document(
    store: MetadataStore,
    title: str,
    content: str,
    *,
    document_id: int | None = None,
    author: str | None = None,
) -> dict[str, Any]:
    """마크다운 문서를 저장하거나 기존 문서를 업데이트한다.

    document_id가 주어지면 기존 문서를 수정한다.
    주어지지 않으면 새 문서를 생성한다.

    content_hash가 동일하면 저장소를 변경하지 않고 기존 문서를 반환한다.

    Args:
        store: 초기화된 MetadataStore 인스턴스.
        title: 문서 제목.
        content: 마크다운 내용.
        document_id: 수정할 기존 문서 ID. None이면 새 문서 생성.
        author: 작성자.

    Returns:
        생성 또는 업데이트된 문서 dict.
        추가 키:
          - "created" (bool): True면 새로 생성됨.
          - "changed" (bool): True면 내용이 변경됨.

    Raises:
        ValueError: document_id가 주어졌으나 해당 문서가 없는 경우.
    """
    content_hash = compute_content_hash(content)

    if document_id is None:
        # 신규 문서 생성
        new_id = await store.create_document(
            source_type="manual",
            title=title,
            original_content=content,
            content_hash=content_hash,
            author=author,
        )
        await store.add_processing_history(
            document_id=new_id,
            action="created",
            status="started",
        )
        doc = await store.get_document(new_id)
        assert doc is not None
        return {**doc, "created": True, "changed": True}

    # 기존 문서 수정
    existing = await store.get_document(document_id)
    if existing is None:
        raise ValueError(f"문서를 찾을 수 없습니다: document_id={document_id}")

    if existing["content_hash"] == content_hash and existing["title"] == title:
        return {**existing, "created": False, "changed": False}

    # 내용 또는 제목 변경
    await store.update_document_content(
        document_id,
        original_content=content,
        content_hash=content_hash,
        title=title,
    )
    await store.update_document_status(document_id, status="changed")
    await store.add_processing_history(
        document_id=document_id,
        action="updated",
        prev_storage_method=existing.get("storage_method"),
        status="started",
    )
    doc = await store.get_document(document_id)
    assert doc is not None
    return {**doc, "created": False, "changed": True}
