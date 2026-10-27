# Built In Dependencies
from threading import Thread
import threading
import time
import queue
import sys
import re
from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
import importlib.util
import sys
import os
from collections import defaultdict
import logging

# Internal Dependencies
from led_mon import platform_utils
from led_mon.platform_utils import IS_WINDOWS, IS_LINUX
# Load API keys before the plugins are imported (via drawing) and read them
platform_utils.load_env_file()
from led_mon.drawing import draw_outline_border, draw_ids, draw_id, DrawingThread
from led_mon.monitors import CPUMonitor, MemoryMonitor, BatteryMonitor, DiskMonitor, NetworkMonitor, get_monitor_brightness
from led_mon import shared_state
from led_mon.shared_state import discover_led_devices
from led_mon.serial_ports import close_all as close_all_ports
from led_mon.rendering import make_base_app_functions, build_app_kwargs, render_panel
from led_mon import control
from led_mon import fps
from led_mon import app_settings

# External Dependencies
import numpy as np
from yaml import safe_load
# evdev (direct keyboard device access) only exists on Linux; elsewhere only pynput is used
if IS_LINUX:
    import evdev
    from evdev import ecodes
else:
    evdev = None
    ecodes = None

# Optional cross-platform keyboard input; on some Linux Wayland setups this may be unavailable.
# Not used on Windows: pynput hooks every keystroke, which antivirus scanners flag as a keylogger,
# so Windows uses registered hotkeys instead (led_mon/hotkeys.py).
try:
    if IS_WINDOWS:
        raise ImportError("Windows uses registered hotkeys")
    from pynput.keyboard import Key, Listener
    PYNPUT_AVAILABLE = True
except Exception:
    Key = None
    Listener = None
    PYNPUT_AVAILABLE = False
from serial.tools import list_ports

log = logging.getLogger(__name__)

LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

log_level = LOG_LEVELS[os.environ.get("LOG_LEVEL", "warning").lower()]
log.setLevel(log_level)

is_frozen = platform_utils.is_frozen

MODIFIER_KEYS = [('KEY_RIGHTALT', 100), ('KEY_LEFTALT', 56)]
# Key press to show widget IDs
KEY_I = ('KEY_I', 23)
# Keypress to force advance to next widget without waiting for time slice to expire
KEY_N = ('KEY_N', 49)
# Used to ensure that advance to next widget from Alt-N keypress occurs only once until key is released and perssed again
global freeze_app_switching
freeze_app_switching = False

# Set to stop the main loop (tray Quit); set reload_config_event to re-read the config file
shutdown_event = threading.Event()
reload_config_event = threading.Event()
# Show app IDs (as with Alt+I) until this monotonic time; used by the tray "Identify panels" item
id_display_until = 0.0

def request_identify(seconds=3.0):
    global id_display_until
    id_display_until = time.monotonic() + seconds

# Still frames sent from the layout editor: {'left': grid, 'right': grid}, shown until preview_until
preview_frames = {}
preview_until = 0.0
preview_lock = threading.Lock()

def show_preview(frames, seconds):
    global preview_frames, preview_until
    with preview_lock:
        preview_frames = frames
        preview_until = time.monotonic() + seconds

def clear_preview():
    global preview_until
    with preview_lock:
        preview_until = 0.0

def active_preview():
    with preview_lock:
        if time.monotonic() < preview_until:
            return preview_frames
    return None

def request_next_app():
    global evdev_next_key_pressed
    # One-shot flag, cleared by the main loop once the apps have advanced
    evdev_next_key_pressed = True

def set_freeze(frozen):
    global freeze_app_switching
    freeze_app_switching = frozen

def is_frozen_switching():
    return freeze_app_switching

# Set while the animation is stopped from the tray: the panels go dark until it is started again
display_paused = threading.Event()

def set_paused(paused):
    if paused:
        display_paused.set()
    else:
        display_paused.clear()
    log.info("Animation " + ("stopped" if paused else "started"))

def is_paused():
    return display_paused.is_set()

# pynput key helpers: Windows reports left/right Alt as alt_l / alt_r (or alt_gr)
def is_alt_key(key):
    if Key is None:
        return False
    return key in tuple(getattr(Key, name) for name in ('alt', 'alt_l', 'alt_r', 'alt_gr') if hasattr(Key, name))

def key_char(key):
    char = getattr(key, 'char', None)
    if char:
        return char.lower()
    # With Alt held, Windows may report only the virtual key code
    vk = getattr(key, 'vk', None)
    if vk is not None and 0x41 <= vk <= 0x5A:
        return chr(vk).lower()
    return None

def find_keyboard_device():
    """Auto-detect keyboard input device from /dev/input/event*"""
    if evdev is None:
        return None
    try:
        devices = [evdev.InputDevice(path) for path in evdev.list_devices()]
        for device in devices:
            # Look for devices with keyboard capabilities (EV_KEY with standard keys)
            capabilities = device.capabilities(verbose=False)
            if evdev.ecodes.EV_KEY in capabilities:
                keys = capabilities[evdev.ecodes.EV_KEY]
                # Check if device has typical keyboard keys (letters and modifiers)
                has_letters = any(k in keys for k in range(evdev.ecodes.KEY_Q, evdev.ecodes.KEY_P + 1))
                has_modifiers = any(k in keys for k in [evdev.ecodes.KEY_LEFTALT, evdev.ecodes.KEY_RIGHTALT])
                if has_letters and has_modifiers:
                    return device.path
    except Exception as e:
        log.warning(f"Warning: Could not auto-detect keyboard device: {e}")
    return None

def evcode_keyloop():
    global freeze_app_switching, evdev_next_key_pressed
    kbd_path = find_keyboard_device()
    keys_pressed = set()
    if kbd_path:
        try:
            device = evdev.InputDevice(kbd_path)
            for event in device.read_loop():
                if event.type == ecodes.EV_KEY:
                    if event.value == 1:
                        keys_pressed.add(event.code)
                    elif event.value == 0:
                        keys_pressed.discard(event.code)

                    if (ecodes.KEY_LEFTALT in keys_pressed or ecodes.KEY_RIGHTALT in keys_pressed) and ecodes.KEY_F in keys_pressed:
                        freeze_app_switching = True
                    elif (ecodes.KEY_LEFTALT in keys_pressed or ecodes.KEY_RIGHTALT in keys_pressed) and ecodes.KEY_U in keys_pressed:
                        freeze_app_switching = False

                    if (ecodes.KEY_LEFTALT in keys_pressed or ecodes.KEY_RIGHTALT in keys_pressed) and ecodes.KEY_N in keys_pressed:
                        evdev_next_key_pressed = True
        except (PermissionError, FileNotFoundError, OSError) as e:
            log.warning(f"Warning: Cannot access keyboard device {kbd_path}: {e}")
                
if evdev is not None:
    Thread(target=evcode_keyloop, daemon=True).start()


def get_config(args):
    # Check for --config-file program arg first, and then CONFIG_FILE environment variable (used by NixOS module),
    # then config-local.yaml / config.yaml (next to the app on Linux, in %APPDATA%\led_mon on Windows)
    config_file = platform_utils.resolve_config_file(args.config_file)
    with open(config_file, 'r') as f:
        config = safe_load(f)
    # Optional left-port / right-port (e.g. COM4 or /dev/ttyACM0) and left-location / right-location
    # (USB position, e.g. 1-4.2) override panel auto-detection
    shared_state.set_overrides_from_config(config)
    return config

def find_led_devices():
    led_devices = discover_led_devices() or []
    if IS_WINDOWS and not led_devices:
        # When started at login, USB devices may still be enumerating
        for _ in range(15):
            time.sleep(2)
            led_devices = discover_led_devices() or []
            if led_devices:
                break
    return led_devices

def list_apps(base_apps, plugin_apps, quads):
    max_len = max(map(lambda x: len(x), base_apps + plugin_apps))
    print("Installed Apps:")
    print("   " + "Name".ljust(max_len+3, ' ') + "Source".ljust(12, " ") + "Configuration")
    configured_apps = defaultdict(list)
    for quad in quads.values():
        for app in quad:
            del app["app"] # Just the list name in the yaml file, value will be set to None
            configured_apps[app["name"]].append(app)
    for app in [*base_apps , *plugin_apps]:
        if not app in configured_apps:
            print(f"   {app.ljust(max_len+3, ' ')}", end='')
            print(f"{'plugin app'.ljust(12, ' ')if app in plugin_apps else 'base app'.ljust(12, ' ')}", end='')
            print(f"None")
        for _app in configured_apps[app]:
            print(f"   {app.ljust(max_len+3, ' ')}", end='')
            print(f"{'plugin app'.ljust(12, ' ')if app in plugin_apps else 'base app'.ljust(12, ' ')}", end='')
            print(f"{_app}")
        
device = None
def app(args, base_apps, plugin_apps):    
    # Initialize evdev device for key listening if not disabled
    global device
    if not args.no_key_listener and evdev is not None:
        # Try to auto-detect keyboard device first
        kbd_path = find_keyboard_device()
        if kbd_path:
            try:
                device = evdev.InputDevice(kbd_path)
                log.info(f"Using keyboard device: {kbd_path}")
            except (PermissionError, FileNotFoundError, OSError) as e:
                log.warning(f"Warning: Cannot access keyboard device {kbd_path}: {e}")
                log.warning("Try running: sudo usermod -a -G input $USER (then log out and back in)")
                log.warning("Or use --no-key-listener to disable keyboard monitoring.")
                args.no_key_listener = True
                device = None
        else:
            log.warning("Warning: Could not find a suitable keyboard device for evdev monitoring.")
            log.warning("Key listening will be limited to pynput only (if available).")
            device = None

    ################################################################################
    ### Parse config file to enable control of apps by quadrant and by time slice ##
    ################################################################################
    config = get_config(args)
    # Track index of active app, for cycling through apps in a quadrant by time slice
    app_idx = defaultdict(int)
    # Each app optionally specifes how long it stays active before cycling to the next one
    app_duration = defaultdict(int)

    def apply_config(config):
        nonlocal duration, quads, top_left, bottom_left, top_right, bottom_right
        duration = config['duration'] #Default config to be applied if not set in an app
        quads = config['quadrants']
        top_left,  bottom_left, top_right, bottom_right, = \
            quads['top-left'], quads['bottom-left'], quads['top-right'], quads['bottom-right']
        app_idx.clear()
        for quad in quads:
            app_idx[quad] = 0
        app_duration.clear()
        for quad in quads.items():
            for app in quad[1]:
                # Keyed by quadrant too, so the same app can have a different duration in each quadrant
                app_duration[(quad[0], app["name"])] = int(app.get("duration", duration))

    duration = quads = top_left = bottom_left = top_right = bottom_right = None
    apply_config(config)

    if args.list_apps:
        list_apps(base_apps, plugin_apps, quads)
        sys.exit()

    # Only one instance may drive the panels; a second launch exits silently (Windows)
    if not platform_utils.acquire_single_instance():
        log.info("led_mon is already running")
        sys.exit(0)

    led_devices = find_led_devices()
    if not len(led_devices):
        log.error("No LED devices found")
        if use_tray(args):
            platform_utils.show_error("No LED Matrix panels were found. Check that the modules are installed, "
                                      "and that no other program (such as the LED Matrix GUI) is using them.")
        sys.exit(0)
    elif len(led_devices) == 1:
        log.warning(f"Only one LED device found ({led_devices[0]}). Right panel args will be ignored")
    else:
        log.info(f"Found LED devices: Left: {led_devices[0]}, Right: {led_devices[1]}")
    # Use the port name when the USB location is unknown
    locations = list(map(lambda x: x[0] or x[1], led_devices))
    drawing_queues = []
    
    # Track key presses to reveal app ID in each quadrant or panel
    global alt_pressed
    alt_pressed = False
    global i_pressed
    i_pressed = False
    global n_pressed
    n_pressed = False
    global evdev_next_key_pressed
    evdev_next_key_pressed = False
    
    # Set up monitors and brightness parameters
    min_background_brightness = 12
    max_background_brightness = 35
    min_foreground_brightness = 24
    max_foreground_brightness = 160
    # Follows the screen brightness unless a level was picked with the brightness slider
    brightness = app_settings.Brightness()

    cpu_monitor = CPUMonitor()
    memory_monitor = MemoryMonitor()
    battery_monitor = BatteryMonitor()
    disk_monitor = DiskMonitor()
    network_monitor = NetworkMonitor()

    # Setup left panel drawing queue
    left_drawing_queue = queue.Queue(2)
    left_drawing_thread = DrawingThread(locations[0], left_drawing_queue)
    left_drawing_thread.set_animate(False)
    left_drawing_thread.start()    
    drawing_queues.append(left_drawing_queue)

    # Setup right panel drawing queue (if panel is present)
    if len(locations) == 2:
        right_drawing_queue = queue.Queue(2)
        right_drawing_thread = DrawingThread(locations[1], right_drawing_queue)
        right_drawing_thread.set_animate(False)
        right_drawing_thread.start()
        drawing_queues.append(right_drawing_queue)
    
    app_functions = make_base_app_functions(cpu_monitor, memory_monitor, battery_monitor, disk_monitor, network_monitor)

    panel_scope_conflicts_logged = set()
    suppressed_panel_apps = {
        'top-left': None,
        'bottom-left': None,
        'top-right': None,
        'bottom-right': None,
    }

    def get_panel_scope_owner(panel_name, top_quadrant_name, top_app, bottom_quadrant_name, bottom_app):
        top_claims_panel = top_app.get("display", True) and top_app.get("scope", None) == "panel"
        bottom_claims_panel = bottom_app.get("display", True) and bottom_app.get("scope", None) == "panel"

        if top_claims_panel and bottom_claims_panel:
            conflict_key = (panel_name, top_app.get("name"), bottom_app.get("name"))
            if conflict_key not in panel_scope_conflicts_logged:
                panel_scope_conflicts_logged.add(conflict_key)
                log.warning(
                    f"Both {top_quadrant_name} ({top_app.get('name')}) and {bottom_quadrant_name} ({bottom_app.get('name')}) "
                    f"claim scope='panel' on {panel_name} panel; preferring {top_quadrant_name}."
                )
            return top_quadrant_name, top_app
        if top_claims_panel:
            return top_quadrant_name, top_app
        if bottom_claims_panel:
            return bottom_quadrant_name, bottom_app
        return None, None

    def maybe_dispose_suppressed_panel_app(quadrant_name):
        if quadrant_name is None:
            return
        app = quads[quadrant_name][app_idx[quadrant_name]]
        dispose_fn = app.get('dispose-fn', None)
        if dispose_fn is None:
            return

        signature = (app.get('name'), app_idx[quadrant_name], dispose_fn)
        if suppressed_panel_apps.get(quadrant_name) == signature:
            return

        func = app_functions.get(dispose_fn)
        if func is None:
            log.error(f"Unrecognized dispose function '{dispose_fn}' for suppressed app '{app.get('name')}' in {quadrant_name}")
            suppressed_panel_apps[quadrant_name] = signature
            return

        try:
            func(**build_app_kwargs(app))
            suppressed_panel_apps[quadrant_name] = signature
        except Exception as e:
            log.error(f"Error disposing suppressed app {app.get('name')} in {quadrant_name}: {e}")
    
    def on_press(key):
        global alt_pressed, i_pressed, n_pressed, freeze_app_switching
        # If pynput is unavailable, this callback will not be used; guard anyway
        if not PYNPUT_AVAILABLE:
            return
        try:
            char = key_char(key)
            if char == 'i':
                i_pressed = True
            elif char == 'n':
                # Advance once per Alt+N press (matches the evdev listener), not on every loop while held
                if alt_pressed and not n_pressed:
                    request_next_app()
                n_pressed = True
            elif char == 'f' and alt_pressed:
                freeze_app_switching = True
            elif char == 'u' and alt_pressed:
                freeze_app_switching = False
            elif is_alt_key(key):
                alt_pressed = True
        except Exception:
            # Be defensive; ignore unexpected pynput key objects
            pass

    def on_release(key):
        global alt_pressed, i_pressed, n_pressed
        if not PYNPUT_AVAILABLE:
            return
        try:
            char = key_char(key)
            if char == 'i':
                i_pressed = False
            elif char == 'n':
                n_pressed = False
            elif is_alt_key(key):
                alt_pressed = False
        except Exception:
            pass
        
    #################################################
    ###      Load app functions from plugins      ###
    #################################################
    if not re.search(r"--disable-plugins|-dp", str(sys.argv)):
        # Try to find plugins directory - either in current dir or installed location
        plugins_dir = platform_utils.plugins_dir()
        for file in os.listdir(plugins_dir):
            if file.endswith('_plugin.py'):
                module_name = re.sub("_plugin.py", "", file)
                file_path = os.path.join(plugins_dir, file)
                spec = importlib.util.spec_from_file_location(module_name, file_path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)

                for obj in module.app_funcs:
                    app_functions[obj["name"]] = obj["fn"]


    # Track last draw time, to enable cycling through them for each quadrannt
    base_time_map = {
        # Each dict stores last draw time for each app
        'top-left': defaultdict(lambda: time.monotonic()),
        'bottom-left': defaultdict(lambda: time.monotonic()),
        'top-right': defaultdict(lambda: time.monotonic()),
        'bottom-right': defaultdict(lambda: time.monotonic()),
    }

    # Used to detect that the key listener was activated, for restoring anination mode after ID display
    global latch_key_combo
    latch_key_combo = False
    def dispose_active_apps():
        for quadrant, apps in quads.items():
            app = apps[app_idx[quadrant]]
            dispose_fn = app.get('dispose-fn', None)
            if dispose_fn and dispose_fn in app_functions:
                try:
                    app_functions[dispose_fn](**build_app_kwargs(app))
                except Exception as e:
                    log.error(f"Error disposing app {app.get('name')} in {quadrant}: {e}")

    def reload_config():
        try:
            new_config = get_config(args)
        except Exception as e:
            log.error(f"Could not reload config, keeping the current one: {e}")
            return
        dispose_active_apps()
        apply_config(new_config)
        for quadrant_times in base_time_map.values():
            quadrant_times.clear()
        for quadrant in suppressed_panel_apps:
            suppressed_panel_apps[quadrant] = None
        log.info("Config reloaded")

    previewing = False
    blanked = False

    def handle_control_request(message):
        cmd = message.get('cmd')
        if cmd == 'status':
            return {'ports': [device for _, device in led_devices],
                    'config_file': platform_utils.resolve_config_file(args.config_file),
                    'brightness': brightness.get()}
        if cmd == 'brightness':
            return {'brightness': brightness.set(message.get('follow_screen'), message.get('level'))}
        if cmd == 'reload':
            clear_preview()
            reload_config_event.set()
            return {}
        if cmd == 'preview':
            show_preview(message.get('frames', {}), float(message.get('seconds', 30)))
            return {}
        if cmd == 'resume':
            clear_preview()
            return {}
        if cmd == 'identify':
            request_identify()
            return {}
        return {'ok': False, 'error': f"Unknown command {cmd}"}

    if IS_WINDOWS:
        control.serve(handle_control_request)

    def render_iteration(args):
        global latch_key_combo, next_key_fired, freeze_app_switching, evdev_next_key_pressed
        nonlocal previewing, blanked
        try:
            if reload_config_event.is_set():
                reload_config_event.clear()
                reload_config()

            # Animation stopped from the tray: turn the panels off once, then wait
            if display_paused.is_set():
                if not blanked:
                    # Stop apps that draw on their own (the equalizer) so they don't light the panels
                    dispose_active_apps()
                    for draw_queue in drawing_queues:
                        # Stop any panel animation, then draw a dark frame once it has stopped
                        draw_queue.put((np.zeros((9,34), dtype = int), False))
                        draw_queue.put((np.zeros((9,34), dtype = int), None))
                    blanked = True
                time.sleep(0.25)
                # Restart panel animations when the display comes back, as after the ID display
                latch_key_combo = True
                return
            blanked = False

            screen_brightness = brightness.ratio(get_monitor_brightness)
            background_value = int(screen_brightness * (max_background_brightness - min_background_brightness) + min_background_brightness)
            foreground_value = int(screen_brightness * (max_foreground_brightness - min_foreground_brightness) + min_foreground_brightness)
            shared_state.foreground_value = foreground_value
            
            # Clamp brightness values to the valid byte range
            background_value = max(0, min(255, background_value))
            foreground_value = max(0, min(255, foreground_value))

            grid = np.zeros((9,34), dtype = int)
            
            # Check for key combo using both evdev (if available) and pynput
            active_keys = device.active_keys(verbose=True) if device else []
            evdev_id_key_pressed = True if (MODIFIER_KEYS[0] in active_keys or MODIFIER_KEYS[1] in active_keys) and KEY_I in active_keys and device else False
            pynput_id_key_pressed = i_pressed and alt_pressed
            id_key_combo_active = (evdev_id_key_pressed or pynput_id_key_pressed) and not args.no_key_listener
            # Tray "Identify panels" works even with --no-key-listener
            id_key_combo_active = id_key_combo_active or time.monotonic() < id_display_until
            shared_state.id_key_press_active = id_key_combo_active
            # evdev_next_key_pressed is a one-shot flag set by the evdev listener, pynput Alt+N and the tray
            next_key_combo_active = evdev_next_key_pressed

            # Track when an app is changed in either panel, used to manage animation state
            idx_changed = {
                left_drawing_queue: False,
            }
            if len(drawing_queues) > 1:
                idx_changed[right_drawing_queue] = False
            # A set of apps to be (potentially) disposed
            apps_to_dispose = []

            active_apps = {
                'top-left': top_left[app_idx['top-left']],
                'bottom-left': bottom_left[app_idx['bottom-left']],
                'top-right': top_right[app_idx['top-right']],
                'bottom-right': bottom_right[app_idx['bottom-right']],
            }

            left_owner_quadrant, _ = get_panel_scope_owner(
                "left",
                'top-left', active_apps['top-left'],
                'bottom-left', active_apps['bottom-left']
            )
            right_owner_quadrant, _ = get_panel_scope_owner(
                "right",
                'top-right', active_apps['top-right'],
                'bottom-right', active_apps['bottom-right']
            )

            suppressed_quadrants_pre_rotation = set()
            if left_owner_quadrant == 'top-left':
                suppressed_quadrants_pre_rotation.add('bottom-left')
            elif left_owner_quadrant == 'bottom-left':
                suppressed_quadrants_pre_rotation.add('top-left')
            if right_owner_quadrant == 'top-right':
                suppressed_quadrants_pre_rotation.add('bottom-right')
            elif right_owner_quadrant == 'bottom-right':
                suppressed_quadrants_pre_rotation.add('top-right')
            # Keep quadrant schedules aligned even when one quadrant is suppressed by
            # a sibling panel-scope app; otherwise manual Alt+N can create lasting skew.
            now = time.monotonic()
            for quadrant, apps in quads.items():
                    app = apps[app_idx[quadrant]]
                    suppressed_for_rotation = quadrant in suppressed_quadrants_pre_rotation
                    can_force_next = next_key_combo_active
                    should_advance = (now - base_time_map[quadrant][app['name']] >= int(app_duration[(quadrant, app['name'])])) or can_force_next
                    if should_advance and not freeze_app_switching:
                            if can_force_next:
                                evdev_next_key_pressed = False
                            # Suppressed quadrants still rotate state, but should not
                            # trigger draw-side animation/dispose transitions.
                            if not suppressed_for_rotation:
                                if 'left' in quadrant:
                                    idx_changed[left_drawing_queue] = True
                                elif len(drawing_queues) > 1:
                                    idx_changed[right_drawing_queue] = True
                                if 'dispose-fn' in app:
                                        apps_to_dispose.append(app)
                            app_idx[quadrant] = (app_idx[quadrant] + 1) % len(quads[quadrant])
                            app = apps[app_idx[quadrant]]
                            base_time_map[quadrant][app['name']] = now
            left_top_app = top_left[app_idx['top-left']]
            left_bottom_app = bottom_left[app_idx['bottom-left']]
            right_top_app = top_right[app_idx['top-right']]
            right_bottom_app = bottom_right[app_idx['bottom-right']]

            left_owner_quadrant, left_owner_app = get_panel_scope_owner(
                "left",
                'top-left', left_top_app,
                'bottom-left', left_bottom_app
            )
            right_owner_quadrant, right_owner_app = get_panel_scope_owner(
                "right",
                'top-right', right_top_app,
                'bottom-right', right_bottom_app
            )

            left_args = [left_owner_app] if left_owner_app else [left_top_app, left_bottom_app]
            right_args = [right_owner_app] if right_owner_app else [right_top_app, right_bottom_app]

            left_suppressed_quadrant = None
            if left_owner_quadrant == 'top-left':
                left_suppressed_quadrant = 'bottom-left'
            elif left_owner_quadrant == 'bottom-left':
                left_suppressed_quadrant = 'top-left'

            right_suppressed_quadrant = None
            if right_owner_quadrant == 'top-right':
                right_suppressed_quadrant = 'bottom-right'
            elif right_owner_quadrant == 'bottom-right':
                right_suppressed_quadrant = 'top-right'

            currently_suppressed_quadrants = set(filter(None, [left_suppressed_quadrant, right_suppressed_quadrant]))
            for quadrant in suppressed_panel_apps:
                if quadrant not in currently_suppressed_quadrants:
                    suppressed_panel_apps[quadrant] = None

            # Capture animating state so we can restart it if necessary after stopping it for ID display
            animating_left = any(arg.get("animate", False) for arg in left_args)
            animating_right = any(arg.get("animate", False) for arg in right_args)
            
            if id_key_combo_active:
                # Show app IDs for each quadrant or panel
                draw_outline_border(grid, background_value)
                # If app takes up entire panel, draw the panel app ID only
                if left_owner_app:
                    draw_id(grid, left_owner_app['name'], foreground_value, args=left_owner_app.get('args', None))
                else:
                    draw_ids(grid, left_args[0]['name'], left_args[1]['name'], foreground_value,
                        targs=left_args[0].get('args', None), bargs=left_args[1].get('args', None))
                left_drawing_queue.put((grid, False))
                
                if len(drawing_queues) > 1:  # Right panel exists
                    grid = np.zeros((9,34), dtype = int)
                    draw_outline_border(grid, background_value)
                    if right_owner_app:
                        draw_id(grid, right_owner_app['name'], foreground_value, args=right_owner_app.get('args', None))
                    else:
                        draw_ids(grid, right_args[0]['name'], right_args[1]['name'], foreground_value,
                            targs=right_args[0].get('args', None), bargs=right_args[1].get('args', None))
                    right_drawing_queue.put((grid, False))
                time.sleep(0.1)
                latch_key_combo = True
                return

            # A still frame sent from the layout editor takes over both panels until it times out
            frames = active_preview()
            if frames is not None:
                if not previewing:
                    # Stop apps that draw on their own (the equalizer) so they don't overwrite the frame
                    dispose_active_apps()
                    previewing = True
                for i, draw_queue in enumerate(drawing_queues):
                    frame = frames.get('left' if i == 0 else 'right')
                    if frame is not None:
                        draw_queue.put((np.array(frame, dtype=int), False))
                time.sleep(0.25)
                latch_key_combo = True
                return
            previewing = False

            for i, draw_queue in enumerate(drawing_queues):
                grid = np.zeros((9,34), dtype = int)
                if i == 0:
                    panel = 'left'
                    _args = left_args
                    suppressed_quadrant = left_suppressed_quadrant
                else:
                    panel = 'right'
                    _args = right_args
                    suppressed_quadrant = right_suppressed_quadrant

                maybe_dispose_suppressed_panel_app(suppressed_quadrant)
                # For persistent-draw apps, we don't submit the grid to the drawing queue
                persistent_draw, animate = render_panel(grid, _args, app_functions, foreground_value, background_value, panel)
                do_animate = None
                if idx_changed[draw_queue]:
                    do_animate = animate
                    idx_changed[draw_queue] = False
                        
                # Restart animation if it was stopped for ID display
                if latch_key_combo:
                    if (animating_left and panel == 'left') or (animating_right and panel == 'right'):
                        do_animate = True
                if not persistent_draw:
                    draw_queue.put((grid, do_animate))
            latch_key_combo = False
            for app in apps_to_dispose:
                dispose_fn = app.get('dispose-fn', None)
                if dispose_fn:
                    func = app_functions[dispose_fn]
                    kwargs = build_app_kwargs(app)
                    func(**kwargs)
            del apps_to_dispose
            time.sleep(0.1)     
        except KeyboardInterrupt:
            raise
        except Exception as e:
            log.error(f"Error in main loop: {e}")
            time.sleep(1.0)
            
    # Informative message if pynput is not available
    if not PYNPUT_AVAILABLE and not IS_WINDOWS and not args.no_key_listener:
        log.warning("Info: pynput is unavailable; running in evdev-only mode. Use Ctrl+C to exit.")

    def run_main_loop():
        platform_utils.init_com_for_thread()
        if IS_WINDOWS and not args.no_key_listener:
            from led_mon import hotkeys
            hotkeys.start({
                'i': request_identify,
                'n': request_next_app,
                'f': lambda: set_freeze(True),
                'u': lambda: set_freeze(False),
            })
        # Prefer pynput if available; fall back silently if it fails to start (common on Wayland)
        if PYNPUT_AVAILABLE and not args.no_key_listener:
            try:
                with Listener(on_press=on_press, on_release=on_release):
                    while not shutdown_event.is_set():
                        render_iteration(args)
            except Exception as e:
                log.error(f"Warning: pynput listener could not be started ({e}). Falling back to evdev-only mode.")
                while not shutdown_event.is_set():
                    render_iteration(args)
        else:
            while not shutdown_event.is_set():
                render_iteration(args)

    def shutdown():
        """Turn the panels dark, stop drawing and release both serial ports (tray Quit)."""
        dispose_active_apps()
        drawing_threads = [left_drawing_thread] + ([right_drawing_thread] if len(locations) == 2 else [])
        for draw_queue in drawing_queues:
            stop_drawing_queue(draw_queue, blank=True)
        for thread in drawing_threads:
            thread.join(timeout=3)
        close_all_ports()
        fps.stop_all()

    if use_tray(args):
        from led_mon import tray
        main_loop_thread = Thread(target=run_main_loop, daemon=True)
        main_loop_thread.start()
        from led_mon.brightness_popup import TrayBrightnessPopup
        brightness_popup = TrayBrightnessPopup(brightness)
        tray.run_tray(
            identify=request_identify,
            next_app=request_next_app,
            set_freeze=set_freeze,
            is_frozen=is_frozen_switching,
            reload_config=reload_config_event.set,
            config_dir=platform_utils.user_config_dir(),
            log_file=platform_utils.log_file_path(),
            quit=shutdown_event.set,
            edit_layout=lambda: open_layout_editor(args),
            set_paused=set_paused,
            is_paused=is_paused,
            show_brightness=brightness_popup.show,
            ohm_path=app_settings.load_settings().get("open_hardware_monitor_path", ""),
        )
        # The tray returns after Quit
        brightness_popup.stop()
        shutdown_event.set()
        main_loop_thread.join(timeout=5)
        shutdown()
    else:
        run_main_loop()

    log.info("Exiting")

def open_layout_editor(args):
    editor_args = ['--edit-layout']
    if args.config_file:
        editor_args += ['--config-file', args.config_file]
    platform_utils.launch_self(*editor_args)

def use_tray(args):
    # Windows runs windowless with a tray icon; --no-tray keeps the console behaviour for debugging
    return IS_WINDOWS and not args.no_tray

def stop_drawing_queue(draw_queue, blank=False):
    if blank:
        # Stop any panel animation and draw a dark frame, so the panels don't keep showing the last frame
        try:
            draw_queue.put((np.zeros((9,34), dtype = int), False), timeout=1)
            draw_queue.put((np.zeros((9,34), dtype = int), None), timeout=1)
            # Wait for the dark frame to be drawn rather than dropping it to make room below
            draw_queue.put(None, timeout=2)
            return
        except queue.Full:
            log.warning("Could not turn a panel dark before exiting")
    # The queue is small and may be full, so make room for the exit sentinel
    while True:
        try:
            draw_queue.put_nowait(None)
            return
        except queue.Full:
            try:
                draw_queue.get_nowait()
            except queue.Empty:
                pass

def main(args):
    # A windowless Windows exe has no console; reuse the one it was started from for --help / --list-apps
    platform_utils.attach_parent_console()
    base_apps = ["cpu", "cpu-graph", "gpu-graph", "net", "disk", "disk-net", "mem-bat", "fps", "snap"]
    plugin_apps = []
    ###############################################################
    ###  Load additional app names from plugins for listing insalled apps  ###
    if not re.search(r"--disable-plugins|-dp", str(sys.argv)):
        # Try to find plugins directory - either in current dir or installed location
        plugins_dir = platform_utils.plugins_dir()
        for file in os.listdir(plugins_dir):
            if file.endswith('_plugin.py'):
                module_name = re.sub("_plugin.py", "", file)
                file_path = os.path.join(plugins_dir, file)
                spec = importlib.util.spec_from_file_location(module_name, file_path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                plugin_apps += map(lambda o: o["name"], module.app_funcs)
    #################################################################
    parser = ArgumentParser(prog="FW LED System Monitor", add_help=False,
                            description="Displays system performance metrics in the Framework 16 LED Matrix input module",
                            formatter_class=ArgumentDefaultsHelpFormatter)
    mode_group = parser.add_argument_group()
    mode_group.add_argument("--help", "-h", action="help",
        help="Show this help message and exit")
    
    mode_group.add_argument("--no-key-listener", "-nkl", action="store_true", help="Do not listen for key presses")
    mode_group.add_argument("--disable-plugins", "-dp", action="store_true", help="Do not load any plugin code")
    mode_group.add_argument("--list-apps", "-la", action="store_true", help="List the installed apps, and exit")
    mode_group.add_argument("--config-file", "-cf", default=None, help="Absolute path to custom config file")
    mode_group.add_argument("--edit-layout", action="store_true", help="Open the layout editor window instead of running the monitor")
    mode_group.add_argument("--diagnose", action="store_true",
        help="Print the LED panels, serial ports and helper apps that are found, and exit")
    if IS_WINDOWS:
        mode_group.add_argument("--no-tray", action="store_true", help="Run in the console without a tray icon")
    
    args = parser.parse_args()
    if args.diagnose:
        from led_mon import diagnose
        diagnose.run(args.config_file)
        return
    if args.edit_layout:
        from led_mon.editor import run_editor
        run_editor(args)
        return
    app(args, base_apps, plugin_apps)

if __name__ == "__main__":
    platform_utils.setup_logging()
    main(sys.argv)


