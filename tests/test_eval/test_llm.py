"""평가 도구용 LLM 클라이언트 빌더 테스트.

``web.app._build_llm_client`` 를 호출하지 않고 패치로 대체해
override → 빌더 호출 → config 복원 흐름만 검증한다.

``context_loop.web.app`` 는 FastAPI 의존이 무거워 테스트 환경에서 직접
import 하지 않는다. ``stub_web_app`` 픽스처가 ``sys.modules`` 에 가짜
모듈을 주입하여 ``build_llm_client`` 의 지연 import 가 그 가짜를 잡도록
한다.
"""

from __future__ import annotations

import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from context_loop.config import Config
from context_loop.eval.llm import _parse_headers_json, build_llm_client


@pytest.fixture
def stub_web_app() -> Any:
    """sys.modules 에 가짜 ``context_loop.web.app`` 를 주입.

    Yields:
        ``MagicMock`` — ``_build_llm_client`` 를 대체. 테스트에서 ``return_value``
        나 ``side_effect`` 로 동작을 지정한다.
    """
    fake_module = types.ModuleType("context_loop.web.app")
    fake_builder = MagicMock(name="_build_llm_client")
    fake_module._build_llm_client = fake_builder  # type: ignore[attr-defined]
    saved = sys.modules.get("context_loop.web.app")
    sys.modules["context_loop.web.app"] = fake_module
    try:
        yield fake_builder
    finally:
        if saved is None:
            sys.modules.pop("context_loop.web.app", None)
        else:
            sys.modules["context_loop.web.app"] = saved


@pytest.fixture
def config(tmp_path: Any) -> Config:
    """기본값을 채운 Config — 실제 default.yaml 로드는 수행됨."""
    cfg = Config(config_path=tmp_path / "user.yaml")
    # 명시 값으로 덮어 — 테스트 안정성
    cfg.set("llm.provider", "endpoint")
    cfg.set("llm.endpoint", "http://default-endpoint/v1")
    cfg.set("llm.model", "default-model")
    cfg.set("llm.api_key", "default-key")
    cfg.set("llm.headers", {"X-Default": "yes"})
    cfg.set("llm.reasoning_profiles", {"off": {"extra_body": {"enable_thinking": False}}})
    return cfg


# ---------------------------------------------------------------------------
# _parse_headers_json
# ---------------------------------------------------------------------------


def test_parse_headers_json_valid() -> None:
    headers = _parse_headers_json('{"X-Org-Id": "abc", "X-Trace": "xyz"}')
    assert headers == {"X-Org-Id": "abc", "X-Trace": "xyz"}


def test_parse_headers_json_coerces_values_to_str() -> None:
    """숫자 값도 문자열로 강제 (HTTP 헤더 규약)."""
    headers = _parse_headers_json('{"X-Count": 42}')
    assert headers == {"X-Count": "42"}


def test_parse_headers_json_invalid_json_raises() -> None:
    with pytest.raises(ValueError, match="JSON 파싱 실패"):
        _parse_headers_json("{not json}")


def test_parse_headers_json_non_object_raises() -> None:
    with pytest.raises(ValueError, match="객체"):
        _parse_headers_json('["array", "not", "object"]')

    with pytest.raises(ValueError, match="객체"):
        _parse_headers_json('"plain string"')


# ---------------------------------------------------------------------------
# build_llm_client — override 없는 경로
# ---------------------------------------------------------------------------


def test_build_llm_client_no_override_calls_builder_directly(
    config: Config, stub_web_app: MagicMock,
) -> None:
    """override 가 모두 비면 _build_llm_client 를 그대로 호출한다."""
    sentinel = object()
    stub_web_app.return_value = sentinel
    result = build_llm_client(config)
    assert result is sentinel
    stub_web_app.assert_called_once_with(config)


def test_build_llm_client_no_override_preserves_config(
    config: Config, stub_web_app: MagicMock,
) -> None:
    """override 없는 경로에서는 config 가 변경되지 않는다."""
    stub_web_app.return_value = object()
    before = config.data
    build_llm_client(config)
    assert config.data == before


# ---------------------------------------------------------------------------
# build_llm_client — override 가 적용되는 경로
# ---------------------------------------------------------------------------


def test_build_llm_client_override_applies_then_restores(
    config: Config, stub_web_app: MagicMock,
) -> None:
    """override 가 임시 적용된 상태에서 빌더가 호출되고 finally 에서 복원된다."""
    captured: dict[str, Any] = {}

    def fake_builder(cfg: Config) -> object:
        captured["provider"] = cfg.get("llm.provider")
        captured["endpoint"] = cfg.get("llm.endpoint")
        captured["model"] = cfg.get("llm.model")
        captured["api_key"] = cfg.get("llm.api_key")
        captured["headers"] = cfg.get("llm.headers")
        captured["reasoning_profiles"] = cfg.get("llm.reasoning_profiles")
        return object()

    stub_web_app.side_effect = fake_builder
    build_llm_client(
        config,
        endpoint_override="http://override/v1",
        model_override="override-model",
        api_key_override="override-key",
        headers_override_json='{"X-New": "v"}',
    )

    # 빌더 호출 시점에는 override 가 반영됨
    assert captured["provider"] == "endpoint"
    assert captured["endpoint"] == "http://override/v1"
    assert captured["model"] == "override-model"
    assert captured["api_key"] == "override-key"
    assert captured["headers"] == {"X-New": "v"}
    # reasoning_profiles 는 override 받지 않으므로 config 의 값 그대로
    assert captured["reasoning_profiles"] == {
        "off": {"extra_body": {"enable_thinking": False}},
    }

    # 호출 종료 후에는 원복
    assert config.get("llm.endpoint") == "http://default-endpoint/v1"
    assert config.get("llm.model") == "default-model"
    assert config.get("llm.api_key") == "default-key"
    assert config.get("llm.headers") == {"X-Default": "yes"}
    assert config.get("llm.provider") == "endpoint"


def test_build_llm_client_partial_override_keeps_other_keys(
    config: Config, stub_web_app: MagicMock,
) -> None:
    """일부 override 만 주면 나머지는 config 값을 유지."""
    captured: dict[str, Any] = {}

    def fake_builder(cfg: Config) -> object:
        captured["endpoint"] = cfg.get("llm.endpoint")
        captured["model"] = cfg.get("llm.model")
        captured["headers"] = cfg.get("llm.headers")
        return object()

    stub_web_app.side_effect = fake_builder
    build_llm_client(config, model_override="only-model-changed")

    # model 만 바뀌고 endpoint/headers 는 config 값
    assert captured["endpoint"] == "http://default-endpoint/v1"
    assert captured["model"] == "only-model-changed"
    assert captured["headers"] == {"X-Default": "yes"}


def test_build_llm_client_restores_on_builder_exception(
    config: Config, stub_web_app: MagicMock,
) -> None:
    """빌더가 예외를 던져도 config 가 복원된다."""
    before = config.data
    stub_web_app.side_effect = RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        build_llm_client(
            config,
            endpoint_override="http://x/v1",
            model_override="m",
        )

    assert config.data == before


def test_build_llm_client_invalid_headers_json_restores_config(
    config: Config, stub_web_app: MagicMock,
) -> None:
    """헤더 JSON 파싱 실패해도 config 복원."""
    before = config.data

    with pytest.raises(ValueError, match="JSON 파싱 실패"):
        build_llm_client(
            config,
            endpoint_override="http://x/v1",
            headers_override_json="{not valid",
        )

    # 빌더는 호출되지 않음 (헤더 파싱이 빌더 호출 전에 실패)
    stub_web_app.assert_not_called()
    assert config.data == before


def test_build_llm_client_forces_endpoint_provider(
    config: Config, stub_web_app: MagicMock,
) -> None:
    """원래 provider 가 anthropic/openai 라도 override 시점엔 endpoint 로 강제."""
    config.set("llm.provider", "anthropic")
    captured_provider: list[str] = []

    def fake_builder(cfg: Config) -> object:
        captured_provider.append(cfg.get("llm.provider"))
        return object()

    stub_web_app.side_effect = fake_builder
    build_llm_client(config, endpoint_override="http://x/v1", model_override="m")

    assert captured_provider == ["endpoint"]
    # 복원 후에는 원래대로
    assert config.get("llm.provider") == "anthropic"
