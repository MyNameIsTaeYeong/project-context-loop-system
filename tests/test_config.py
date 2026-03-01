"""config 모듈 테스트."""

from pathlib import Path

import yaml

from context_sync.config import (
    _deep_merge,
    get_data_dir,
    init_config,
    load_config,
    load_default_config,
    save_config,
)


class TestDeepMerge:
    def test_flat_merge(self) -> None:
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self) -> None:
        base = {"app": {"data_dir": "/default", "log_level": "INFO"}}
        override = {"app": {"log_level": "DEBUG"}}
        result = _deep_merge(base, override)
        assert result == {"app": {"data_dir": "/default", "log_level": "DEBUG"}}

    def test_override_replaces_non_dict(self) -> None:
        base = {"a": [1, 2]}
        override = {"a": [3, 4]}
        result = _deep_merge(base, override)
        assert result == {"a": [3, 4]}

    def test_does_not_mutate_base(self) -> None:
        base = {"app": {"data_dir": "/original"}}
        override = {"app": {"data_dir": "/changed"}}
        _deep_merge(base, override)
        assert base["app"]["data_dir"] == "/original"


class TestLoadDefaultConfig:
    def test_loads_bundled_defaults(self) -> None:
        config = load_default_config()
        assert "app" in config
        assert "sync" in config
        assert "sources" in config
        assert config["sync"]["interval_minutes"] == 30


class TestLoadConfig:
    def test_returns_defaults_when_no_user_config(self, tmp_path: Path) -> None:
        config = load_config(tmp_path / "nonexistent.yaml")
        defaults = load_default_config()
        assert config == defaults

    def test_merges_user_config_over_defaults(self, tmp_path: Path) -> None:
        user_config = {"sync": {"interval_minutes": 60}}
        user_path = tmp_path / "config.yaml"
        with open(user_path, "w") as f:
            yaml.dump(user_config, f)

        config = load_config(user_path)
        assert config["sync"]["interval_minutes"] == 60
        # 다른 기본값은 유지
        assert config["sync"]["max_concurrent"] == 3


class TestSaveConfig:
    def test_saves_and_reloads(self, tmp_path: Path) -> None:
        config = {"app": {"data_dir": "~/test-dir", "log_level": "DEBUG"}}
        path = tmp_path / "config.yaml"
        save_config(config, path)

        with open(path, encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
        assert loaded == config

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "dir" / "config.yaml"
        save_config({"a": 1}, path)
        assert path.exists()


class TestGetDataDir:
    def test_expands_user_home(self) -> None:
        config = {"app": {"data_dir": "~/.context-sync/data"}}
        result = get_data_dir(config)
        assert result == Path.home() / ".context-sync" / "data"

    def test_default_when_missing(self) -> None:
        result = get_data_dir({})
        assert result == Path.home() / ".context-sync" / "data"


class TestInitConfig:
    def test_creates_config_if_not_exists(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        config = init_config(path)
        assert path.exists()
        assert "app" in config

    def test_loads_existing_config(self, tmp_path: Path) -> None:
        path = tmp_path / "config.yaml"
        user_config = {"sync": {"interval_minutes": 15}}
        with open(path, "w") as f:
            yaml.dump(user_config, f)

        config = init_config(path)
        assert config["sync"]["interval_minutes"] == 15
