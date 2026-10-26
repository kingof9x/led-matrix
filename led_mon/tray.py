# System tray icon for running windowless on Windows (pystray + Pillow).
#
# Menu (left or right click):
#   Edit layout... / Brightness...
#   Stop animation / Start animation   - the icon turns grey while stopped
#   Identify panels / Next app / Freeze app switching
#   Open Hardware Monitor               - only when it is found
#   Open Framework Control              - only when it is installed
#   Open config folder / Open log / Reload config
#   Start with Windows                  - checkbox
#   Quit

# Built In Dependencies
import logging

# Internal Dependencies
from led_mon import platform_utils, external_apps, startup

log = logging.getLogger(__name__)

DISPLAY_NAME = "LED Matrix Monitor"
LIT_COLOR = (255, 176, 32, 255)
DIM_COLOR = (90, 90, 90, 255)


def make_icon_image(size=64, lit=True):
    """Draw the tray / exe icon: two LED-matrix panels of lit dots, grey when stopped."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    unit = size / 16
    for panel_x in (2, 9):
        draw.rounded_rectangle(
            [panel_x * unit, 1 * unit, (panel_x + 5) * unit, 15 * unit],
            radius=unit, fill=(40, 40, 40, 255),
        )
        for col in range(2):
            for row in range(6):
                on = lit and (row + col + panel_x) % 3 != 0
                color = LIT_COLOR if on else DIM_COLOR
                cx = (panel_x + 1.5 + col * 2) * unit
                cy = (2.5 + row * 2) * unit
                r = 0.7 * unit
                draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    return img


class AppFinder:
    """Remembers where Open Hardware Monitor and Framework Control are; refreshed each time the menu opens."""

    def __init__(self, ohm_path=""):
        self.ohm_path = ohm_path
        self.ohm = None
        self.framework_control = None

    def refresh(self):
        try:
            self.ohm = external_apps.find_open_hardware_monitor(self.ohm_path)
        except Exception as e:
            log.error(f"Error looking for Open Hardware Monitor: {e}")
            self.ohm = None
        try:
            self.framework_control = external_apps.find_framework_control()
        except Exception as e:
            log.error(f"Error looking for Framework Control: {e}")
            self.framework_control = None


def animation_label(paused):
    return "Start animation" if paused else "Stop animation"


def tooltip(paused):
    return DISPLAY_NAME + (" (stopped)" if paused else "")


def startup_checked(item):
    try:
        return startup.is_enabled()
    except Exception as e:
        log.error(f"Could not read the startup setting: {e}")
        return False


def build_menu(pystray, actions, finder, is_paused, is_frozen, edit_layout_available, startup_supported=None):
    """The tray menu. actions maps a name to the function called when that item is chosen."""
    startup_supported = startup.supported() if startup_supported is None else startup_supported
    item = pystray.MenuItem
    return pystray.Menu(
        item("Edit layout...", actions["edit_layout"], default=True, visible=edit_layout_available),
        item("Brightness...", actions["brightness"]),
        pystray.Menu.SEPARATOR,
        item(lambda i: animation_label(is_paused()), actions["toggle_animation"]),
        item("Identify panels", actions["identify"]),
        item("Next app", actions["next_app"]),
        item("Freeze app switching", actions["toggle_freeze"], checked=lambda i: is_frozen()),
        pystray.Menu.SEPARATOR,
        item("Open Hardware Monitor", actions["open_ohm"], visible=lambda i: finder.ohm is not None),
        item("Open Framework Control", actions["open_fc"], visible=lambda i: finder.framework_control is not None),
        pystray.Menu.SEPARATOR,
        item("Open config folder", actions["open_folder"]),
        item("Open log", actions["open_log"]),
        item("Reload config", actions["reload_config"]),
        pystray.Menu.SEPARATOR,
        item("Start with Windows", actions["toggle_startup"], checked=startup_checked, visible=startup_supported),
        pystray.Menu.SEPARATOR,
        item("Quit", actions["quit"]),
    )


def _icon_class(pystray, finder):
    """On Windows, open the menu on left click too, and look for the apps again before showing it."""
    if not platform_utils.IS_WINDOWS:
        return pystray.Icon
    from pystray._util import win32

    class ClickMenuIcon(pystray.Icon):
        def _on_notify(self, wparam, lparam):
            if lparam in (win32.WM_LBUTTONUP, win32.WM_RBUTTONUP):
                finder.refresh()
                self._update_menu()  # visibility of the app items is decided when the menu is built
                lparam = win32.WM_RBUTTONUP
            return super()._on_notify(wparam, lparam)

    return ClickMenuIcon


def run_tray(identify, next_app, set_freeze, is_frozen, reload_config, config_dir, log_file, quit,
             edit_layout=None, set_paused=None, is_paused=None, show_brightness=None, ohm_path=""):
    """Show the tray icon and block until Quit is chosen."""
    import pystray

    platform_utils.enable_dpi_awareness()
    finder = AppFinder(ohm_path)
    finder.refresh()

    def notify(icon, message):
        if not message:
            return
        log.warning(message)
        try:
            icon.notify(message, DISPLAY_NAME)
        except Exception:
            pass

    def open_folder(icon, item):
        try:
            platform_utils.open_path(config_dir)
        except Exception as e:
            log.error(f"Could not open {config_dir}: {e}")

    def open_log(icon, item):
        try:
            platform_utils.open_path(log_file)
        except Exception as e:
            log.error(f"Could not open {log_file}: {e}")

    def open_editor(icon, item):
        try:
            edit_layout()
        except Exception as e:
            log.error(f"Could not open the layout editor: {e}")

    def open_brightness(icon, item):
        try:
            show_brightness()
        except Exception as e:
            notify(icon, f"Could not open the brightness slider: {e}")

    def toggle_animation(icon, item):
        paused = not is_paused()
        set_paused(paused)
        icon.icon = make_icon_image(lit=not paused)
        icon.title = tooltip(paused)

    def toggle_freeze(icon, item):
        set_freeze(not is_frozen())

    def open_ohm(icon, item):
        finder.refresh()
        notify(icon, external_apps.open_target(finder.ohm, "Open Hardware Monitor"))

    def open_fc(icon, item):
        finder.refresh()
        notify(icon, external_apps.open_target(finder.framework_control, "Framework Control"))

    def toggle_startup(icon, item):
        try:
            enable = not startup.is_enabled()
            startup.set_enabled(enable)
            log.info("Start with Windows " + ("on" if enable else "off"))
        except Exception as e:
            notify(icon, f"Could not change the startup setting: {e}")
        icon.update_menu()

    def on_quit(icon, item):
        log.info("Quit requested from tray")
        quit()
        icon.stop()

    actions = {
        "edit_layout": open_editor,
        "brightness": open_brightness,
        "toggle_animation": toggle_animation,
        "identify": lambda icon, item: identify(),
        "next_app": lambda icon, item: next_app(),
        "toggle_freeze": toggle_freeze,
        "open_ohm": open_ohm,
        "open_fc": open_fc,
        "open_folder": open_folder,
        "open_log": open_log,
        "reload_config": lambda icon, item: reload_config(),
        "toggle_startup": toggle_startup,
        "quit": on_quit,
    }
    menu = build_menu(pystray, actions, finder, is_paused, is_frozen, edit_layout is not None)
    icon_class = _icon_class(pystray, finder)
    icon = icon_class("led_mon", make_icon_image(lit=not is_paused()), tooltip(is_paused()), menu)
    icon.run()
