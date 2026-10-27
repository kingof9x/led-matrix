# Layout editor: converting between pages and the config's quadrant lists, and the control channel
import os
import uuid

import pytest
import yaml

from led_mon import control, platform_utils
from led_mon.editor import catalog, layout as L
from led_mon.editor.layout import Layout, Page

BUNDLED_CONFIG = os.path.join(platform_utils.package_dir(), "config.yaml")


def names(page):
    return tuple(a["name"] if a else None for a in (page.full, page.top, page.bottom))


def test_bundled_config_becomes_pages():
    with open(BUNDLED_CONFIG) as f:
        layout, warnings = L.from_config(yaml.safe_load(f))
    assert warnings == []
    assert [names(p) for p in layout.pages["left"]] == [
        ("time", None, None), ("equalizer", None, None), (None, "cpu", "mem-bat"), ("snap", None, None)]
    assert [names(p) for p in layout.pages["right"]] == [
        ("weather", None, None), ("equalizer", None, None), (None, "temp", "fan"), ("snap", None, None)]
    assert layout.settings == {"duration": 10}


def test_round_trip_keeps_pages_and_settings():
    with open(BUNDLED_CONFIG) as f:
        layout, _ = L.from_config(yaml.safe_load(f))
    layout.settings["left-port"] = "COM4"
    text = L.dump(L.to_config(layout))
    again, warnings = L.from_config(yaml.safe_load(text))
    assert warnings == []
    assert again.settings == {"duration": 10, "left-port": "COM4"}
    for side in L.SIDES:
        assert [names(p) for p in again.pages[side]] == [names(p) for p in layout.pages[side]]
        assert [p.duration for p in again.pages[side]] == [p.duration for p in layout.pages[side]]


def test_pages_keep_top_and_bottom_lists_in_step():
    layout = Layout()
    layout.pages["left"] = [
        Page(duration=20, full=catalog.new_app("time", "left")),
        Page(duration=40, top=catalog.new_app("cpu", "left")),
        Page(duration=5, bottom=catalog.new_app("net", "left")),
    ]
    quads = L.to_config(layout)["quadrants"]
    top, bottom = quads["top-left"], quads["bottom-left"]
    assert [a["duration"] for a in top] == [a["duration"] for a in bottom] == [20, 40, 5]
    assert top[0]["scope"] == "panel" and bottom[0]["display"] is False
    assert top[2]["display"] is False and bottom[2]["name"] == "net"
    # Placeholder names are unique within each quadrant
    for apps in quads.values():
        assert len({a["name"] for a in apps}) == len(apps)
        # The monitor's --list-apps expects the "app" key of the YAML list items
        assert all("app" in a for a in apps)


def test_side_specific_args_follow_the_panel():
    layout = Layout()
    layout.pages["right"][0].full = catalog.new_app("equalizer", "left")
    entry = L.to_config(layout)["quadrants"]["top-right"][0]
    assert entry["args"]["side"] == "right"
    assert entry["persistent-draw"] is True and entry["dispose-fn"] == "equalizer_dispose"


def test_same_app_twice_in_a_quadrant_is_rejected(tmp_path):
    layout = Layout()
    layout.pages["left"] = [Page(top=catalog.new_app("cpu", "left")), Page(top=catalog.new_app("cpu", "left"))]
    assert L.validate(layout)
    with pytest.raises(ValueError):
        L.save(layout, str(tmp_path / "config-local.yaml"))
    # Top and bottom are separate quadrants, so this is fine
    layout.pages["left"] = [Page(top=catalog.new_app("cpu", "left"), bottom=catalog.new_app("cpu", "left"))]
    assert L.validate(layout) == []


def test_save_keeps_a_backup(tmp_path):
    path = tmp_path / "config-local.yaml"
    path.write_text("duration: 3\n")
    L.save(Layout(), str(path))
    assert (tmp_path / "config-local.yaml.bak").read_text() == "duration: 3\n"
    assert yaml.safe_load(path.read_text())["quadrants"]["top-left"][0]["display"] is False


def test_uneven_quadrant_lists_are_paired_with_a_warning():
    config = {"duration": 10, "quadrants": {
        "top-left": [{"app": None, "name": "cpu"}, {"app": None, "name": "net"}],
        "bottom-left": [{"app": None, "name": "disk"}],
        "top-right": [], "bottom-right": []}}
    layout, warnings = L.from_config(config)
    assert warnings
    assert [names(p) for p in layout.pages["left"]] == [(None, "cpu", "disk"), (None, "net", None)]


def test_snapshot_files_skip_raw_files():
    files = catalog.snapshot_files("left")
    assert "zigzag.json" in files
    assert not any(f.endswith("_raw.json") for f in files)


def unique_control_address(monkeypatch):
    # A listener from an earlier test keeps its pipe; Windows refuses a second pipe with the same name
    name = control.address() + "-" + uuid.uuid4().hex[:8]
    monkeypatch.setattr(control, "address", lambda: name)


def test_control_channel_round_trip(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    unique_control_address(monkeypatch)
    assert control.request({"cmd": "status"}) is None  # nothing running yet
    seen = []

    def handler(message):
        seen.append(message)
        if message["cmd"] == "boom":
            raise RuntimeError("bad")
        return {"echo": message["cmd"]}

    assert control.serve(handler) is not None
    assert control.request({"cmd": "status"}) == {"echo": "status", "ok": True}
    assert control.request({"cmd": "boom"})["ok"] is False
    assert [m["cmd"] for m in seen] == ["status", "boom"]


def test_preview_renders_like_the_monitor():
    pytest.importorskip("numpy")
    pytest.importorskip("psutil")
    try:
        from led_mon.editor import preview
    except Exception as e:  # plugins need the full dependencies (audio libraries, scipy)
        pytest.skip(f"full dependencies not installed: {e}")
    renderer = preview.PreviewRenderer()
    cpu_page = Page(top=catalog.new_app("cpu", "left"))
    grid = renderer.render(cpu_page, "left")
    assert grid.shape == (9, 34)
    # The CPU app's grid border is drawn in the top half only
    assert grid[:, 0].all() and not grid[:, 33].any()
    eq_page = Page(full=catalog.new_app("equalizer", "left"))
    assert renderer.render(eq_page, "left").any()


def test_monitor_shows_preview_and_reloads(monkeypatch, tmp_path):
    """Drive the monitor's main loop with fake panels through the control channel, as the editor does."""
    pytest.importorskip("numpy")
    pytest.importorskip("psutil")
    try:
        from led_mon import led_system_monitor as monitor
    except Exception as e:
        pytest.skip(f"full dependencies not installed: {e}")
    import queue
    import threading
    import time
    import types
    import numpy as np

    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    unique_control_address(monkeypatch)
    drawn = {"left": queue.Queue(), "right": queue.Queue()}

    class FakeDrawingThread:
        def __init__(self, location, input_queue):
            self.side = "left" if location == "1-1" else "right"
            self.input_queue = input_queue

        def set_animate(self, animate):
            pass

        def start(self):
            def run():
                while (item := self.input_queue.get()) is not None:
                    drawn[self.side].put(item[0].copy())
            threading.Thread(target=run, daemon=True).start()

    def latest(side, wait=1.5):
        time.sleep(wait)
        frame = None
        while not drawn[side].empty():
            frame = drawn[side].get()
        return frame

    monkeypatch.setattr(monitor, "DrawingThread", FakeDrawingThread)
    monkeypatch.setattr(monitor, "find_led_devices", lambda: [("1-1", "PANEL0"), ("1-2", "PANEL1")])
    monkeypatch.setattr(monitor, "IS_WINDOWS", True)  # the control channel is only started on Windows
    monkeypatch.setattr(monitor, "use_tray", lambda args: False)
    monitor.shutdown_event.clear()

    config = str(tmp_path / "config-local.yaml")
    layout = Layout()
    layout.pages["left"][0].top = catalog.new_app("cpu", "left")
    L.save(layout, config)
    args = types.SimpleNamespace(no_key_listener=True, config_file=config, list_apps=False, no_tray=True)
    thread = threading.Thread(target=monitor.app, args=(args, [], []), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while (status := control.request({"cmd": "status"})) is None and time.monotonic() < deadline:
            time.sleep(0.1)
        assert status["ports"] == ["PANEL0", "PANEL1"]
        assert latest("left")[:, 0].all()  # the CPU app's border

        frame = np.zeros((9, 34), dtype=int)
        frame[4, :] = 200
        assert control.request({"cmd": "preview", "frames": {"left": frame.tolist()}, "seconds": 30})["ok"]
        assert (latest("left") == frame).all()
        assert control.request({"cmd": "resume"})["ok"]
        assert not (latest("left") == frame).all()

        layout.pages["left"][0].top = None
        L.save(layout, config)
        assert control.request({"cmd": "reload"})["ok"]
        assert not latest("left").any()  # now an empty page
    finally:
        monitor.shutdown_event.set()
        thread.join(timeout=5)
        monitor.shutdown_event.clear()


def test_classic_preset_is_a_valid_layout():
    layout = Layout(pages=L.preset_pages("Classic monitor"), settings={"duration": 10, "left-port": "COM4"})
    assert L.validate(layout) == []
    config = L.to_config(layout)
    assert [a["name"] for a in config["quadrants"]["top-left"]] == ["cpu"]
    assert [a["name"] for a in config["quadrants"]["bottom-left"]] == ["mem-bat"]
    assert [a["name"] for a in config["quadrants"]["top-right"]] == ["disk"]
    assert [a["name"] for a in config["quadrants"]["bottom-right"]] == ["net"]
    assert config["left-port"] == "COM4"
    again, warnings = L.from_config(config)
    assert warnings == []
    assert again.pages["left"][0].top["name"] == "cpu"


def test_port_box_saves_usb_location_when_known():
    pytest.importorskip("tkinter")
    try:
        from led_mon.editor.window import build_port_choices, AUTO_PORT
    except ImportError as e:
        pytest.skip(f"full dependencies not installed: {e}")
    ports = [("COM4", "1-3.2"), ("COM5", None)]
    choices, current = build_port_choices(ports)
    assert current == AUTO_PORT
    assert choices["COM4 (USB 1-3.2)"] == ("COM4", "1-3.2")
    assert choices["COM5"] == ("COM5", None)

    # A saved location shows as the panel now at that position
    choices, current = build_port_choices([("COM6", "1-3.2")], location_setting="1-3.2")
    assert current == "COM6 (USB 1-3.2)"

    # A saved location with no panel there is still shown, and kept if chosen again
    choices, current = build_port_choices([], location_setting="1-4.2")
    assert choices[current] == (None, "1-4.2")

    # An existing port pin is kept as a port pin
    choices, current = build_port_choices(ports, port_setting="com4")
    assert current == "COM4 (USB 1-3.2)"
    assert choices[current] == ("COM4", None)
    choices, current = build_port_choices([], port_setting="COM9")
    assert choices[current] == ("COM9", None)
