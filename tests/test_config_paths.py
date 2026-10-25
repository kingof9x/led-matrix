# Config / log path resolution per OS (os differences simulated with monkeypatch)
import os

import pytest

from led_mon import platform_utils


@pytest.fixture
def windows(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_utils, "IS_WINDOWS", True)
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.delenv("CONFIG_FILE", raising=False)
    return tmp_path


@pytest.fixture
def linux(monkeypatch):
    monkeypatch.setattr(platform_utils, "IS_WINDOWS", False)
    monkeypatch.delenv("CONFIG_FILE", raising=False)


def test_windows_first_run_copies_bundled_config(windows):
    path = platform_utils.resolve_config_file()
    assert path == os.path.join(str(windows), "Roaming", "led_mon", "config.yaml")
    bundled = os.path.join(platform_utils.package_dir(), "config.yaml")
    with open(path) as copied, open(bundled) as original:
        assert copied.read() == original.read()


def test_windows_prefers_config_local(windows):
    cfg_dir = windows / "Roaming" / "led_mon"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yaml").write_text("duration: 1\n")
    (cfg_dir / "config-local.yaml").write_text("duration: 2\n")
    assert platform_utils.resolve_config_file() == str(cfg_dir / "config-local.yaml")


def test_windows_existing_config_not_overwritten(windows):
    cfg_dir = windows / "Roaming" / "led_mon"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.yaml").write_text("duration: 1\n")
    assert platform_utils.resolve_config_file() == str(cfg_dir / "config.yaml")
    assert (cfg_dir / "config.yaml").read_text() == "duration: 1\n"


def test_cli_and_env_take_priority(windows, monkeypatch):
    monkeypatch.setenv("CONFIG_FILE", "/env/config.yaml")
    assert platform_utils.resolve_config_file("/cli/config.yaml") == "/cli/config.yaml"
    assert platform_utils.resolve_config_file() == "/env/config.yaml"
    # Nothing is copied when a config file is given explicitly
    assert not (windows / "Roaming" / "led_mon").exists()


def test_windows_log_path(windows):
    assert platform_utils.log_file_path() == os.path.join(str(windows), "Local", "led_mon", "led_mon.log")


def test_linux_uses_package_dir(linux):
    expected = os.path.join(platform_utils.package_dir(), "config.yaml")
    local = os.path.join(platform_utils.package_dir(), "config-local.yaml")
    if os.path.exists(local):
        expected = local
    assert platform_utils.resolve_config_file() == expected
    assert platform_utils.user_config_dir() == platform_utils.package_dir()
