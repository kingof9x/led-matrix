# The CPU graph, GPU graph and disk + network apps, and the cpu app showing every core
import sys
import logging
import importlib

import pytest

np = pytest.importorskip("numpy")
psutil = pytest.importorskip("psutil")

from led_mon import gpu
from led_mon.editor import catalog
from led_mon.platform_utils import IS_WINDOWS

if IS_WINDOWS:
    # led_mon.monitors needs it on Windows; the quick CI test job doesn't install it, the build job does
    pytest.importorskip("wmi")


@pytest.fixture
def drawing(monkeypatch):
    # Without the plugins, which need the audio libraries
    monkeypatch.setattr(sys, "argv", ["led_mon", "--disable-plugins"])
    return importlib.import_module("led_mon.drawing")


@pytest.fixture
def app_functions(drawing):
    from led_mon.rendering import make_base_app_functions
    from led_mon.monitors import CPUMonitor, MemoryMonitor, BatteryMonitor, DiskMonitor, NetworkMonitor
    return make_base_app_functions(CPUMonitor(), MemoryMonitor(), BatteryMonitor(), DiskMonitor(), NetworkMonitor())


def blank():
    return np.zeros((9, 34), dtype=int)


def test_busiest_engine_per_gpu_like_task_manager():
    values = {
        "pid_10_luid_0x00000000_0x0000D1F2_phys_0_eng_0_engtype_3D": 30.0,
        "pid_20_luid_0x00000000_0x0000D1F2_phys_0_eng_0_engtype_3D": 25.0,
        "pid_20_luid_0x00000000_0x0000D1F2_phys_0_eng_3_engtype_VideoDecode": 40.0,
        "pid_30_luid_0x00000001_0x0000A000_phys_0_eng_0_engtype_Copy": 90.0,
        "pid_30_luid_0x00000001_0x0000A000_phys_0_eng_1_engtype_Copy": 30.0,
        "not a gpu engine": 99.0,
    }
    assert gpu.busiest_engines(values) == {(0, 0xD1F2): 55.0, (1, 0xA000): 100.0}


def test_identical_gpus_get_distinct_names():
    assert gpu.unique_names(["A", "B", "A", "A"]) == ["A", "B", "A #2", "A #3"]


def test_gpu_monitor_never_raises_and_picks_the_named_gpu():
    m = gpu.GpuMonitor()
    rows = m.read_all()
    assert isinstance(rows, list)
    m._last = (float("inf"), [("iGPU", 0.25), ("dGPU", 0.75)])
    assert m.names() == ["iGPU", "dGPU"]
    assert m.get("dGPU") == 0.75
    assert m.get(None) == 0.25
    assert m.get("unplugged eGPU") == 0.25


@pytest.mark.skipif(not IS_WINDOWS, reason="DXGI and performance counters are Windows only")
def test_windows_gpu_reader_runs():
    pytest.importorskip("win32pdh")
    adapters = gpu._dxgi_adapters()
    assert all(isinstance(name, str) and len(luid) == 2 for name, luid in adapters)
    reader = gpu.WindowsGpuReader()
    usage = reader.utilization()
    assert all(0 <= v <= 100 for v in usage.values())


def test_core_groups_cover_every_logical_cpu():
    from led_mon.monitors import cpu_core_groups
    groups = cpu_core_groups()
    flat = sorted(i for g in groups for i in g)
    assert flat == list(range(psutil.cpu_count()))
    physical = psutil.cpu_count(logical=False)
    if physical:
        assert len(groups) == physical


def test_core_groups_pair_neighbours_when_topology_is_unknown(monkeypatch):
    from led_mon import monitors
    monkeypatch.setattr(monitors.os, "name", "nt")
    monkeypatch.setattr(monitors.psutil, "cpu_count", lambda logical=True: 24 if logical else 12)
    groups = monitors.cpu_core_groups()
    assert len(groups) == 12 and groups[0] == [0, 1] and groups[-1] == [22, 23]


def test_cpu_app_shows_all_twelve_cores(drawing):
    for y in (0, 16):
        grid = blank()
        values = [1.0] * 12
        drawing.draw_app("cpu", grid, values, 160, y)
        drawing.draw_app_border("cpu", grid, 35, y)
        lit_rows = [r for r in range(34) if (grid[1:8, r] == 160).all()]
        assert len(lit_rows) == 12
        # The spiral grid's divider lines aren't drawn over the bars
        assert (grid[1:8, y + 4] == 160).all()


def test_cpu_app_keeps_spirals_for_eight_cores(drawing):
    grid = blank()
    drawing.draw_app("cpu", grid, [1.0] * 8, 160, 0)
    drawing.draw_app_border("cpu", grid, 35, 0)
    assert (grid[:, 4] == 35).all()  # the 2 x 4 grid lines


def test_history_graph_rows_newest_first(drawing):
    grid = blank()
    drawing.draw_app("cpu-graph", grid, [1.0, 0.5, None, 0.0], 160, 0)
    assert (grid[1:8, 1] == 160).all()
    assert list(grid[1:5, 2]) == [160, 160, 160, 80]  # 3.5 pixels: the last one half as bright
    assert not grid[:, 3].any() and not grid[:, 4].any()


def test_history_monitor_samples_once_per_interval():
    from led_mon.monitors import HistoryMonitor
    readings = iter([0.5, 2.0, None])
    m = HistoryMonitor(lambda: next(readings), interval=0, size=2)
    assert m.get() == [0.5]
    assert m.get() == [1.0, 0.5]  # clamped
    assert m.get() == [None, 1.0]  # no reading, and only the last two kept
    slow = HistoryMonitor(lambda: 0.3, interval=3600)
    assert slow.get() == slow.get() == [0.3]


def test_disk_net_bars(drawing):
    for y in (0, 16):
        grid = blank()
        drawing.draw_app("disk-net", grid, (1.0, 0.0), (0.0, 1.0), 160, y)
        rows = list(drawing.half_rows(y))
        assert (grid[1:4, rows[0]:rows[6] + 1] == 160).all()      # disk read, full
        assert not grid[5:8, rows[0]:rows[6] + 1].any()          # disk write, idle
        assert not grid[1:4, rows[8]:rows[-1] + 1].any()         # upload, idle
        assert (grid[5:8, rows[8]:rows[-1] + 1] == 160).all()     # download, full


@pytest.mark.parametrize("name", ["cpu-graph", "gpu-graph", "disk-net", "cpu"])
def test_new_apps_render_in_both_halves_without_errors(name, app_functions, caplog):
    from led_mon.rendering import render_panel
    for app in ({"name": name}, {"name": name, "args": {"border": False}}, catalog.new_app(name, "left")):
        grid = blank()
        with caplog.at_level(logging.ERROR):
            render_panel(grid, [dict(app), dict(app)], app_functions, 160, 35)
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_editor_offers_the_new_apps():
    names = [a.name for a in catalog.available()]
    for name in ("cpu-graph", "gpu-graph", "disk-net"):
        assert name in names and catalog.get(name).size == catalog.HALF
    # No GPU picked yet means the first GPU found, so no args are written
    assert catalog.new_app("gpu-graph", "left") == {"name": "gpu-graph"}
    options = catalog.gpu_options()
    assert options[0] == ("", catalog.FIRST_GPU)


def test_new_apps_have_id_patterns(drawing):
    for name in ("cpu-graph", "gpu-graph", "disk-net"):
        assert drawing.id_patterns[name].shape == (7, 15)
