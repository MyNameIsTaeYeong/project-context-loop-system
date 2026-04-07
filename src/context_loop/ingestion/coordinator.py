"""Coordinator Agent — 멀티에이전트 문서 생성 파이프라인 조율 (D-027).

전체 흐름:
1. GitSourceConfig 로드 + 검증
2. 레포지토리별 clone/pull + git_code 저장 (git_repository 모듈 위임)
3. 상품별 파일 수집 → 디렉토리 그룹핑
4. Worker Agent 병렬 디스패치 → Level 1 파일 요약 + Level 2 디렉토리 문서
5. Category Agent 병렬 디스패치 → Level 3 상품×카테고리 관점 문서
6. 결과 저장 (code_summary, code_doc) + document_sources 연결
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from context_loop.config import Config
from context_loop.ingestion.git_config import (
    CategoryConfig,
    GitSourceConfig,
    load_git_source_config,
)
from context_loop.ingestion.git_repository import (
    FileInfo,
    _repo_clone_dir,
    collect_files,
    group_files_by_directory,
    parse_product_scopes,
    sync_repository,
)
from context_loop.storage.metadata_store import MetadataStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data types — 계층 간 전달 데이터
# ---------------------------------------------------------------------------


@dataclass
class FileSummary:
    """Level 1: 단일 파일 요약."""

    relative_path: str
    summary: str


@dataclass
class DirectorySummary:
    """Level 2: 디렉토리 종합 문서 (Worker 출력)."""

    directory: str
    product: str
    file_summaries: list[FileSummary]
    document: str  # Worker가 생성한 관점 중립 종합 문서


@dataclass
class CategoryDocument:
    """Level 3: 상품×카테고리 관점 문서 (Category Agent 출력)."""

    product: str
    category: str
    document: str
    source_directories: list[str]


@dataclass
class ProductResult:
    """단일 상품의 처리 결과."""

    product: str
    directory_summaries: list[DirectorySummary] = field(default_factory=list)
    category_documents: list[CategoryDocument] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)


@dataclass
class PipelineResult:
    """전체 파이프라인 실행 결과."""

    product_results: list[ProductResult] = field(default_factory=list)
    total_files_processed: int = 0
    total_directories: int = 0
    total_documents_generated: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Agent Protocols — Worker/Category Agent의 인터페이스
# ---------------------------------------------------------------------------


class WorkerAgent(Protocol):
    """Worker Agent 프로토콜 (Phase 9.5에서 LLM 구현)."""

    async def process_directory(
        self,
        directory: str,
        product: str,
        files: list[FileInfo],
    ) -> DirectorySummary:
        """디렉토리의 파일을 분석하여 Level 1 + Level 2 문서를 생성한다."""
        ...


class CategoryAgentProtocol(Protocol):
    """Category Agent 프로토콜 (Phase 9.6에서 LLM 구현)."""

    async def generate_document(
        self,
        product: str,
        category: CategoryConfig,
        directory_summaries: list[DirectorySummary],
    ) -> CategoryDocument:
        """Level 2 결과를 받아 Level 3 관점 문서를 생성한다."""
        ...


# ---------------------------------------------------------------------------
# Coordinator Agent
# ---------------------------------------------------------------------------


class CoordinatorAgent:
    """멀티에이전트 파이프라인 조율자 (D-027).

    Args:
        store: 초기화된 MetadataStore 인스턴스.
        config: 애플리케이션 설정.
        git_config: 파싱된 GitSourceConfig. None이면 config에서 로드.
        worker: Worker Agent 구현체.
        category_agent: Category Agent 구현체.
    """

    def __init__(
        self,
        store: MetadataStore,
        config: Config,
        git_config: GitSourceConfig | None = None,
        worker: WorkerAgent | None = None,
        category_agent: CategoryAgentProtocol | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._git_config = git_config or load_git_source_config(config)
        self._worker = worker
        self._category_agent = category_agent
        self._semaphore = asyncio.Semaphore(
            self._git_config.processing.max_concurrent_workers
        )

    # --- Public API ---

    async def run(self) -> PipelineResult:
        """전체 파이프라인을 실행한다.

        Returns:
            PipelineResult.
        """
        result = PipelineResult()

        # 1. 설정 검증
        issues = self._git_config.validate()
        if issues:
            for issue in issues:
                logger.warning("설정 문제: %s", issue)
                result.errors.append({"phase": "validation", "error": issue})

        if not self._git_config.enabled:
            logger.info("Git 소스가 비활성화되어 있습니다.")
            return result

        # 2. 레포지토리별 동기화 + 문서 생성
        for repo_config in self._git_config.repositories:
            try:
                product_results = await self._process_repository(repo_config)
                result.product_results.extend(product_results)
            except Exception as exc:
                logger.error("레포 처리 실패: %s — %s", repo_config.url, exc)
                result.errors.append(
                    {"phase": "repository", "repo": repo_config.url, "error": str(exc)}
                )

        # 3. 집계
        for pr in result.product_results:
            result.total_directories += len(pr.directory_summaries)
            result.total_documents_generated += len(pr.category_documents)
            result.total_files_processed += sum(
                len(ds.file_summaries) for ds in pr.directory_summaries
            )
            result.errors.extend(pr.errors)

        logger.info(
            "파이프라인 완료: 상품=%d, 디렉토리=%d, 문서=%d, 파일=%d, 오류=%d",
            len(result.product_results),
            result.total_directories,
            result.total_documents_generated,
            result.total_files_processed,
            len(result.errors),
        )
        return result

    # --- Internal ---

    async def _process_repository(
        self,
        repo_config: Any,
    ) -> list[ProductResult]:
        """단일 레포지토리를 처리한다.

        1. git clone/pull + 원본 코드 DB 저장 (git_code)
        2. 상품 스코프 파싱
        3. 상품별 파일 수집 + Worker/Category Agent 실행
        """
        repo_dict = {
            "url": repo_config.url,
            "branch": repo_config.branch,
            "products": repo_config.products,
        }

        # 1. git clone/pull + git_code 저장
        clone_dir = _repo_clone_dir(self._config.data_dir, repo_config.url)
        sync_result = await sync_repository(self._store, self._config, repo_dict)
        logger.info(
            "레포 동기화: %s (생성=%d, 갱신=%d)",
            repo_config.url,
            len(sync_result.created),
            len(sync_result.updated),
        )

        # 2. 상품 스코프 파싱
        scopes = parse_product_scopes(
            repo_dict,
            clone_dir=clone_dir,
            supported_extensions=self._git_config.supported_extensions or None,
        )

        # 3. 상품별 파일 수집 + Worker/Category Agent 실행
        product_results: list[ProductResult] = []
        for scope in scopes:
            product_files = collect_files(
                clone_dir,
                [scope],
                self._git_config.supported_extensions,
                self._git_config.file_size_limit_kb,
            )
            if not product_files:
                logger.info("상품 %s: 수집된 파일 없음, 건너뜀", scope.name)
                continue

            try:
                pr = await self._process_product(scope.name, product_files)
                product_results.append(pr)
            except Exception as exc:
                logger.error("상품 처리 실패: %s — %s", scope.name, exc)
                product_results.append(
                    ProductResult(
                        product=scope.name,
                        errors=[{"phase": "product", "error": str(exc)}],
                    )
                )

        return product_results

    async def _process_product(
        self,
        product: str,
        files: list[FileInfo],
    ) -> ProductResult:
        """단일 상품을 처리한다.

        1. 디렉토리별 그룹핑
        2. Worker Agent 병렬 디스패치 → Level 1+2
        3. Category Agent 병렬 디스패치 → Level 3
        """
        result = ProductResult(product=product)

        # 디렉토리 그룹핑
        groups = group_files_by_directory(
            files,
            max_files_per_group=self._git_config.processing.max_files_per_worker,
            min_files_per_group=self._git_config.processing.min_files_per_worker,
        )

        if not groups:
            logger.info("상품 %s: 처리할 파일 없음", product)
            return result

        # Worker Agent 병렬 실행
        if self._worker:
            worker_tasks = [
                self._run_worker(product, directory, dir_files)
                for directory, dir_files in groups.items()
            ]
            worker_results = await asyncio.gather(*worker_tasks, return_exceptions=True)

            for wr in worker_results:
                if isinstance(wr, Exception):
                    result.errors.append(
                        {"phase": "worker", "product": product, "error": str(wr)}
                    )
                else:
                    result.directory_summaries.append(wr)

        # Category Agent 병렬 실행
        if self._category_agent and result.directory_summaries:
            categories = self._git_config.get_category_list()
            cat_tasks = [
                self._run_category_agent(product, cat, result.directory_summaries)
                for cat in categories
            ]
            cat_results = await asyncio.gather(*cat_tasks, return_exceptions=True)

            for cr in cat_results:
                if isinstance(cr, Exception):
                    result.errors.append(
                        {"phase": "category", "product": product, "error": str(cr)}
                    )
                else:
                    result.category_documents.append(cr)

        logger.info(
            "상품 %s 처리 완료: 디렉토리=%d, 카테고리 문서=%d",
            product,
            len(result.directory_summaries),
            len(result.category_documents),
        )
        return result

    async def _run_worker(
        self,
        product: str,
        directory: str,
        files: list[FileInfo],
    ) -> DirectorySummary:
        """세마포어로 동시성을 제어하며 Worker를 실행한다."""
        async with self._semaphore:
            assert self._worker is not None
            return await self._worker.process_directory(directory, product, files)

    async def _run_category_agent(
        self,
        product: str,
        category: CategoryConfig,
        directory_summaries: list[DirectorySummary],
    ) -> CategoryDocument:
        """Category Agent를 실행한다."""
        assert self._category_agent is not None
        return await self._category_agent.generate_document(
            product, category, directory_summaries
        )

    # --- Storage Helpers ---

    async def store_directory_summary(
        self,
        summary: DirectorySummary,
    ) -> int:
        """Level 2 디렉토리 요약을 code_summary 문서로 저장한다.

        Returns:
            생성된 문서 ID.
        """
        from context_loop.ingestion.git_repository import compute_content_hash

        source_id = f"{summary.product}:{summary.directory}"
        content_hash = compute_content_hash(summary.document)

        # 기존 문서 확인
        existing_docs = await self._store.list_documents(source_type="code_summary")
        existing = next(
            (d for d in existing_docs if d.get("source_id") == source_id), None
        )

        if existing and existing["content_hash"] == content_hash:
            return existing["id"]

        if existing:
            await self._store.update_document_content(
                existing["id"], summary.document, content_hash
            )
            await self._store.update_document_status(existing["id"], status="changed")
            return existing["id"]

        doc_id = await self._store.create_document(
            source_type="code_summary",
            source_id=source_id,
            title=f"[{summary.product}] {summary.directory} 요약",
            original_content=summary.document,
            content_hash=content_hash,
        )
        return doc_id

    async def store_category_document(
        self,
        cat_doc: CategoryDocument,
        source_git_code_ids: list[int] | None = None,
    ) -> int:
        """Level 3 카테고리 문서를 code_doc으로 저장한다.

        Args:
            cat_doc: Category Agent 출력.
            source_git_code_ids: 연결할 git_code 문서 ID 목록 (document_sources용).

        Returns:
            생성된 문서 ID.
        """
        from context_loop.ingestion.git_repository import compute_content_hash

        source_id = f"{cat_doc.product}:{cat_doc.category}"
        content_hash = compute_content_hash(cat_doc.document)
        cat_config = self._git_config.get_category(cat_doc.category)
        title_suffix = cat_config.display_name if cat_config else cat_doc.category

        existing_docs = await self._store.list_documents(source_type="code_doc")
        existing = next(
            (d for d in existing_docs if d.get("source_id") == source_id), None
        )

        if existing and existing["content_hash"] == content_hash:
            return existing["id"]

        if existing:
            await self._store.update_document_content(
                existing["id"], cat_doc.document, content_hash
            )
            await self._store.update_document_status(existing["id"], status="changed")
            doc_id = existing["id"]
        else:
            doc_id = await self._store.create_document(
                source_type="code_doc",
                source_id=source_id,
                title=f"[{cat_doc.product}] {title_suffix}",
                original_content=cat_doc.document,
                content_hash=content_hash,
            )

        # document_sources 연결 (D-026)
        if source_git_code_ids:
            await self._store.delete_document_sources(doc_id)
            for git_code_id in source_git_code_ids:
                await self._store.add_document_source(doc_id, git_code_id)

        return doc_id

    async def run_and_store(self) -> PipelineResult:
        """파이프라인을 실행하고 결과를 DB에 저장한다.

        run()의 결과를 받아 code_summary와 code_doc을 저장하고,
        document_sources 연결까지 수행하는 상위 레벨 메서드.
        """
        result = await self.run()

        for pr in result.product_results:
            # Level 2 저장
            for ds in pr.directory_summaries:
                try:
                    await self.store_directory_summary(ds)
                except Exception as exc:
                    logger.error(
                        "디렉토리 요약 저장 실패: %s/%s — %s",
                        ds.product, ds.directory, exc,
                    )

            # Level 3 저장
            for cd in pr.category_documents:
                try:
                    await self.store_category_document(cd)
                except Exception as exc:
                    logger.error(
                        "카테고리 문서 저장 실패: %s/%s — %s",
                        cd.product, cd.category, exc,
                    )

        return result
