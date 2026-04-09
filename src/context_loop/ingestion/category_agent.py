"""Category Agent — Level 3 상품×카테고리별 관점 문서 생성 (D-027, D-028, Phase 9.6).

Level 2 디렉토리 문서(Worker 출력)를 종합하여 카테고리별 관점 문서를 생성한다.
- config에 정의된 카테고리 프롬프트를 system 프롬프트로 사용
- orchestrator 엔드포인트(고성능 모델, Opus급) 사용 (D-029)
- 관점 부여는 이 에이전트가 담당 (Worker는 관점 중립)

입력이 클 경우 map-reduce 방식으로 분할 처리한다:
- Map: 디렉토리 요약을 글자수 기반 배치로 나누어 부분 관점 문서 생성 (병렬)
- Reduce: 부분 문서를 종합하여 최종 카테고리 문서 생성
- 배치가 1개이면 단일 호출로 처리 (Reduce 생략)
"""

from __future__ import annotations

import asyncio
import logging

from context_loop.ingestion.coordinator import (
    CategoryDocument,
    DirectorySummary,
)
from context_loop.ingestion.git_config import CategoryConfig
from context_loop.processor.llm_client import LLMClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_CATEGORY_USER_TEMPLATE = """\
다음은 [{product}] 상품의 코드 분석 결과입니다.
각 디렉토리별로 관점 중립적인 요약이 제공됩니다.
이 정보를 바탕으로 **{display_name}** 관점의 문서를 작성하세요.

대상 독자: {target_audience}

## 디렉토리별 분석 결과

{directory_summaries_text}

## 작성 지침
1. 위 분석 결과를 종합하여 [{product}] 상품의 **{display_name}** 문서를 작성하세요.
2. 대상 독자({target_audience})가 필요로 하는 정보에 초점을 맞추세요.
3. 코드 분석에 없는 내용을 추측하지 마세요.
4. 한국어로 작성하세요.
5. 마크다운 형식으로 구조화하세요."""

_MAP_USER_TEMPLATE = """\
다음은 [{product}] 상품의 일부 디렉토리에 대한 코드 분석 결과입니다.
(배치 {batch_index}/{total_batches})

이 정보를 바탕으로 **{display_name}** 관점의 **부분 문서**를 작성하세요.

대상 독자: {target_audience}

## 디렉토리별 분석 결과

{directory_summaries_text}

## 작성 지침
1. 위 분석 결과를 바탕으로 **{display_name}** 관점의 부분 문서를 작성하세요.
2. 대상 독자({target_audience})가 필요로 하는 정보에 초점을 맞추세요.
3. 이 부분 문서는 나중에 다른 배치의 결과와 종합됩니다. 핵심 내용을 빠짐없이 포함하세요.
4. 코드 분석에 없는 내용을 추측하지 마세요.
5. 한국어로 작성하세요."""

_REDUCE_USER_TEMPLATE = """\
다음은 [{product}] 상품의 **{display_name}** 관점으로 작성된 부분 문서들입니다.
이 부분 문서들을 종합하여 하나의 완성된 문서를 작성하세요.

대상 독자: {target_audience}

## 부분 문서들

{partial_documents_text}

## 작성 지침
1. 위 부분 문서들을 종합하여 [{product}] 상품의 **{display_name}** 최종 문서를 작성하세요.
2. 중복 내용은 통합하고, 관련 내용은 논리적으로 재구성하세요.
3. 대상 독자({target_audience})가 필요로 하는 정보에 초점을 맞추세요.
4. 부분 문서에 없는 내용을 추가하지 마세요.
5. 한국어로 작성하세요.
6. 마크다운 형식으로 구조화하세요."""


# ---------------------------------------------------------------------------
# Category Agent
# ---------------------------------------------------------------------------


class LLMCategoryAgent:
    """LLM 기반 Category Agent 구현체.

    입력 크기에 따라 자동으로 처리 방식을 결정한다:
    - 총 글자수가 max_chars_per_batch 이내: 단일 LLM 호출
    - 초과: map-reduce 방식 (배치별 부분 문서 생성 → 종합)

    Args:
        llm: Level 3 카테고리 문서 생성용 LLM 클라이언트 (고성능 모델).
        max_chars_per_batch: 배치당 최대 글자수. 이 값에 따라 분할 횟수가
            동적으로 결정된다.
    """

    def __init__(
        self,
        llm: LLMClient,
        *,
        max_chars_per_batch: int = 8000,
    ) -> None:
        self._llm = llm
        self._max_chars_per_batch = max_chars_per_batch

    async def generate_document(
        self,
        product: str,
        category: CategoryConfig,
        directory_summaries: list[DirectorySummary],
    ) -> CategoryDocument:
        """Level 2 결과를 받아 Level 3 관점 문서를 생성한다.

        Args:
            product: 상품명.
            category: 카테고리 설정 (프롬프트 포함).
            directory_summaries: Worker가 생성한 디렉토리별 요약 목록.

        Returns:
            카테고리별 관점 문서.
        """
        source_directories = [ds.directory for ds in directory_summaries]

        if not directory_summaries:
            logger.warning(
                "상품 %s, 카테고리 %s: 디렉토리 요약이 없어 빈 문서 반환",
                product, category.name,
            )
            return CategoryDocument(
                product=product,
                category=category.name,
                document="",
                source_directories=[],
            )

        # 글자수 기반 동적 배치 분할
        batches = self._split_into_batches(directory_summaries)

        if len(batches) == 1:
            # 단일 배치 → 단일 호출 (Reduce 불필요)
            document = await self._generate_single(
                product, category, batches[0],
            )
        else:
            # 다중 배치 → Map-Reduce
            document = await self._generate_map_reduce(
                product, category, batches,
            )

        return CategoryDocument(
            product=product,
            category=category.name,
            document=document.strip(),
            source_directories=source_directories,
        )

    # --- 단일 호출 ---

    async def _generate_single(
        self,
        product: str,
        category: CategoryConfig,
        summaries: list[DirectorySummary],
    ) -> str:
        """배치가 1개일 때 단일 LLM 호출로 문서를 생성한다."""
        directory_summaries_text = self._build_summaries_text(summaries)

        user_prompt = _CATEGORY_USER_TEMPLATE.format(
            product=product,
            display_name=category.display_name,
            target_audience=category.target_audience,
            directory_summaries_text=directory_summaries_text,
        )
        system_prompt = category.prompt.strip()

        logger.info(
            "Category Agent 단일 호출: 상품=%s, 카테고리=%s, 디렉토리=%d개",
            product, category.name, len(summaries),
        )

        return await self._llm.complete(
            user_prompt,
            system=system_prompt,
            max_tokens=16384,
            temperature=0.1,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

    # --- Map-Reduce ---

    async def _generate_map_reduce(
        self,
        product: str,
        category: CategoryConfig,
        batches: list[list[DirectorySummary]],
    ) -> str:
        """Map-Reduce로 문서를 생성한다.

        Map: 배치별 부분 관점 문서 생성 (병렬)
        Reduce: 부분 문서를 종합하여 최종 문서 생성
        """
        total_dirs = sum(len(b) for b in batches)
        logger.info(
            "Category Agent map-reduce: 상품=%s, 카테고리=%s, "
            "디렉토리=%d개, 배치=%d개",
            product, category.name, total_dirs, len(batches),
        )

        # Map: 병렬로 부분 문서 생성
        map_tasks = [
            self._map_batch(product, category, batch, i + 1, len(batches))
            for i, batch in enumerate(batches)
        ]
        map_results = await asyncio.gather(*map_tasks, return_exceptions=True)

        partial_docs: list[str] = []
        for i, result in enumerate(map_results):
            if isinstance(result, Exception):
                logger.warning(
                    "Map 배치 %d/%d 실패: %s", i + 1, len(batches), result,
                )
            else:
                partial_docs.append(result)

        if not partial_docs:
            return f"[{product}] {category.display_name}: 모든 배치 처리가 실패했습니다."

        # 부분 문서가 1개뿐이면 Reduce 생략
        if len(partial_docs) == 1:
            return partial_docs[0]

        # Reduce: 부분 문서 종합
        return await self._reduce(product, category, partial_docs)

    async def _map_batch(
        self,
        product: str,
        category: CategoryConfig,
        batch: list[DirectorySummary],
        batch_index: int,
        total_batches: int,
    ) -> str:
        """Map 단계: 단일 배치의 부분 관점 문서를 생성한다."""
        directory_summaries_text = self._build_summaries_text(batch)

        user_prompt = _MAP_USER_TEMPLATE.format(
            product=product,
            display_name=category.display_name,
            target_audience=category.target_audience,
            directory_summaries_text=directory_summaries_text,
            batch_index=batch_index,
            total_batches=total_batches,
        )
        system_prompt = category.prompt.strip()

        logger.info(
            "Map 배치 %d/%d: 상품=%s, 카테고리=%s, 디렉토리=%d개",
            batch_index, total_batches, product, category.name, len(batch),
        )

        return await self._llm.complete(
            user_prompt,
            system=system_prompt,
            max_tokens=8192,
            temperature=0.1,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

    async def _reduce(
        self,
        product: str,
        category: CategoryConfig,
        partial_docs: list[str],
    ) -> str:
        """Reduce 단계: 부분 문서들을 종합하여 최종 문서를 생성한다."""
        partial_documents_text = "\n\n---\n\n".join(
            f"### 부분 문서 {i + 1}\n\n{doc}"
            for i, doc in enumerate(partial_docs)
        )

        user_prompt = _REDUCE_USER_TEMPLATE.format(
            product=product,
            display_name=category.display_name,
            target_audience=category.target_audience,
            partial_documents_text=partial_documents_text,
        )
        system_prompt = category.prompt.strip()

        logger.info(
            "Reduce: 상품=%s, 카테고리=%s, 부분문서=%d개",
            product, category.name, len(partial_docs),
        )

        return await self._llm.complete(
            user_prompt,
            system=system_prompt,
            max_tokens=16384,
            temperature=0.1,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

    # --- Helpers ---

    def _split_into_batches(
        self,
        directory_summaries: list[DirectorySummary],
    ) -> list[list[DirectorySummary]]:
        """디렉토리 요약을 글자수 기반으로 동적 분할한다.

        각 배치의 총 글자수가 max_chars_per_batch를 넘지 않도록 나눈다.
        단, 개별 디렉토리가 max_chars_per_batch보다 큰 경우에도 별도 배치에 포함한다.
        """
        batches: list[list[DirectorySummary]] = []
        current_batch: list[DirectorySummary] = []
        current_chars = 0

        for ds in directory_summaries:
            doc_len = len(ds.document)
            if current_batch and current_chars + doc_len > self._max_chars_per_batch:
                batches.append(current_batch)
                current_batch = []
                current_chars = 0
            current_batch.append(ds)
            current_chars += doc_len

        if current_batch:
            batches.append(current_batch)

        return batches

    @staticmethod
    def _build_summaries_text(
        directory_summaries: list[DirectorySummary],
    ) -> str:
        """디렉토리 요약들을 하나의 텍스트로 합친다."""
        parts = [
            f"### {ds.directory}\n\n{ds.document}"
            for ds in directory_summaries
        ]
        return "\n\n".join(parts)
