# The temperature and fan source setting (sensor_sources.py) and the Libre Hardware Monitor reader
import json
import time
from types import SimpleNamespace

import pytest

from led_mon import sensor_sources, app_settings
from led_mon.sensor_sources import (SensorSource, AUTO, FRAMEWORK_CONTROL, LIBRE_HARDWARE_MONITOR, FRAMEWORK_TOOL,
                                    describe, diagnose_lines)
from led_mon.libre_hardware_monitor_sensors import parse_sensors, LibreHardwareMonitorSensors, read_once as lhm_read_once
from led_mon import framework_control_sensors, framework_tool

from test_framework_tool import FC_FW16, SAMPLE_FW16, wait_for


class FakeReader:
    """Stands in for a source's cached background reader."""

    def __init__(self, temps=(), fans=(), answered=True, error=""):
        self.values = (list(temps), list(fans))
        self.answered = answered
        self.error = error
        self.calls = 0

    def get(self):
        self.calls += 1
        return self.values


def make(tmp_path, choice=None, **readers):
    path = str(tmp_path / "settings.json")
    if choice is not None:
        app_settings.save_settings({"sensor_source": choice}, path)
    defaults = {FRAMEWORK_CONTROL: FakeReader(), LIBRE_HARDWARE_MONITOR: FakeReader(), FRAMEWORK_TOOL: FakeReader()}
    defaults.update(readers)
    return SensorSource(path, readers=defaults), defaults, path


# ---------------------------------------------------------------------------
# The choice

def test_default_is_automatic(tmp_path):
    source, _, _ = make(tmp_path)
    assert source.choice == AUTO
    assert app_settings.DEFAULT_SETTINGS["sensor_source"] == AUTO


def test_unknown_choice_falls_back_to_automatic(tmp_path):
    source, _, _ = make(tmp_path, "open_hardware_monitor")
    assert source.choice == AUTO


def test_set_saves_to_settings_json_and_keeps_other_settings(tmp_path):
    path = str(tmp_path / "settings.json")
    app_settings.save_settings({"brightness_level": 33}, path)
    source = SensorSource(path, readers={})
    source.set(LIBRE_HARDWARE_MONITOR)
    with open(path, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["sensor_source"] == LIBRE_HARDWARE_MONITOR
    assert saved["brightness_level"] == 33
    assert SensorSource(path, readers={}).choice == LIBRE_HARDWARE_MONITOR


def test_reload_picks_up_the_windows_change(tmp_path):
    source, _, path = make(tmp_path, AUTO)
    app_settings.save_settings({"sensor_source": FRAMEWORK_TOOL}, path)
    assert source.reload() == FRAMEWORK_TOOL
    assert source.choice == FRAMEWORK_TOOL


def test_every_source_has_a_label_and_help():
    for key, label in sensor_sources.SOURCES:
        assert label
        assert sensor_sources.HELP[key]
    assert "admin" in sensor_sources.HELP[LIBRE_HARDWARE_MONITOR]
    assert "admin" in sensor_sources.HELP[FRAMEWORK_TOOL]
    assert "no admin" in sensor_sources.HELP[FRAMEWORK_CONTROL]


# ---------------------------------------------------------------------------
# Each fixed choice reads only that source

@pytest.mark.parametrize("choice", [FRAMEWORK_CONTROL, LIBRE_HARDWARE_MONITOR, FRAMEWORK_TOOL])
def test_fixed_choice_reads_only_that_source(tmp_path, choice):
    readers = {key: FakeReader([40.0 + i], [1000 + i]) for i, key in enumerate(sensor_sources.AUTO_ORDER)}
    source, readers, _ = make(tmp_path, choice, **readers)
    temps, fans = source.readings()
    assert (temps, fans) == readers[choice].values
    for key, reader in readers.items():
        assert reader.calls == (1 if key == choice else 0)
    assert source.temps_from == choice and source.fans_from == choice


def test_fixed_choice_does_not_fall_back(tmp_path):
    source, readers, _ = make(tmp_path, LIBRE_HARDWARE_MONITOR,
                              **{FRAMEWORK_CONTROL: FakeReader([44.0], [0, 0]),
                                 LIBRE_HARDWARE_MONITOR: FakeReader([], [], error="not running")})
    assert source.readings() == ([], [])
    assert readers[FRAMEWORK_CONTROL].calls == 0
    assert source.temps_from is None


# ---------------------------------------------------------------------------
# Automatic

def test_auto_uses_framework_control_first_and_stops_there(tmp_path):
    source, readers, _ = make(tmp_path, AUTO, **{FRAMEWORK_CONTROL: FakeReader([44.0], [0, 0]),
                                                 LIBRE_HARDWARE_MONITOR: FakeReader([50.0], [9])})
    assert source.readings() == ([44.0], [0, 0])
    assert readers[LIBRE_HARDWARE_MONITOR].calls == 0
    assert readers[FRAMEWORK_TOOL].calls == 0


def test_auto_fills_fans_from_the_next_source(tmp_path):
    source, readers, _ = make(tmp_path, AUTO, **{LIBRE_HARDWARE_MONITOR: FakeReader([55.0, 38.0], []),
                                                 FRAMEWORK_TOOL: FakeReader([30.0], [1873, 1902])})
    assert source.readings() == ([55.0, 38.0], [1873, 1902])
    assert source.temps_from == LIBRE_HARDWARE_MONITOR
    assert source.fans_from == FRAMEWORK_TOOL


def test_auto_waits_for_each_sources_first_answer(tmp_path):
    source, readers, _ = make(tmp_path, AUTO, **{FRAMEWORK_CONTROL: FakeReader(answered=False),
                                                 FRAMEWORK_TOOL: FakeReader([30.0], [1])})
    assert source.readings() == ([], [])
    assert readers[LIBRE_HARDWARE_MONITOR].calls == 0
    readers[FRAMEWORK_CONTROL].answered = True
    readers[LIBRE_HARDWARE_MONITOR].answered = False
    assert source.readings() == ([], [])
    assert readers[FRAMEWORK_TOOL].calls == 0
    readers[LIBRE_HARDWARE_MONITOR].answered = True
    assert source.readings() == ([30.0], [1])


def test_auto_nothing_available(tmp_path):
    source, _, _ = make(tmp_path, AUTO)
    assert source.readings() == ([], [])
    assert source.temps_from is None and source.fans_from is None


# ---------------------------------------------------------------------------
# Status shown in the layout editor

def test_status_and_describe(tmp_path):
    source, _, _ = make(tmp_path, AUTO, **{FRAMEWORK_CONTROL: FakeReader([], [], error="not installed"),
                                           LIBRE_HARDWARE_MONITOR: FakeReader([55.0, 38.0], []),
                                           FRAMEWORK_TOOL: FakeReader([], [], error="needs led_mon to run as admin")})
    assert describe(source.status()).startswith("No panel is showing")
    source.readings()
    status = source.status()
    assert status["reading"] and status["temps"] == 2 and status["fans"] == 0
    text = describe(status)
    assert "Temperatures: 2 from Libre Hardware Monitor" in text
    assert "Fans: none" in text
    assert "framework_tool: needs led_mon to run as admin" in text
    assert describe(None) == "The monitor isn't running."


def test_status_goes_stale_when_nothing_reads(tmp_path, monkeypatch):
    source, _, _ = make(tmp_path, FRAMEWORK_CONTROL, **{FRAMEWORK_CONTROL: FakeReader([44.0], [0, 0])})
    source.readings()
    assert source.status()["reading"]
    source._last_read -= sensor_sources.STALE_SEC + 1
    assert not source.status()["reading"]


def test_status_is_json_safe(tmp_path):
    source, _, _ = make(tmp_path, AUTO, **{FRAMEWORK_CONTROL: FakeReader([44.0], [0, 0])})
    source.readings()
    json.dumps(source.status())


# ---------------------------------------------------------------------------
# --diagnose, with each source's real parser behind a fake read

def test_diagnose_lines_report_each_source_and_the_choice(tmp_path):
    path = str(tmp_path / "settings.json")
    app_settings.save_settings({"sensor_source": AUTO}, path)

    def lhm_missing():
        raise RuntimeError("namespace not found")

    read_once = {
        FRAMEWORK_CONTROL: lambda: framework_control_sensors.read_once(lambda: FC_FW16),
        LIBRE_HARDWARE_MONITOR: lhm_missing,
        FRAMEWORK_TOOL: lambda: framework_tool.read_once(lambda: SAMPLE_FW16),
    }
    original = sensor_sources.default_readers
    sensor_sources.default_readers = lambda: {
        FRAMEWORK_CONTROL: framework_control_sensors.FrameworkControlSensors(fetcher=lambda: FC_FW16),
        LIBRE_HARDWARE_MONITOR: FakeReader(),
        FRAMEWORK_TOOL: FakeReader(),
    }
    try:
        text = "\n".join(diagnose_lines(read_once, path, wait_sec=3))
    finally:
        sensor_sources.default_readers = original
    assert "Chosen source: Automatic (auto)" in text
    assert "Framework Control: 4 temperatures [44, 43, 41, 40], 2 fans [0, 0]" in text
    assert "Libre Hardware Monitor: not available (RuntimeError: namespace not found)" in text
    assert "framework_tool: 5 temperatures" in text
    assert "Panels get: temperatures from Framework Control, fans from Framework Control" in text


def test_read_once_errors():
    with pytest.raises(RuntimeError):
        framework_control_sensors.read_once(lambda: None)
    with pytest.raises(FileNotFoundError):
        framework_tool.read_once(lambda: None)


# ---------------------------------------------------------------------------
# Libre Hardware Monitor (WMI Sensor rows)

def row(parent, name, kind, value, index=0):
    return SimpleNamespace(Parent=parent, Identifier=f"{parent}/{kind.lower()}/{index}", Name=name,
                           SensorType=kind, Value=value, Index=index)


# Shaped like Libre Hardware Monitor's WMI rows on an AMD laptop with an NVMe drive
LHM_ROWS = [
    row("/nvme/0", "Composite Temperature", "Temperature", 38.0, 0),
    row("/nvme/0", "Temperature #1", "Temperature", 41.0, 1),
    row("/amdcpu/0", "Core #1", "Temperature", 50.0, 3),
    row("/amdcpu/0", "Core #2", "Temperature", 52.0, 4),
    row("/amdcpu/0", "Core (Tctl/Tdie)", "Temperature", 55.0, 2),
    row("/gpu-amd/0", "GPU Core", "Temperature", 47.0, 0),
    row("/gpu-amd/0", "GPU Hot Spot", "Temperature", 0.0, 1),
    row("/lpc/it8xxx/0", "Fan #2", "Fan", 1902.4, 1),
    row("/lpc/it8xxx/0", "Fan #1", "Fan", 1873.0, 0),
    row("/amdcpu/0", "Core #1", "Load", 12.0, 0),
    row("/amdcpu/0", "Broken", "Temperature", None, 9),
]


def test_lhm_parse_orders_cpu_gpu_drives_and_limits_per_hardware():
    temps, fans = parse_sensors(LHM_ROWS)
    # CPU: Tctl/Tdie first, then one core (two per hardware); GPU core (hot spot 0 skipped); NVMe composite first
    assert temps == [55.0, 50.0, 47.0, 38.0, 41.0]
    assert fans == [1873, 1902]


def test_lhm_parse_dict_rows_and_nothing():
    assert parse_sensors([]) == ([], [])
    assert parse_sensors([{"Parent": "/amdcpu/0", "Name": "Core (Tctl/Tdie)", "SensorType": "Temperature",
                           "Value": 60, "Index": 0}]) == ([60.0], [])


def test_lhm_read_once_raises_when_not_running():
    def query():
        raise OSError("Invalid namespace")
    with pytest.raises(OSError):
        lhm_read_once(query)


def test_lhm_reader_uses_query_and_reports_missing_app():
    reader = LibreHardwareMonitorSensors(query=lambda: LHM_ROWS)
    assert not reader.answered
    temps, fans = wait_for(reader, lambda v: v[0])
    assert fans == [1873, 1902]
    assert reader.answered and reader.error == ""

    def query():
        raise OSError("Invalid namespace")
    missing = LibreHardwareMonitorSensors(query=query)
    missing.get()
    end = time.monotonic() + 2
    while not missing.answered and time.monotonic() < end:
        time.sleep(0.01)
    assert missing.get() == ([], [])
    assert "not running" in missing.error


def test_lhm_reader_waits_longer_after_a_failure():
    calls = []

    def query():
        calls.append(1)
        raise OSError("Invalid namespace")
    reader = LibreHardwareMonitorSensors(query=query)
    reader.get()
    time.sleep(0.1)
    # Past the normal 2 s cache, but within the 10 s retry wait: not asked again
    reader._last_refresh -= 3
    reader.get()
    time.sleep(0.1)
    assert len(calls) == 1


def test_lhm_reader_says_when_it_sees_no_sensors():
    reader = LibreHardwareMonitorSensors(query=lambda: [])
    reader.get()
    end = time.monotonic() + 2
    while not reader.answered and time.monotonic() < end:
        time.sleep(0.01)
    assert "admin" in reader.error


# ---------------------------------------------------------------------------
# framework_tool reader's new answered/error state

def test_framework_tool_answered_and_error():
    def needs_admin():
        error = OSError("elevation")
        error.winerror = 740
        raise error
    thermal = framework_tool.FrameworkToolThermal(runner=needs_admin)
    assert not thermal.answered
    thermal.get()
    end = time.monotonic() + 2
    while not thermal.answered and time.monotonic() < end:
        time.sleep(0.01)
    assert thermal.error == "needs led_mon to run as admin"


def test_new_modules_are_in_the_exe_spec():
    import os
    spec = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "led_mon.spec"),
                encoding="utf-8").read()
    for module in ("led_mon.sensor_sources", "led_mon.framework_control_sensors",
                   "led_mon.libre_hardware_monitor_sensors", "led_mon.framework_tool"):
        assert f"'{module}'" in spec
