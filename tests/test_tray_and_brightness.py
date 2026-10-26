# Tray menu, the brightness setting and the brightness window's placement
import importlib
import json
import sys

import pytest

from led_mon import app_settings


def load_pystray(monkeypatch):
    pytest.importorskip("PIL")
    # The dummy backend needs no desktop; it must be chosen before pystray is imported
    monkeypatch.setenv("PYSTRAY_BACKEND", "dummy")
    sys.modules.pop("pystray", None)
    try:
        return importlib.import_module("pystray")
    except ImportError:
        pytest.skip("pystray is not installed")


def visible_labels(menu):
    return [str(item.text) for item in menu.items if item.visible and item is not menu.SEPARATOR]


def make_menu(monkeypatch, state, startup_supported=True, edit_layout_available=True):
    pystray = load_pystray(monkeypatch)
    from led_mon import tray
    finder = tray.AppFinder()
    noop = lambda icon, item: None
    actions = {name: noop for name in (
        "edit_layout", "brightness", "toggle_animation", "identify", "next_app", "toggle_freeze", "open_ohm",
        "open_fc", "open_folder", "open_log", "reload_config", "toggle_startup", "quit")}

    def toggle_animation(icon, item):
        state["paused"] = not state["paused"]
    actions["toggle_animation"] = toggle_animation
    menu = tray.build_menu(pystray, actions, finder, lambda: state["paused"], lambda: False,
                           edit_layout_available, startup_supported=startup_supported)
    return menu, finder


def test_menu_items_and_found_apps(monkeypatch):
    menu, finder = make_menu(monkeypatch, {"paused": False})
    base = ["Edit layout...", "Brightness...", "Stop animation", "Identify panels", "Next app",
            "Freeze app switching", "Open config folder", "Open log", "Reload config", "Start with Windows", "Quit"]
    assert visible_labels(menu) == base
    finder.ohm = object()
    finder.framework_control = object()
    labels = visible_labels(menu)
    assert labels.index("Open Hardware Monitor") == labels.index("Freeze app switching") + 1
    assert "Open Framework Control" in labels


def test_stop_and_start_animation(monkeypatch):
    state = {"paused": False}
    menu, _ = make_menu(monkeypatch, state)
    item = [i for i in menu.items if str(i.text) == "Stop animation"][0]
    item(None)
    assert state["paused"] is True and str(item.text) == "Start animation"
    item(None)
    assert state["paused"] is False and str(item.text) == "Stop animation"


def test_start_with_windows_item(monkeypatch):
    menu, _ = make_menu(monkeypatch, {"paused": False})
    from led_mon import tray
    on = {"value": False}
    monkeypatch.setattr(tray.startup, "is_enabled", lambda: on["value"])
    item = [i for i in menu.items if str(i.text) == "Start with Windows"][0]
    assert item.checked is False
    on["value"] = True
    assert item.checked is True
    # Hidden where it doesn't apply (Linux)
    menu, _ = make_menu(monkeypatch, {"paused": False}, startup_supported=False)
    assert "Start with Windows" not in visible_labels(menu)


def test_icon_is_grey_when_stopped():
    pytest.importorskip("PIL")
    from led_mon import tray
    lit = tray.make_icon_image(64, lit=True)
    dim = tray.make_icon_image(64, lit=False)
    assert lit.size == (64, 64)
    assert tray.LIT_COLOR in {c for _, c in lit.getcolors(4096)}
    assert tray.LIT_COLOR not in {c for _, c in dim.getcolors(4096)}
    assert tray.tooltip(True).endswith("(stopped)")


def test_brightness_defaults_follow_the_screen(tmp_path):
    brightness = app_settings.Brightness(str(tmp_path / "settings.json"))
    assert brightness.get() == {"follow_screen": True, "level": 70}
    assert brightness.ratio(lambda: 0.4) == 0.4


def test_brightness_level_is_saved_and_kept(tmp_path):
    path = tmp_path / "cfg" / "settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"open_hardware_monitor_path": "D:\\OHM"}))
    brightness = app_settings.Brightness(str(path))
    assert brightness.set(False, 35) == {"follow_screen": False, "level": 35}
    assert brightness.ratio(lambda: 1.0) == 0.35
    saved = json.loads(path.read_text())
    assert saved["brightness_level"] == 35 and saved["follow_screen_brightness"] is False
    # Other settings in the file are kept
    assert saved["open_hardware_monitor_path"] == "D:\\OHM"
    assert app_settings.Brightness(str(path)).get() == {"follow_screen": False, "level": 35}


def test_brightness_level_is_clamped_and_bad_file_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    brightness = app_settings.Brightness(str(path))
    assert brightness.get()["follow_screen"] is True
    assert brightness.set(level=250, save=False)["level"] == 100
    assert brightness.set(level=-5, save=False)["level"] == 0
    assert brightness.set(level="x", save=False)["level"] == 70


def test_popup_opens_above_the_pointer_inside_the_screen():
    pytest.importorskip("tkinter")
    from led_mon.brightness_popup import popup_position
    area = (0, 0, 1920, 1040)
    assert popup_position((1800, 1000), (240, 100), area) == (1560, 900)
    # Near the left or top edge it stays on screen
    assert popup_position((50, 40), (240, 100), area) == (0, 0)
    # A pointer below the work area (on the taskbar) still puts the window above the taskbar
    assert popup_position((1900, 1070), (240, 100), area) == (1660, 940)


def test_tray_brightness_window_opens_changes_and_closes(tmp_path):
    tk = pytest.importorskip("tkinter")
    try:
        tk.Tk().destroy()
    except tk.TclError:
        pytest.skip("no desktop to open a window on")
    import time
    from led_mon.brightness_popup import BrightnessControl, TrayBrightnessPopup
    brightness = app_settings.Brightness(str(tmp_path / "settings.json"))
    popup = TrayBrightnessPopup(brightness)
    popup.show()
    found = {}

    def slide():
        controls = [w for w in popup.window.winfo_children()[0].winfo_children() if isinstance(w, BrightnessControl)]
        controls[0].scale.set(40)
        found["label"] = controls[0].value_label.cget("text")
    deadline = time.monotonic() + 10
    while popup.window is None and time.monotonic() < deadline:
        time.sleep(0.05)
    popup.calls.put(slide)
    popup.calls.put(popup._close)
    while "label" not in found and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)
    popup.stop()
    assert found["label"] == "40%"
    assert app_settings.Brightness(str(tmp_path / "settings.json")).get() == {"follow_screen": False, "level": 40}
