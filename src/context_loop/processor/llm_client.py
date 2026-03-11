"""LLM 클라이언트 추상화 레이어.

OpenAI와 Anthropic API를 동일한 인터페이스로 래핑한다.
config.yaml의 llm.provider 설정에 따라 적절한 구현체를 반환한다.
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
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 파싱 실패: {e}\n원본: {candidate[:200]}") from e
