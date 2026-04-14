"""Coordinator — Git 코드 수집 + 파이프라인 처리 조율.

전체 흐름:
1. GitSourceConfig 로드 + 검증
2. 레포지토리별 clone/pull
3. 상품별 파일 수집
4. git_code 문서 DB 저장 (원본 코드)
5. 신규/변경된 git_code를 기존 파이프라인(chunker → embedder → graph_extractor)으로
   hybrid 방식으로 직접 처리 (LLM Classifier 건너뜀)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

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
# Data types
# ---------------------------------------------------------------------------


@dataclass
class ProductResult:
    """단일 상품의 처리 결과."""

    product: str
    errors: list[dict[str, str]] = field(default_factory=list)
    files: list[FileInfo] = field(default_factory=list)
    repo_url: str = ""


@dataclass
class PipelineResult:
    """전체 파이프라인 실행 결과."""

    product_results: list[ProductResult] = field(default_factory=list)
    total_files_processed: int = 0
    errors: list[dict[str, str]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class CoordinatorAgent:
    """Git 코드 수집 + 파이프라인 처리 조율자.

    Args:
        store: 초기화된 MetadataStore 인스턴스.
        config: 애플리케이션 설정.
        git_config: 파싱된 GitSourceConfig. None이면 config에서 로드.
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
        *,
        vector_store: VectorStore | None = None,
        graph_store: GraphStore | None = None,
        pipeline_llm_client: LLMClient | None = None,
        embedding_client: Embeddings | None = None,
    ) -> None:
        self._store = store
        self._config = config
        self._git_config = git_config or load_git_source_config(config)
        # 기존 처리 파이프라인 의존성
        self._vector_store = vector_store
        self._graph_store = graph_store
        self._pipeline_llm_client = pipeline_llm_client
        self._embedding_client = embedding_client

    # --- Public API ---

    async def run(self) -> PipelineResult:
        """Git 레포지토리를 동기화하고 파일을 수집한다.

        Returns:
            PipelineResult (파일 목록 포함, DB 저장은 하지 않음).
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

        # 2. 레포지토리별 동기화
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
            result.total_files_processed += len(pr.files)
            result.errors.extend(pr.errors)

        logger.info(
            "파이프라인 완료: 상품=%d, 파일=%d, 오류=%d",
            len(result.product_results),
            result.total_files_processed,
            len(result.errors),
        )
        return result

    async def run_and_store(self) -> PipelineResult:
        """레포지토리를 동기화하고 git_code를 저장 + 파이프라인 처리한다.

        run()의 결과를 받아:
        1. git_code 문서 저장 (원본 코드)
        2. 신규/변경된 git_code를 파이프라인으로 처리 (hybrid 고정, Classifier 건너뜀)
        """
        result = await self.run()

        pipeline_processed = 0
        pipeline_failed = 0

        for pr in result.product_results:
            for fi in pr.files:
                try:
                    doc_result = await store_git_code(
                        self._store, fi, pr.repo_url,
                    )
                    # 신규 또는 변경된 파일만 파이프라인 처리
                    if doc_result.get("changed", False):
                        pipe_result = await self._process_through_pipeline(
                            doc_result["id"],
                        )
                        if pipe_result:
                            pipeline_processed += 1
                        elif self._pipeline_available:
                            pipeline_failed += 1
                except Exception as exc:
                    logger.error(
                        "git_code 처리 실패: %s — %s",
                        fi.relative_path, exc,
                    )

        if self._pipeline_available:
            logger.info(
                "파이프라인 처리 집계: 성공=%d, 실패=%d",
                pipeline_processed, pipeline_failed,
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

        # 3. 상품별 파일 수집
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

            pr = ProductResult(
                product=scope.name,
                files=product_files,
                repo_url=repo_config.url,
            )
            product_results.append(pr)

            logger.info(
                "상품 %s: 파일 %d개 수집",
                scope.name, len(product_files),
            )

        return product_results

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

        git_code는 항상 hybrid로 처리 (Classifier 건너뜀).
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
                storage_method_override="hybrid",
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
