# "Start with Windows": an entry under HKCU\Software\Microsoft\Windows\CurrentVersion\Run.
#
# The installer writes the same value name on a first install, so the tray menu and the installer agree.
# Task Manager > Startup apps can also disable the entry; it records that under
# ...\Explorer\StartupApproved\Run, and we treat a disabled entry as off.

# Built In Dependencies
import os
import sys

# Internal Dependencies
from led_mon import platform_utils

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
VALUE_NAME = "led_mon"


class WinRegistry:
    """Reads and writes string/binary values under HKEY_CURRENT_USER."""

    def __init__(self):
        import winreg
        self.winreg = winreg

    def get(self, key, name):
        try:
            with self.winreg.OpenKey(self.winreg.HKEY_CURRENT_USER, key) as k:
                return self.winreg.QueryValueEx(k, name)[0]
        except FileNotFoundError:
            return None

    def set(self, key, name, value):
        with self.winreg.CreateKey(self.winreg.HKEY_CURRENT_USER, key) as k:
            self.winreg.SetValueEx(k, name, 0, self.winreg.REG_SZ, value)

    def delete(self, key, name):
        try:
            with self.winreg.OpenKey(self.winreg.HKEY_CURRENT_USER, key, 0, self.winreg.KEY_SET_VALUE) as k:
                self.winreg.DeleteValue(k, name)
        except FileNotFoundError:
            pass


def supported():
    return platform_utils.IS_WINDOWS


def _registry(registry):
    return registry if registry is not None else WinRegistry()


def launch_command():
    """The command Windows runs at login: this exe, or pythonw with the repo's main.py when run from source."""
    if platform_utils.is_frozen():
        return f'"{sys.executable}"'
    python = sys.executable
    pythonw = os.path.join(os.path.dirname(python), "pythonw.exe")
    if os.path.isfile(pythonw):
        python = pythonw
    script = os.path.join(os.path.dirname(platform_utils.package_dir()), "main.py")
    return f'"{python}" "{script}"'


def _disabled_in_task_manager(registry):
    # The first byte is even (02, 06) when enabled and odd (03) when disabled
    value = registry.get(APPROVED_KEY, VALUE_NAME)
    return isinstance(value, (bytes, bytearray)) and len(value) > 0 and value[0] % 2 == 1


def is_enabled(registry=None):
    registry = _registry(registry)
    if not registry.get(RUN_KEY, VALUE_NAME):
        return False
    return not _disabled_in_task_manager(registry)


def set_enabled(enabled, registry=None, command=None):
    """Adds this app to, or removes it from, the programs Windows starts at login."""
    registry = _registry(registry)
    if enabled:
        registry.set(RUN_KEY, VALUE_NAME, command or launch_command())
    else:
        registry.delete(RUN_KEY, VALUE_NAME)
    # Clear any enabled/disabled state left by Task Manager so ours is the one that counts
    registry.delete(APPROVED_KEY, VALUE_NAME)
