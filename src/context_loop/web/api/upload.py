"""파일 업로드 API 엔드포인트."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, UploadFile

from context_loop.ingestion.uploader import UnsupportedFileTypeError, upload_file
from context_loop.storage.metadata_store import MetadataStore
from context_loop.web.dependencies import get_meta_store

logger = logging.getLogger(__name__)

router = APIRouter()

_ALLOWED_EXTENSIONS = {".md", ".txt", ".html"}


@router.post("/api/upload")
async def upload_file_api(
    file: UploadFile,
    background_tasks: BackgroundTasks,
    meta_store: MetadataStore = Depends(get_meta_store),
):
    """파일을 업로드하고 문서를 생성한다."""
    filename = file.filename or "upload.md"
    suffix = Path(filename).suffix.lower()

    if suffix not in _ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"지원하지 않는 파일 형식: {suffix}")

    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = Path(tmp.name)

    final_path = tmp_path.parent / filename
    try:
        tmp_path.rename(final_path)
        result = await upload_file(meta_store, final_path)
    except UnsupportedFileTypeError as exc:
        raise HTTPException(400, str(exc))
    finally:
        final_path.unlink(missing_ok=True)
        tmp_path.unlink(missing_ok=True)

    response = Response(status_code=204)
    response.headers["HX-Redirect"] = f"/documents/{result['id']}"
    return response
