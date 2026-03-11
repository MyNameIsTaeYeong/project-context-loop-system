"""인증 토큰 관리 모듈.

OS keyring을 통해 API 토큰을 안전하게 저장/조회/삭제한다.
설정 파일에 시크릿이 노출되지 않도록 보장한다.
"""

from __future__ import annotations

import keyring

_SERVICE_PREFIX = "context-loop"


def _service_name(provider: str) -> str:
    """keyring 서비스 이름을 생성한다."""
    return f"{_SERVICE_PREFIX}.{provider}"


def store_token(provider: str, username: str, token: str) -> None:
    """토큰을 OS keyring에 저장한다.

    Args:
        provider: 서비스 제공자 식별자 (예: "confluence", "openai", "anthropic").
        username: 사용자 식별자 (예: 이메일 주소).
        token: 저장할 토큰 값.
    """
    keyring.set_password(_service_name(provider), username, token)


def get_token(provider: str, username: str) -> str | None:
    """OS keyring에서 토큰을 조회한다.

    Args:
        provider: 서비스 제공자 식별자.
        username: 사용자 식별자.

    Returns:
        저장된 토큰 문자열. 없으면 None.
    """
    return keyring.get_password(_service_name(provider), username)


def delete_token(provider: str, username: str) -> None:
    """OS keyring에서 토큰을 삭제한다.

    Args:
        provider: 서비스 제공자 식별자.
        username: 사용자 식별자.

    Raises:
        keyring.errors.PasswordDeleteError: 토큰이 존재하지 않는 경우.
    """
    keyring.delete_password(_service_name(provider), username)
