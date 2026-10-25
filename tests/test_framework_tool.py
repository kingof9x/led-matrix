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
