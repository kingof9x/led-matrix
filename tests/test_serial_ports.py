# Shared serial handles (used on Windows, where COM ports can only be opened once)
import pytest
import serial

from led_mon import serial_ports


class FakeSerial:
    open_ports = set()

    def __init__(self, name, baud):
        if name in FakeSerial.open_ports:
            raise serial.SerialException("access denied")
        FakeSerial.open_ports.add(name)
        self.name = name
        self.is_open = True
        self.fail = False
        self.written = []

    def write(self, data):
        if self.fail:
            raise serial.SerialException("handle invalidated")
        self.written.append(bytes(data))
        return len(data)

    def close(self):
        if self.is_open:
            FakeSerial.open_ports.discard(self.name)
        self.is_open = False


@pytest.fixture(autouse=True)
def windows_ports(monkeypatch):
    FakeSerial.open_ports = set()
    monkeypatch.setattr(serial_ports, "IS_WINDOWS", True)
    monkeypatch.setattr(serial_ports.serial, "Serial", FakeSerial)
    yield
    serial_ports.close_all()


def test_same_port_is_shared():
    a = serial_ports.open_port("COM4")
    b = serial_ports.open_port("com4")
    a.write(b"1")
    b.write(b"2")
    assert a._entry is b._entry
    assert a._entry.serial.written == [b"1", b"2"]


def test_port_closed_when_last_handle_closes():
    a = serial_ports.open_port("COM4")
    b = serial_ports.open_port("COM4")
    a.close()
    assert "COM4" in FakeSerial.open_ports
    b.close()
    assert "COM4" not in FakeSerial.open_ports


def test_broken_port_is_reopened():
    a = serial_ports.open_port("COM4")
    a._entry.serial.fail = True
    with pytest.raises(serial.SerialException):
        a.write(b"x")
    # The other user of the port sees the failure too and reconnects
    with pytest.raises(serial.SerialException):
        a.write(b"y")
    b = serial_ports.open_port("COM4")
    b.write(b"z")
    assert b._entry is not a._entry
    assert b._entry.serial.written == [b"z"]
    a.close()
    assert "COM4" in FakeSerial.open_ports


def test_close_all_releases_ports():
    serial_ports.open_port("COM4")
    serial_ports.open_port("COM5")
    serial_ports.close_all()
    assert FakeSerial.open_ports == set()


def test_quit_draws_a_dark_frame_before_stopping():
    pytest.importorskip("numpy")
    pytest.importorskip("psutil")
    try:
        from led_mon import led_system_monitor as monitor
    except Exception as e:
        pytest.skip(f"full dependencies not installed: {e}")
    import queue
    import threading
    import numpy as np

    draw_queue = queue.Queue(2)
    draw_queue.put((np.ones((9, 34), dtype=int), None))  # a frame still waiting to be drawn
    received = []

    def drawing_thread():
        while True:
            item = draw_queue.get()
            received.append(item)
            if item is None:
                return

    t = threading.Thread(target=drawing_thread)
    t.start()
    monitor.stop_drawing_queue(draw_queue, blank=True)
    t.join(timeout=5)
    assert received[-1] is None
    grid, animate = received[-3]
    assert animate is False and not grid.any()
    grid, animate = received[-2]
    assert animate is None and not grid.any()
