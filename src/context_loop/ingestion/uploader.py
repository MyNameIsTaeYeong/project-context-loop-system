"""파일 업로드 처리 모듈.

마크다운(.md), 텍스트(.txt), HTML(.html) 파일을 받아
원본 내용을 정규화하고 메타데이터 저장소에 등록한다.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from context_loop.storage.metadata_store import MetadataStore

_SUPPORTED_EXTENSIONS = {".md", ".txt", ".html"}


class UnsupportedFileTypeError(Exception):
    """지원하지 않는 파일 형식일 때 발생한다."""


def compute_content_hash(content: str) -> str:
    """문자열 내용의 SHA-256 해시를 반환한다.

    Args:
        content: 해시를 계산할 텍스트.

    Returns:
        64자 16진수 해시 문자열.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def read_file(file_path: Path) -> str:
    """파일을 읽어 문자열로 반환한다.

    Args:
        file_path: 읽을 파일 경로.

    Returns:
        파일 내용 문자열.

    Raises:
        UnsupportedFileTypeError: 지원하지 않는 확장자인 경우.
        FileNotFoundError: 파일이 존재하지 않는 경우.
    """
    suffix = file_path.suffix.lower()
    if suffix not in _SUPPORTED_EXTENSIONS:
        raise UnsupportedFileTypeError(
            f"지원하지 않는 파일 형식입니다: {suffix}. "
            f"지원 형식: {', '.join(sorted(_SUPPORTED_EXTENSIONS))}"
        )
    return file_path.read_text(encoding="utf-8")


async def upload_file(
    store: MetadataStore,
    file_path: Path,
    *,
    title: str | None = None,
    author: str | None = None,
) -> dict[str, Any]:
    """파일을 업로드하여 문서로 등록한다.

    동일 파일(source_id = 파일명)이 이미 존재하면 content_hash를 비교하여
    변경된 경우 원본을 갱신하고 status를 'changed'로 마킹한다.
    변경이 없으면 기존 문서 정보를 그대로 반환한다.

    Args:
        store: 초기화된 MetadataStore 인스턴스.
        file_path: 업로드할 파일 경로.
        title: 문서 제목. None이면 파일명(확장자 제외)을 사용한다.
        author: 작성자. None이면 저장하지 않는다.

    Returns:
        생성 또는 업데이트된 문서 dict.
        추가 키:
          - "created" (bool): True면 새로 생성됨, False면 기존 문서 처리.
          - "changed" (bool): True면 내용이 변경됨.

    Raises:
        UnsupportedFileTypeError: 지원하지 않는 파일 형식.
        FileNotFoundError: 파일이 없는 경우.
    """
    content = read_file(file_path)
    content_hash = compute_content_hash(content)
    doc_title = title or file_path.stem
    source_id = file_path.name  # 파일명을 source_id로 사용

    # 기존 문서 확인
    existing_docs = await store.list_documents(source_type="upload")
    existing = next(
        (d for d in existing_docs if d.get("source_id") == source_id),
        None,
    )

    if existing is None:
        # 신규 문서 생성
        doc_id = await store.create_document(
            source_type="upload",
            source_id=source_id,
            title=doc_title,
            original_content=content,
            content_hash=content_hash,
            author=author,
        )
        await store.add_processing_history(
            document_id=doc_id,
            action="created",
            status="started",
        )
        doc = await store.get_document(doc_id)
        assert doc is not None
        return {**doc, "created": True, "changed": True}

    # 기존 문서 — 해시 비교
    if existing["content_hash"] == content_hash:
        return {**existing, "created": False, "changed": False}

    # 내용 변경됨 — 원본 갱신 후 status = 'changed'
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
