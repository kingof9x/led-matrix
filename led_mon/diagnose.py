# --diagnose: print the LED panels, serial ports and helper apps led_mon finds, then exit.
# Runs without opening any port, so it also works while the monitor is running.

# Built In Dependencies
import sys
import logging

# Internal Dependencies
from led_mon import platform_utils, app_settings, shared_state

log = logging.getLogger(__name__)


def _usb_id(device):
    vid = f"{device.vid:04X}" if getattr(device, "vid", None) is not None else "----"
    pid = f"{device.pid:04X}" if getattr(device, "pid", None) is not None else "----"
    return f"{vid}:{pid}"


def _panel(entry):
    if entry is None:
        return "not found"
    location, device = entry
    return f"{device}  (USB location {location})" if location else f"{device}  (USB location unknown)"


def report(config_file=None, device_list=None, find_apps=None):
    """The diagnose report, as a list of lines."""
    from serial.tools import list_ports
    lines = ["led_mon diagnose"]
    lines.append(f"Log file:      {platform_utils.log_file_path()}")
    lines.append(f"Settings file: {app_settings.settings_path()}")

    config = {}
    try:
        path = platform_utils.resolve_config_file(config_file)
        lines.append(f"Config file:   {path}")
        import yaml
        with open(path, "r") as f:
            config = yaml.safe_load(f) or {}
    except Exception as e:
        lines.append(f"Config file:   could not be read ({e})")
    shared_state.set_overrides_from_config(config)
    for side in ("left", "right"):
        for key in (f"{side}-port", f"{side}-location"):
            if config.get(key):
                lines.append(f"  {key}: {config[key]}")

    if device_list is None:
        device_list = list(list_ports.comports())
    lines.append("")
    lines.append("Serial ports:")
    if not device_list:
        lines.append("  none")
    for d in device_list:
        mark = "  <- LED Matrix" if shared_state.is_led_matrix_port(d) else ""
        lines.append(f"  {d.device}  location={d.location}  usb={_usb_id(d)}  {d.description}{mark}")

    panels = shared_state.discover_led_devices(device_list) or []
    lines.append("")
    lines.append("LED panels:")
    lines.append(f"  Left:  {_panel(panels[0] if len(panels) > 0 else None)}")
    lines.append(f"  Right: {_panel(panels[1] if len(panels) > 1 else None)}")

    if find_apps is None and platform_utils.IS_WINDOWS:
        def find_apps():
            from led_mon import external_apps
            ohm_path = app_settings.load_settings().get("open_hardware_monitor_path", "")
            return (external_apps.find_open_hardware_monitor(ohm_path), external_apps.find_framework_control())
    if find_apps is not None:
        lines.append("")
        try:
            ohm, fc = find_apps()
            lines.append(f"Open Hardware Monitor: {ohm.path if ohm else 'not found'}")
            lines.append(f"Framework Control:     {fc.path if fc else 'not found'}")
        except Exception as e:
            lines.append(f"Could not look for Open Hardware Monitor / Framework Control: {e}")
    return lines


def run(config_file=None):
    lines = report(config_file)
    if sys.stdout is None:
        # The windowless exe has no console when it isn't started from a command prompt: use the log
        for line in lines:
            log.info(line)
    else:
        print("\n".join(lines), flush=True)
