"""EndpointLLMClient 테스트."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from context_loop.processor.llm_client import EndpointLLMClient


# --- Mock helpers ---


def _make_client() -> tuple[EndpointLLMClient, AsyncMock]:
    """EndpointLLMClient를 생성하고 내부 _client를 mock으로 교체한다."""
    client = EndpointLLMClient("http://test/v1", "model-a")
    mock_inner = AsyncMock()
    client._client = mock_inner
    return client, mock_inner


def _setup_response(mock_inner: AsyncMock, text: str) -> None:
    """스트리밍 응답을 설정한다."""

    async def _stream_chunks():
        """텍스트를 스트리밍 청크로 반환하는 async iterator."""
        chunk = MagicMock()
        chunk.choices = [MagicMock()]
        chunk.choices[0].delta.content = text
        yield chunk

    mock_inner.chat.completions.create.return_value = _stream_chunks()


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
