"""평가 도구용 LLM 클라이언트 빌더.

``web.app._build_llm_client`` 를 그대로 재사용해 ``llm.provider`` 분기,
``llm.headers`` (사내 LLM 게이트웨이 커스텀 헤더), ``llm.reasoning_profiles``
(Qwen3 ``enable_thinking`` 같은 모델별 reasoning 페이로드) 가 자동 주입되도록 한다.

CLI 가 별도 endpoint/model 을 override 하는 경우, ``config.llm.*`` 를
임시로 덮어쓴 뒤 빌더를 호출하고 finally 에서 복원한다 — 같은 헬퍼를
Generator/Judge 등 여러 번 호출해도 서로 영향 주지 않는다.

레이어:
- ``_parse_headers_json``: CLI ``--*-headers`` JSON 문자열 → dict
- ``build_llm_client``: 저수준 — config 의 ``llm.*`` 를 임시 override 하고 빌더 호출
- ``build_eval_llm_client``: ``config.eval.{role}.*`` + CLI override 합성
"""

from __future__ import annotations

import json
from typing import Any, Literal

from context_loop.config import Config
from context_loop.processor.llm_client import LLMClient

EvalRole = Literal["generator", "judge"]


def _parse_headers_json(headers_json: str) -> dict[str, str]:
    """``--*-headers`` CLI 인자(JSON 객체 문자열) 를 dict 로 파싱.

    빈 문자열은 빈 dict 를 반환한다.

    Raises:
        ValueError: JSON 파싱 실패 또는 객체가 아닌 경우.
    """
    if not headers_json:
        return {}
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
    headers_override: dict[str, str] | None = None,
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
        headers_override: ``llm.headers`` 임시 값 (dict). 기본 헤더와
            머지하지 않고 통째로 교체. ``None`` 이면 ``config.llm.headers``
            그대로 사용. 빈 dict ``{}`` 는 헤더 없이 호출.

    Returns:
        ``LLMClient`` 인스턴스.
    """
    # 지연 import — web.app 의 무거운 import chain (FastAPI 등) 회피
    from context_loop.web.app import _build_llm_client  # noqa: PLC0415

    has_override = bool(
        endpoint_override
        or model_override
        or api_key_override
        or headers_override is not None
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
        if headers_override is not None:
            config.set("llm.headers", headers_override)
        return _build_llm_client(config)
    finally:
        for k, v in saved.items():
            config.set(f"llm.{k}", v)


def build_eval_llm_client(
    config: Config,
    role: EvalRole,
    *,
    endpoint_override: str = "",
    model_override: str = "",
    api_key_override: str = "",
    headers_override_json: str = "",
) -> LLMClient:
    """``config.eval.{role}.*`` + CLI override 를 합성해 LLM 클라이언트 생성.

    우선순위 (높음 → 낮음):
        1. CLI override (인자로 전달된 ``*_override`` 값)
        2. ``config.eval.{role}.*`` (yaml 설정 — 운영 디폴트)
        3. ``config.llm.*`` (전역 폴백 — ``build_llm_client`` 가 처리)

    role 별 설정과 CLI 오버라이드가 모두 비면 system LLM (``llm.*``) 과
    같은 클라이언트가 생성된다 (자기 평가 편향 가능 — 호출부에서 경고).

    Args:
        config: 애플리케이션 Config.
        role: ``"generator"`` 또는 ``"judge"``.
        endpoint_override: CLI ``--{role}-endpoint``.
        model_override: CLI ``--{role}-model``.
        api_key_override: CLI ``--{role}-api-key``.
        headers_override_json: CLI ``--{role}-headers`` (JSON 객체 문자열).

    Returns:
        ``LLMClient`` 인스턴스.

    Raises:
        ValueError: ``role`` 이 알 수 없는 값이거나 헤더 JSON 파싱 실패.
    """
    if role not in ("generator", "judge"):
        raise ValueError(f"알 수 없는 role: {role}")

    role_path = f"eval.{role}"
    role_endpoint = config.get(f"{role_path}.endpoint", "") or ""
    role_model = config.get(f"{role_path}.model", "") or ""
    role_api_key = config.get(f"{role_path}.api_key", "") or ""
    role_headers_raw = config.get(f"{role_path}.headers") or {}

    final_endpoint = endpoint_override or role_endpoint
    final_model = model_override or role_model
    final_api_key = api_key_override or role_api_key

    # 헤더 우선순위: CLI JSON > config.eval.{role}.headers (dict) > None (= llm.headers)
    final_headers: dict[str, str] | None
    if headers_override_json:
        final_headers = _parse_headers_json(headers_override_json)
    elif role_headers_raw:
        if not isinstance(role_headers_raw, dict):
            raise ValueError(
                f"config.{role_path}.headers 는 dict 이어야 합니다 — got {type(role_headers_raw).__name__}",
            )
        final_headers = {str(k): str(v) for k, v in role_headers_raw.items()}
    else:
        final_headers = None  # build_llm_client 가 config.llm.headers 를 그대로 사용

    return build_llm_client(
        config,
        endpoint_override=final_endpoint,
        model_override=final_model,
        api_key_override=final_api_key,
        headers_override=final_headers,
    )


def role_is_configured(
    config: Config,
    role: EvalRole,
    *,
    endpoint_override: str = "",
    model_override: str = "",
) -> bool:
    """``role`` 이 system LLM 과 별도 모델로 구성되었는지 판정.

    CLI override 또는 ``config.eval.{role}.endpoint``/``model`` 중 하나라도
    채워져 있으면 True. 자기 평가 편향 경고 표시 여부 결정에 사용.
    """
    if endpoint_override or model_override:
        return True
    role_path = f"eval.{role}"
    return bool(
        config.get(f"{role_path}.endpoint") or config.get(f"{role_path}.model"),
    )
