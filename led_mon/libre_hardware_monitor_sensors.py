# Temperatures and fan speeds on Windows, read from Libre Hardware Monitor
# (https://github.com/LibreHardwareMonitor/LibreHardwareMonitor).
# While it runs, Libre Hardware Monitor shares its sensors through Windows (WMI, namespace root\LibreHardwareMonitor).
# It must run as admin to see most sensors.

# Built In Dependencies
import threading
import time
import logging

# Internal Dependencies
from led_mon import platform_utils

log = logging.getLogger(__name__)

CACHE_SEC = 2.0
# When Libre Hardware Monitor isn't running, ask again less often
RETRY_SEC = 10.0
WMI_NAMESPACE = r"root\LibreHardwareMonitor"
QUERY = ("SELECT Name, Identifier, Parent, SensorType, Value, Index FROM Sensor "
         "WHERE SensorType = 'Temperature' OR SensorType = 'Fan'")
# At most this many temperatures from one piece of hardware, so the CPU's many core sensors
# don't fill all eight places on the panel
PER_HARDWARE = 2
# Hardware order on the panel: CPU, GPU, then the rest (motherboard, embedded controller, ...), then drives
_HARDWARE_ORDER = (("/amdcpu", 0), ("/intelcpu", 0), ("/cpu", 0), ("/gpu", 1),
                   ("/nvme", 3), ("/hdd", 3), ("/ssd", 3), ("/storage", 3))
# The sensor that best stands for its hardware goes first
_MAIN_SENSOR_NAMES = ("tctl", "package", "core (tctl", "gpu core", "composite", "core average")


def _field(sensor, name, default=None):
    if isinstance(sensor, dict):
        return sensor.get(name, default)
    return getattr(sensor, name, default)


def _hardware_rank(parent):
    parent = (parent or "").lower()
    for prefix, rank in _HARDWARE_ORDER:
        if parent.startswith(prefix):
            return rank
    return 2


def _sensor_rank(name):
    name = (name or "").lower()
    return 0 if any(part in name for part in _MAIN_SENSOR_NAMES) else 1


def _index(sensor):
    try:
        return int(_field(sensor, "Index", 0) or 0)
    except (TypeError, ValueError):
        return 0


_HARDWARE_NAMES = (("/amdcpu", "CPU"), ("/intelcpu", "CPU"), ("/cpu", "CPU"), ("/gpu", "GPU"), ("/nvme", "SSD"),
                   ("/hdd", "Drive"), ("/ssd", "SSD"), ("/storage", "Drive"), ("/lpc", "Board"),
                   ("/motherboard", "Board"), ("/ram", "Memory"), ("/memory", "Memory"), ("/battery", "Battery"))


def _hardware_name(parent):
    lower = (parent or "").lower()
    for prefix, name in _HARDWARE_NAMES:
        if lower.startswith(prefix):
            # A second drive or GPU gets its number: "SSD 2"
            number = lower.rstrip("/").rsplit("/", 1)[-1]
            return f"{name} {int(number) + 1}" if number.isdigit() and int(number) > 0 else name
    return parent.strip("/") or "Other"


def named_temps(sensors):
    """[(sensor name, C)] for every temperature it reads, e.g. ("CPU: Core (Tctl/Tdie)", 55.0); CPU first."""
    temps = []
    for sensor in sensors:
        if (_field(sensor, "SensorType") or "").lower() != "temperature":
            continue
        try:
            value = float(_field(sensor, "Value"))
        except (TypeError, ValueError):
            continue
        if value > 0:
            parent = _field(sensor, "Parent") or ""
            name = f"{_hardware_name(parent)}: {_field(sensor, 'Name') or '?'}"
            temps.append(((_hardware_rank(parent), parent, _sensor_rank(_field(sensor, "Name")), _index(sensor)),
                          name, value))
    named, seen = [], set()
    for _, name, value in sorted(temps):
        unique, n = name, 2
        while unique in seen:
            unique, n = f"{name} {n}", n + 1
        seen.add(unique)
        named.append((unique, value))
    return named


def parse_sensors(sensors):
    """Turn Libre Hardware Monitor's WMI Sensor rows into ([temps in C], [fan RPMs])."""
    temps_by_hardware = {}
    fans = []
    for sensor in sensors:
        try:
            value = float(_field(sensor, "Value"))
        except (TypeError, ValueError):
            continue
        kind = (_field(sensor, "SensorType") or "").lower()
        parent = _field(sensor, "Parent") or ""
        if kind == "temperature":
            # 0 or less: no reading (e.g. a sensor it can't read without admin rights)
            if value > 0:
                temps_by_hardware.setdefault(parent, []).append(
                    (_sensor_rank(_field(sensor, "Name")), _index(sensor), value))
        elif kind == "fan":
            fans.append((_hardware_rank(parent), parent, _index(sensor), max(0, int(round(value)))))
    temps = []
    for parent in sorted(temps_by_hardware, key=lambda p: (_hardware_rank(p), p)):
        temps += [value for _, _, value in sorted(temps_by_hardware[parent])[:PER_HARDWARE]]
    return temps, [rpm for _, _, _, rpm in sorted(fans)]


def _query():
    """Sensor rows from Libre Hardware Monitor. Raises when it isn't running (the WMI namespace is missing)."""
    import wmi
    platform_utils.init_com_for_thread()
    return wmi.WMI(namespace=WMI_NAMESPACE).query(QUERY)


def read_once(query=None):
    """([temps], [fan RPMs]) read straight away (used by --diagnose). Raises when it isn't running."""
    return parse_sensors((query or _query)())


class LibreHardwareMonitorSensors:
    """Cached readings. Refreshes in the background so the main loop never waits on WMI."""

    def __init__(self, query=None):
        self._query = query or _query
        self._lock = threading.Lock()
        self._values = ([], [])
        self._last_refresh = 0.0
        self._wait = CACHE_SEC
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
                log.info("Reading temperatures and fans from Libre Hardware Monitor")
            else:
                log.info(f"Libre Hardware Monitor isn't available{reason}")

    def _refresh(self):
        try:
            rows = list(self._query())
            values = parse_sensors(rows)
            named = named_temps(rows)
            self.error = "" if values[0] or values[1] else "it gave no temperatures or fans (is it running as admin?)"
            available, reason, wait = True, "", self.interval
        except Exception as e:
            values = ([], [])
            named = []
            self.error = f"not running ({e})"
            available, reason, wait = False, f" (not running: {e})", max(RETRY_SEC, self.interval)
        with self._lock:
            self._values = values
            self._named = named
            self._wait = wait
            self._refreshing = False
        self._set_available(available, reason)

    @property
    def answered(self):
        """True once the first refresh has finished, whether or not Libre Hardware Monitor was there."""
        return self._available is not None

    def named(self):
        """[(sensor name, C)] from the last reading."""
        with self._lock:
            return list(self._named)

    def get(self):
        """Return ([temps], [fan RPMs]) from the last reading, starting a refresh if it is stale."""
        with self._lock:
            now = time.monotonic()
            if not self._refreshing and now - self._last_refresh >= self._wait:
                self._refreshing = True
                self._last_refresh = now
                threading.Thread(target=self._refresh, daemon=True).start()
            return self._values


sensors = LibreHardwareMonitorSensors()
