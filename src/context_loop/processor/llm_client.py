"""LLM 클라이언트 추상화 레이어.

OpenAI, Anthropic API 및 OpenAI 호환 자체 엔드포인트를 동일한 인터페이스로 래핑한다.
config.yaml의 llm.provider 설정에 따라 적절한 구현체를 반환한다.
- "openai": OpenAI API (api_key 방식)
- "anthropic": Anthropic Claude API (api_key 방식)
- "endpoint": OpenAI 호환 자체 모델 서버 (endpoint URL 방식)
"""

from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

logger = logging.getLogger(__name__)


class LLMClient(ABC):
    """LLM 클라이언트 추상 기본 클래스."""

    @abstractmethod
    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        reasoning_mode: str | None = None,
        purpose: str | None = None,
        **kwargs: Any,
    ) -> str:
        """텍스트 완성 요청을 보내고 응답 문자열을 반환한다.

        Args:
            prompt: 사용자 프롬프트.
            system: 시스템 프롬프트. None이면 사용하지 않는다.
            max_tokens: 최대 출력 토큰 수.
            temperature: 샘플링 온도 (0.0 = 결정적).
            reasoning_mode: reasoning 프로파일 이름 (예: "off"). 모델별 실제
                페이로드(extra_body 등)는 클라이언트가 설정에서 매핑한다.
                지원하지 않는 클라이언트는 무시한다.
            purpose: 호출 목적 식별자 (예: "answer_generation"). 타이밍 로그에
                포함되어 어느 단계가 오래 걸리는지 식별하는 데 사용된다.
            **kwargs: 구현체별 추가 파라미터 (예: extra_body).

        Returns:
            LLM 응답 문자열.
        """

    async def stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        reasoning_mode: str | None = None,
        purpose: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """텍스트 완성 응답을 토큰/청크 단위로 스트리밍한다.

        기본 구현은 ``complete()`` 호출 후 결과를 한 번에 yield 한다.
        토큰 단위 스트리밍을 지원하는 클라이언트는 이 메서드를 오버라이드한다.

        Args:
            prompt, system, max_tokens, temperature, reasoning_mode, purpose,
                kwargs: ``complete()`` 와 동일.

        Yields:
            응답 문자열 청크.
        """
        result = await self.complete(
            prompt,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_mode=reasoning_mode,
            purpose=purpose,
            **kwargs,
        )
        if result:
            yield result

    async def stream_events(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        reasoning_mode: str | None = None,
        purpose: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, str]]:
        """추론(thinking)과 본문 응답을 분리한 이벤트 스트림.

        각 이벤트는 ``{"kind": "reasoning" | "content", "content": "..."}``
        형태의 dict 이다. 기본 구현은 ``stream()`` 결과를 모두 ``"content"`` 로
        분류한다. 추론 토큰을 별도 노출하는 클라이언트는 이 메서드를
        오버라이드한다.
        """
        async for chunk in self.stream(
            prompt,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_mode=reasoning_mode,
            purpose=purpose,
            **kwargs,
        ):
            yield {"kind": "content", "content": chunk}


class AnthropicClient(LLMClient):
    """Anthropic Claude API 클라이언트.

    Args:
        api_key: Anthropic API 키.
        model: 사용할 모델 ID.
    """

    def __init__(self, api_key: str, model: str = "claude-haiku-4-5-20251001") -> None:
        import anthropic  # noqa: PLC0415
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        reasoning_mode: str | None = None,
        purpose: str | None = None,
        **kwargs: Any,
    ) -> str:
        del reasoning_mode  # Anthropic 클라이언트는 미지원, 무시
        api_kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            api_kwargs["system"] = system
        # anthropic SDK는 temperature를 float으로 지원
        if temperature != 0.0:
            api_kwargs["temperature"] = temperature

        prompt_chars = len(prompt) + (len(system) if system else 0)
        start = time.perf_counter()
        try:
            response = await self._client.messages.create(**api_kwargs)
            text = response.content[0].text  # type: ignore[index,union-attr]
            return text
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "LLM call | purpose=%s | provider=anthropic | model=%s | "
                "elapsed_ms=%.1f | prompt_chars=%d",
                purpose or "unspecified", self._model, elapsed_ms, prompt_chars,
            )


class OpenAIClient(LLMClient):
    """OpenAI Chat Completions API 클라이언트.

    Args:
        api_key: OpenAI API 키.
        model: 사용할 모델 ID.
    """

    def __init__(self, api_key: str, model: str = "gpt-4o-mini") -> None:
        from openai import AsyncOpenAI  # noqa: PLC0415
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        reasoning_mode: str | None = None,
        purpose: str | None = None,
        **kwargs: Any,
    ) -> str:
        del reasoning_mode  # OpenAI 클라이언트는 미지원, 무시
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        prompt_chars = len(prompt) + (len(system) if system else 0)
        start = time.perf_counter()
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,  # type: ignore[arg-type]
                max_tokens=max_tokens,
                temperature=temperature,
            )
            return response.choices[0].message.content or ""
        finally:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "LLM call | purpose=%s | provider=openai | model=%s | "
                "elapsed_ms=%.1f | prompt_chars=%d",
                purpose or "unspecified", self._model, elapsed_ms, prompt_chars,
            )


class EndpointLLMClient(LLMClient):
    """OpenAI 호환 자체 모델 서버(엔드포인트) 기반 LLM 클라이언트.

    자체 호스팅된 LLM 서버(vLLM, Ollama 등 OpenAI 호환 API)와 통신한다.
    API 키가 필요 없는 경우 api_key를 빈 문자열로 설정한다.

    ``reasoning_profiles`` 는 호출부의 의도(예: ``"off"``)를 모델별 페이로드로
    매핑하는 설정이다. 호출부가 ``reasoning_mode="off"`` 만 넘기면 클라이언트가
    설정에서 매칭되는 ``extra_body`` 를 자동 주입한다. 모델 교체 시 호출부
    수정 없이 설정만 갈아끼우면 된다.

    프로파일 형식::

        {
            "off": {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}},
            "low": {"extra_body": {"chat_template_kwargs": {"reasoning_effort": "low"}}},
        }

    Args:
        endpoint: 모델 서버 엔드포인트 URL (예: "http://localhost:8080/v1").
        model: 사용할 모델 ID.
        api_key: 엔드포인트 인증 키. 불필요한 경우 빈 문자열.
        timeout: HTTP 요청 타임아웃(초). 대형 입력 처리 시 충분히 높게 설정.
        headers: 모든 요청에 추가할 커스텀 헤더. None 또는 빈 dict이면 미사용.
        reasoning_profiles: ``reasoning_mode`` 이름 → 페이로드 매핑.
            None이면 ``reasoning_mode`` 인자는 무시된다.
    """

    def __init__(
        self,
        endpoint: str,
        model: str,
        api_key: str = "none",
        timeout: float = 600.0,
        headers: dict[str, str] | None = None,
        reasoning_profiles: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        import httpx  # noqa: PLC0415
        from openai import AsyncOpenAI  # noqa: PLC0415

        client_kwargs: dict[str, Any] = {
            "api_key": api_key or "none",
            "base_url": endpoint,
            "timeout": httpx.Timeout(timeout, connect=10.0),
        }
        if headers:
            client_kwargs["default_headers"] = dict(headers)
        self._client = AsyncOpenAI(**client_kwargs)
        self._model = model
        self._reasoning_profiles = reasoning_profiles or {}

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        reasoning_mode: str | None = None,
        purpose: str | None = None,
        **kwargs: Any,
    ) -> str:
        # stream() 가 자체적으로 타이밍을 로깅하므로 여기서는 별도 로그 없음.
        parts: list[str] = []
        async for chunk in self.stream(
            prompt,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_mode=reasoning_mode,
            purpose=purpose,
            **kwargs,
        ):
            parts.append(chunk)
        return "".join(parts)

    async def stream(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        reasoning_mode: str | None = None,
        purpose: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        """OpenAI 호환 서버에서 본문 토큰 청크를 그대로 스트리밍한다.

        ``complete()`` 는 이 메서드의 결과를 누적해 단일 문자열로 반환한다.
        추론 토큰은 본문이 아니므로 이 스트림에서 제외된다. 추론까지 받고
        싶으면 ``stream_events()`` 를 사용한다.
        """
        async for event in self.stream_events(
            prompt,
            system=system,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_mode=reasoning_mode,
            purpose=purpose,
            **kwargs,
        ):
            if event["kind"] == "content":
                yield event["content"]

    async def stream_events(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        reasoning_mode: str | None = None,
        purpose: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, str]]:
        """추론(thinking)과 본문을 분리한 이벤트 스트림을 반환한다.

        vLLM 등 reasoning 모델은 OpenAI 호환 응답에 ``delta.reasoning_content``
        필드로 추론 토큰을 별도 전송한다. 본 메서드는 이를 ``"reasoning"``
        이벤트로, 일반 ``delta.content`` 는 ``"content"`` 이벤트로 분리해 yield
        한다.
        """
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        api_kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": True,
        }
        extra_body = self._resolve_extra_body(reasoning_mode, kwargs.get("extra_body"))
        if extra_body is not None:
            api_kwargs["extra_body"] = extra_body

        prompt_chars = len(prompt) + (len(system) if system else 0)
        start = time.perf_counter()
        ttft_ms: float | None = None
        chunk_count = 0
        output_chars = 0
        reasoning_chars = 0
        try:
            stream = await self._client.chat.completions.create(**api_kwargs)  # type: ignore[arg-type]
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                reasoning = getattr(delta, "reasoning", None)
                if isinstance(reasoning, str) and reasoning:
                    if ttft_ms is None:
                        ttft_ms = (time.perf_counter() - start) * 1000
                    reasoning_chars += len(reasoning)
                    yield {"kind": "reasoning", "content": reasoning}
                content = getattr(delta, "content", None)
                if isinstance(content, str) and content:
                    if ttft_ms is None:
                        ttft_ms = (time.perf_counter() - start) * 1000
                    chunk_count += 1
                    output_chars += len(content)
                    yield {"kind": "content", "content": content}
        finally:
            total_ms = (time.perf_counter() - start) * 1000
            logger.info(
                "LLM call | purpose=%s | provider=endpoint | model=%s | "
                "ttft_ms=%.1f | total_ms=%.1f | chunks=%d | "
                "prompt_chars=%d | output_chars=%d | reasoning_chars=%d",
                purpose or "unspecified", self._model,
                ttft_ms if ttft_ms is not None else -1.0,
                total_ms, chunk_count, prompt_chars, output_chars, reasoning_chars,
            )

    def _resolve_extra_body(
        self,
        reasoning_mode: str | None,
        explicit_extra_body: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """호출부의 ``extra_body`` 또는 reasoning 프로파일에서 페이로드를 결정한다.

        명시적으로 전달된 ``extra_body`` 가 우선한다. 그 다음 ``reasoning_mode``
        에 매칭되는 프로파일을 조회한다. 매칭 프로파일이 없으면 None.
        """
        if explicit_extra_body is not None:
            return explicit_extra_body
        if reasoning_mode is None:
            return None
        profile = self._reasoning_profiles.get(reasoning_mode)
        if not profile:
            return None
        body = profile.get("extra_body")
        return body if isinstance(body, dict) else None


def extract_json(text: str) -> Any:
    """LLM 응답에서 JSON을 추출한다.

    마크다운 코드 블록 또는 순수 JSON 형태를 모두 처리한다.

    Args:
        text: LLM 응답 텍스트.

    Returns:
        파싱된 JSON 객체.

    Raises:
        ValueError: JSON을 찾을 수 없거나 파싱 실패 시.
    """
    # Qwen3 등 추론 모델의 <think>...</think> 태그 제거
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()

    # ```json ... ``` 블록 우선 추출
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if match:
        candidate = match.group(1).strip()
    else:
        # { ... } 또는 [ ... ] 직접 추출
        match2 = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
        if not match2:
            raise ValueError(f"JSON을 찾을 수 없습니다: {text[:200]}")
        candidate = match2.group(1).strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # max_tokens 제한으로 잘린 JSON 복구 시도
    repaired = _repair_truncated_json(candidate)
    if repaired is not None:
        return repaired

    raise ValueError(f"JSON 파싱 실패\n원본: {candidate[:200]}")


def _repair_truncated_json(text: str) -> Any | None:
    """잘린 JSON 문자열의 괄호를 닫아 복구를 시도한다.

    마지막 완전한 항목까지만 유지하고, 닫히지 않은 괄호를 순서대로 닫는다.

    Returns:
        복구된 JSON 객체. 복구 실패 시 None.
    """
    # 마지막 불완전한 항목 제거: 마지막 완전한 }나 ] 이후를 자름
    last_complete = max(text.rfind("}"), text.rfind("]"))
    if last_complete == -1:
        return None
    text = text[: last_complete + 1]

    # 닫히지 않은 괄호를 순서대로 닫기
    stack: list[str] = []
    in_string = False
    escape = False
    for ch in text:
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in ("{", "["):
            stack.append("}" if ch == "{" else "]")
        elif ch in ("}", "]"):
            if stack:
                stack.pop()

    text += "".join(reversed(stack))
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
