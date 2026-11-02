# Parsing of `framework_tool --thermal` output (captured samples) and failure handling
import time

from led_mon.framework_tool import parse_thermal, FrameworkToolThermal

# framework_tool 0.4.x on a Framework 16 (AMD 7040) with the dGPU module
SAMPLE_FW16 = """\
  F75303_Local: 43 C
  F75303_CPU:   51 C
  F75303_DDR:   40 C
  APU:          58 C
  dGPU VR:      NotPowered
  dGPU VRAM:    NotPowered
  dGPU AMB:     38 C
  dGPU temp:    NotPowered
  Left Fan:     1873 RPM
  Right Fan:    1902 RPM
  AP Throttle Status
    Soft (AMD SPPT):  false
    Hard (PROCHOT#):  false
"""

# Older output format
SAMPLE_OLD = """\
  F75303_Local: 45 C
  F75303_CPU:   52 C
  F75303_DDR:   43 C
  APU:          60 C
  Fan Speed:     0 RPM
  Fan Speed:  65534 RPM (Stalled)
"""


def test_parse_fw16_sample():
    temps, fans = parse_thermal(SAMPLE_FW16)
    assert temps == [43, 51, 40, 58, 38]
    assert fans == [1873, 1902]


def test_parse_old_sample_with_stalled_fan():
    temps, fans = parse_thermal(SAMPLE_OLD)
    assert temps == [45, 52, 43, 60]
    assert fans == [0, 0]


def test_parse_windows_line_endings_and_garbage():
    temps, fans = parse_thermal("Failed to read thermal information: Error\r\n  APU:   61 C\r\n")
    assert temps == [61]
    assert fans == []


def wait_for(thermal, predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        values = thermal.get()
        if predicate(values):
            return values
        time.sleep(0.01)
    return thermal.get()


def test_cached_reader_uses_runner_output():
    thermal = FrameworkToolThermal(runner=lambda: SAMPLE_FW16)
    temps, fans = wait_for(thermal, lambda v: v[0])
    assert fans == [1873, 1902]


def test_cached_reader_survives_missing_tool():
    calls = []

    def failing_runner():
        calls.append(1)
        raise FileNotFoundError("framework_tool.exe")

    thermal = FrameworkToolThermal(runner=failing_runner)
    assert thermal.get() == ([], [])
    time.sleep(0.1)
    assert thermal.get() == ([], [])
    # Cached: only one attempt within the cache window
    assert len(calls) == 1


def test_elevation_error_stops_retrying():
    calls = []

    def needs_admin_runner():
        calls.append(1)
        error = OSError("The requested operation requires elevation")
        error.winerror = 740
        raise error

    thermal = FrameworkToolThermal(runner=needs_admin_runner)
    thermal.get()
    time.sleep(0.1)
    # Even once the cache window has passed, the tool isn't started again
    thermal._last_refresh -= 10
    assert thermal.get() == ([], [])
    time.sleep(0.1)
    assert len(calls) == 1


# Framework Control's /api/thermal
from led_mon.framework_control_sensors import parse_thermal as parse_fc_thermal, FrameworkControlSensors

# Captured from Framework Control on a Framework 16 (Ryzen 9 7940HS), GPU module off, fans idle
FC_FW16 = {
    "fans": [{"name": "Left Fan", "rpm": 0}, {"name": "Right Fan", "rpm": 0}],
    "temps": {"ambient_f75303@4d": 41, "apu_f75303@4d": 43, "charger_f75303@4d": 40, "cpu@4c": 44,
              "gpu_amb_f75303@4d": 0, "gpu_vr_f75303@4d": 0, "gpu_vram_f75303@4d": 0},
}

FC_UNDER_LOAD = {
    "fans": [{"name": "Left Fan", "rpm": 3120}, {"name": "Right Fan", "rpm": "bad"}, {"name": "Right Fan", "rpm": 3080}],
    "temps": {"gpu_vr_f75303@4d": 55, "cpu@4c": 88, "apu_f75303@4d": 80, "broken": None},
}


def test_parse_fc_skips_unpowered_sensors_and_puts_cpu_first():
    temps, fans = parse_fc_thermal(FC_FW16)
    assert temps == [44, 43, 41, 40]
    assert fans == [0, 0]


def test_parse_fc_orders_cpu_apu_gpu_and_skips_bad_values():
    temps, fans = parse_fc_thermal(FC_UNDER_LOAD)
    assert temps == [88, 80, 55]
    assert fans == [3120, 3080]


def test_parse_fc_empty_answer():
    assert parse_fc_thermal({}) == ([], [])


def test_fc_reader_uses_fetched_values():
    reader = FrameworkControlSensors(fetcher=lambda: FC_UNDER_LOAD)
    temps, fans = wait_for(reader, lambda v: v[0])
    assert temps == [88, 80, 55]
    assert fans == [3120, 3080]


def test_fc_reader_empty_when_not_installed_or_unreachable():
    for fetcher in (lambda: None, lambda: (_ for _ in ()).throw(ConnectionRefusedError("refused"))):
        reader = FrameworkControlSensors(fetcher=fetcher)
        reader.get()
        time.sleep(0.1)
        assert reader.get() == ([], [])


def test_fc_reader_clears_old_values_when_service_stops():
    answers = [FC_UNDER_LOAD]

    def fetcher():
        if answers:
            return answers.pop()
        raise ConnectionRefusedError("refused")

    reader = FrameworkControlSensors(fetcher=fetcher)
    wait_for(reader, lambda v: v[0])
    reader._last_refresh -= 10
    assert wait_for(reader, lambda v: not v[0]) == ([], [])
