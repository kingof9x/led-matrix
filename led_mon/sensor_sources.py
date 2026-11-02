# Where the Temperatures and Fans apps get their readings on Windows (psutil can't read them there).
# The choice is "sensor_source" in settings.json, picked in the Temperatures and Fans app settings of the layout editor:
#   auto                    try Framework Control, then Libre Hardware Monitor, then framework_tool
#   framework_control       Framework Control's local service (no admin rights needed)
#   libre_hardware_monitor  Libre Hardware Monitor's sensors through WMI (it must run as admin)
#   framework_tool          framework_tool --thermal (led_mon must run as admin)

# Built In Dependencies
import threading
import time
import logging

# Internal Dependencies
from led_mon import app_settings

log = logging.getLogger(__name__)

SETTING = "sensor_source"
AUTO = "auto"
FRAMEWORK_CONTROL = "framework_control"
LIBRE_HARDWARE_MONITOR = "libre_hardware_monitor"
FRAMEWORK_TOOL = "framework_tool"

# (key, label) in the order shown in the layout editor
SOURCES = (
    (AUTO, "Automatic"),
    (FRAMEWORK_CONTROL, "Framework Control"),
    (LIBRE_HARDWARE_MONITOR, "Libre Hardware Monitor"),
    (FRAMEWORK_TOOL, "framework_tool"),
)
LABELS = dict(SOURCES)
AUTO_ORDER = (FRAMEWORK_CONTROL, LIBRE_HARDWARE_MONITOR, FRAMEWORK_TOOL)

# What each choice needs, shown under the choice in the layout editor
HELP = {
    AUTO: "Tries Framework Control, then Libre Hardware Monitor, then framework_tool, and uses the first "
          "one that gives readings (temperatures and fans can come from different ones).",
    FRAMEWORK_CONTROL: "Needs Framework Control installed; its service runs in the background, so nothing has "
                       "to be open and no admin rights are needed. On a Framework 16 it gives the CPU, APU, "
                       "ambient and charger temperatures and both fans.",
    LIBRE_HARDWARE_MONITOR: "Needs Libre Hardware Monitor open (it can sit in the tray) and running as admin; "
                            "led_mon reads its sensors through Windows. Which sensors show depends on the "
                            "laptop: GPU and SSD temperatures are likely, fans may be missing.",
    FRAMEWORK_TOOL: "Needs framework_tool.exe next to led_mon.exe or on PATH, and led_mon itself "
                    "running as admin; otherwise Windows refuses to start it.",
}

# How often each source is read, in seconds ("sensor_refresh_seconds" in settings.json)
REFRESH_SETTING = "sensor_refresh_seconds"
DEFAULT_REFRESH = 2.0
REFRESH_CHOICES = (1, 2, 5, 10, 30)
REFRESH_HELP = ("How often the temperatures and fans are read. Shorter follows changes faster; "
                "framework_tool and Libre Hardware Monitor use a little more CPU when read often.")

# Readings older than this mean no panel is showing the Temperatures or Fans app
STALE_SEC = 10.0


def normalize(choice):
    return choice if choice in LABELS else AUTO


def normalize_refresh(seconds):
    try:
        return max(0.5, min(60.0, float(seconds)))
    except (TypeError, ValueError):
        return DEFAULT_REFRESH


def to_unit(celsius, unit):
    """A temperature in the unit picked for the Temperatures app: C (as read) or F."""
    return celsius * 9 / 5 + 32 if str(unit).upper().startswith("F") else celsius


def pick(named, chosen, count=3):
    """The values to show: for each place, the chosen sensor's reading, or (when none is chosen) the next
    sensor in the source's own order that isn't shown elsewhere. A chosen sensor that gives no reading
    right now shows as None."""
    values = dict(named)
    chosen = list(chosen or [])[:count] + [""] * (count - len(list(chosen or [])[:count]))
    taken = {name for name in chosen if name}
    spare = [name for name, _ in named if name not in taken]
    result = []
    for name in chosen:
        if name:
            result.append(values.get(name))
        else:
            result.append(values[spare.pop(0)] if spare else None)
    return result


def default_readers():
    """The cached background reader for each source, created on first use."""
    from led_mon import framework_control_sensors, libre_hardware_monitor_sensors, framework_tool
    return {
        FRAMEWORK_CONTROL: framework_control_sensors.sensors,
        LIBRE_HARDWARE_MONITOR: libre_hardware_monitor_sensors.sensors,
        FRAMEWORK_TOOL: framework_tool.thermal,
    }


def read_once_functions():
    """Each source's direct (uncached) read, for --diagnose."""
    from led_mon import framework_control_sensors, libre_hardware_monitor_sensors, framework_tool
    return {
        FRAMEWORK_CONTROL: framework_control_sensors.read_once,
        LIBRE_HARDWARE_MONITOR: libre_hardware_monitor_sensors.read_once,
        FRAMEWORK_TOOL: framework_tool.read_once,
    }


class SensorSource:
    """The chosen source, and the readings for the Temperatures and Fans apps."""

    def __init__(self, path=None, readers=None):
        self.path = path
        self._readers = readers
        self._lock = threading.Lock()
        self.choice = AUTO
        self.temps_from = None
        self.fans_from = None
        self._counts = (0, 0)
        self._last_read = 0.0
        self.refresh = DEFAULT_REFRESH
        self.reload()

    @property
    def readers(self):
        if self._readers is None:
            self._readers = default_readers()
            self._apply_refresh()
        return self._readers

    def _apply_refresh(self):
        if self._readers is None:
            return
        for reader in self._readers.values():
            reader.interval = self.refresh

    def reload(self):
        """Read the choice from settings.json again (the window changed it)."""
        settings = app_settings.load_settings(self.path)
        choice = normalize(settings.get(SETTING, AUTO))
        self.refresh = normalize_refresh(settings.get(REFRESH_SETTING, DEFAULT_REFRESH))
        self._apply_refresh()
        with self._lock:
            changed = choice != self.choice
            self.choice = choice
            if changed:
                self.temps_from = self.fans_from = None
                self._counts = (0, 0)
        if changed:
            log.info(f"Temperatures and fans now come from: {LABELS[choice]}")
        return choice

    def set(self, choice, save=True):
        choice = normalize(choice)
        if save:
            settings = app_settings.load_settings(self.path)
            settings[SETTING] = choice
            app_settings.save_settings(settings, self.path)
        with self._lock:
            self.choice = choice
            self.temps_from = self.fans_from = None
        return choice

    def set_refresh(self, seconds, save=True):
        self.refresh = normalize_refresh(seconds)
        if save:
            settings = app_settings.load_settings(self.path)
            settings[REFRESH_SETTING] = self.refresh
            app_settings.save_settings(settings, self.path)
        self._apply_refresh()
        return self.refresh

    def named_temps(self):
        """[(sensor name, C)] from the source the temperatures come from right now (call readings() first)."""
        with self._lock:
            key = self.temps_from or (self.choice if self.choice != AUTO else None)
        if not key:
            return []
        reader = self.readers[key]
        return reader.named() if hasattr(reader, "named") else []

    def readings(self):
        """([temps in C], [fan RPMs]) from the chosen source."""
        with self._lock:
            choice = self.choice
        if choice != AUTO:
            temps, fans = self.readers[choice].get()
            temps_from = choice if temps else None
            fans_from = choice if fans else None
        else:
            temps, fans, temps_from, fans_from = [], [], None, None
            for key in AUTO_ORDER:
                reader = self.readers[key]
                source_temps, source_fans = reader.get()
                if not temps and source_temps:
                    temps, temps_from = source_temps, key
                if not fans and source_fans:
                    fans, fans_from = source_fans, key
                # Wait for each source's first answer before starting the next one, so a working
                # Framework Control doesn't also start Libre Hardware Monitor queries or framework_tool
                if (temps and fans) or not reader.answered:
                    break
        with self._lock:
            self.temps_from, self.fans_from = temps_from, fans_from
            self._counts = (len(temps), len(fans))
            self._last_read = time.monotonic()
        return temps, fans

    def status(self):
        """For the layout editor: the choice, and where the last readings came from."""
        with self._lock:
            reading = time.monotonic() - self._last_read < STALE_SEC
            state = {"choice": self.choice, "reading": reading, "refresh": self.refresh,
                     "temps_from": self.temps_from, "fans_from": self.fans_from,
                     "temps": self._counts[0], "fans": self._counts[1]}
        if reading:
            keys = [self.choice] if self.choice != AUTO else list(AUTO_ORDER)
            errors = {}
            for key in keys:
                reader = self.readers[key]
                if reader.answered and getattr(reader, "error", ""):
                    errors[key] = reader.error
            state["errors"] = errors
            state["sensors"] = [name for name, _ in self.named_temps()]
        return state


def describe(status):
    """One or two lines for the layout editor from status()."""
    if not status:
        return "The monitor isn't running."
    if not status.get("reading"):
        return "No panel is showing Temperatures or Fans right now, so nothing is being read."
    parts = []
    for what, count_key, from_key in (("Temperatures", "temps", "temps_from"), ("Fans", "fans", "fans_from")):
        source = status.get(from_key)
        if source:
            parts.append(f"{what}: {status.get(count_key, 0)} from {LABELS.get(source, source)}")
        else:
            parts.append(f"{what}: none")
    text = "Now reading. " + "; ".join(parts) + "."
    errors = status.get("errors") or {}
    if errors and (not status.get("temps_from") or not status.get("fans_from")):
        text += "\n" + "\n".join(f"{LABELS.get(k, k)}: {v}" for k, v in errors.items())
    return text


_source = None
_source_lock = threading.Lock()


def current():
    """The process-wide SensorSource, loaded from settings.json on first use."""
    global _source
    with _source_lock:
        if _source is None:
            _source = SensorSource()
        return _source


def windows_readings():
    """([temps], [fan RPMs]) for the Temperatures and Fans apps on Windows."""
    return current().readings()


def named_temps():
    """[(sensor name, C)] on this computer, from the chosen source."""
    return windows_named_temps()


def windows_named_temps():
    """[(sensor name, C)] for the Temperatures app on Windows."""
    source = current()
    source.readings()
    return source.named_temps()


def diagnose_lines(read_once=None, settings_path=None, wait_sec=8.0):
    """The --diagnose section: the chosen source, each source read directly, and what the choice gives."""
    lines = ["Temperatures and fans:"]
    source = SensorSource(settings_path)
    lines.append(f"  Chosen source: {LABELS[source.choice]} ({source.choice})")
    read_once = read_once or read_once_functions()
    for key in AUTO_ORDER:
        try:
            temps, fans = read_once[key]()
            lines.append(f"  {LABELS[key]}: {len(temps)} temperatures {[round(t) for t in temps]}, "
                         f"{len(fans)} fans {fans}")
        except Exception as e:
            lines.append(f"  {LABELS[key]}: not available ({type(e).__name__}: {e})")
    # The same path the panels use, through the background readers
    end = time.monotonic() + wait_sec
    keys = [source.choice] if source.choice != AUTO else list(AUTO_ORDER)
    while True:
        temps, fans = source.readings()
        if (temps and fans) or time.monotonic() >= end or all(source.readers[k].answered for k in keys):
            break
        time.sleep(0.2)
    # One more pass, so a source that just answered is counted
    source.readings()
    lines.append(f"  Panels get: temperatures from {LABELS.get(source.temps_from, 'nowhere')}, "
                 f"fans from {LABELS.get(source.fans_from, 'nowhere')}")
    named = source.named_temps()
    lines.append(f"  Read every {source.refresh:g} s. Sensors the Temperatures app can show: "
                 + (", ".join(f"{name} ({value:.0f} C)" for name, value in named) or "none"))
    return lines
