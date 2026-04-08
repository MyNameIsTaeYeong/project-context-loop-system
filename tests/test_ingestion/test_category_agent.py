"""Category Agent 테스트 (Phase 9.6)."""

from __future__ import annotations

from unittest.mock import AsyncMock, call

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


# --- Tests: 단일 호출 (기본 동작) ---


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
        """단일 호출 시 max_tokens=16384, temperature=0.1이 전달된다."""
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
        """단일 호출 시 LLM 오류가 전파된다."""
        mock_llm = _make_mock_llm()
        mock_llm.complete.side_effect = RuntimeError("LLM 호출 실패")
        agent = LLMCategoryAgent(mock_llm)

        with pytest.raises(RuntimeError, match="LLM 호출 실패"):
            await agent.generate_document(
                product="vpc",
                category=_make_category(),
                directory_summaries=[_make_dir_summary()],
            )

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


# --- Tests: 배치 분할 ---


class TestBatchSplitting:
    async def test_small_input_single_batch(self) -> None:
        """총 글자수가 max_chars_per_batch 이내이면 단일 호출."""
        mock_llm = _make_mock_llm()
        agent = LLMCategoryAgent(mock_llm, max_chars_per_batch=80000)

        summaries = [_make_dir_summary(document="짧은 문서")]

        await agent.generate_document(
            product="vpc",
            category=_make_category(),
            directory_summaries=summaries,
        )

        # 단일 호출 = complete 1회
        assert mock_llm.complete.call_count == 1

    async def test_large_input_triggers_map_reduce(self) -> None:
        """총 글자수가 max_chars_per_batch 초과이면 map-reduce로 전환."""
        mock_llm = _make_mock_llm("부분 문서")
        agent = LLMCategoryAgent(mock_llm, max_chars_per_batch=200)

        # 각 100자 × 5 = 500자 → 200자 배치로 분할 → 3배치
        summaries = [
            _make_dir_summary(directory=f"dir_{i}", document="X" * 100)
            for i in range(5)
        ]

        await agent.generate_document(
            product="vpc",
            category=_make_category(),
            directory_summaries=summaries,
        )

        # map 3회 + reduce 1회 = 4회
        assert mock_llm.complete.call_count == 4

    async def test_exact_boundary_no_split(self) -> None:
        """총 글자수가 정확히 max_chars_per_batch이면 분할하지 않는다."""
        mock_llm = _make_mock_llm()
        agent = LLMCategoryAgent(mock_llm, max_chars_per_batch=100)

        # 정확히 100자
        summaries = [_make_dir_summary(document="A" * 100)]

        await agent.generate_document(
            product="vpc",
            category=_make_category(),
            directory_summaries=summaries,
        )

        assert mock_llm.complete.call_count == 1

    async def test_dynamic_batch_count(self) -> None:
        """디렉토리 크기에 따라 배치 수가 동적으로 결정된다."""
        agent = LLMCategoryAgent(_make_mock_llm(), max_chars_per_batch=300)

        # 짧은 문서 5개 → 1배치
        short_summaries = [
            _make_dir_summary(directory=f"s_{i}", document="X" * 50)
            for i in range(5)
        ]
        assert len(agent._split_into_batches(short_summaries)) == 1

        # 긴 문서 5개 → 5배치
        long_summaries = [
            _make_dir_summary(directory=f"l_{i}", document="X" * 400)
            for i in range(5)
        ]
        assert len(agent._split_into_batches(long_summaries)) == 5

        # 혼합: 짧은 3 + 긴 2 → 배치 수가 다름
        mixed = [
            _make_dir_summary(directory="a", document="X" * 100),
            _make_dir_summary(directory="b", document="X" * 100),
            _make_dir_summary(directory="c", document="X" * 100),
            _make_dir_summary(directory="d", document="X" * 400),
            _make_dir_summary(directory="e", document="X" * 400),
        ]
        batches = agent._split_into_batches(mixed)
        assert len(batches) == 3  # [a,b,c], [d], [e]

    async def test_single_oversized_directory(self) -> None:
        """개별 디렉토리가 max_chars_per_batch보다 커도 별도 배치에 포함된다."""
        agent = LLMCategoryAgent(_make_mock_llm(), max_chars_per_batch=100)

        summaries = [_make_dir_summary(document="X" * 500)]

        batches = agent._split_into_batches(summaries)
        assert len(batches) == 1
        assert len(batches[0]) == 1


# --- Tests: Map-Reduce ---


class TestMapReduce:
    async def test_map_calls_are_parallel(self) -> None:
        """Map 배치들이 asyncio.gather로 병렬 호출된다."""
        call_order: list[str] = []

        async def mock_complete(prompt, **kwargs):
            if "배치" in prompt:
                call_order.append("map")
                return "부분 문서"
            call_order.append("reduce")
            return "최종 문서"

        mock_llm = AsyncMock()
        mock_llm.complete.side_effect = mock_complete
        agent = LLMCategoryAgent(mock_llm, max_chars_per_batch=100)

        summaries = [
            _make_dir_summary(directory=f"dir_{i}", document="X" * 100)
            for i in range(3)
        ]

        result = await agent.generate_document(
            product="vpc",
            category=_make_category(),
            directory_summaries=summaries,
        )

        # map이 먼저, reduce가 마지막
        assert call_order[-1] == "reduce"
        assert call_order.count("map") == 3
        assert result.document == "최종 문서"

    async def test_map_batch_failure_is_tolerated(self) -> None:
        """Map 배치 일부가 실패해도 나머지로 Reduce를 수행한다."""
        call_count = 0

        async def mock_complete(prompt, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("첫 번째 배치 실패")
            if "부분 문서" in prompt:
                return "최종 문서"
            return "부분 문서"

        mock_llm = AsyncMock()
        mock_llm.complete.side_effect = mock_complete
        agent = LLMCategoryAgent(mock_llm, max_chars_per_batch=100)

        summaries = [
            _make_dir_summary(directory=f"dir_{i}", document="X" * 100)
            for i in range(3)
        ]

        result = await agent.generate_document(
            product="vpc",
            category=_make_category(),
            directory_summaries=summaries,
        )

        # 실패한 배치를 제외하고 Reduce 수행됨
        assert result.document == "최종 문서"

    async def test_all_map_batches_fail(self) -> None:
        """모든 Map 배치가 실패하면 오류 메시지를 반환한다."""
        mock_llm = AsyncMock()
        mock_llm.complete.side_effect = RuntimeError("실패")
        agent = LLMCategoryAgent(mock_llm, max_chars_per_batch=100)

        summaries = [
            _make_dir_summary(directory=f"dir_{i}", document="X" * 100)
            for i in range(3)
        ]

        result = await agent.generate_document(
            product="vpc",
            category=_make_category(),
            directory_summaries=summaries,
        )

        assert "실패" in result.document

    async def test_map_max_tokens_is_8192(self) -> None:
        """Map 호출은 max_tokens=8192를 사용한다."""
        mock_llm = _make_mock_llm("부분 문서")
        agent = LLMCategoryAgent(mock_llm, max_chars_per_batch=100)

        summaries = [
            _make_dir_summary(directory=f"dir_{i}", document="X" * 100)
            for i in range(2)
        ]

        await agent.generate_document(
            product="vpc",
            category=_make_category(),
            directory_summaries=summaries,
        )

        # 첫 번째 호출 (Map)의 max_tokens 확인
        first_call = mock_llm.complete.call_args_list[0]
        assert first_call.kwargs["max_tokens"] == 8192

    async def test_reduce_max_tokens_is_16384(self) -> None:
        """Reduce 호출은 max_tokens=16384를 사용한다."""
        mock_llm = _make_mock_llm("부분 문서")
        agent = LLMCategoryAgent(mock_llm, max_chars_per_batch=100)

        summaries = [
            _make_dir_summary(directory=f"dir_{i}", document="X" * 100)
            for i in range(2)
        ]

        await agent.generate_document(
            product="vpc",
            category=_make_category(),
            directory_summaries=summaries,
        )

        # 마지막 호출 (Reduce)의 max_tokens 확인
        last_call = mock_llm.complete.call_args_list[-1]
        assert last_call.kwargs["max_tokens"] == 16384

    async def test_reduce_prompt_contains_partial_docs(self) -> None:
        """Reduce 프롬프트에 모든 부분 문서가 포함된다."""
        responses = iter(["부분A", "부분B", "최종 결과"])

        async def mock_complete(prompt, **kwargs):
            return next(responses)

        mock_llm = AsyncMock()
        mock_llm.complete.side_effect = mock_complete
        agent = LLMCategoryAgent(mock_llm, max_chars_per_batch=100)

        summaries = [
            _make_dir_summary(directory=f"dir_{i}", document="X" * 100)
            for i in range(2)
        ]

        await agent.generate_document(
            product="vpc",
            category=_make_category(),
            directory_summaries=summaries,
        )

        # Reduce 호출의 프롬프트에 부분 문서가 포함
        reduce_prompt = mock_llm.complete.call_args_list[-1][0][0]
        assert "부분A" in reduce_prompt
        assert "부분B" in reduce_prompt

    async def test_single_surviving_batch_skips_reduce(self) -> None:
        """Map 결과가 1개뿐이면 Reduce를 건너뛴다."""
        call_count = 0

        async def mock_complete(prompt, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("실패")
            return "유일한 부분 문서"

        mock_llm = AsyncMock()
        mock_llm.complete.side_effect = mock_complete
        agent = LLMCategoryAgent(mock_llm, max_chars_per_batch=100)

        summaries = [
            _make_dir_summary(directory=f"dir_{i}", document="X" * 100)
            for i in range(2)
        ]

        result = await agent.generate_document(
            product="vpc",
            category=_make_category(),
            directory_summaries=summaries,
        )

        # Map 2회 (1 실패 + 1 성공), Reduce 없음 → 총 2회
        assert mock_llm.complete.call_count == 2
        assert result.document == "유일한 부분 문서"

    async def test_source_directories_includes_all_even_with_map_reduce(self) -> None:
        """map-reduce에서도 source_directories에 모든 디렉토리가 포함된다."""
        mock_llm = _make_mock_llm("문서")
        agent = LLMCategoryAgent(mock_llm, max_chars_per_batch=100)

        summaries = [
            _make_dir_summary(directory=f"dir_{i}", document="X" * 100)
            for i in range(5)
        ]

        result = await agent.generate_document(
            product="vpc",
            category=_make_category(),
            directory_summaries=summaries,
        )

        assert result.source_directories == [f"dir_{i}" for i in range(5)]
