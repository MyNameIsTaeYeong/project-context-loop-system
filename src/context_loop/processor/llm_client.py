"""LLM 클라이언트 추상화 레이어.

OpenAI, Anthropic API 및 OpenAI 호환 자체 엔드포인트를 동일한 인터페이스로 래핑한다.
config.yaml의 llm.provider 설정에 따라 적절한 구현체를 반환한다.
- "openai": OpenAI API (api_key 방식)
- "anthropic": Anthropic Claude API (api_key 방식)
- "endpoint": OpenAI 호환 자체 모델 서버 (endpoint URL 방식)
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any


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
    ) -> str:
        """텍스트 완성 요청을 보내고 응답 문자열을 반환한다.

        Args:
            prompt: 사용자 프롬프트.
            system: 시스템 프롬프트. None이면 사용하지 않는다.
            max_tokens: 최대 출력 토큰 수.
            temperature: 샘플링 온도 (0.0 = 결정적).

        Returns:
            LLM 응답 문자열.
        """


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
    ) -> str:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system
        # anthropic SDK는 temperature를 float으로 지원
        if temperature != 0.0:
            kwargs["temperature"] = temperature

        response = await self._client.messages.create(**kwargs)
        return response.content[0].text  # type: ignore[index,union-attr]


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
    ) -> str:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,  # type: ignore[arg-type]
            max_tokens=max_tokens,
            temperature=temperature,
        )
        return response.choices[0].message.content or ""


class EndpointLLMClient(LLMClient):
    """OpenAI 호환 자체 모델 서버(엔드포인트) 기반 LLM 클라이언트.

    자체 호스팅된 LLM 서버(vLLM, Ollama 등 OpenAI 호환 API)와 통신한다.
    API 키가 필요 없는 경우 api_key를 빈 문자열로 설정한다.

    Args:
        endpoint: 모델 서버 엔드포인트 URL (예: "http://localhost:8080/v1").
        model: 사용할 모델 ID.
        api_key: 엔드포인트 인증 키. 불필요한 경우 빈 문자열.
    """

    def __init__(
        self,
        endpoint: str,
        model: str,
        api_key: str = "none",
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        from openai import AsyncOpenAI  # noqa: PLC0415
        self._client = AsyncOpenAI(api_key=api_key or "none", base_url=endpoint)
        self._model = model
        self._extra_body = extra_body

    async def complete(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> str:
        messages: list[dict[str, str]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if self._extra_body:
            kwargs["extra_body"] = self._extra_body
        response = await self._client.chat.completions.create(**kwargs)  # type: ignore[arg-type]
        return response.choices[0].message.content or ""


def extract_json(text: str) -> Any:
    """LLM 응답에서 JSON을 추출한다.

    마크다운 코드 블록 또는 순수 JSON 형태를 모두 처리한다.
    Qwen3 등 추론 모델의 <think>...</think> 블록도 자동 제거한다.

    Args:
        text: LLM 응답 텍스트.

    Returns:
        파싱된 JSON 객체.

    Raises:
        ValueError: JSON을 찾을 수 없거나 파싱 실패 시.
    """
    # 추론 모델(Qwen3 등)의 <think>...</think> 블록 제거
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
