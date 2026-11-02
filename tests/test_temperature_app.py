# The Temperatures app: three numbers, each from a chosen sensor, in C or F; and how often sensors are read
import time
from types import SimpleNamespace

import numpy as np
import pytest

from led_mon import sensor_sources, app_settings, framework_control_sensors, framework_tool
from led_mon import libre_hardware_monitor_sensors as lhm
from led_mon.sensor_sources import pick, to_unit, SensorSource, FRAMEWORK_CONTROL, LIBRE_HARDWARE_MONITOR, FRAMEWORK_TOOL
from led_mon.patterns import digits_3_x_5
from led_mon.editor import catalog

from test_framework_tool import FC_FW16, SAMPLE_FW16, wait_for
from test_sensor_sources import LHM_ROWS, FakeReader


# ---------------------------------------------------------------------------
# Picking the three sensors

NAMED = [("cpu@4c", 44.0), ("apu_f75303@4d", 43.0), ("ambient_f75303@4d", 41.0), ("charger_f75303@4d", 40.0)]


def test_pick_automatic_takes_the_first_three():
    assert pick(NAMED, []) == [44.0, 43.0, 41.0]
    assert pick(NAMED, None) == [44.0, 43.0, 41.0]


def test_pick_chosen_sensors_and_fills_blanks_with_unused_ones():
    assert pick(NAMED, ["charger_f75303@4d", "", "cpu@4c"]) == [40.0, 43.0, 44.0]
    assert pick(NAMED, ["", "", "apu_f75303@4d"]) == [44.0, 41.0, 43.0]


def test_pick_missing_sensor_and_too_few_sensors():
    assert pick(NAMED, ["gpu_vr_f75303@4d"]) == [None, 44.0, 43.0]
    assert pick(NAMED[:1], []) == [44.0, None, None]
    assert pick([], ["cpu@4c", "", ""]) == [None, None, None]


def test_unit_conversion():
    assert to_unit(100, "C") == 100
    assert to_unit(100, "F") == 212
    assert to_unit(44, "f") == pytest.approx(111.2)


# ---------------------------------------------------------------------------
# Sensor names from each source

def test_framework_control_names():
    assert framework_control_sensors.named_temps(FC_FW16) == NAMED


def test_framework_tool_names():
    names = framework_tool.named_temps(SAMPLE_FW16)
    assert names == [("F75303_Local", 43.0), ("F75303_CPU", 51.0), ("F75303_DDR", 40.0), ("APU", 58.0),
                     ("dGPU AMB", 38.0)]
    # Two sensors with the same name stay apart
    assert framework_tool.named_temps("  Fan Temp: 40 C\n  Fan Temp: 41 C\n") == [("Fan Temp", 40.0), ("Fan Temp 2", 41.0)]


def test_libre_hardware_monitor_names_list_every_sensor():
    names = lhm.named_temps(LHM_ROWS)
    assert names[0] == ("CPU: Core (Tctl/Tdie)", 55.0)
    assert [n for n, _ in names] == ["CPU: Core (Tctl/Tdie)", "CPU: Core #1", "CPU: Core #2", "GPU: GPU Core",
                                     "SSD: Composite Temperature", "SSD: Temperature #1"]
    second = [SimpleNamespace(Parent="/nvme/1", Name="Composite", SensorType="Temperature", Value=33, Index=0)]
    assert lhm.named_temps(second) == [("SSD 2: Composite", 33.0)]


def test_readers_keep_names():
    reader = framework_control_sensors.FrameworkControlSensors(fetcher=lambda: FC_FW16)
    wait_for(reader, lambda v: v[0])
    assert reader.named() == NAMED
    tool = framework_tool.FrameworkToolThermal(runner=lambda: SAMPLE_FW16)
    wait_for(tool, lambda v: v[0])
    assert tool.named()[3] == ("APU", 58.0)
    libre = lhm.LibreHardwareMonitorSensors(query=lambda: LHM_ROWS)
    wait_for(libre, lambda v: v[0])
    assert libre.named()[0] == ("CPU: Core (Tctl/Tdie)", 55.0)


class NamedReader(FakeReader):
    def __init__(self, named, fans=()):
        super().__init__([v for _, v in named], fans)
        self._named = named

    def named(self):
        return list(self._named)


def test_source_names_come_from_where_the_temperatures_come_from(tmp_path):
    path = str(tmp_path / "settings.json")
    readers = {FRAMEWORK_CONTROL: FakeReader([], []), LIBRE_HARDWARE_MONITOR: NamedReader([("GPU: GPU Core", 47.0)]),
               FRAMEWORK_TOOL: NamedReader([("APU", 58.0)], [1000])}
    source = SensorSource(path, readers=readers)
    source.readings()
    assert source.named_temps() == [("GPU: GPU Core", 47.0)]
    source.set(FRAMEWORK_TOOL)
    source.readings()
    assert source.named_temps() == [("APU", 58.0)]
    source.readings()
    assert source.status()["sensors"] == ["APU"]


# ---------------------------------------------------------------------------
# How often sensors are read

def test_refresh_setting_saved_and_given_to_every_reader(tmp_path):
    path = str(tmp_path / "settings.json")
    readers = {key: FakeReader() for key in sensor_sources.AUTO_ORDER}
    source = SensorSource(path, readers=readers)
    assert source.refresh == sensor_sources.DEFAULT_REFRESH
    source.set_refresh(10)
    assert app_settings.load_settings(path)["sensor_refresh_seconds"] == 10
    assert all(reader.interval == 10 for reader in readers.values())
    app_settings.save_settings({"sensor_refresh_seconds": 5}, path)
    source.reload()
    assert all(reader.interval == 5 for reader in readers.values())
    assert sensor_sources.normalize_refresh("bad") == sensor_sources.DEFAULT_REFRESH
    assert sensor_sources.normalize_refresh(0) == 0.5


def test_reader_waits_for_its_interval():
    calls = []

    def fetcher():
        calls.append(1)
        return FC_FW16
    reader = framework_control_sensors.FrameworkControlSensors(fetcher=fetcher)
    reader.interval = 10
    wait_for(reader, lambda v: v[0])
    reader._last_refresh -= 3   # past the old fixed 2 seconds, still within 10
    reader.get()
    time.sleep(0.1)
    assert len(calls) == 1
    reader._last_refresh -= 10
    reader.get()
    time.sleep(0.1)
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Drawing

def plugin():
    pytest.importorskip("psutil")
    import importlib
    try:
        return importlib.import_module("led_mon.plugins.temp_fan_plugin")
    except Exception as e:  # loading drawing.py loads every plugin, which needs the full dependencies
        pytest.skip(f"full dependencies not installed: {e}")


def digit_at(grid, x, top):
    return grid[x:x + 3, top:top + 5].T > 0


def test_three_numbers_in_the_top_half():
    p = plugin()
    grid = np.zeros((9, 34), dtype=int)
    p.draw_temp_numbers(grid, [44, 52.6, 7], 200, 0)
    assert (digit_at(grid, 1, 0) == digits_3_x_5["4"]).all() and (digit_at(grid, 5, 0) == digits_3_x_5["4"]).all()
    assert (digit_at(grid, 1, 6) == digits_3_x_5["5"]).all() and (digit_at(grid, 5, 6) == digits_3_x_5["3"]).all()
    assert (digit_at(grid, 3, 11) == digits_3_x_5["7"]).all()
    # The middle number is dimmer; nothing is drawn in the bottom half
    assert grid[:, 6:11].max() < grid[:, 0:5].max()
    assert not grid[:, 16:].any()


def test_three_digit_fahrenheit_and_missing_reading_in_the_bottom_half():
    p = plugin()
    grid = np.zeros((9, 34), dtype=int)
    p.draw_temp_numbers(grid, [111.2, None, 250], 200, 16)
    assert grid[0, 17:22].all()      # the narrow 1
    assert (digit_at(grid, 2, 17) == digits_3_x_5["1"]).all() and (digit_at(grid, 6, 17) == digits_3_x_5["1"]).all()
    assert (digit_at(grid, 1, 23) == digits_3_x_5["-"]).all()  # no reading: --
    assert (digit_at(grid, 2, 29) == digits_3_x_5["9"]).all()   # 199 at most
    assert not grid[:, :17].any()


def test_temperature_monitor_uses_the_chosen_sensors_and_unit(monkeypatch):
    p = plugin()
    monkeypatch.setattr(p.TemperatureMonitor, "named", staticmethod(lambda: NAMED))
    assert p.temperature_monitor.get(["ambient_f75303@4d", "", ""], "F") == [pytest.approx(105.8), 111.2,
                                                                            pytest.approx(109.4)]
    # The monitor registers each plugin's drawing functions when it loads them
    from led_mon import drawing
    monkeypatch.setitem(drawing.direct_draw_funcs, "temp", p.direct_draw_funcs["temp"])
    grid = np.zeros((9, 34), dtype=int)
    p.draw_temps("temp", grid, 200, 0, sensor_1="charger_f75303@4d", unit="C")
    assert (digit_at(grid, 1, 0) == digits_3_x_5["4"]).all() and (digit_at(grid, 5, 0) == digits_3_x_5["0"]).all()


# ---------------------------------------------------------------------------
# Layout editor settings

def test_temperatures_app_settings():
    info = catalog.get("temp")
    keys = [s.key for s in info.settings]
    assert keys == ["sensor_1", "sensor_2", "sensor_3", "unit", "sensor_source"]
    entry = catalog.new_app("temp", "left")
    # Blank sensors (automatic) aren't written; the source lives in settings.json, not the layout
    assert entry.get("args") == {"unit": "C"}
    assert [s.key for s in catalog.get("fan").settings] == ["sensor_source"]
    assert "sensor_source" not in (catalog.new_app("fan", "left").get("args") or {})
