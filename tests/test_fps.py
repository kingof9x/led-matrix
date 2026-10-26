# The fps app: reading PresentMon's CSV output, and drawing
import sys
import time
import logging
import textwrap
import importlib

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("psutil")

from led_mon import fps
from led_mon.editor import catalog
from led_mon.platform_utils import IS_WINDOWS

if IS_WINDOWS:
    pytest.importorskip("wmi")

HEADER = ["Application", "ProcessID", "SwapChainAddress", "PresentRuntime", "CPUStartTime", "MsBetweenPresents"]


@pytest.fixture
def drawing(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["led_mon", "--disable-plugins"])
    return importlib.import_module("led_mon.drawing")


def stats_with(frames, now=100.0):
    stats = fps.FrameStats()
    stats.add_header(HEADER)
    for app, pid, swapchain, t, ms in frames:
        assert stats.add_row([app, str(pid), swapchain, "DXGI", str(t), str(ms)], now=now)
    return stats


def test_fps_from_the_busiest_swap_chain_over_the_last_second():
    frames = [("game.exe", 7, "0xA", i / 60, 1000 / 60) for i in range(120)]
    frames += [("game.exe", 7, "0xB", i / 10, 100.0) for i in range(20)]
    frames += [("other.exe", 9, "0xC", i / 30, 1000 / 30) for i in range(30)]
    stats = stats_with(frames)
    assert stats.fps(7, now=100.0) == pytest.approx(60)
    assert stats.fps(9, now=100.0) == pytest.approx(30)
    assert stats.pids_named("GAME.EXE") == [7]
    # Stopped presenting: 0
    assert stats.fps(7, now=100.0 + fps.STALE_SECONDS + 1) == 0.0
    assert stats.fps(12345, now=100.0) == 0.0


def test_rows_that_arent_frames_are_ignored():
    stats = fps.FrameStats()
    assert not stats.add_row(["anything"])  # before the header
    stats.add_header(HEADER)
    assert not stats.add_row(["game.exe", "not a pid", "0xA", "DXGI", "1.0", "16.6"])
    assert not stats.add_row(["game.exe"])


def test_draws_digits_graph_and_question_mark(drawing):
    grid = np.zeros((9, 34), dtype=int)
    drawing.draw_fps(grid, 144.4, [144, 72, None], True, 160)
    rows = grid.T
    for slot, digit in enumerate("144"):
        assert (rows[1 + slot * 8:8 + slot * 8, :] == drawing.numerals[digit] * 160).all()
    assert (grid[1:8, 26] == 160).all()           # 144 of 144: full row
    assert list(grid[1:5, 27]) == [160, 160, 160, 80]  # 72 of 144: 3.5 pixels
    assert not grid[:, 28].any()                    # no reading

    grid = np.zeros((9, 34), dtype=int)
    drawing.draw_fps(grid, 60, [], True, 160)
    assert not grid.T[1:8, :].any()                 # leading blank for two digits
    assert (grid.T[9:16, :] == drawing.numerals["6"] * 160).all()

    grid = np.zeros((9, 34), dtype=int)
    drawing.draw_fps(grid, None, [], False, 160)
    assert (grid.T[13:19, :] == drawing.symbols["?"] * 160).all()


FAKE_PRESENTMON = '''
import sys, time
args = sys.argv[1:]
assert "--output_stdout" in args and "--session_name" in args
if "--terminate_existing_session" in args:
    sys.exit(0)
if "MODE" == "denied":
    sys.stderr.write("error: failed to start trace session (access denied).\\n")
    sys.exit(5)
print("Application,ProcessID,SwapChainAddress,PresentRuntime,CPUStartTime,MsBetweenPresents", flush=True)
t = 0.0
while True:
    for _ in range(10):
        t += 0.02
        print(f"game.exe,4242,0x1,DXGI,{t:.4f},20.0", flush=True)
    time.sleep(0.2)
'''


def fake_presentmon(tmp_path, mode):
    script = tmp_path / "fake_presentmon.py"
    script.write_text(textwrap.dedent(FAKE_PRESENTMON).replace("MODE", mode))
    return [sys.executable, str(script)]


@pytest.fixture
def fake_capture(monkeypatch, tmp_path):
    def make(mode, process_name=None):
        cmd = fake_presentmon(tmp_path, mode)
        monkeypatch.setattr(fps, "IS_WINDOWS", True)
        monkeypatch.setattr(fps, "find_presentmon", lambda: cmd[0])
        monkeypatch.setattr(fps, "foreground_process_id", lambda: None)
        capture = fps.PresentMonCapture(process_name)
        # Run the fake through Python, with PresentMon's arguments
        original = capture.command
        monkeypatch.setattr(capture, "command", lambda exe: cmd + original(exe)[1:])
        monkeypatch.setattr(fps.platform_utils, "subprocess_flags", lambda: 0)
        return capture
    return make


def wait_for(condition, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.1)
    return False


def test_capture_by_app_name(fake_capture):
    capture = fake_capture("ok", "Game.exe")
    try:
        assert wait_for(lambda: (capture.get()[0] or 0) > 40)
        value, status = capture.get()
        assert status == fps.OK and value == pytest.approx(50, rel=0.1)
    finally:
        capture.stop()
    assert capture.process is None


def test_capture_follows_the_app_in_front(fake_capture, monkeypatch):
    capture = fake_capture("ok")
    monkeypatch.setattr(fps, "foreground_process_id", lambda: 4242)
    try:
        assert wait_for(lambda: (capture.get()[0] or 0) > 40)
        monkeypatch.setattr(fps, "foreground_process_id", lambda: 1)
        assert capture.get() == (0.0, fps.OK)
    finally:
        capture.stop()


def test_access_denied_shows_no_permission(fake_capture, caplog):
    capture = fake_capture("denied")
    with caplog.at_level(logging.WARNING):
        capture.get()
        assert wait_for(lambda: capture.status == fps.NO_PERMISSION)
    assert capture.get() == (None, fps.NO_PERMISSION)
    assert "Performance Log Users" in caplog.text


def test_not_available_without_presentmon(monkeypatch):
    monkeypatch.setattr(fps, "find_presentmon", lambda: None)
    capture = fps.PresentMonCapture()
    assert capture.get() == (None, fps.NOT_AVAILABLE)


def test_fps_app_renders_without_errors(drawing, caplog, monkeypatch):
    monkeypatch.setattr(fps, "find_presentmon", lambda: None)
    from led_mon.rendering import make_base_app_functions, render_panel
    from led_mon.monitors import CPUMonitor, MemoryMonitor, BatteryMonitor, DiskMonitor, NetworkMonitor
    functions = make_base_app_functions(CPUMonitor(), MemoryMonitor(), BatteryMonitor(), DiskMonitor(), NetworkMonitor())
    for app in ({"name": "fps", "scope": "panel"}, {"name": "fps", "scope": "panel", "args": {"process": "game.exe"}}):
        grid = np.zeros((9, 34), dtype=int)
        with caplog.at_level(logging.ERROR):
            render_panel(grid, [app], functions, 160, 35)
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert grid.any()  # the "?"


def test_editor_offers_the_fps_app(drawing):
    info = catalog.get("fps")
    assert info.size == catalog.FULL
    assert catalog.new_app("fps", "left") == {"name": "fps"}
    assert drawing.id_patterns["fps"].shape == (9, 34)
