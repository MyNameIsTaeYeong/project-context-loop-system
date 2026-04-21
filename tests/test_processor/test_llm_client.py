"""EndpointLLMClient 테스트."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from context_loop.processor.llm_client import EndpointLLMClient


# --- Mock helpers ---


def _make_client() -> tuple[EndpointLLMClient, AsyncMock]:
    """EndpointLLMClient를 생성하고 내부 _client를 mock으로 교체한다."""
    client = EndpointLLMClient("http://test/v1", "model-a")
    mock_inner = AsyncMock()
    client._client = mock_inner
    return client, mock_inner


def _make_chunk(content: str | None) -> MagicMock:
    """delta.content를 가진 스트림 청크 mock을 만든다."""
    chunk = MagicMock()
    choice = MagicMock()
    choice.delta.content = content
    chunk.choices = [choice]
    return chunk


async def _async_iter(chunks: list[MagicMock]):
    """주어진 청크 리스트를 비동기 이터레이터로 변환한다."""
    for chunk in chunks:
        yield chunk


def _setup_stream(mock_inner: AsyncMock, deltas: list[str | None]) -> None:
    """스트리밍 응답을 설정한다 (delta.content 시퀀스)."""
    chunks = [_make_chunk(d) for d in deltas]
    mock_inner.chat.completions.create.return_value = _async_iter(chunks)


def _setup_response(mock_inner: AsyncMock, text: str) -> None:
    """단일 텍스트로 스트리밍 응답을 설정한다 (헬퍼)."""
    _setup_stream(mock_inner, [text])


# --- Tests ---


class TestEndpointLLMClient:
    async def test_complete_returns_response(self) -> None:
        """정상 호출 시 응답 텍스트를 반환한다."""
        client, mock = _make_client()
        _setup_response(mock, "일반 응답")

        result = await client.complete("테스트")

        assert result == "일반 응답"

    async def test_system_prompt_passed(self) -> None:
        """system 프롬프트가 올바르게 전달된다."""
        client, mock = _make_client()
        _setup_response(mock, "응답")

        await client.complete("테스트", system="시스템 프롬프트")

        call_kwargs = mock.chat.completions.create.call_args.kwargs
        messages = call_kwargs["messages"]
        assert messages[0] == {"role": "system", "content": "시스템 프롬프트"}
        assert messages[1] == {"role": "user", "content": "테스트"}

    async def test_extra_body_passed(self) -> None:
        """extra_body가 전달된다."""
        client, mock = _make_client()
        _setup_response(mock, "응답")

        await client.complete(
            "테스트",
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )

        call_kwargs = mock.chat.completions.create.call_args.kwargs
        assert call_kwargs["extra_body"] == {
            "chat_template_kwargs": {"enable_thinking": False}
        }

    def test_headers_passed_to_openai_client(self) -> None:
        """headers가 AsyncOpenAI의 default_headers로 전달된다."""
        with patch("openai.AsyncOpenAI") as mock_openai:
            EndpointLLMClient(
                "http://test/v1",
                "model-a",
                headers={"X-Org-Id": "abc", "X-Trace": "1"},
            )

        call_kwargs = mock_openai.call_args.kwargs
        assert call_kwargs["default_headers"] == {
            "X-Org-Id": "abc",
            "X-Trace": "1",
        }

    def test_headers_omitted_when_none(self) -> None:
        """headers가 None이면 default_headers를 전달하지 않는다."""
        with patch("openai.AsyncOpenAI") as mock_openai:
            EndpointLLMClient("http://test/v1", "model-a")

        call_kwargs = mock_openai.call_args.kwargs
        assert "default_headers" not in call_kwargs

    def test_headers_omitted_when_empty(self) -> None:
        """headers가 빈 dict면 default_headers를 전달하지 않는다."""
        with patch("openai.AsyncOpenAI") as mock_openai:
            EndpointLLMClient("http://test/v1", "model-a", headers={})

        call_kwargs = mock_openai.call_args.kwargs
        assert "default_headers" not in call_kwargs

    async def test_stream_flag_passed(self) -> None:
        """stream=True가 항상 요청에 포함된다."""
        client, mock = _make_client()
        _setup_stream(mock, ["x"])

        await client.complete("테스트")

        assert mock.chat.completions.create.call_args.kwargs["stream"] is True

    async def test_stream_aggregates_deltas(self) -> None:
        """여러 delta 청크가 순서대로 누적되어 반환된다."""
        client, mock = _make_client()
        _setup_stream(mock, ["안녕", "하세", "요"])

        result = await client.complete("테스트")

        assert result == "안녕하세요"

    async def test_stream_skips_none_and_empty_deltas(self) -> None:
        """content가 None이거나 빈 문자열인 청크는 무시된다."""
        client, mock = _make_client()
        _setup_stream(mock, ["a", None, "", "b"])

        result = await client.complete("테스트")

        assert result == "ab"
