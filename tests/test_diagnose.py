# --diagnose report, with fake serial ports and config (no hardware needed)
from serial.tools.list_ports_common import ListPortInfo

from led_mon import diagnose, shared_state


def port(device, location=None, vid=0x32AC, pid=0x0020, description="n/a"):
    info = ListPortInfo(device, skip_link_detection=True)
    info.vid = vid
    info.pid = pid
    info.location = location
    info.description = description
    return info


def write_config(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return str(path)


def test_report_lists_ports_and_panels(tmp_path, monkeypatch):
    monkeypatch.setattr(shared_state, "port_overrides", {})
    monkeypatch.setattr(shared_state, "location_overrides", {})
    config = write_config(tmp_path, "duration: 10\n")
    ports = [port("COM3", location="1-2:x.0", vid=0x8087, pid=0x0A2B, description="Bluetooth"),
             port("COM4", location="1-3.2:x.0", description="LED Matrix"),
             port("COM5", location="1-3.3:x.0", description="LED Matrix")]
    text = "\n".join(diagnose.report(config, ports, find_apps=lambda: (None, None)))
    assert f"Config file:   {config}" in text
    assert "COM3  location=1-2:x.0  usb=8087:0A2B  Bluetooth\n" in text
    assert "COM4  location=1-3.2:x.0  usb=32AC:0020  LED Matrix  <- LED Matrix" in text
    assert "Left:  COM4  (USB location 1-3.2:x.0)" in text
    assert "Right: COM5  (USB location 1-3.3:x.0)" in text
    assert "Open Hardware Monitor: not found" in text
    assert "Framework Control:     not found" in text


def test_report_uses_the_config_pins(tmp_path, monkeypatch):
    monkeypatch.setattr(shared_state, "port_overrides", {})
    monkeypatch.setattr(shared_state, "location_overrides", {})
    config = write_config(tmp_path, "left-location: 1-3.3\n")
    ports = [port("COM4", location="1-3.2:x.0"), port("COM5", location="1-3.3:x.0")]
    text = "\n".join(diagnose.report(config, ports, find_apps=lambda: (None, None)))
    assert "left-location: 1-3.3" in text
    assert "Left:  COM5" in text
    assert "Right: COM4" in text


def test_report_without_panels_or_config(tmp_path, monkeypatch):
    monkeypatch.setattr(shared_state, "port_overrides", {})
    monkeypatch.setattr(shared_state, "location_overrides", {})
    text = "\n".join(diagnose.report(str(tmp_path / "missing.yaml"), [], find_apps=None))
    assert "could not be read" in text
    assert "Serial ports:\n  none" in text
    assert "Left:  not found" in text
    assert "Right: not found" in text
