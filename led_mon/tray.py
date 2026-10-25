# System tray icon for running windowless on Windows (pystray + Pillow).

# Built In Dependencies
import logging

# Internal Dependencies
from led_mon import platform_utils

log = logging.getLogger(__name__)


def make_icon_image(size=64):
    """Draw the tray / exe icon: two LED-matrix panels of lit dots."""
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
                lit = (row + col + panel_x) % 3 != 0
                color = (255, 176, 32, 255) if lit else (90, 90, 90, 255)
                cx = (panel_x + 1.5 + col * 2) * unit
                cy = (2.5 + row * 2) * unit
                r = 0.7 * unit
                draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    return img


def run_tray(identify, next_app, set_freeze, is_frozen, reload_config, config_dir, log_file, quit):
    """Show the tray icon and block until Quit is chosen."""
    import pystray

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

    def toggle_freeze(icon, item):
        set_freeze(not is_frozen())

    def on_quit(icon, item):
        log.info("Quit requested from tray")
        quit()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Identify panels", lambda icon, item: identify()),
        pystray.MenuItem("Next app", lambda icon, item: next_app()),
        pystray.MenuItem("Freeze app switching", toggle_freeze, checked=lambda item: is_frozen()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Open config folder", open_folder),
        pystray.MenuItem("Open log", open_log),
        pystray.MenuItem("Reload config", lambda icon, item: reload_config()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )
    icon = pystray.Icon("led_mon", make_icon_image(), "LED Matrix Monitor", menu)
    icon.run()
