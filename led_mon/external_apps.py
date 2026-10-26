# Finds and opens Open Hardware Monitor and Framework Control on Windows.
#
# Open Hardware Monitor (https://github.com/openhardwaremonitor) has no installer: it is a
#   folder unzipped anywhere, so we look for it in a few likely places.
# Framework Control (https://github.com/ozturkkl/framework-control) installs a Windows service
#   with a web page on 127.0.0.1 and adds "Framework Control" shortcuts to the Start Menu
#   and desktop for all users.

# Built In Dependencies
import os
import re
import logging
import webbrowser
import xml.etree.ElementTree as ET
from typing import NamedTuple, Optional

# Internal Dependencies
from led_mon import platform_utils

log = logging.getLogger(__name__)

OHM_EXE = "OpenHardwareMonitor.exe"
OHM_SHORTCUT_PREFIXES = ("open hardware monitor", "openhardwaremonitor")
FC_SHORTCUT_NAME = "Framework Control"
FC_INSTALL_FOLDER = "FrameworkControl"
FC_SERVICE_CONFIG = "FrameworkControlService.xml"
START_MENU = os.path.join("Microsoft", "Windows", "Start Menu", "Programs")


class Target(NamedTuple):
    kind: str            # "file" (exe or shortcut to open), "url", or "running"
    path: str            # file path or URL; for "running" the exe path if it could be read
    pid: Optional[int] = None


# ---------------------------------------------------------------------------
# Open Hardware Monitor

def _compact(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _find_exe_under(root, exe_name=OHM_EXE):
    """
    Looks for exe_name in root, and in folders in root whose name contains
      "openhardwaremonitor" (and one level below them, which is how the zip unpacks:
      openhardwaremonitor-v0.9.6\\OpenHardwareMonitor\\OpenHardwareMonitor.exe).
    """
    if not root or not os.path.isdir(root):
        return None
    direct = os.path.join(root, exe_name)
    if os.path.isfile(direct):
        return direct
    try:
        entries = list(os.scandir(root))
    except OSError:
        return None
    for entry in entries:
        try:
            if not entry.is_dir() or "openhardwaremonitor" not in _compact(entry.name):
                continue
        except OSError:
            continue
        candidate = os.path.join(entry.path, exe_name)
        if os.path.isfile(candidate):
            return candidate
        try:
            for sub in os.scandir(entry.path):
                if sub.is_dir():
                    candidate = os.path.join(sub.path, exe_name)
                    if os.path.isfile(candidate):
                        return candidate
        except OSError:
            continue
    return None


def _running_processes(exe_name):
    """Returns [(pid, exe path or "")] for running processes with that exe name."""
    try:
        import psutil
    except ImportError:
        return []
    found = []
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name == exe_name.lower():
                found.append((proc.pid, proc.info.get("exe") or ""))
        except Exception:
            continue
    return found


def _ohm_search_roots(environ):
    roots = []
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        if environ.get(var):
            roots.append(environ[var])
    local = environ.get("LOCALAPPDATA")
    if local:
        roots += [os.path.join(local, "Programs"), local]
    home = environ.get("USERPROFILE")
    if home:
        roots.append(home)
        for sub in ("Desktop", "Downloads", "Documents", "Tools", "Apps"):
            roots.append(os.path.join(home, sub))
    onedrive = environ.get("OneDrive")
    if onedrive:
        roots += [os.path.join(onedrive, "Desktop"), os.path.join(onedrive, "Documents")]
    drive = environ.get("SystemDrive")
    if drive:
        drive_root = drive + os.sep
        roots += [drive_root, os.path.join(drive_root, "Tools"), os.path.join(drive_root, "Apps")]
    roots.append(platform_utils.app_dir())
    # Keep order, drop duplicates
    seen, unique = set(), []
    for root in roots:
        key = os.path.normcase(os.path.normpath(root))
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def _find_ohm_shortcut(environ):
    for base_var in ("APPDATA", "ProgramData"):
        base = environ.get(base_var)
        if not base:
            continue
        folder = os.path.join(base, START_MENU)
        for dirpath in (folder, *_subdirs(folder)):
            try:
                for entry in os.scandir(dirpath):
                    lower = entry.name.lower()
                    if lower.endswith(".lnk") and lower.startswith(OHM_SHORTCUT_PREFIXES):
                        return entry.path
            except OSError:
                continue
    return None


def _subdirs(folder):
    try:
        return [e.path for e in os.scandir(folder) if e.is_dir()]
    except OSError:
        return []


def find_open_hardware_monitor(configured_path="", environ=None, running=None):
    """
    Finds Open Hardware Monitor. In order:
      1. already running (so we don't start a second copy)
      2. the path in settings.json (the exe, or the folder it is in)
      3. a Start Menu shortcut
      4. likely folders: Program Files, the user's Desktop/Downloads/Documents, C:\\,
         and the folder this app is in

    Args:
        configured_path: open_hardware_monitor_path from settings.json
        environ: environment variables (for tests)
        running: [(pid, exe)] of running copies (for tests); looked up when None

    Returns:
        A Target, or None if it is not found.
    """
    environ = os.environ if environ is None else environ
    if running is None:
        running = _running_processes(OHM_EXE)
    for pid, exe in running:
        return Target("running", exe, pid)

    if configured_path:
        path = os.path.expandvars(os.path.expanduser(configured_path.strip().strip('"')))
        if os.path.isdir(path):
            path = os.path.join(path, OHM_EXE)
        if os.path.isfile(path):
            return Target("file", path)

    shortcut = _find_ohm_shortcut(environ)
    if shortcut:
        return Target("file", shortcut)

    for root in _ohm_search_roots(environ):
        exe = _find_exe_under(root)
        if exe:
            return Target("file", exe)
    return None


# ---------------------------------------------------------------------------
# Framework Control

def _read_fc_port(config_path):
    """Reads FRAMEWORK_CONTROL_PORT from the service's FrameworkControlService.xml."""
    try:
        root = ET.parse(config_path).getroot()
    except (OSError, ET.ParseError):
        return None
    for env in root.iter("env"):
        if env.get("name") == "FRAMEWORK_CONTROL_PORT":
            value = (env.get("value") or "").strip()
            if value.isdigit() and 0 < int(value) < 65536:
                return int(value)
    return None


def find_framework_control(environ=None):
    """
    Finds Framework Control: its Start Menu or desktop shortcut, or, if the shortcuts
      were not created, the service's local web page.

    Returns:
        A Target, or None if it is not installed.
    """
    environ = os.environ if environ is None else environ
    places = []
    for base_var in ("ProgramData", "ALLUSERSPROFILE", "APPDATA"):
        if environ.get(base_var):
            places.append(os.path.join(environ[base_var], START_MENU))
    for base_var in ("PUBLIC", "USERPROFILE"):
        if environ.get(base_var):
            places.append(os.path.join(environ[base_var], "Desktop"))
    for folder in places:
        for ext in (".lnk", ".url"):
            shortcut = os.path.join(folder, FC_SHORTCUT_NAME + ext)
            if os.path.isfile(shortcut):
                return Target("file", shortcut)

    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        if not environ.get(var):
            continue
        config = os.path.join(environ[var], FC_INSTALL_FOLDER, FC_SERVICE_CONFIG)
        port = _read_fc_port(config)
        if port:
            return Target("url", f"http://127.0.0.1:{port}")
    return None


# ---------------------------------------------------------------------------
# Opening

def _show_window_of(pid, title_part="Hardware Monitor"):
    """Restores and brings forward the main window of a running process. Windows only."""
    if not platform_utils.IS_WINDOWS or pid is None:
        return False
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, lparam):
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value == pid:
            length = user32.GetWindowTextLengthW(hwnd)
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if title_part.lower() in buffer.value.lower():
                found.append(hwnd)
        return True

    user32.EnumWindows(callback, 0)
    SW_SHOW, SW_RESTORE = 5, 9
    for hwnd in found:
        user32.ShowWindow(hwnd, SW_SHOW)
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        if user32.IsWindowVisible(hwnd):
            return True
    return False


def open_target(target, app_name):
    """
    Opens what find_open_hardware_monitor or find_framework_control returned.

    Returns:
        A message to show the user, or None if it opened.
    """
    if target is None:
        return f"{app_name} was not found."
    try:
        if target.kind == "running":
            if _show_window_of(target.pid):
                return None
            return f"{app_name} is already running. Open it from its own icon in the system tray."
        if target.kind == "url":
            webbrowser.open(target.path)
            return None
        # Goes through the Windows shell, so shortcuts work and apps that ask for
        # admin rights (Open Hardware Monitor does) get the UAC prompt.
        platform_utils.open_path(target.path)
        return None
    except OSError as e:
        if getattr(e, "winerror", None) == 1223:  # The user cancelled the UAC prompt
            return None
        log.error(f"Could not open {app_name} ({target.path}): {e}")
        return f"Could not open {app_name}: {e}"
