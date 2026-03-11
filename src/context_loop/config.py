"""설정 로드/저장 모듈.

YAML 기반 설정 파일을 관리한다.
- 기본 설정(config/default.yaml)을 로드
- 사용자 설정(~/.context-loop/config.yaml)으로 오버라이드
- 런타임에서 설정 값 접근
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "default.yaml"
_USER_CONFIG_DIR = Path.home() / ".context-loop"
_USER_CONFIG_PATH = _USER_CONFIG_DIR / "config.yaml"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """base dict에 override dict를 재귀적으로 병합한다."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _load_yaml(path: Path) -> dict[str, Any]:
    """YAML 파일을 읽어 dict로 반환한다. 파일이 없으면 빈 dict."""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


class Config:
    """애플리케이션 설정을 관리하는 클래스.

    Args:
        config_path: 사용자 설정 파일 경로. None이면 기본 경로 사용.
    """

    def __init__(self, config_path: Path | None = None) -> None:
        self._user_config_path = config_path or _USER_CONFIG_PATH
        self._data: dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        """기본 설정과 사용자 설정을 (재)로드한다."""
        default = _load_yaml(_DEFAULT_CONFIG_PATH)
        user = _load_yaml(self._user_config_path)
        self._data = _deep_merge(default, user)

    def get(self, key_path: str, default: Any = None) -> Any:
        """점(.) 구분 키 경로로 설정 값을 가져온다.

        Args:
            key_path: 점으로 구분된 키 경로 (예: "app.data_dir").
            default: 키가 없을 때 반환할 기본값.

        Returns:
            설정 값 또는 기본값.
        """
        keys = key_path.split(".")
        current: Any = self._data
        for k in keys:
            if isinstance(current, dict) and k in current:
                current = current[k]
            else:
                return default
        return current

    def set(self, key_path: str, value: Any) -> None:
        """점(.) 구분 키 경로로 설정 값을 변경한다 (메모리 내).

        Args:
            key_path: 점으로 구분된 키 경로.
            value: 설정할 값.
        """
        keys = key_path.split(".")
        current = self._data
        for k in keys[:-1]:
            if k not in current or not isinstance(current[k], dict):
                current[k] = {}
            current = current[k]
        current[keys[-1]] = value

    def save(self) -> None:
        """현재 설정을 사용자 설정 파일에 저장한다."""
        self._user_config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._user_config_path, "w", encoding="utf-8") as f:
            yaml.dump(self._data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    @property
    def data_dir(self) -> Path:
        """데이터 저장 디렉토리 경로."""
        raw = self.get("app.data_dir", "~/.context-loop/data")
        return Path(raw).expanduser()

    @property
    def data(self) -> dict[str, Any]:
        """전체 설정 dict (읽기 전용 사본)."""
        return copy.deepcopy(self._data)
