"""파일 업로드 처리 모듈.

MD, TXT, HTML 파일을 읽어 파싱/정규화 후 메타데이터 저장소에 등록한다.
"""

from __future__ import annotations

from pathlib import Path

from context_loop.processor.parser import (
    compute_content_hash,
    extract_title_from_content,
    is_supported_file,
    normalize_content,
)
from context_loop.storage.metadata_store import MetadataStore


class FileUploader:
    """파일 업로드를 처리하는 클래스.

    Args:
        store: 메타데이터 저장소.
    """

    def __init__(self, store: MetadataStore) -> None:
        self._store = store

    async def upload_file(
        self,
        file_path: Path,
        title: str | None = None,
        author: str | None = None,
    ) -> int:
        """파일을 읽어 문서로 등록한다.

        Args:
            file_path: 업로드할 파일 경로.
            title: 문서 제목. None이면 콘텐츠에서 자동 추출.
            author: 작성자.

        Returns:
            생성된 문서 ID.

        Raises:
            ValueError: 지원하지 않는 파일 형식인 경우.
            FileNotFoundError: 파일이 존재하지 않는 경우.
        """
        if not file_path.exists():
            raise FileNotFoundError(f"파일을 찾을 수 없습니다: {file_path}")

        if not is_supported_file(file_path):
            raise ValueError(
                f"지원하지 않는 파일 형식입니다: {file_path.suffix}. "
                "지원 형식: .md, .txt, .html"
            )

        raw_content = file_path.read_text(encoding="utf-8")
        source_format = file_path.suffix.lstrip(".").lower()
        normalized = normalize_content(raw_content, source_format)
        content_hash = compute_content_hash(normalized)

        if title is None:
            title = extract_title_from_content(normalized)

        doc_id = await self._store.create_document(
            source_type="upload",
            title=title,
            original_content=normalized,
            content_hash=content_hash,
            author=author,
        )

        return doc_id

    async def upload_content(
        self,
        content: str,
        filename: str,
        title: str | None = None,
        author: str | None = None,
    ) -> int:
        """바이트/문자열 콘텐츠를 직접 문서로 등록한다 (웹 업로드용).

        Args:
            content: 파일 콘텐츠 문자열.
            filename: 원본 파일명 (확장자 판별용).
            title: 문서 제목.
            author: 작성자.

        Returns:
            생성된 문서 ID.
        """
        ext = Path(filename).suffix.lstrip(".").lower()
        if ext not in ("md", "txt", "html", "htm"):
            raise ValueError(f"지원하지 않는 파일 형식입니다: .{ext}")

        normalized = normalize_content(content, ext)
        content_hash = compute_content_hash(normalized)

        if title is None:
            title = extract_title_from_content(normalized)

        doc_id = await self._store.create_document(
            source_type="upload",
            title=title,
            original_content=normalized,
            content_hash=content_hash,
            author=author,
        )

        return doc_id
