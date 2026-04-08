"""Worker Agent — Level 1 파일 요약 + Level 2 디렉토리 문서 생성 (D-027, Phase 9.5).

디렉토리 단위로 코드 파일을 분석하여:
- Level 1: 개별 파일 요약 (worker 엔드포인트, 경량 모델)
- Level 2: 파일 요약을 종합한 디렉토리 문서 (synthesizer 엔드포인트, 중간 모델)

관점 중립적 사실 요약을 생성하며, 관점 부여는 Category Agent(Phase 9.6)가 담당한다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from context_loop.ingestion.coordinator import (
    DirectorySummary,
    FileSummary,
)
from context_loop.ingestion.git_repository import FileInfo
from context_loop.processor.llm_client import LLMClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_FILE_SUMMARY_SYSTEM = (
    "당신은 코드 문서화 전문가입니다. "
    "주어진 코드 파일을 분석하여 관점 중립적인 사실 요약을 작성합니다. "
    "특정 관점(아키텍처, 개발, 인프라 등)에 편향되지 않도록 합니다."
)

_FILE_SUMMARY_TEMPLATE = """\
다음 코드 파일을 분석하여 한국어로 요약하세요.

## 파일 정보
- 경로: {relative_path}
- 상품: {product}

## 코드
```
{content}
```

## 요약 형식
1. **목적**: 이 파일의 주요 역할 (1~2문장)
2. **핵심 구성요소**: 주요 함수/클래스/리소스 목록과 각각의 역할
3. **의존성**: 외부 모듈, 서비스, 리소스 의존 관계
4. **데이터 흐름**: 입력 → 처리 → 출력 흐름 (해당되는 경우)

간결하게 작성하되, 코드에 없는 내용을 추측하지 마세요."""

_DIR_SYNTHESIS_SYSTEM = (
    "당신은 소프트웨어 문서화 전문가입니다. "
    "여러 파일의 요약을 종합하여 디렉토리 전체를 설명하는 문서를 작성합니다. "
    "관점 중립적인 사실 기반 문서를 작성합니다."
)

_DIR_SYNTHESIS_TEMPLATE = """\
다음은 [{product}] 상품의 `{directory}` 디렉토리에 포함된 파일들의 요약입니다.
이 요약들을 종합하여 디렉토리 전체를 설명하는 문서를 작성하세요.

## 파일 요약

{file_summaries_text}

## 작성 지침
1. **디렉토리 개요**: 이 디렉토리의 전체적인 역할과 목적 (2~3문장)
2. **구성 파일**: 각 파일이 어떤 역할을 담당하는지 간략히 설명
3. **파일 간 관계**: 파일들이 서로 어떻게 연결되고 협력하는지
4. **핵심 데이터 흐름**: 이 디렉토리를 통과하는 주요 데이터/처리 흐름
5. **외부 의존성**: 이 디렉토리가 의존하는 외부 모듈/서비스

관점 중립적으로 사실만 기술하세요. 특정 독자(아키텍트, 개발자 등)를 가정하지 마세요."""


# ---------------------------------------------------------------------------
# Worker Agent
# ---------------------------------------------------------------------------


class LLMWorkerAgent:
    """LLM 기반 Worker Agent 구현체.

    Args:
        worker_llm: Level 1 파일 요약용 LLM 클라이언트 (경량 모델).
        synthesizer_llm: Level 2 디렉토리 종합용 LLM 클라이언트 (중간 모델).
        max_file_tokens: 파일 내용 최대 글자수 (초과 시 절삭).
        max_concurrent_files: Level 1 병렬 처리 동시성 제한.
    """

    def __init__(
        self,
        worker_llm: LLMClient,
        synthesizer_llm: LLMClient,
        *,
        max_file_tokens: int = 12000,
        max_concurrent_files: int = 5,
    ) -> None:
        self._worker_llm = worker_llm
        self._synthesizer_llm = synthesizer_llm
        self._max_file_tokens = max_file_tokens
        self._file_semaphore = asyncio.Semaphore(max_concurrent_files)

    async def process_directory(
        self,
        directory: str,
        product: str,
        files: list[FileInfo],
    ) -> DirectorySummary:
        """디렉토리의 파일을 분석하여 Level 1 + Level 2 문서를 생성한다.

        Args:
            directory: 디렉토리 상대 경로.
            product: 상품명.
            files: 디렉토리 내 파일 목록.

        Returns:
            Level 1 파일 요약 + Level 2 디렉토리 종합 문서.
        """
        if not files:
            return DirectorySummary(
                directory=directory,
                product=product,
                file_summaries=[],
                document="",
            )

        # Level 1: 파일별 요약 (병렬)
        file_summaries = await self._summarize_files(product, files)

        # Level 2: 디렉토리 종합 문서
        document = await self._synthesize_directory(
            directory, product, file_summaries
        )

        return DirectorySummary(
            directory=directory,
            product=product,
            file_summaries=file_summaries,
            document=document,
        )

    # --- Level 1: 파일 요약 ---

    async def _summarize_files(
        self,
        product: str,
        files: list[FileInfo],
    ) -> list[FileSummary]:
        """Level 1 — 개별 파일 요약을 병렬 생성한다."""
        tasks = [self._summarize_one_file(product, f) for f in files]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        summaries: list[FileSummary] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.warning(
                    "파일 요약 실패: %s — %s", files[i].relative_path, result
                )
                summaries.append(
                    FileSummary(
                        relative_path=files[i].relative_path,
                        summary=f"[요약 실패: {result}]",
                    )
                )
            else:
                summaries.append(result)

        return summaries

    async def _summarize_one_file(
        self,
        product: str,
        file_info: FileInfo,
    ) -> FileSummary:
        """단일 파일을 LLM으로 요약한다."""
        async with self._file_semaphore:
            content = file_info.content
            if len(content) > self._max_file_tokens:
                content = content[: self._max_file_tokens] + "\n\n... (이하 생략)"

            prompt = _FILE_SUMMARY_TEMPLATE.format(
                relative_path=file_info.relative_path,
                product=product,
                content=content,
            )

            summary = await self._worker_llm.complete(
                prompt,
                system=_FILE_SUMMARY_SYSTEM,
                max_tokens=4096,
                temperature=0.1,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )

            return FileSummary(
                relative_path=file_info.relative_path,
                summary=summary.strip(),
            )

    # --- Level 2: 디렉토리 종합 ---

    async def _synthesize_directory(
        self,
        directory: str,
        product: str,
        file_summaries: list[FileSummary],
    ) -> str:
        """Level 2 — 파일 요약을 종합하여 디렉토리 문서를 생성한다."""
        # 성공한 요약만 사용
        valid_summaries = [
            fs for fs in file_summaries if not fs.summary.startswith("[요약 실패")
        ]
        if not valid_summaries:
            return f"[{product}] {directory}: 모든 파일 요약이 실패하여 디렉토리 문서를 생성할 수 없습니다."

        file_summaries_text = "\n\n".join(
            f"### {fs.relative_path}\n{fs.summary}" for fs in valid_summaries
        )

        prompt = _DIR_SYNTHESIS_TEMPLATE.format(
            product=product,
            directory=directory,
            file_summaries_text=file_summaries_text,
        )

        document = await self._synthesizer_llm.complete(
            prompt,
            system=_DIR_SYNTHESIS_SYSTEM,
            max_tokens=8192,
            temperature=0.1,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

        return document.strip()
