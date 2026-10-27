# Panel discovery with fake serial ports (no hardware needed)
from serial.tools.list_ports_common import ListPortInfo

from led_mon import shared_state
from led_mon.shared_state import discover_led_devices


def port(device, location=None, vid=0x32AC, pid=0x0020, description="n/a"):
    info = ListPortInfo(device, skip_link_detection=True)
    info.vid = vid
    info.pid = pid
    info.location = location
    info.description = description
    return info


def test_matches_by_vid_pid_only():
    ports = [
        port("COM3", vid=0x8087, pid=0x0001),  # something else
        port("COM4", location="1-3.2:x.0"),
    ]
    assert discover_led_devices(ports, overrides={}) == [("1-3.2:x.0", "COM4")]


def test_matches_by_description_as_before():
    ports = [port("/dev/ttyACM0", location="1-3.2:1.0", vid=None, pid=None,
                  description="LED Matrix Input Module")]
    assert discover_led_devices(ports, overrides={}) == [("1-3.2:1.0", "/dev/ttyACM0")]


def test_left_right_sorted_by_location():
    ports = [
        port("/dev/ttyACM1", location="1-3.3:1.0"),  # right
        port("/dev/ttyACM0", location="1-3.2:1.0"),  # left
    ]
    assert [d for _, d in discover_led_devices(ports, overrides={})] == ["/dev/ttyACM0", "/dev/ttyACM1"]


def test_location_none_falls_back_to_port_name():
    ports = [port("COM7"), port("COM4")]
    assert discover_led_devices(ports, overrides={}) == [(None, "COM4"), (None, "COM7")]


def test_located_ports_sort_before_unlocated():
    ports = [port("COM2"), port("COM9", location="1-3.2:x.0")]
    assert [d for _, d in discover_led_devices(ports, overrides={})] == ["COM9", "COM2"]


def test_overrides_pin_both_sides():
    ports = [port("COM4", location="1-3.2:x.0"), port("COM5", location="1-3.3:x.0")]
    result = discover_led_devices(ports, overrides={"left": "COM5", "right": "COM4"})
    assert result == [("1-3.3:x.0", "COM5"), ("1-3.2:x.0", "COM4")]


def test_override_one_side_auto_detects_other():
    ports = [port("COM4", location="1-3.2:x.0"), port("COM5", location="1-3.3:x.0")]
    assert [d for _, d in discover_led_devices(ports, overrides={"right": "com4"})] == ["COM5", "com4"]
    assert [d for _, d in discover_led_devices(ports, overrides={"left": "COM5"})] == ["COM5", "COM4"]


def test_overrides_default_to_shared_state(monkeypatch):
    monkeypatch.setattr(shared_state, "port_overrides", {"left": "COM5"})
    ports = [port("COM4"), port("COM5")]
    assert [d for _, d in discover_led_devices(ports)] == ["COM5", "COM4"]


def test_no_panels():
    assert discover_led_devices([port("COM1", vid=1, pid=2)], overrides={}) == []


def test_location_pin_picks_the_panel_at_that_usb_position():
    ports = [port("COM4", location="1-3.3:x.0"), port("COM5", location="1-4.2:x.0")]
    result = discover_led_devices(ports, overrides={}, locations_pinned={"left": "1-4.2", "right": "1-3.3"})
    assert result == [("1-4.2:x.0", "COM5"), ("1-3.3:x.0", "COM4")]


def test_location_pin_one_side_auto_detects_other():
    ports = [port("COM4", location="1-3.2:x.0"), port("COM5", location="1-3.3:x.0")]
    result = discover_led_devices(ports, overrides={}, locations_pinned={"left": "1-3.3"})
    assert [d for _, d in result] == ["COM5", "COM4"]


def test_location_pin_not_found_falls_back_to_auto_detection():
    ports = [port("COM4", location="1-3.2:x.0"), port("COM5", location="1-3.3:x.0")]
    result = discover_led_devices(ports, overrides={}, locations_pinned={"left": "9-9.9"})
    assert [d for _, d in result] == ["COM4", "COM5"]


def test_location_pin_ignores_ports_that_are_not_led_panels():
    ports = [port("COM3", location="1-4.2:x.0", vid=0x8087, pid=0x0001), port("COM4", location="1-3.2:x.0")]
    result = discover_led_devices(ports, overrides={}, locations_pinned={"left": "1-4.2"})
    assert [d for _, d in result] == ["COM4"]


def test_port_pin_wins_over_location_pin():
    ports = [port("COM4", location="1-3.2:x.0"), port("COM5", location="1-3.3:x.0")]
    result = discover_led_devices(ports, overrides={"left": "COM4"}, locations_pinned={"left": "1-3.3"})
    assert [d for _, d in result] == ["COM4", "COM5"]


def test_overrides_are_read_from_the_config(monkeypatch):
    monkeypatch.setattr(shared_state, "port_overrides", {})
    monkeypatch.setattr(shared_state, "location_overrides", {})
    shared_state.set_overrides_from_config({"duration": 10, "right-port": "COM7", "left-location": "1-4.2"})
    assert shared_state.port_overrides == {"right": "COM7"}
    assert shared_state.location_overrides == {"left": "1-4.2"}
    ports = [port("COM4", location="1-3.2:x.0"), port("COM5", location="1-4.2:x.0"), port("COM7", location="1-3.3:x.0")]
    assert [d for _, d in discover_led_devices(ports)] == ["COM5", "COM7"]
    shared_state.set_overrides_from_config({})
    assert shared_state.port_overrides == {} and shared_state.location_overrides == {}
