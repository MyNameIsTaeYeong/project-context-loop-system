"""Category Agent 테스트 (Phase 9.6)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from context_loop.ingestion.category_agent import LLMCategoryAgent
from context_loop.ingestion.coordinator import (
    CategoryDocument,
    DirectorySummary,
    FileSummary,
)
from context_loop.ingestion.git_config import CategoryConfig


# --- Helpers ---


def _make_category(
    name: str = "architecture",
    display_name: str = "아키텍처",
    target_audience: str = "아키텍트, 시니어 개발자",
    prompt: str = "당신은 소프트웨어 아키텍트입니다.",
) -> CategoryConfig:
    return CategoryConfig(
        name=name,
        display_name=display_name,
        target_audience=target_audience,
        prompt=prompt,
    )


def _make_dir_summary(
    directory: str = "services/vpc/handler",
    product: str = "vpc",
    document: str = "# handler\n디렉토리 문서 내용",
) -> DirectorySummary:
    return DirectorySummary(
        directory=directory,
        product=product,
        file_summaries=[
            FileSummary("handler.go", "핸들러 파일 요약"),
            FileSummary("util.go", "유틸 파일 요약"),
        ],
        document=document,
    )


def _make_mock_llm(response: str = "# VPC 아키텍처\n생성된 문서 내용") -> AsyncMock:
    mock = AsyncMock()
    mock.complete.return_value = response
    return mock


# --- Tests ---


class TestLLMCategoryAgentBasic:
    async def test_generate_document_returns_category_document(self) -> None:
        """정상 호출 시 CategoryDocument를 반환한다."""
        mock_llm = _make_mock_llm()
        agent = LLMCategoryAgent(mock_llm)

        result = await agent.generate_document(
            product="vpc",
            category=_make_category(),
            directory_summaries=[_make_dir_summary()],
        )

        assert isinstance(result, CategoryDocument)
        assert result.product == "vpc"
        assert result.category == "architecture"
        assert result.document == "# VPC 아키텍처\n생성된 문서 내용"

    async def test_product_and_category_in_result(self) -> None:
        """반환된 문서에 product, category가 올바르게 설정된다."""
        mock_llm = _make_mock_llm()
        agent = LLMCategoryAgent(mock_llm)

        result = await agent.generate_document(
            product="billing",
            category=_make_category(name="pricing", display_name="과금 체계"),
            directory_summaries=[_make_dir_summary(product="billing")],
        )

        assert result.product == "billing"
        assert result.category == "pricing"

    async def test_source_directories_populated(self) -> None:
        """source_directories가 입력 DirectorySummary들의 directory 목록과 일치한다."""
        mock_llm = _make_mock_llm()
        agent = LLMCategoryAgent(mock_llm)

        summaries = [
            _make_dir_summary(directory="dir1"),
            _make_dir_summary(directory="dir2"),
            _make_dir_summary(directory="dir3"),
        ]

        result = await agent.generate_document(
            product="vpc",
            category=_make_category(),
            directory_summaries=summaries,
        )

        assert result.source_directories == ["dir1", "dir2", "dir3"]

    async def test_document_is_stripped(self) -> None:
        """LLM 응답의 앞뒤 공백이 제거된다."""
        mock_llm = _make_mock_llm("  \n문서 내용\n  ")
        agent = LLMCategoryAgent(mock_llm)

        result = await agent.generate_document(
            product="vpc",
            category=_make_category(),
            directory_summaries=[_make_dir_summary()],
        )

        assert result.document == "문서 내용"


class TestLLMCategoryAgentPrompts:
    async def test_system_prompt_from_category_config(self) -> None:
        """CategoryConfig.prompt가 system 프롬프트로 전달된다."""
        mock_llm = _make_mock_llm()
        agent = LLMCategoryAgent(mock_llm)

        custom_prompt = "당신은 DevOps 엔지니어입니다. 인프라 관점으로 분석하세요."
        category = _make_category(prompt=custom_prompt)

        await agent.generate_document(
            product="vpc",
            category=category,
            directory_summaries=[_make_dir_summary()],
        )

        call_kwargs = mock_llm.complete.call_args
        assert call_kwargs.kwargs["system"] == custom_prompt

    async def test_user_prompt_includes_all_directories(self) -> None:
        """모든 DirectorySummary의 document가 user 프롬프트에 포함된다."""
        mock_llm = _make_mock_llm()
        agent = LLMCategoryAgent(mock_llm)

        summaries = [
            _make_dir_summary(directory="dir_a", document="문서A 내용"),
            _make_dir_summary(directory="dir_b", document="문서B 내용"),
        ]

        await agent.generate_document(
            product="vpc",
            category=_make_category(),
            directory_summaries=summaries,
        )

        user_prompt = mock_llm.complete.call_args[0][0]
        assert "dir_a" in user_prompt
        assert "문서A 내용" in user_prompt
        assert "dir_b" in user_prompt
        assert "문서B 내용" in user_prompt

    async def test_target_audience_in_prompt(self) -> None:
        """target_audience가 user 프롬프트에 포함된다."""
        mock_llm = _make_mock_llm()
        agent = LLMCategoryAgent(mock_llm)

        category = _make_category(target_audience="인프라 엔지니어, SRE")

        await agent.generate_document(
            product="vpc",
            category=category,
            directory_summaries=[_make_dir_summary()],
        )

        user_prompt = mock_llm.complete.call_args[0][0]
        assert "인프라 엔지니어, SRE" in user_prompt

    async def test_max_tokens_and_temperature(self) -> None:
        """LLM 호출 시 max_tokens=16384, temperature=0.1이 전달된다."""
        mock_llm = _make_mock_llm()
        agent = LLMCategoryAgent(mock_llm)

        await agent.generate_document(
            product="vpc",
            category=_make_category(),
            directory_summaries=[_make_dir_summary()],
        )

        call_kwargs = mock_llm.complete.call_args.kwargs
        assert call_kwargs["max_tokens"] == 16384
        assert call_kwargs["temperature"] == 0.1


class TestLLMCategoryAgentEdgeCases:
    async def test_empty_directory_summaries(self) -> None:
        """빈 입력 시 빈 문서를 반환하고 LLM을 호출하지 않는다."""
        mock_llm = _make_mock_llm()
        agent = LLMCategoryAgent(mock_llm)

        result = await agent.generate_document(
            product="vpc",
            category=_make_category(),
            directory_summaries=[],
        )

        assert result.document == ""
        assert result.source_directories == []
        mock_llm.complete.assert_not_called()

    async def test_llm_error_propagates(self) -> None:
        """LLM 호출 실패 시 예외가 전파된다."""
        mock_llm = _make_mock_llm()
        mock_llm.complete.side_effect = RuntimeError("LLM 호출 실패")
        agent = LLMCategoryAgent(mock_llm)

        with pytest.raises(RuntimeError, match="LLM 호출 실패"):
            await agent.generate_document(
                product="vpc",
                category=_make_category(),
                directory_summaries=[_make_dir_summary()],
            )

    async def test_long_input_truncation(self) -> None:
        """입력이 max_input_chars를 초과하면 절삭된다."""
        mock_llm = _make_mock_llm()
        agent = LLMCategoryAgent(mock_llm, max_input_chars=200)

        # 긴 문서를 가진 디렉토리 요약 생성
        summaries = [
            _make_dir_summary(directory=f"dir_{i}", document="X" * 100)
            for i in range(10)
        ]

        await agent.generate_document(
            product="vpc",
            category=_make_category(),
            directory_summaries=summaries,
        )

        user_prompt = mock_llm.complete.call_args[0][0]
        # 모든 10개 디렉토리가 포함될 수 없음 (200자 제한)
        assert "생략" in user_prompt

    async def test_single_directory_no_truncation(self) -> None:
        """단일 디렉토리가 max_input_chars 이내이면 절삭하지 않는다."""
        mock_llm = _make_mock_llm()
        agent = LLMCategoryAgent(mock_llm, max_input_chars=80000)

        summaries = [_make_dir_summary(document="짧은 문서")]

        await agent.generate_document(
            product="vpc",
            category=_make_category(),
            directory_summaries=summaries,
        )

        user_prompt = mock_llm.complete.call_args[0][0]
        assert "생략" not in user_prompt
        assert "짧은 문서" in user_prompt

    async def test_enable_thinking_false(self) -> None:
        """enable_thinking=False가 extra_body로 전달된다."""
        mock_llm = _make_mock_llm()
        agent = LLMCategoryAgent(mock_llm)

        await agent.generate_document(
            product="vpc",
            category=_make_category(),
            directory_summaries=[_make_dir_summary()],
        )

        call_kwargs = mock_llm.complete.call_args.kwargs
        assert call_kwargs["extra_body"] == {
            "chat_template_kwargs": {"enable_thinking": False}
        }
