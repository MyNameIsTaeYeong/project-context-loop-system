"""인증 토큰 관리 모듈 (keyring 연동).

OS 네이티브 키체인을 통해 API 토큰을 안전하게 저장/조회/삭제한다.
설정 파일에 시크릿이 노출되지 않도록 보장한다.
"""

from __future__ import annotations

import keyring
from keyring.errors import PasswordDeleteError

SERVICE_NAME = "context-sync"


def _make_key(source_type: str, key_name: str) -> str:
    """키링에서 사용할 키 이름을 생성한다.

    Args:
        source_type: 소스 타입 (예: "confluence", "openai").
        key_name: 키 이름 (예: "api_token", "api_key").

    Returns:
        조합된 키 문자열.
    """
    return f"{source_type}:{key_name}"


def store_token(source_type: str, key_name: str, token: str) -> None:
    """토큰을 OS 키체인에 저장한다.

    Args:
        source_type: 소스 타입 (예: "confluence", "openai").
        key_name: 키 이름 (예: "api_token", "api_key").
        token: 저장할 토큰 값.
    """
    keyring.set_password(SERVICE_NAME, _make_key(source_type, key_name), token)


def get_token(source_type: str, key_name: str) -> str | None:
    """OS 키체인에서 토큰을 조회한다.

    Args:
        source_type: 소스 타입.
        key_name: 키 이름.

    Returns:
        토큰 문자열. 없으면 None.
    """
    return keyring.get_password(SERVICE_NAME, _make_key(source_type, key_name))


def delete_token(source_type: str, key_name: str) -> bool:
    """OS 키체인에서 토큰을 삭제한다.

    Args:
        source_type: 소스 타입.
        key_name: 키 이름.

    Returns:
        삭제 성공 여부.
    """
    try:
        keyring.delete_password(SERVICE_NAME, _make_key(source_type, key_name))
    except PasswordDeleteError:
        return False
    return True


def has_token(source_type: str, key_name: str) -> bool:
    """토큰이 키체인에 존재하는지 확인한다.

    Args:
        source_type: 소스 타입.
        key_name: 키 이름.

    Returns:
        토큰 존재 여부.
    """
    return get_token(source_type, key_name) is not None
