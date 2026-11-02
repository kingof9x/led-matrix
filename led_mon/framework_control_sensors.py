# Temperatures and fan speeds on Windows, read from Framework Control (https://github.com/ozturkkl/framework-control).
# Its Windows service runs framework_tool with system rights and shares the result on
# http://127.0.0.1:<port>/api/thermal, so led_mon gets the values without admin rights.

# Built In Dependencies
import json
import threading
import time
import logging
import urllib.request

# Internal Dependencies
from led_mon import external_apps

log = logging.getLogger(__name__)

CACHE_SEC = 2.0
TIMEOUT_SEC = 1.0
# The CPU first on the panel, then the APU, the GPU module, then the rest (ambient, charger, ...)
_NAME_ORDER = (("cpu", 0), ("apu", 1), ("gpu", 2))


def _rank(name):
    name = name.lower()
    for part, rank in _NAME_ORDER:
        if name.startswith(part):
            return rank
    return 3


def named_temps(data):
    """[(sensor name, C)] from /api/thermal JSON, CPU first. Sensors reading 0 (not powered) are left out."""
    temps = []
    for name, value in (data.get("temps") or {}).items():
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        # 0: the sensor isn't powered, e.g. the GPU module's sensors while it is off
        if value > 0:
            temps.append((_rank(name), str(name), value))
    return [(name, value) for _, name, value in sorted(temps)]


def parse_thermal(data):
    """Turn /api/thermal JSON ({"temps": {name: C}, "fans": [{name, rpm}]}) into ([temps in C], [fan RPMs])."""
    temps = named_temps(data)
    fans = []
    for fan in data.get("fans") or []:
        try:
            fans.append(max(0, int(fan.get("rpm"))))
        except (TypeError, ValueError, AttributeError):
            continue
    return [value for _, value in temps], fans


def _fetch():
    """/api/thermal JSON from the Framework Control service, or None when it isn't installed."""
    port = external_apps.framework_control_port()
    if not port:
        return None
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/thermal", timeout=TIMEOUT_SEC) as response:
        return json.load(response)


def read_once(fetcher=None):
    """([temps], [fan RPMs]) read straight away (used by --diagnose). Raises when the service doesn't answer."""
    data = (fetcher or _fetch)()
    if not data:
        raise RuntimeError("Framework Control isn't installed (no FrameworkControlService.xml)")
    return parse_thermal(data)


class FrameworkControlSensors:
    """Cached readings. Refreshes in the background so the main loop never waits on the service."""

    def __init__(self, fetcher=None):
        self._fetcher = fetcher or _fetch
        self._lock = threading.Lock()
        self._values = ([], [])
        self._last_refresh = 0.0
        self._refreshing = False
        self._available = None
        self.error = ""
        self._named = []
        # Seconds between readings (the "Read every" setting of the Temperatures and Fans apps)
        self.interval = CACHE_SEC

    def _set_available(self, available, reason=""):
        if available != self._available:
            self._available = available
            if available:
                log.info("Reading temperatures and fans from Framework Control")
            else:
                log.info(f"Framework Control isn't available{reason}")

    def _refresh(self):
        try:
            data = self._fetcher()
            values = parse_thermal(data) if data else ([], [])
            named = named_temps(data) if data else []
            available, reason = bool(data), "" if data else " (not installed)"
            self.error = "" if data else "not installed"
        except Exception as e:
            values = ([], [])
            named = []
            available, reason = False, f" ({e})"
            self.error = f"didn't answer ({e})"
        # Store the values before saying it answered, so a reader never sees "answered" with old values
        with self._lock:
            self._values = values
            self._named = named
            self._refreshing = False
        self._set_available(available, reason)

    @property
    def answered(self):
        """True once the first refresh has finished, whether or not Framework Control was there."""
        return self._available is not None

    def named(self):
        """[(sensor name, C)] from the last reading."""
        with self._lock:
            return list(self._named)

    def get(self):
        """Return ([temps], [fan RPMs]) from the last reading, starting a refresh if it is stale."""
        with self._lock:
            now = time.monotonic()
            if not self._refreshing and now - self._last_refresh >= self.interval:
                self._refreshing = True
                self._last_refresh = now
                threading.Thread(target=self._refresh, daemon=True).start()
            return self._values


sensors = FrameworkControlSensors()
