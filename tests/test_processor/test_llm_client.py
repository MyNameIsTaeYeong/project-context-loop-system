"""EndpointLLMClient 스트리밍 테스트."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock

import pytest

from context_loop.processor.llm_client import EndpointLLMClient


# --- Mock helpers ---


@dataclass
class _MockDelta:
    content: str | None = None


@dataclass
class _MockChoice:
    delta: _MockDelta


@dataclass
class _MockChunk:
    choices: list[_MockChoice]


async def _mock_stream_iter(chunks: list[str]):
    """스트리밍 응답을 시뮬레이션하는 async iterator."""
    for text in chunks:
        yield _MockChunk(choices=[_MockChoice(delta=_MockDelta(content=text))])


class _MockStreamResponse:
    """async for로 동작하는 스트림 mock."""

    def __init__(self, chunks: list[str]) -> None:
        self._chunks = chunks

    def __aiter__(self):
        return _mock_stream_iter(self._chunks).__aiter__()


def _make_client(stream: bool = False) -> tuple[EndpointLLMClient, AsyncMock]:
    """EndpointLLMClient를 생성하고 내부 _client를 mock으로 교체한다."""
    client = EndpointLLMClient("http://test/v1", "model-a", stream=stream)
    mock_inner = AsyncMock()
    client._client = mock_inner
    return client, mock_inner


def _setup_non_stream(mock_inner: AsyncMock, text: str) -> None:
    """일반 응답을 설정한다."""
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = text
    mock_inner.chat.completions.create.return_value = mock_response


def _setup_stream(mock_inner: AsyncMock, chunks: list[str]) -> None:
    """스트리밍 응답을 설정한다."""
    mock_inner.chat.completions.create.return_value = _MockStreamResponse(chunks)


# --- Tests ---


class TestEndpointLLMClientStream:
    async def test_non_stream_mode(self) -> None:
        """stream=False일 때 일반 응답을 사용한다."""
        client, mock = _make_client(stream=False)
        _setup_non_stream(mock, "일반 응답")

        result = await client.complete("테스트")

        assert result == "일반 응답"
        call_kwargs = mock.chat.completions.create.call_args
        assert "stream" not in call_kwargs.kwargs

    async def test_stream_mode_assembles_chunks(self) -> None:
        """stream=True일 때 청크를 조립하여 전체 문자열을 반환한다."""
        client, mock = _make_client(stream=True)
        _setup_stream(mock, ["Hello", " ", "World"])

        result = await client.complete("테스트")

        assert result == "Hello World"

    async def test_stream_mode_passes_stream_flag(self) -> None:
        """stream=True일 때 API 호출에 stream=True가 전달된다."""
        client, mock = _make_client(stream=True)
        _setup_stream(mock, ["ok"])

        await client.complete("테스트")

        call_kwargs = mock.chat.completions.create.call_args
        assert call_kwargs.kwargs.get("stream") is True

    async def test_stream_mode_empty_response(self) -> None:
        """스트리밍에서 빈 응답이면 빈 문자열을 반환한다."""
        client, mock = _make_client(stream=True)
        _setup_stream(mock, [])

        result = await client.complete("테스트")

        assert result == ""

    async def test_stream_mode_none_content_skipped(self) -> None:
        """delta.content가 None인 청크는 무시한다."""

        async def _mixed_stream():
            yield _MockChunk(choices=[_MockChoice(delta=_MockDelta(content=None))])
            yield _MockChunk(choices=[_MockChoice(delta=_MockDelta(content="hello"))])
            yield _MockChunk(choices=[_MockChoice(delta=_MockDelta(content=None))])
            yield _MockChunk(choices=[_MockChoice(delta=_MockDelta(content=" world"))])

        class _MixedCtx:
            def __aiter__(self):
                return _mixed_stream()

        client, mock = _make_client(stream=True)
        mock.chat.completions.create.return_value = _MixedCtx()

        result = await client.complete("테스트")

        assert result == "hello world"

    async def test_stream_mode_with_system_prompt(self) -> None:
        """스트리밍에서도 system 프롬프트가 올바르게 전달된다."""
        client, mock = _make_client(stream=True)
        _setup_stream(mock, ["응답"])

        await client.complete("테스트", system="시스템 프롬프트")

        call_kwargs = mock.chat.completions.create.call_args.kwargs
        messages = call_kwargs["messages"]
        assert messages[0] == {"role": "system", "content": "시스템 프롬프트"}
        assert messages[1] == {"role": "user", "content": "테스트"}

    async def test_stream_mode_with_extra_body(self) -> None:
        """스트리밍에서도 extra_body가 전달된다."""
        client, mock = _make_client(stream=True)
        _setup_stream(mock, ["응답"])

        await client.complete(
            "테스트",
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

        call_kwargs = mock.chat.completions.create.call_args.kwargs
        assert call_kwargs["extra_body"] == {
            "chat_template_kwargs": {"enable_thinking": False}
        }

    async def test_default_is_non_stream(self) -> None:
        """기본값은 stream=False이다."""
        client, mock = _make_client()  # stream 미지정
        _setup_non_stream(mock, "일반")

        result = await client.complete("테스트")

        assert result == "일반"
