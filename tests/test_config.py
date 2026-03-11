"""config 모듈 테스트."""

from pathlib import Path

import yaml

from context_loop.config import Config


def test_load_default_config(tmp_path: Path) -> None:
    """기본 설정이 로드되는지 확인한다."""
    config = Config(config_path=tmp_path / "nonexistent.yaml")
    assert config.get("app.data_dir") == "~/.context-loop/data"
    assert config.get("app.log_level") == "INFO"
    assert config.get("web.port") == 8000


def test_user_config_override(tmp_path: Path) -> None:
    """사용자 설정이 기본 설정을 오버라이드하는지 확인한다."""
    user_config = tmp_path / "config.yaml"
    user_config.write_text(yaml.dump({"web": {"port": 9000}, "app": {"log_level": "DEBUG"}}))

    config = Config(config_path=user_config)
    assert config.get("web.port") == 9000
    assert config.get("app.log_level") == "DEBUG"
    # 오버라이드하지 않은 값은 기본값 유지
    assert config.get("app.data_dir") == "~/.context-loop/data"


def test_get_missing_key_returns_default(tmp_path: Path) -> None:
    """존재하지 않는 키에 대해 기본값을 반환하는지 확인한다."""
    config = Config(config_path=tmp_path / "nonexistent.yaml")
    assert config.get("nonexistent.key") is None
    assert config.get("nonexistent.key", "fallback") == "fallback"


def test_set_and_save(tmp_path: Path) -> None:
    """설정 변경 후 저장이 정상 동작하는지 확인한다."""
    user_config = tmp_path / "config.yaml"
    config = Config(config_path=user_config)
    config.set("web.port", 5555)
    config.save()

    # 저장된 파일 다시 로드
    config2 = Config(config_path=user_config)
    assert config2.get("web.port") == 5555


def test_data_dir_property(tmp_path: Path) -> None:
    """data_dir 프로퍼티가 Path 객체를 반환하는지 확인한다."""
    config = Config(config_path=tmp_path / "nonexistent.yaml")
    assert isinstance(config.data_dir, Path)


def test_reload(tmp_path: Path) -> None:
    """reload() 후 새 값이 반영되는지 확인한다."""
    user_config = tmp_path / "config.yaml"
    config = Config(config_path=user_config)
    assert config.get("web.port") == 8000

    user_config.write_text(yaml.dump({"web": {"port": 7777}}))
    config.reload()
    assert config.get("web.port") == 7777
