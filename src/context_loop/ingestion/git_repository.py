"""Git 레포지토리 수집 모듈.

Git 레포지토리를 clone/pull 하고, 상품별 스코핑에 따라
코드 파일을 수집하여 git_code 문서로 저장한다.
변경 감지(git diff 기반)를 통해 증분 처리를 지원한다.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from context_loop.config import Config
from context_loop.storage.metadata_store import MetadataStore

logger = logging.getLogger(__name__)


@dataclass
class ProductScope:
    """상품별 코드 스코프 정의."""

    name: str
    display_name: str
    paths: list[str]
    exclude: list[str] = field(default_factory=list)


@dataclass
class FileInfo:
    """수집 대상 코드 파일 정보."""

    relative_path: str
    absolute_path: Path
    product: str
    content: str
    content_hash: str
    size_bytes: int


@dataclass
class SyncResult:
    """레포지토리 동기화 결과."""

    repo_url: str
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)

    @property
    def total_processed(self) -> int:
        return len(self.created) + len(self.updated) + len(self.unchanged)


def compute_content_hash(content: str) -> str:
    """문자열 내용의 SHA-256 해시를 반환한다."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _matches_any(path: str, patterns: list[str]) -> bool:
    """경로가 glob 패턴 리스트 중 하나에 매칭되는지 확인한다."""
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
    return False


def parse_product_scopes(
    repo_config: dict[str, Any],
    clone_dir: Path | None = None,
    supported_extensions: list[str] | None = None,
) -> list[ProductScope]:
    """레포지토리 설정에서 상품 스코프 목록을 파싱한다.

    paths가 정의되지 않은 상품이 있고 clone_dir이 제공되면,
    레포 전체를 스캔하여 상품명이 포함된 파일 경로를 자동으로 채운다.

    Args:
        repo_config: 레포지토리 설정 dict.
        clone_dir: 로컬 clone 경로. 제공 시 paths 자동 탐지 활성화.
        supported_extensions: 자동 탐지 시 대상 파일 확장자.
    """
    from context_loop.ingestion.scope_analyzer import resolve_product_paths

    products_raw = repo_config.get("products") or {}

    # paths가 비어있는 상품명 수집
    needs_resolve: list[str] = []
    for name, cfg in products_raw.items():
        if not cfg.get("paths"):
            needs_resolve.append(name)

    # 자동 탐지 실행 (필요한 경우)
    resolved: dict[str, list[str]] = {}
    if needs_resolve and clone_dir is not None:
        # 모든 상품의 exclude 패턴을 합산하여 전달
        all_excludes: list[str] = []
        for name in needs_resolve:
            cfg = products_raw.get(name) or {}
            all_excludes.extend(cfg.get("exclude", []))
        resolved = resolve_product_paths(
            clone_dir, needs_resolve, supported_extensions,
            exclude_patterns=all_excludes or None,
        )

    scopes: list[ProductScope] = []
    for name, cfg in products_raw.items():
        paths = cfg.get("paths", [])
        if not paths and name in resolved:
            paths = resolved[name]
        scopes.append(
            ProductScope(
                name=name,
                display_name=cfg.get("display_name", name),
                paths=paths,
                exclude=cfg.get("exclude", []),
            )
        )
    return scopes


def match_product(relative_path: str, scopes: list[ProductScope]) -> str | None:
    """파일 경로가 속하는 상품을 판별한다.

    Returns:
        매칭되는 상품 이름. 매칭되는 상품이 없으면 None.
    """
    for scope in scopes:
        if _matches_any(relative_path, scope.exclude):
            continue
        if _matches_any(relative_path, scope.paths):
            return scope.name
    return None


def filter_file(
    relative_path: str,
    file_size: int,
    supported_extensions: list[str],
    file_size_limit_kb: int,
) -> bool:
    """파일이 수집 대상인지 판별한다.

    Returns:
        True면 수집 대상, False면 제외.
    """
    suffix = Path(relative_path).suffix.lower()
    if suffix not in supported_extensions:
        return False
    if file_size > file_size_limit_kb * 1024:
        return False
    return True


async def _run_git(
    args: list[str],
    cwd: Path | None = None,
) -> tuple[int, str, str]:
    """git 명령을 실행하고 (returncode, stdout, stderr)를 반환한다."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


async def clone_or_pull(
    repo_url: str,
    clone_dir: Path,
    branch: str = "main",
) -> tuple[bool, str | None]:
    """레포지토리를 clone 또는 pull 한다.

    Args:
        repo_url: Git 레포지토리 URL.
        clone_dir: 로컬 clone 경로.
        branch: 대상 브랜치.

    Returns:
        (is_new_clone, previous_commit_hash).
        새 clone이면 (True, None), pull이면 (False, pull 전 커밋 해시).
    """
    if (clone_dir / ".git").is_dir():
        # 기존 clone — pull 전 현재 커밋 해시 저장
        rc, prev_hash, _ = await _run_git(["rev-parse", "HEAD"], cwd=clone_dir)
        if rc != 0:
            prev_hash = None
        else:
            prev_hash = prev_hash.strip()

        rc, stdout, stderr = await _run_git(
            ["pull", "origin", branch], cwd=clone_dir
        )
        if rc != 0:
            raise RuntimeError(f"git pull 실패: {stderr}")
        logger.info("git pull 완료: %s", repo_url)
        return False, prev_hash

    # 새 clone
    clone_dir.parent.mkdir(parents=True, exist_ok=True)
    rc, stdout, stderr = await _run_git(
        ["clone", "--branch", branch, "--single-branch", repo_url, str(clone_dir)]
    )
    if rc != 0:
        raise RuntimeError(f"git clone 실패: {stderr}")
    logger.info("git clone 완료: %s → %s", repo_url, clone_dir)
    return True, None


async def get_changed_files(
    clone_dir: Path,
    prev_commit: str | None,
) -> list[str] | None:
    """이전 커밋 이후 변경된 파일 목록을 반환한다.

    Args:
        clone_dir: 로컬 clone 경로.
        prev_commit: 이전 커밋 해시. None이면 전체 파일 대상(초기 clone).

    Returns:
        변경 파일 경로 리스트. None이면 전체 파일을 처리해야 함.
    """
    if prev_commit is None:
        return None

    rc, current_hash, _ = await _run_git(["rev-parse", "HEAD"], cwd=clone_dir)
    if rc != 0:
        return None
    current_hash = current_hash.strip()

    if current_hash == prev_commit:
        return []  # 변경 없음

    rc, stdout, stderr = await _run_git(
        ["diff", "--name-only", prev_commit, current_hash],
        cwd=clone_dir,
    )
    if rc != 0:
        logger.warning("git diff 실패, 전체 파일 처리로 폴백: %s", stderr)
        return None

    return [line for line in stdout.strip().split("\n") if line]


async def get_deleted_files(
    clone_dir: Path,
    prev_commit: str,
) -> list[str]:
    """이전 커밋 이후 삭제된 파일 목록을 반환한다."""
    rc, stdout, stderr = await _run_git(
        ["diff", "--name-only", "--diff-filter=D", prev_commit, "HEAD"],
        cwd=clone_dir,
    )
    if rc != 0:
        return []
    return [line for line in stdout.strip().split("\n") if line]


def collect_files(
    clone_dir: Path,
    scopes: list[ProductScope],
    supported_extensions: list[str],
    file_size_limit_kb: int,
    changed_files: list[str] | None = None,
) -> list[FileInfo]:
    """레포지토리에서 수집 대상 파일을 수집한다.

    Args:
        clone_dir: 로컬 clone 경로.
        scopes: 상품 스코프 목록.
        supported_extensions: 지원 확장자 목록.
        file_size_limit_kb: 파일 크기 제한 (KB).
        changed_files: 변경 파일 목록. None이면 전체 파일 수집.

    Returns:
        수집된 FileInfo 리스트.
    """
    files: list[FileInfo] = []

    if changed_files is not None:
        # 증분: 변경 파일만 처리
        candidates = changed_files
    else:
        # 전체: 레포 전체 순회
        candidates = []
        for abs_path in clone_dir.rglob("*"):
            if abs_path.is_file() and ".git" not in abs_path.parts:
                candidates.append(str(abs_path.relative_to(clone_dir)))

    for rel_path in candidates:
        abs_path = clone_dir / rel_path
        if not abs_path.is_file():
            continue

        file_size = abs_path.stat().st_size
        if not filter_file(rel_path, file_size, supported_extensions, file_size_limit_kb):
            continue

        product = match_product(rel_path, scopes)
        if product is None:
            continue

        try:
            content = abs_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError) as exc:
            logger.warning("파일 읽기 실패, 건너뜀: %s — %s", rel_path, exc)
            continue

        files.append(
            FileInfo(
                relative_path=rel_path,
                absolute_path=abs_path,
                product=product,
                content=content,
                content_hash=compute_content_hash(content),
                size_bytes=file_size,
            )
        )

    return files


async def store_git_code(
    store: MetadataStore,
    file_info: FileInfo,
    repo_url: str,
) -> dict[str, Any]:
    """코드 파일을 git_code 문서로 저장한다.

    Args:
        store: 초기화된 MetadataStore 인스턴스.
        file_info: 수집된 파일 정보.
        repo_url: 원본 레포지토리 URL.

    Returns:
        문서 dict에 created, changed 키가 추가된 결과.
    """
    source_id = file_info.relative_path

    # 기존 문서 확인
    existing_docs = await store.list_documents(source_type="git_code")
    existing = next(
        (d for d in existing_docs if d.get("source_id") == source_id),
        None,
    )

    if existing is None:
        doc_id = await store.create_document(
            source_type="git_code",
            source_id=source_id,
            title=Path(source_id).name,
            original_content=file_info.content,
            content_hash=file_info.content_hash,
            url=repo_url,
            author=file_info.product,
        )
        await store.add_processing_history(
            document_id=doc_id,
            action="created",
            status="completed",
        )
        doc = await store.get_document(doc_id)
        assert doc is not None
        return {**doc, "created": True, "changed": True}

    if existing["content_hash"] == file_info.content_hash:
        return {**existing, "created": False, "changed": False}

    # 내용 변경됨
    await store.update_document_content(
        existing["id"],
        original_content=file_info.content,
        content_hash=file_info.content_hash,
    )
    await store.update_document_status(existing["id"], status="changed")
    await store.add_processing_history(
        document_id=existing["id"],
        action="updated",
        prev_storage_method=existing.get("storage_method"),
        status="completed",
    )
    doc = await store.get_document(existing["id"])
    assert doc is not None
    return {**doc, "created": False, "changed": True}


async def delete_removed_files(
    store: MetadataStore,
    deleted_paths: list[str],
) -> list[str]:
    """삭제된 파일의 git_code 문서를 제거한다.

    Returns:
        실제 삭제된 source_id 리스트.
    """
    removed: list[str] = []
    existing_docs = await store.list_documents(source_type="git_code")
    existing_by_source_id = {d["source_id"]: d for d in existing_docs}

    for path in deleted_paths:
        doc = existing_by_source_id.get(path)
        if doc:
            await store.delete_document(doc["id"])
            removed.append(path)
            logger.info("삭제된 파일 문서 제거: %s", path)
    return removed


def _repo_clone_dir(data_dir: Path, repo_url: str) -> Path:
    """레포지토리의 로컬 clone 디렉토리 경로를 결정한다."""
    # URL에서 레포 이름을 추출 (예: "git@github.com:company/repo.git" → "repo")
    name = repo_url.rstrip("/").rsplit("/", 1)[-1]
    if name.endswith(".git"):
        name = name[:-4]
    return data_dir / "git_repos" / name


async def sync_repository(
    store: MetadataStore,
    config: Config,
    repo_config: dict[str, Any],
) -> SyncResult:
    """단일 레포지토리를 동기화한다.

    전체 흐름:
    1. clone 또는 pull
    2. 변경 파일 감지 (git diff)
    3. 상품 스코핑 + 파일 필터링
    4. git_code 문서 저장 (생성/갱신)
    5. 삭제된 파일 처리

    Args:
        store: 초기화된 MetadataStore 인스턴스.
        config: 애플리케이션 설정.
        repo_config: 레포지토리별 설정 dict.

    Returns:
        동기화 결과.
    """
    repo_url: str = repo_config["url"]
    branch: str = repo_config.get("branch", "main")
    result = SyncResult(repo_url=repo_url)

    # git 설정 로드
    git_config = config.get("sources.git", {})
    supported_extensions: list[str] = git_config.get("supported_extensions", [])
    file_size_limit_kb: int = git_config.get("file_size_limit_kb", 500)

    # 상품 스코프 파싱
    scopes = parse_product_scopes(repo_config)
    if not scopes:
        logger.warning("상품 스코프가 정의되지 않음: %s", repo_url)
        return result

    # clone/pull
    clone_dir = _repo_clone_dir(config.data_dir, repo_url)
    is_new_clone, prev_commit = await clone_or_pull(repo_url, clone_dir, branch)

    # 변경 파일 감지
    changed_files = await get_changed_files(clone_dir, prev_commit)

    if changed_files is not None and len(changed_files) == 0:
        logger.info("변경 사항 없음: %s", repo_url)
        return result

    # 파일 수집
    files = collect_files(
        clone_dir, scopes, supported_extensions, file_size_limit_kb, changed_files
    )

    # git_code 문서 저장
    for file_info in files:
        try:
            doc_result = await store_git_code(store, file_info, repo_url)
            if doc_result["created"]:
                result.created.append(file_info.relative_path)
            elif doc_result["changed"]:
                result.updated.append(file_info.relative_path)
            else:
                result.unchanged.append(file_info.relative_path)
        except Exception as exc:
            logger.error("파일 저장 실패: %s — %s", file_info.relative_path, exc)
            result.errors.append(
                {"path": file_info.relative_path, "error": str(exc)}
            )

    # 삭제된 파일 처리
    if not is_new_clone and prev_commit:
        deleted_paths = await get_deleted_files(clone_dir, prev_commit)
        result.deleted = await delete_removed_files(store, deleted_paths)

    logger.info(
        "레포 동기화 완료: %s (생성=%d, 갱신=%d, 삭제=%d, 무변경=%d, 오류=%d)",
        repo_url,
        len(result.created),
        len(result.updated),
        len(result.deleted),
        len(result.unchanged),
        len(result.errors),
    )
    return result


async def sync_all_repositories(
    store: MetadataStore,
    config: Config,
) -> list[SyncResult]:
    """설정된 모든 Git 레포지토리를 동기화한다.

    Args:
        store: 초기화된 MetadataStore 인스턴스.
        config: 애플리케이션 설정.

    Returns:
        각 레포지토리의 동기화 결과 리스트.
    """
    if not config.get("sources.git.enabled", False):
        logger.info("Git 소스가 비활성화되어 있습니다.")
        return []

    repositories = config.get("sources.git.repositories", [])
    if not repositories:
        logger.info("동기화할 Git 레포지토리가 없습니다.")
        return []

    results: list[SyncResult] = []
    for repo_config in repositories:
        try:
            result = await sync_repository(store, config, repo_config)
            results.append(result)
        except Exception as exc:
            logger.error("레포 동기화 실패: %s — %s", repo_config.get("url", "?"), exc)
            results.append(
                SyncResult(
                    repo_url=repo_config.get("url", "?"),
                    errors=[{"path": "", "error": str(exc)}],
                )
            )
    return results


def get_changed_products(
    files: list[FileInfo],
) -> set[str]:
    """변경된 파일 목록에서 영향받는 상품 이름 집합을 반환한다.

    후속 Phase(9.4~9.6)에서 변경된 상품만 재처리할 때 사용한다.
    """
    return {f.product for f in files}


def group_files_by_directory(
    files: list[FileInfo],
    max_files_per_group: int = 30,
    min_files_per_group: int = 3,
) -> dict[str, list[FileInfo]]:
    """파일을 디렉토리 단위로 그룹핑한다.

    D-027 Worker Agent 배정 단위로 사용된다.
    - max_files_per_group 초과 시 서브디렉토리로 분할
    - min_files_per_group 미만 시 상위 디렉토리와 병합

    Returns:
        {directory_path: [FileInfo, ...]}
    """
    # 1단계: 직속 디렉토리별 그룹핑
    groups: dict[str, list[FileInfo]] = {}
    for f in files:
        dir_path = str(Path(f.relative_path).parent)
        groups.setdefault(dir_path, []).append(f)

    # 2단계: 너무 큰 그룹은 서브디렉토리로 분할
    final: dict[str, list[FileInfo]] = {}
    for dir_path, dir_files in groups.items():
        if len(dir_files) <= max_files_per_group:
            final[dir_path] = dir_files
        else:
            # 서브디렉토리 기준으로 재분할
            sub_groups: dict[str, list[FileInfo]] = {}
            for f in dir_files:
                rel = str(Path(f.relative_path).relative_to(dir_path))
                parts = Path(rel).parts
                sub_key = str(Path(dir_path) / parts[0]) if len(parts) > 1 else dir_path
                sub_groups.setdefault(sub_key, []).append(f)
            final.update(sub_groups)

    # 3단계: 너무 작은 그룹은 상위 디렉토리와 병합
    merged: dict[str, list[FileInfo]] = {}
    for dir_path, dir_files in final.items():
        if len(dir_files) < min_files_per_group:
            parent = str(Path(dir_path).parent)
            merged.setdefault(parent, []).extend(dir_files)
        else:
            merged.setdefault(dir_path, []).extend(dir_files)

    return merged
