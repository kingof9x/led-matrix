# What the layout editor knows about each app: a friendly name, whether it fills half a panel
# or the whole panel, and the settings it offers. Apps from plugins not listed here still work,
# with no settings form.

# Built In Dependencies
import os
import copy
from dataclasses import dataclass, field

# Internal Dependencies
from led_mon.platform_utils import package_dir

HALF = "half"
FULL = "full"

# Where a setting is stored: in the app's args mapping, or on the app entry itself (e.g. animate)
ARGS = "args"
APP = "app"


@dataclass
class Setting:
    key: str
    label: str
    kind: str                     # bool | choice | text | int | list | multi | snapshot | gpu
    default: object = None
    options: tuple = ()
    target: str = ARGS
    help: str = ""


@dataclass
class AppInfo:
    name: str
    label: str
    description: str
    size: str = HALF
    settings: list = field(default_factory=list)
    # Fixed parts of the config entry the app needs to run
    fixed: dict = field(default_factory=dict)
    fixed_args: dict = field(default_factory=dict)


APPS = [
    AppInfo("cpu", "CPU cores", "Load on each CPU core, drawn as small spirals (or a bar per core when there are more than eight).", HALF),
    AppInfo("cpu-graph", "CPU graph", "Overall CPU use over the last 15 seconds. Each row is one second, newest at the top; a longer row is a busier CPU.", HALF),
    AppInfo("gpu-graph", "GPU graph", "Overall GPU use over the last 15 seconds, like the CPU graph.", HALF, [
        Setting("gpu", "GPU", "gpu", None, help="Which graphics card to show, when the computer has more than one"),
    ]),
    AppInfo("disk-net", "Disk + network", "Disk read and write bars on the upper part, network upload and download bars on the lower part.", HALF),
    AppInfo("mem-bat", "Memory + battery", "Memory use on top, battery level below (pulses when charging).", HALF),
    AppInfo("disk", "Disk", "Disk read (left bar) and write (right bar) activity.", HALF),
    AppInfo("net", "Network", "Upload (left bar) and download (right bar) activity.", HALF),
    AppInfo("temp", "Temperatures", "Up to eight temperature sensors as spirals. On Windows this needs framework_tool.", HALF),
    AppInfo("fan", "Fans", "Speed of the two fans as bars. On Windows this needs framework_tool.", HALF),
    AppInfo("time", "Clock", "The current time.", FULL, [
        Setting("fmt_24_hour", "24-hour clock", "bool", False),
        Setting("timezone", "Time zone", "text", "", help="Blank for this computer's time zone, or an IANA name such as Europe/London"),
    ]),
    AppInfo("weather", "Weather", "Current weather or a forecast, from your location.", FULL, [
        Setting("units", "Units", "choice", "metric", ("metric", "imperial", "standard")),
        Setting("zip_info", "Zip / postcode, country", "list", [], help='For example: 10001, US. Blank to find your location automatically'),
        Setting("lat_lon", "Latitude, longitude", "list", [], help="Used instead of the zip code if set"),
        Setting("measures", "Show", "multi", ["temp_condition", "wind_chill", "wind"], ("temp_condition", "wind_chill", "wind")),
        Setting("measures-duration", "Seconds per measure", "int", 20),
        Setting("forecast", "Show a forecast", "bool", False),
        Setting("forecast_day", "Forecast day (1-5)", "choice", 1, (1, 2, 3, 4, 5)),
        Setting("forecast_hour", "Forecast hour", "choice", 12, (0, 3, 6, 9, 12, 15, 18, 21)),
    ], fixed_args={"id_key_override": ["forecast", "weather_forecast", "weather_current"]}),
    AppInfo("equalizer", "Equalizer", "Bars that move with the sound that is playing.", FULL, [
        Setting("input-mode", "Listen to", "choice", "playback", ("playback", "microphone"),
                help="playback: what the speakers play. microphone: the default microphone"),
        Setting("input-device", "Microphone name", "text", "", help="Optional, part of the device name, for microphone mode"),
    ], fixed={"persistent-draw": True, "dispose-fn": "equalizer_dispose"},
       fixed_args={"external-filter": False, "border": False}),
    AppInfo("fps", "FPS counter", "Frames per second of a game, with a graph of the last few seconds below. "
            "Windows only, and needs a one-time setup (see the README); shows ? until then.", FULL, [
        Setting("process", "App to watch", "text", "",
                help="Blank: whichever app is in front. Or the program's file name, such as eldenring.exe"),
    ]),
    AppInfo("snap", "Picture", "A still pattern from the snapshot files.", FULL, [
        Setting("file", "Picture", "snapshot", None),
        Setting("animate", "Scroll", "bool", False, target=APP, help="Scroll the picture up the panel"),
    ], fixed_args={"path": "snapshot_files"}),
]

_BY_NAME = {a.name: a for a in APPS}

# Names the monitor uses for empty slots (display: false); never offered as apps
HIDDEN = {"none", "equalizer_dispose"}


def get(name):
    info = _BY_NAME.get(name)
    if info is None:
        info = AppInfo(name, name, "App from a plugin.", HALF)
    return info


def available(plugin_names=()):
    """Apps to offer: the known ones, then any other plugin apps."""
    extra = [get(n) for n in plugin_names if n not in _BY_NAME and n not in HIDDEN]
    return APPS + extra


def snapshot_files(side):
    """Picture files usable on one panel: those in snapshot_files/<side>/ and snapshot_files/."""
    base = os.path.join(package_dir(), "snapshot_files")
    names = set()
    for folder in (os.path.join(base, side), base):
        try:
            for f in os.listdir(folder):
                if f.endswith(".json") and not f.endswith("_raw.json"):
                    names.add(f)
        except OSError:
            pass
    return sorted(names)


# Shown in the GPU picker for "no gpu arg"
FIRST_GPU = "First GPU found"


def gpu_options():
    """Choices for the GPU picker: (value, label), the first meaning no gpu arg."""
    try:
        from led_mon.gpu import gpu_names
        names = gpu_names()
    except Exception:
        names = []
    return [("", FIRST_GPU)] + [(n, n) for n in names]


def new_app(name, side):
    """A config entry for a newly placed app, with default settings."""
    info = get(name)
    entry = {"name": name}
    args = {}
    for s in info.settings:
        if s.kind == "snapshot":
            files = snapshot_files(side)
            value = files[0] if files else None
        else:
            value = copy.deepcopy(s.default)
        if value in (None, "", []) and s.kind in ("text", "list", "snapshot", "gpu"):
            continue
        if s.target == APP:
            entry[s.key] = value
        else:
            args[s.key] = value
    if args or info.fixed_args:
        entry["args"] = args
    return finalize(entry, side)


def finalize(entry, side):
    """Add what the app needs to run on this panel (fixed keys, the panel side)."""
    entry = copy.deepcopy(entry)
    info = get(entry["name"])
    entry.update(copy.deepcopy(info.fixed))
    if info.fixed_args:
        entry.setdefault("args", {})
        entry["args"].update(copy.deepcopy(info.fixed_args))
    if entry["name"] == "equalizer":
        entry.setdefault("args", {})["side"] = side
    if entry["name"] == "snap":
        entry.setdefault("args", {})["panel"] = side
    return entry


def label(entry):
    if entry is None:
        return "(empty)"
    return get(entry["name"]).label
