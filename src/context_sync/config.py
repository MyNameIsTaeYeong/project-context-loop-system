"""설정 로드/저장 모듈.

YAML 기반 설정 파일을 로드하고, 기본값과 병합하여 관리한다.
설정 파일 위치: ~/.context-sync/config.yaml
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_DIR = Path.home() / ".context-sync"
DEFAULT_CONFIG_PATH = DEFAULT_CONFIG_DIR / "config.yaml"
BUNDLED_DEFAULT_PATH = Path(__file__).parent.parent.parent / "config" / "default.yaml"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """base 딕셔너리 위에 override를 재귀적으로 병합한다.

    Args:
        base: 기본값 딕셔너리.
        override: 덮어쓸 값 딕셔너리.

    Returns:
        병합된 새 딕셔너리.
    """
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def load_default_config() -> dict[str, Any]:
    """번들된 기본 설정 파일을 로드한다.

    Returns:
        기본 설정 딕셔너리.
    """
    if not BUNDLED_DEFAULT_PATH.exists():
        return {}
    with open(BUNDLED_DEFAULT_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(config_path: Path | None = None) -> dict[str, Any]:
    """사용자 설정 파일을 로드하고 기본값과 병합한다.

    기본값 위에 사용자 설정을 덮어쓰는 방식으로 동작한다.
    사용자 설정 파일이 없으면 기본값만 반환한다.

    Args:
        config_path: 사용자 설정 파일 경로. None이면 기본 경로 사용.

    Returns:
        병합된 설정 딕셔너리.
    """
    defaults = load_default_config()
    path = config_path or DEFAULT_CONFIG_PATH

    if not path.exists():
        return defaults

    with open(path, encoding="utf-8") as f:
        user_config = yaml.safe_load(f) or {}

    return _deep_merge(defaults, user_config)


def save_config(config: dict[str, Any], config_path: Path | None = None) -> Path:
    """설정을 YAML 파일로 저장한다.

    Args:
        config: 저장할 설정 딕셔너리.
        config_path: 저장 경로. None이면 기본 경로 사용.

    Returns:
        저장된 파일 경로.
    """
    path = config_path or DEFAULT_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    return path


def get_data_dir(config: dict[str, Any]) -> Path:
    """설정에서 데이터 디렉토리 경로를 가져온다.

    Args:
        config: 설정 딕셔너리.

    Returns:
        확장된 데이터 디렉토리 경로.
    """
    raw = config.get("app", {}).get("data_dir", "~/.context-sync/data")
    return Path(raw).expanduser()


def init_config(config_path: Path | None = None) -> dict[str, Any]:
    """설정을 초기화한다. 파일이 없으면 기본값으로 생성한다.

    Args:
        config_path: 설정 파일 경로. None이면 기본 경로 사용.

    Returns:
        초기화된 설정 딕셔너리.
    """
    path = config_path or DEFAULT_CONFIG_PATH

    if path.exists():
        return load_config(path)

    defaults = load_default_config()
    save_config(defaults, path)
    return defaults
