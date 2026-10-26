from led_mon import startup


class FakeRegistry:
    def __init__(self):
        self.values = {}

    def get(self, key, name):
        return self.values.get((key, name))

    def set(self, key, name, value):
        self.values[(key, name)] = value

    def delete(self, key, name):
        self.values.pop((key, name), None)


RUN = (startup.RUN_KEY, startup.VALUE_NAME)
APPROVED = (startup.APPROVED_KEY, startup.VALUE_NAME)


def test_off_by_default():
    assert startup.is_enabled(FakeRegistry()) is False


def test_enable_and_disable():
    reg = FakeRegistry()
    startup.set_enabled(True, reg, command='"C:\\Apps\\led_mon.exe"')
    assert reg.values[RUN] == '"C:\\Apps\\led_mon.exe"'
    assert startup.is_enabled(reg) is True
    startup.set_enabled(False, reg)
    assert RUN not in reg.values
    assert startup.is_enabled(reg) is False


def test_disabled_in_task_manager_counts_as_off():
    reg = FakeRegistry()
    startup.set_enabled(True, reg, command="x")
    reg.values[APPROVED] = bytes([3]) + bytes(11)
    assert startup.is_enabled(reg) is False
    reg.values[APPROVED] = bytes([2]) + bytes(11)
    assert startup.is_enabled(reg) is True


def test_enabling_clears_task_manager_disable():
    reg = FakeRegistry()
    reg.values[RUN] = "x"
    reg.values[APPROVED] = bytes([3]) + bytes(11)
    startup.set_enabled(True, reg, command="x")
    assert APPROVED not in reg.values
    assert startup.is_enabled(reg) is True


def test_enabling_updates_a_moved_exe():
    reg = FakeRegistry()
    reg.values[RUN] = '"C:\\old\\led_mon.exe"'
    startup.set_enabled(True, reg, command='"D:\\new\\led_mon.exe"')
    assert reg.values[RUN] == '"D:\\new\\led_mon.exe"'


def test_launch_command_from_source():
    command = startup.launch_command()
    assert command.endswith('main.py"')
    assert command.startswith('"')


import os

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows registry")
def test_real_registry_round_trip(monkeypatch):
    import winreg
    scratch = r"Software\led_mon_test"
    monkeypatch.setattr(startup, "RUN_KEY", scratch + r"\Run")
    monkeypatch.setattr(startup, "APPROVED_KEY", scratch + r"\Approved")
    try:
        assert startup.is_enabled() is False
        startup.set_enabled(True, command='"C:\\x\\led_mon.exe"')
        assert startup.is_enabled() is True
        startup.set_enabled(False)
        assert startup.is_enabled() is False
        startup.set_enabled(False)  # turning off twice is fine
    finally:
        for sub in ("Run", "Approved", ""):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, scratch + ("\\" + sub if sub else ""))
            except OSError:
                pass
