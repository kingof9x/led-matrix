import os

from led_mon import external_apps
from led_mon.external_apps import Target, find_framework_control, find_open_hardware_monitor

START_MENU = os.path.join("Microsoft", "Windows", "Start Menu", "Programs")


def touch(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("x")
    return str(path)


def fake_env(tmp_path):
    env = {
        "ProgramFiles": str(tmp_path / "Program Files"),
        "ProgramFiles(x86)": str(tmp_path / "Program Files (x86)"),
        "ProgramData": str(tmp_path / "ProgramData"),
        "APPDATA": str(tmp_path / "Users" / "me" / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(tmp_path / "Users" / "me" / "AppData" / "Local"),
        "USERPROFILE": str(tmp_path / "Users" / "me"),
        "PUBLIC": str(tmp_path / "Users" / "Public"),
        "SystemDrive": str(tmp_path / "C"),
    }
    for value in env.values():
        os.makedirs(value, exist_ok=True)
    return env


def test_ohm_not_found(tmp_path, monkeypatch):
    monkeypatch.setattr(external_apps.platform_utils, "app_dir", lambda: str(tmp_path / "app"))
    assert find_open_hardware_monitor("", fake_env(tmp_path), running=[]) is None


def test_ohm_running_wins(tmp_path):
    env = fake_env(tmp_path)
    exe = touch(tmp_path / "Program Files" / "OpenHardwareMonitor" / "OpenHardwareMonitor.exe")
    found = find_open_hardware_monitor(exe, env, running=[(1234, "C:\\OHM\\OpenHardwareMonitor.exe")])
    assert found == Target("running", "C:\\OHM\\OpenHardwareMonitor.exe", 1234)


def test_ohm_configured_exe_and_folder(tmp_path):
    env = fake_env(tmp_path)
    exe = touch(tmp_path / "somewhere" / "OpenHardwareMonitor.exe")
    assert find_open_hardware_monitor(exe, env, running=[]) == Target("file", exe)
    assert find_open_hardware_monitor(os.path.dirname(exe), env, running=[]) == Target("file", exe)
    assert find_open_hardware_monitor('"' + exe + '"', env, running=[]) == Target("file", exe)


def test_ohm_bad_configured_path_falls_back_to_search(tmp_path):
    env = fake_env(tmp_path)
    exe = touch(tmp_path / "Program Files" / "OpenHardwareMonitor" / "OpenHardwareMonitor.exe")
    found = find_open_hardware_monitor(str(tmp_path / "missing.exe"), env, running=[])
    assert found == Target("file", exe)


def test_ohm_unzipped_in_downloads(tmp_path):
    env = fake_env(tmp_path)
    exe = touch(tmp_path / "Users" / "me" / "Downloads" / "openhardwaremonitor-v0.9.6"
                / "OpenHardwareMonitor" / "OpenHardwareMonitor.exe")
    assert find_open_hardware_monitor("", env, running=[]) == Target("file", exe)


def test_ohm_ignores_unrelated_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(external_apps.platform_utils, "app_dir", lambda: str(tmp_path / "app"))
    env = fake_env(tmp_path)
    touch(tmp_path / "Users" / "me" / "Downloads" / "games" / "x" / "OpenHardwareMonitor.exe")
    assert find_open_hardware_monitor("", env, running=[]) is None


def test_ohm_start_menu_shortcut(tmp_path):
    env = fake_env(tmp_path)
    lnk = touch(os.path.join(env["APPDATA"], START_MENU, "Utilities", "Open Hardware Monitor.lnk"))
    assert find_open_hardware_monitor("", env, running=[]) == Target("file", lnk)


def test_ohm_next_to_app(tmp_path, monkeypatch):
    env = fake_env(tmp_path)
    exe = touch(tmp_path / "app" / "OpenHardwareMonitor.exe")
    monkeypatch.setattr(external_apps.platform_utils, "app_dir", lambda: str(tmp_path / "app"))
    assert find_open_hardware_monitor("", env, running=[]) == Target("file", exe)


def test_fc_not_installed(tmp_path):
    assert find_framework_control(fake_env(tmp_path)) is None


def test_fc_start_menu_shortcut(tmp_path):
    env = fake_env(tmp_path)
    lnk = touch(os.path.join(env["ProgramData"], START_MENU, "Framework Control.lnk"))
    assert find_framework_control(env) == Target("file", lnk)


def test_fc_desktop_url_shortcut(tmp_path):
    env = fake_env(tmp_path)
    url = touch(os.path.join(env["PUBLIC"], "Desktop", "Framework Control.url"))
    assert find_framework_control(env) == Target("file", url)


SERVICE_XML = """<service>
  <id>FrameworkControlService</id>
  <env name="FRAMEWORK_CONTROL_ALLOWED_ORIGINS" value="x" />
  <env name="FRAMEWORK_CONTROL_PORT" value="{port}" />
</service>"""


def write_service_xml(tmp_path, port):
    path = tmp_path / "Program Files" / "FrameworkControl" / "FrameworkControlService.xml"
    os.makedirs(path.parent, exist_ok=True)
    path.write_text(SERVICE_XML.format(port=port))


def test_fc_service_without_shortcuts_opens_web_page(tmp_path):
    env = fake_env(tmp_path)
    write_service_xml(tmp_path, 8090)
    assert find_framework_control(env) == Target("url", "http://127.0.0.1:8090")


def test_fc_unfilled_port_placeholder_is_ignored(tmp_path):
    env = fake_env(tmp_path)
    write_service_xml(tmp_path, "@FRAMEWORK_CONTROL_PORT@")
    assert find_framework_control(env) is None


def test_open_target_messages(monkeypatch):
    assert "not found" in external_apps.open_target(None, "Framework Control")
    monkeypatch.setattr(external_apps, "_show_window_of", lambda pid: False)
    message = external_apps.open_target(Target("running", "", 5), "Open Hardware Monitor")
    assert "already running" in message
    opened = []
    monkeypatch.setattr(external_apps.webbrowser, "open", opened.append)
    assert external_apps.open_target(Target("url", "http://127.0.0.1:8090"), "Framework Control") is None
    assert opened == ["http://127.0.0.1:8090"]


def test_open_target_uac_cancel_is_silent(monkeypatch):
    def cancelled(path):
        e = OSError("cancelled")
        e.winerror = 1223
        raise e
    monkeypatch.setattr(external_apps.platform_utils, "open_path", cancelled)
    assert external_apps.open_target(Target("file", "x.exe"), "Open Hardware Monitor") is None
