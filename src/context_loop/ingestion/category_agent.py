"""Category Agent — Level 3 상품×카테고리별 관점 문서 생성 (D-027, D-028, Phase 9.6).

Level 2 디렉토리 문서(Worker 출력)를 종합하여 카테고리별 관점 문서를 생성한다.
- config에 정의된 카테고리 프롬프트를 system 프롬프트로 사용
- orchestrator 엔드포인트(고성능 모델, Opus급) 사용 (D-029)
- 관점 부여는 이 에이전트가 담당 (Worker는 관점 중립)
"""

from __future__ import annotations

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


# ---------------------------------------------------------------------------
# Category Agent
# ---------------------------------------------------------------------------


class LLMCategoryAgent:
    """LLM 기반 Category Agent 구현체.

    Args:
        llm: Level 3 카테고리 문서 생성용 LLM 클라이언트 (고성능 모델).
        max_input_chars: 디렉토리 요약 합산 최대 글자수 (초과 시 절삭).
    """

    def __init__(
        self,
        llm: LLMClient,
        *,
        max_input_chars: int = 80000,
    ) -> None:
        self._llm = llm
        self._max_input_chars = max_input_chars

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

        # 디렉토리 요약 텍스트 조립
        directory_summaries_text = self._build_summaries_text(directory_summaries)

        # User 프롬프트 생성
        user_prompt = _CATEGORY_USER_TEMPLATE.format(
            product=product,
            display_name=category.display_name,
            target_audience=category.target_audience,
            directory_summaries_text=directory_summaries_text,
        )

        # System 프롬프트 = config에 정의된 카테고리 프롬프트 (D-028)
        system_prompt = category.prompt.strip()

        logger.info(
            "Category Agent 실행: 상품=%s, 카테고리=%s, 디렉토리=%d개",
            product, category.name, len(directory_summaries),
        )

        document = await self._llm.complete(
            user_prompt,
            system=system_prompt,
            max_tokens=16384,
            temperature=0.1,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

        return CategoryDocument(
            product=product,
            category=category.name,
            document=document.strip(),
            source_directories=source_directories,
        )

    def _build_summaries_text(
        self,
        directory_summaries: list[DirectorySummary],
    ) -> str:
        """디렉토리 요약들을 하나의 텍스트로 합친다.

        max_input_chars를 초과하면 뒤쪽 디렉토리를 절삭한다.
        """
        parts: list[str] = []
        total_chars = 0

        for ds in directory_summaries:
            part = f"### {ds.directory}\n\n{ds.document}"
            part_len = len(part)

            if total_chars + part_len > self._max_input_chars:
                remaining = self._max_input_chars - total_chars
                if remaining > 100:
                    parts.append(part[:remaining] + "\n\n... (이하 생략)")
                else:
                    parts.append(
                        f"\n\n... (나머지 디렉토리 {len(directory_summaries) - len(parts)}개 생략)"
                    )
                break

            parts.append(part)
            total_chars += part_len

        return "\n\n".join(parts)
