# Temperatures and fan speeds on Windows, read with Framework's framework_tool (`framework_tool --thermal`).
# psutil has no sensors_temperatures() / sensors_fans() on Windows.
# https://github.com/FrameworkComputer/framework-system

# Built In Dependencies
import os
import re
import shutil
import subprocess
import threading
import time
import logging

# Internal Dependencies
from led_mon import platform_utils

log = logging.getLogger(__name__)

CACHE_SEC = 2.0
TIMEOUT_SEC = 10
# WinError 740: the exe asks for admin rights and the app isn't running as admin
ERROR_ELEVATION_REQUIRED = 740

# e.g. "  F75303_Local:  45 C"  /  "  APU:           60 C"
_TEMP_RE = re.compile(r'^\s*(?P<name>[^:]+):\s+(?P<value>-?\d+(?:\.\d+)?)\s*C\s*$')
# e.g. "  Left Fan:     1234 RPM"  /  "  Fan Speed:  0 RPM"  /  "  Right Fan:  65534 RPM (Stalled)"
_FAN_RE = re.compile(r'^\s*(?P<name>[^:]+):\s+(?P<value>\d+)\s*RPM(?P<stalled>\s*\(Stalled\))?', re.IGNORECASE)


def named_temps(text):
    """[(sensor name, C)] from `framework_tool --thermal` output, in the order it lists them."""
    temps = []
    seen = set()
    for line in text.splitlines():
        if _FAN_RE.match(line):
            continue
        m = _TEMP_RE.match(line)
        if m:
            name = m.group('name').strip()
            unique, n = name, 2
            while unique in seen:
                unique, n = f"{name} {n}", n + 1
            seen.add(unique)
            temps.append((unique, float(m.group('value'))))
    return temps


def parse_thermal(text):
    """Parse `framework_tool --thermal` output into ([temps in C], [fan RPMs])."""
    temps = []
    fans = []
    for line in text.splitlines():
        m = _FAN_RE.match(line)
        if m:
            fans.append(0 if m.group('stalled') else int(m.group('value')))
            continue
        m = _TEMP_RE.match(line)
        if m:
            temps.append(float(m.group('value')))
    return temps, fans


def find_framework_tool():
    """framework_tool(.exe) on PATH, or next to the app exe."""
    for name in ('framework_tool', 'framework_tool.exe'):
        path = shutil.which(name)
        if path:
            return path
    for directory in {platform_utils.app_dir(), platform_utils.package_dir()}:
        for name in ('framework_tool.exe', 'framework_tool'):
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                return path
    return None


def read_once(runner=None):
    """([temps], [fan RPMs]) read straight away (used by --diagnose). Raises when the tool can't run."""
    output = (runner or FrameworkToolThermal()._run_tool)()
    if output is None:
        raise FileNotFoundError("framework_tool.exe wasn't found on PATH or next to led_mon.exe")
    return parse_thermal(output)


class FrameworkToolThermal:
    """Cached thermal readings. Refreshes in the background so the main loop never waits on the tool."""

    def __init__(self, runner=None):
        self._runner = runner or self._run_tool
        self._lock = threading.Lock()
        self._values = ([], [])
        self._last_refresh = 0.0
        self._refreshing = False
        self._warned = False
        self._tool = None
        # Set when Windows refuses to start the tool without admin rights; it won't work until a restart
        self._needs_admin = False
        self._answered = False
        self.error = ""
        self._named = []
        # Seconds between readings (the "Read every" setting of the Temperatures and Fans apps)
        self.interval = CACHE_SEC

    def _warn_once(self, message):
        if not self._warned:
            self._warned = True
            log.warning(message)

    def _run_tool(self):
        if self._tool is None:
            self._tool = find_framework_tool()
        if not self._tool:
            self._warn_once("framework_tool was not found on PATH or next to led_mon.exe; "
                            "temperature and fan apps will be empty")
            return None
        result = subprocess.run(
            [self._tool, '--thermal'],
            capture_output=True, text=True, timeout=TIMEOUT_SEC, stdin=subprocess.DEVNULL,
            creationflags=platform_utils.subprocess_flags(),
        )
        if result.returncode != 0:
            raise RuntimeError(f"exit code {result.returncode}: {result.stderr.strip() or result.stdout.strip()}")
        return result.stdout

    def _refresh(self):
        self.error = ""
        try:
            output = self._runner()
            if output is None:
                self.error = "framework_tool.exe wasn't found"
            else:
                values = parse_thermal(output)
                if not values[0] and not values[1]:
                    self.error = "it gave no readings"
                    self._warn_once(f"framework_tool --thermal gave no readings: {output.strip()[:200]}")
                named = named_temps(output)
                with self._lock:
                    self._values = values
                    self._named = named
        except OSError as e:
            if getattr(e, "winerror", None) == ERROR_ELEVATION_REQUIRED:
                self._needs_admin = True
                self.error = "needs led_mon to run as admin"
                self._warn_once("framework_tool needs admin rights, so it won't be run again until led_mon "
                                "restarts; choose another source in the Temperatures or Fans app settings of the layout editor")
            else:
                self.error = f"failed ({e})"
                self._warn_once(f"framework_tool --thermal failed; temperature and fan apps will be empty: {e}")
        except Exception as e:
            self.error = f"failed ({e})"
            self._warn_once(f"framework_tool --thermal failed; temperature and fan apps will be empty: {e}")
        finally:
            with self._lock:
                self._refreshing = False
                self._answered = True

    @property
    def answered(self):
        """True once the first run has finished, whether or not it worked."""
        return self._answered

    def named(self):
        """[(sensor name, C)] from the last reading."""
        with self._lock:
            return list(self._named)

    def get(self):
        """Return ([temps], [fan RPMs]) from the last reading, starting a refresh if it is stale."""
        with self._lock:
            now = time.monotonic()
            if not self._refreshing and not self._needs_admin and now - self._last_refresh >= self.interval:
                self._refreshing = True
                self._last_refresh = now
                threading.Thread(target=self._refresh, daemon=True).start()
            return self._values


thermal = FrameworkToolThermal()
