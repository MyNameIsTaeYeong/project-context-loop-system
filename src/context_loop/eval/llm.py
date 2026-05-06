"""평가 도구용 LLM 클라이언트 빌더.

``web.app._build_llm_client`` 를 그대로 재사용해 ``llm.provider`` 분기,
``llm.headers`` (사내 LLM 게이트웨이 커스텀 헤더), ``llm.reasoning_profiles``
(Qwen3 ``enable_thinking`` 같은 모델별 reasoning 페이로드) 가 자동 주입되도록 한다.

CLI 가 별도 endpoint/model 을 override 하는 경우, ``config.llm.*`` 를
임시로 덮어쓴 뒤 빌더를 호출하고 finally 에서 복원한다 — 같은 헬퍼를
Generator/Judge 등 여러 번 호출해도 서로 영향 주지 않는다.
"""

from __future__ import annotations

import json
from typing import Any

from context_loop.config import Config
from context_loop.processor.llm_client import LLMClient


def _parse_headers_json(headers_json: str) -> dict[str, str]:
    """``--*-headers`` CLI 인자(JSON 객체 문자열) 를 dict 로 파싱.

    Raises:
        ValueError: JSON 파싱 실패 또는 객체가 아닌 경우.
    """
    try:
        parsed = json.loads(headers_json)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"headers JSON 파싱 실패 — {exc}: {headers_json}",
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError("headers 는 JSON 객체여야 합니다.")
    return {str(k): str(v) for k, v in parsed.items()}


def build_llm_client(
    config: Config,
    *,
    endpoint_override: str = "",
    model_override: str = "",
    api_key_override: str = "",
    headers_override_json: str = "",
) -> LLMClient:
    """``web.app._build_llm_client`` 를 그대로 재사용해 LLM 클라이언트를 생성한다.

    핵심 동작:
    - override 가 모두 비어 있으면 ``_build_llm_client(config)`` 그대로 호출
      → ``llm.provider`` 분기(endpoint/openai/anthropic) 와 헤더, reasoning
      프로파일까지 한 번에 적용된다.
    - override 가 하나라도 있으면 ``llm.provider`` 를 ``"endpoint"`` 로 강제하고
      해당 키만 임시 덮어쓴 뒤 빌더 호출, finally 에서 원복.
    - reasoning_profiles 는 항상 config 의 값을 따른다 (모델 family 별 매핑이라
      override 시점에 별도 인자로 받기엔 표현이 어색하기 때문).

    Args:
        config: 베이스 ``Config``. 함수 호출 후 원래 상태로 복원된다.
        endpoint_override: ``llm.endpoint`` 임시 값.
        model_override: ``llm.model`` 임시 값.
        api_key_override: ``llm.api_key`` 임시 값.
        headers_override_json: ``llm.headers`` 임시 값 (JSON 객체 문자열).
            기본 헤더와 머지하지 않고 통째로 교체. 빈 값이면 ``config.llm.headers``
            그대로 사용.

    Returns:
        ``LLMClient`` 인스턴스.

    Raises:
        ValueError: ``headers_override_json`` 파싱 실패 시.
    """
    # 지연 import — web.app 의 무거운 import chain (FastAPI 등) 회피
    from context_loop.web.app import _build_llm_client  # noqa: PLC0415

    has_override = bool(
        endpoint_override
        or model_override
        or api_key_override
        or headers_override_json
    )
    if not has_override:
        return _build_llm_client(config)

    saved: dict[str, Any] = {
        "provider": config.get("llm.provider"),
        "endpoint": config.get("llm.endpoint"),
        "model": config.get("llm.model"),
        "api_key": config.get("llm.api_key"),
        "headers": config.get("llm.headers"),
    }
    try:
        config.set("llm.provider", "endpoint")
        if endpoint_override:
            config.set("llm.endpoint", endpoint_override)
        if model_override:
            config.set("llm.model", model_override)
        if api_key_override:
            config.set("llm.api_key", api_key_override)
        if headers_override_json:
            config.set("llm.headers", _parse_headers_json(headers_override_json))
        return _build_llm_client(config)
    finally:
        for k, v in saved.items():
            config.set(f"llm.{k}", v)
