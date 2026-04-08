"""Worker Agent (LLMWorkerAgent) 테스트 — Phase 9.5."""

from __future__ import annotations

from pathlib import Path

import pytest

from context_loop.ingestion.coordinator import DirectorySummary, FileSummary
from context_loop.ingestion.git_repository import FileInfo
from context_loop.ingestion.worker_agent import (
    LLMWorkerAgent,
    _DIR_SYNTHESIS_SYSTEM,
    _DIR_SYNTHESIS_TEMPLATE,
    _FILE_SUMMARY_SYSTEM,
    _FILE_SUMMARY_TEMPLATE,
)
from context_loop.processor.llm_client import LLMClient


# ---------------------------------------------------------------------------
# Mock LLM Clients (기존 프로젝트 패턴)
# ---------------------------------------------------------------------------


class MockLLMClient(LLMClient):
    """단일 고정 응답을 반환하는 Mock."""

    def __init__(self, response: str = "mock response") -> None:
        self._response = response
        self.calls: list[dict] = []

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        **kwargs,
    ) -> str:
        self.calls.append({
            "prompt": prompt,
            "system": system,
            "max_tokens": max_tokens,
            "temperature": temperature,
        })
        return self._response


class SequentialMockLLMClient(LLMClient):
    """호출 순서대로 다른 응답을 반환하는 Mock."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = responses
        self._index = 0
        self.calls: list[dict] = []

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        **kwargs,
    ) -> str:
        self.calls.append({
            "prompt": prompt,
            "system": system,
            "max_tokens": max_tokens,
            "temperature": temperature,
        })
        if self._index < len(self._responses):
            resp = self._responses[self._index]
            self._index += 1
            return resp
        return "fallback response"


class FailingLLMClient(LLMClient):
    """항상 예외를 발생시키는 Mock."""

    async def complete(self, prompt: str, **kwargs) -> str:
        raise RuntimeError("LLM 서버 연결 실패")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_file(
    path: str = "services/vpc/main.go",
    product: str = "vpc",
    content: str = "package main\n\nfunc main() {}",
) -> FileInfo:
    return FileInfo(
        relative_path=path,
        absolute_path=Path(f"/tmp/repo/{path}"),
        product=product,
        content=content,
        content_hash="abc123",
        size_bytes=len(content),
    )


# ---------------------------------------------------------------------------
# Tests: Level 1 — 파일 요약
# ---------------------------------------------------------------------------


class TestFileSummary:
    """Level 1 파일 요약 관련 테스트."""

    async def test_single_file_summary(self) -> None:
        """파일 1개 → FileSummary 1개 생성."""
        worker_llm = MockLLMClient("파일 요약 결과")
        synth_llm = MockLLMClient("디렉토리 문서")
        agent = LLMWorkerAgent(worker_llm, synth_llm)

        files = [_make_file()]
        result = await agent.process_directory("services/vpc", "vpc", files)

        assert len(result.file_summaries) == 1
        assert result.file_summaries[0].relative_path == "services/vpc/main.go"
        assert result.file_summaries[0].summary == "파일 요약 결과"

    async def test_multiple_files_call_count(self) -> None:
        """파일 N개 → Level 1 호출 N회, Level 2 호출 1회."""
        worker_llm = MockLLMClient("요약")
        synth_llm = MockLLMClient("종합 문서")
        agent = LLMWorkerAgent(worker_llm, synth_llm)

        files = [
            _make_file(f"services/vpc/file{i}.go") for i in range(5)
        ]
        result = await agent.process_directory("services/vpc", "vpc", files)

        assert len(worker_llm.calls) == 5  # Level 1: 파일 수만큼
        assert len(synth_llm.calls) == 1   # Level 2: 1회
        assert len(result.file_summaries) == 5

    async def test_file_summary_prompt_contains_content(self) -> None:
        """Level 1 프롬프트에 파일 내용이 포함되는지 확인."""
        worker_llm = MockLLMClient("요약")
        synth_llm = MockLLMClient("문서")
        agent = LLMWorkerAgent(worker_llm, synth_llm)

        content = "func CreateVPC() { return vpc.New() }"
        files = [_make_file(content=content)]
        await agent.process_directory("services/vpc", "vpc", files)

        prompt = worker_llm.calls[0]["prompt"]
        assert "CreateVPC" in prompt
        assert "services/vpc/main.go" in prompt
        assert worker_llm.calls[0]["system"] == _FILE_SUMMARY_SYSTEM

    async def test_file_summary_uses_worker_endpoint(self) -> None:
        """Level 1은 worker LLM을, Level 2는 synthesizer LLM을 사용."""
        worker_llm = MockLLMClient("worker 응답")
        synth_llm = MockLLMClient("synthesizer 응답")
        agent = LLMWorkerAgent(worker_llm, synth_llm)

        files = [_make_file()]
        result = await agent.process_directory("services/vpc", "vpc", files)

        # worker_llm이 Level 1 호출
        assert len(worker_llm.calls) == 1
        assert worker_llm.calls[0]["max_tokens"] == 4096

        # synth_llm이 Level 2 호출
        assert len(synth_llm.calls) == 1
        assert synth_llm.calls[0]["max_tokens"] == 8192

        assert result.file_summaries[0].summary == "worker 응답"
        assert result.document == "synthesizer 응답"

    async def test_long_file_truncated(self) -> None:
        """max_file_tokens 초과 시 내용이 절삭된다."""
        worker_llm = MockLLMClient("요약")
        synth_llm = MockLLMClient("문서")
        agent = LLMWorkerAgent(worker_llm, synth_llm, max_file_tokens=100)

        long_content = "x" * 500
        files = [_make_file(content=long_content)]
        await agent.process_directory("services/vpc", "vpc", files)

        prompt = worker_llm.calls[0]["prompt"]
        assert "이하 생략" in prompt
        # 원본 500자가 아닌 절삭된 내용
        assert "x" * 500 not in prompt


# ---------------------------------------------------------------------------
# Tests: Level 1 — 에러 처리
# ---------------------------------------------------------------------------


class TestFileSummaryErrors:
    """Level 1 파일 요약 에러 처리 테스트."""

    async def test_single_file_failure_does_not_block_others(self) -> None:
        """1개 파일 실패 시 나머지는 정상 처리."""
        responses = ["성공 요약 1", RuntimeError("실패!"), "성공 요약 3"]

        class PartialFailLLM(LLMClient):
            def __init__(self) -> None:
                self._index = 0

            async def complete(self, prompt: str, **kwargs) -> str:
                idx = self._index
                self._index += 1
                r = responses[idx]
                if isinstance(r, Exception):
                    raise r
                return r

        worker_llm = PartialFailLLM()
        synth_llm = MockLLMClient("종합 문서")
        agent = LLMWorkerAgent(worker_llm, synth_llm, max_concurrent_files=1)

        files = [_make_file(f"file{i}.go") for i in range(3)]
        result = await agent.process_directory("dir", "vpc", files)

        assert len(result.file_summaries) == 3
        assert result.file_summaries[0].summary == "성공 요약 1"
        assert result.file_summaries[1].summary.startswith("[요약 실패")
        assert result.file_summaries[2].summary == "성공 요약 3"

    async def test_all_files_fail_produces_error_document(self) -> None:
        """모든 파일 요약 실패 시 디렉토리 문서에 에러 메시지."""
        agent = LLMWorkerAgent(
            FailingLLMClient(), MockLLMClient("이건 호출 안 됨")
        )

        files = [_make_file("a.go"), _make_file("b.go")]
        result = await agent.process_directory("dir", "vpc", files)

        assert all(
            fs.summary.startswith("[요약 실패") for fs in result.file_summaries
        )
        assert "실패" in result.document
        # synthesizer가 호출되지 않아야 함
        assert result.document.startswith("[vpc]")


# ---------------------------------------------------------------------------
# Tests: Level 2 — 디렉토리 종합 문서
# ---------------------------------------------------------------------------


class TestDirectorySynthesis:
    """Level 2 디렉토리 종합 문서 테스트."""

    async def test_directory_document_contains_summaries(self) -> None:
        """Level 2 프롬프트에 모든 Level 1 요약이 포함된다."""
        worker_llm = SequentialMockLLMClient(["요약A", "요약B"])
        synth_llm = MockLLMClient("종합 결과")
        agent = LLMWorkerAgent(worker_llm, synth_llm)

        files = [_make_file("a.go"), _make_file("b.go")]
        result = await agent.process_directory("dir", "vpc", files)

        synth_prompt = synth_llm.calls[0]["prompt"]
        assert "요약A" in synth_prompt
        assert "요약B" in synth_prompt
        assert synth_llm.calls[0]["system"] == _DIR_SYNTHESIS_SYSTEM
        assert result.document == "종합 결과"

    async def test_directory_document_excludes_failed_summaries(self) -> None:
        """실패한 요약은 Level 2 프롬프트에서 제외된다."""

        class OneFailLLM(LLMClient):
            def __init__(self) -> None:
                self._index = 0

            async def complete(self, prompt: str, **kwargs) -> str:
                idx = self._index
                self._index += 1
                if idx == 1:
                    raise RuntimeError("fail")
                return f"요약{idx}"

        worker_llm = OneFailLLM()
        synth_llm = MockLLMClient("종합")
        agent = LLMWorkerAgent(worker_llm, synth_llm, max_concurrent_files=1)

        files = [_make_file("a.go"), _make_file("b.go"), _make_file("c.go")]
        await agent.process_directory("dir", "vpc", files)

        synth_prompt = synth_llm.calls[0]["prompt"]
        assert "요약0" in synth_prompt
        assert "요약2" in synth_prompt
        assert "요약 실패" not in synth_prompt


# ---------------------------------------------------------------------------
# Tests: 전체 흐름
# ---------------------------------------------------------------------------


class TestProcessDirectory:
    """process_directory 전체 흐름 테스트."""

    async def test_empty_files_returns_empty(self) -> None:
        """빈 파일 리스트 → 빈 결과."""
        agent = LLMWorkerAgent(MockLLMClient(), MockLLMClient())
        result = await agent.process_directory("dir", "vpc", [])

        assert result.directory == "dir"
        assert result.product == "vpc"
        assert result.file_summaries == []
        assert result.document == ""

    async def test_return_type_correctness(self) -> None:
        """반환 타입이 DirectorySummary이고 모든 필드가 올바른 타입."""
        agent = LLMWorkerAgent(MockLLMClient("요약"), MockLLMClient("문서"))
        files = [_make_file()]
        result = await agent.process_directory("services/vpc", "vpc", files)

        assert isinstance(result, DirectorySummary)
        assert isinstance(result.directory, str)
        assert isinstance(result.product, str)
        assert isinstance(result.file_summaries, list)
        assert all(isinstance(fs, FileSummary) for fs in result.file_summaries)
        assert isinstance(result.document, str)
        assert result.directory == "services/vpc"
        assert result.product == "vpc"

    async def test_concurrent_file_limit(self) -> None:
        """max_concurrent_files가 동시성을 제한하는지 확인."""
        import asyncio

        max_concurrent = 0
        current_concurrent = 0

        class TrackingLLM(LLMClient):
            async def complete(self, prompt: str, **kwargs) -> str:
                nonlocal max_concurrent, current_concurrent
                current_concurrent += 1
                if current_concurrent > max_concurrent:
                    max_concurrent = current_concurrent
                await asyncio.sleep(0.01)
                current_concurrent -= 1
                return "요약"

        agent = LLMWorkerAgent(
            TrackingLLM(), MockLLMClient("문서"), max_concurrent_files=2
        )
        files = [_make_file(f"f{i}.go") for i in range(6)]
        await agent.process_directory("dir", "vpc", files)

        assert max_concurrent <= 2

    async def test_full_pipeline_with_sequential_responses(self) -> None:
        """전체 흐름: 파일 3개 → Level 1 × 3 → Level 2 × 1."""
        worker_llm = SequentialMockLLMClient([
            "main.go: VPC 생성 로직",
            "subnet.go: 서브넷 관리",
            "nat.go: NAT 게이트웨이",
        ])
        synth_llm = MockLLMClient(
            "VPC 디렉토리는 VPC 생성, 서브넷, NAT 관리를 담당합니다."
        )
        agent = LLMWorkerAgent(worker_llm, synth_llm)

        files = [
            _make_file("services/vpc/main.go", content="func CreateVPC(){}"),
            _make_file("services/vpc/subnet.go", content="func CreateSubnet(){}"),
            _make_file("services/vpc/nat.go", content="func CreateNAT(){}"),
        ]
        result = await agent.process_directory("services/vpc", "vpc", files)

        # Level 1 결과
        assert len(result.file_summaries) == 3
        assert "VPC 생성" in result.file_summaries[0].summary

        # Level 2 결과
        assert "VPC 디렉토리" in result.document
        assert result.directory == "services/vpc"
        assert result.product == "vpc"
