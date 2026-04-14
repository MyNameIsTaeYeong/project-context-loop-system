"""Coordinator Agent — Git 코드 기반 파일 요약 파이프라인 조율.

전체 흐름:
1. GitSourceConfig 로드 + 검증
2. 레포지토리별 clone/pull
3. 상품별 파일 수집 → 디렉토리 그룹핑
4. Worker Agent 병렬 디스패치 → Level 1 파일별 요약 (code_file_summary)
5. 결과 저장 (git_code + code_file_summary) + document_sources 연결
6. 저장된 문서를 기존 파이프라인(chunker → embedder → graph_extractor)으로 처리
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from context_loop.config import Config
from context_loop.ingestion.git_config import (
    GitSourceConfig,
    load_git_source_config,
)
from context_loop.ingestion.git_repository import (
    FileInfo,
    _repo_clone_dir,
    clone_or_pull,
    collect_files,
    group_files_by_directory,
    parse_product_scopes,
    store_git_code,
)
from context_loop.storage.metadata_store import MetadataStore

if TYPE_CHECKING:
    from langchain_core.embeddings import Embeddings

    from context_loop.processor.llm_client import LLMClient
    from context_loop.storage.graph_store import GraphStore
    from context_loop.storage.vector_store import VectorStore

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
    """Worker Agent 출력 컨테이너.

    Worker가 디렉토리 내 파일들을 분석한 결과.
    file_summaries(Level 1)가 핵심이며, document 필드는 미사용.
    """

    directory: str
    product: str
    file_summaries: list[FileSummary]
    document: str = ""


@dataclass
class ProductResult:
    """단일 상품의 처리 결과."""

    product: str
    directory_summaries: list[DirectorySummary] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    files: list[FileInfo] = field(default_factory=list)
    repo_url: str = ""


@dataclass
class PipelineResult:
    """전체 파이프라인 실행 결과."""

    product_results: list[ProductResult] = field(default_factory=list)
    total_files_processed: int = 0
    total_directories: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Agent Protocol — Worker Agent 인터페이스
# ---------------------------------------------------------------------------


class WorkerAgent(Protocol):
    """Worker Agent 프로토콜."""

    async def process_directory(
        self,
        directory: str,
        product: str,
        files: list[FileInfo],
    ) -> DirectorySummary:
        """디렉토리의 파일을 분석하여 파일별 요약을 생성한다."""
        ...


# ---------------------------------------------------------------------------
# Coordinator Agent
# ---------------------------------------------------------------------------


class CoordinatorAgent:
    """파이프라인 조율자.

    Args:
        store: 초기화된 MetadataStore 인스턴스.
        config: 애플리케이션 설정.
        git_config: 파싱된 GitSourceConfig. None이면 config에서 로드.
        worker: Worker Agent 구현체.
        vector_store: VectorStore 인스턴스 (파이프라인용, optional).
        graph_store: GraphStore 인스턴스 (파이프라인용, optional).
        pipeline_llm_client: LLM 클라이언트 (파이프라인용, optional).
        embedding_client: 임베딩 클라이언트 (파이프라인용, optional).
    """

    def __init__(
        self,
        store: MetadataStore,
        config: Config,
        git_config: GitSourceConfig | None = None,
        worker: WorkerAgent | None = None,
        *,
        vector_store: VectorStore | None = None,
        graph_store: GraphStore | None = None,
        pipeline_llm_client: LLMClient | None = None,
        embedding_client: Embeddings | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._git_config = git_config or load_git_source_config(config)
        self._worker = worker
        self._semaphore = asyncio.Semaphore(
            self._git_config.processing.max_concurrent_workers
        )
        # 기존 처리 파이프라인 의존성
        self._vector_store = vector_store
        self._graph_store = graph_store
        self._pipeline_llm_client = pipeline_llm_client
        self._embedding_client = embedding_client

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
            result.total_files_processed += sum(
                len(ds.file_summaries) for ds in pr.directory_summaries
            )
            result.errors.extend(pr.errors)

        logger.info(
            "파이프라인 완료: 상품=%d, 디렉토리=%d, 파일=%d, 오류=%d",
            len(result.product_results),
            result.total_directories,
            result.total_files_processed,
            len(result.errors),
        )
        return result

    # --- Internal ---

    async def _process_repository(
        self,
        repo_config: Any,
    ) -> list[ProductResult]:
        """단일 레포지토리를 처리한다."""
        repo_dict = {
            "url": repo_config.url,
            "branch": repo_config.branch,
            "products": repo_config.products,
        }

        # 1. git clone/pull
        clone_dir = _repo_clone_dir(self._config.data_dir, repo_config.url)
        is_new, prev_commit = await clone_or_pull(
            repo_config.url, clone_dir, repo_config.branch,
        )
        logger.info(
            "레포 %s: %s",
            repo_config.url,
            "새로 clone" if is_new else f"pull 완료 (이전: {prev_commit})",
        )

        # 2. 상품 스코프 파싱
        scopes = parse_product_scopes(
            repo_dict,
            clone_dir=clone_dir,
            supported_extensions=self._git_config.supported_extensions or None,
        )

        # 3. 상품별 파일 수집 + Worker Agent 실행
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
                pr.files = product_files
                pr.repo_url = repo_config.url
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
        2. Worker Agent 병렬 디스패치 → Level 1 파일 요약
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

        logger.info(
            "상품 %s 처리 완료: 디렉토리=%d, 파일 요약=%d",
            product,
            len(result.directory_summaries),
            sum(len(ds.file_summaries) for ds in result.directory_summaries),
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

    # --- Pipeline Processing ---

    @property
    def _pipeline_available(self) -> bool:
        """파이프라인 의존성이 모두 설정되었는지 확인한다."""
        return all([
            self._vector_store is not None,
            self._graph_store is not None,
            self._pipeline_llm_client is not None,
            self._embedding_client is not None,
        ])

    async def _process_through_pipeline(self, document_id: int) -> dict[str, Any] | None:
        """저장된 문서를 기존 파이프라인으로 처리한다.

        classifier → chunker → embedder → graph_extractor 순서로 처리.
        파이프라인 의존성이 미설정이면 건너뛴다.
        실패해도 예외를 전파하지 않는다 (저장은 이미 완료).
        """
        if not self._pipeline_available:
            logger.debug(
                "파이프라인 의존성 미설정, 건너뜀: document_id=%d", document_id,
            )
            return None

        try:
            from context_loop.processor.pipeline import PipelineConfig, process_document

            pipeline_config = PipelineConfig(
                chunk_size=self._config.get("processor.chunk_size", 512),
                chunk_overlap=self._config.get("processor.chunk_overlap", 50),
                embedding_model=self._config.get(
                    "processor.embedding_model", "text-embedding-3-small",
                ),
            )

            result = await process_document(
                document_id,
                meta_store=self._store,
                vector_store=self._vector_store,  # type: ignore[arg-type]
                graph_store=self._graph_store,  # type: ignore[arg-type]
                llm_client=self._pipeline_llm_client,  # type: ignore[arg-type]
                embedding_client=self._embedding_client,  # type: ignore[arg-type]
                config=pipeline_config,
            )
            logger.info(
                "파이프라인 처리 완료: document_id=%d, method=%s, chunks=%d, nodes=%d",
                document_id,
                result["storage_method"],
                result["chunk_count"],
                result["node_count"],
            )
            return result
        except Exception as exc:
            logger.error(
                "파이프라인 처리 실패: document_id=%d — %s", document_id, exc,
            )
            return None

    # --- Storage Helpers ---

    async def store_file_summary(
        self,
        file_summary: FileSummary,
        product: str,
        source_git_code_id: int | None = None,
    ) -> tuple[int, bool]:
        """Level 1 파일 요약을 code_file_summary 문서로 저장한다.

        Args:
            file_summary: Worker Agent의 파일별 요약.
            product: 상품명.
            source_git_code_id: 연결할 git_code 문서 ID (document_sources용).

        Returns:
            (문서 ID, 파이프라인 처리 필요 여부) 튜플.
            신규/변경 시 True, 무변경 시 False.
        """
        from context_loop.ingestion.git_repository import compute_content_hash

        source_id = f"{product}:{file_summary.relative_path}"
        content_hash = compute_content_hash(file_summary.summary)

        existing_docs = await self._store.list_documents(
            source_type="code_file_summary",
        )
        existing = next(
            (d for d in existing_docs if d.get("source_id") == source_id), None
        )

        needs_pipeline = False
        if existing and existing["content_hash"] == content_hash:
            doc_id = existing["id"]
        elif existing:
            await self._store.update_document_content(
                existing["id"], file_summary.summary, content_hash
            )
            await self._store.update_document_status(
                existing["id"], status="changed",
            )
            doc_id = existing["id"]
            needs_pipeline = True
        else:
            filename = file_summary.relative_path.rsplit("/", 1)[-1]
            doc_id = await self._store.create_document(
                source_type="code_file_summary",
                source_id=source_id,
                title=f"[{product}] {filename}",
                original_content=file_summary.summary,
                content_hash=content_hash,
            )
            needs_pipeline = True

        # document_sources: code_file_summary → git_code
        if source_git_code_id is not None:
            await self._store.delete_document_sources(doc_id)
            await self._store.add_document_source(
                doc_id, source_git_code_id, file_summary.relative_path,
            )

        return doc_id, needs_pipeline

    async def run_and_store(self) -> PipelineResult:
        """파이프라인을 실행하고 결과를 DB에 저장한다.

        run()의 결과를 받아:
        1. git_code 문서 저장 (원본 코드 파일 — 파이프라인 불필요)
        2. code_file_summary 저장 (Level 1) + document_sources 연결 + 파이프라인
        """
        result = await self.run()

        pipeline_processed = 0
        pipeline_failed = 0

        for pr in result.product_results:
            # git_code 저장 + git_code_map 구축
            git_code_map: dict[str, int] = {}
            for fi in pr.files:
                try:
                    doc_result = await store_git_code(
                        self._store, fi, pr.repo_url,
                    )
                    git_code_map[fi.relative_path] = doc_result["id"]
                except Exception as exc:
                    logger.error(
                        "git_code 저장 실패: %s — %s",
                        fi.relative_path, exc,
                    )

            # Level 1 저장 (code_file_summary) + document_sources 연결 + 파이프라인
            for ds in pr.directory_summaries:
                for fs in ds.file_summaries:
                    try:
                        git_code_id = git_code_map.get(fs.relative_path)
                        doc_id, needs_pipeline = await self.store_file_summary(
                            fs, ds.product,
                            source_git_code_id=git_code_id,
                        )
                        if needs_pipeline:
                            pipe_result = await self._process_through_pipeline(doc_id)
                            if pipe_result:
                                pipeline_processed += 1
                            else:
                                pipeline_failed += 1
                    except Exception as exc:
                        logger.error(
                            "파일 요약 저장 실패: %s/%s — %s",
                            ds.product, fs.relative_path, exc,
                        )

        if self._pipeline_available:
            logger.info(
                "파이프라인 처리 집계: 성공=%d, 실패=%d",
                pipeline_processed, pipeline_failed,
            )

        return result
