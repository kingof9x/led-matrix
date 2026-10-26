# Drawing one panel's frame from its active apps.
# Shared by the monitor main loop and the layout editor's preview, so the preview matches the panels.

# Built In Dependencies
import logging

# Internal Dependencies
from led_mon.drawing import draw_app, draw_app_border
from led_mon.monitors import HistoryMonitor, overall_cpu_ratio
from led_mon import gpu as gpu_info
from led_mon import fps as fps_info

log = logging.getLogger(__name__)

# Brightness used when the screen brightness is unknown (the editor preview)
DEFAULT_FOREGROUND = 160
DEFAULT_BACKGROUND = 35


def make_base_app_functions(cpu_monitor, memory_monitor, battery_monitor, disk_monitor, network_monitor):
    """The built-in apps, keyed by app name. Plugins add more (see plugins-README.md).

    Each takes **kwargs so app args meant for the renderer (such as border: false) are ignored.
    """
    def draw_cpu(arg, grid, foreground_value, idx, **kwargs):
        last_cpu_values = cpu_monitor.get()
        draw_app(arg, grid, last_cpu_values, foreground_value, idx)

    def draw_mem_bat(arg, grid, foreground_value, idx, **kwargs):
        last_memory_values = memory_monitor.get()
        last_battery_values = battery_monitor.get()
        draw_app("mem", grid, last_memory_values, foreground_value, idx)
        draw_app("bat", grid, last_battery_values[0], last_battery_values[1], foreground_value, idx+3)

    def draw_disk(arg, grid, foreground_value, idx, **kwargs):
        last_disk_read, last_disk_write = disk_monitor.get()
        draw_app(arg, grid, last_disk_read, foreground_value, bar_x_offset=1, y=idx) # Read
        draw_app(arg, grid, last_disk_write, foreground_value, bar_x_offset=5, y=idx) # Write

    def draw_net(arg, grid, foreground_value, idx, **kwargs):
        last_network_upload, last_network_download = network_monitor.get()
        draw_app(arg, grid, last_network_upload, foreground_value, bar_x_offset=1, y=idx)
        draw_app(arg, grid, last_network_download, foreground_value, bar_x_offset=5, y=idx)

    def draw_snap(arg, grid, foreground_value, idx, **kwargs):
        draw_app(arg, grid, foreground_value, **kwargs)

    cpu_history = HistoryMonitor(overall_cpu_ratio)
    def draw_cpu_graph(arg, grid, foreground_value, idx, **kwargs):
        draw_app(arg, grid, cpu_history.get(), foreground_value, idx)

    # One history per GPU, keyed by the gpu arg (blank: the first GPU found)
    gpu_histories = {}
    def draw_gpu_graph(arg, grid, foreground_value, idx, gpu=None, **kwargs):
        key = gpu or ""
        if key not in gpu_histories:
            gpu_histories[key] = HistoryMonitor(lambda: gpu_info.shared_monitor().get(key or None))
        draw_app(arg, grid, gpu_histories[key].get(), foreground_value, idx)

    # Readings once a second for the graph under the FPS, per watched app
    fps_histories = {}
    def draw_fps(arg, grid, foreground_value, idx, process=None, **kwargs):
        capture = fps_info.capture_for(process)
        value, status = capture.get()
        if capture not in fps_histories:
            fps_histories[capture] = HistoryMonitor(lambda: capture.get()[0], size=8, clamp=False)
        draw_app(arg, grid, value, fps_histories[capture].get(), status == fps_info.OK, foreground_value)

    def draw_disk_net(arg, grid, foreground_value, idx, **kwargs):
        draw_app(arg, grid, disk_monitor.get(), network_monitor.get(), foreground_value, idx)

    return {
        "cpu": draw_cpu,
        "mem-bat": draw_mem_bat,
        "disk": draw_disk,
        "net": draw_net,
        "snap": draw_snap,
        "cpu-graph": draw_cpu_graph,
        "gpu-graph": draw_gpu_graph,
        "disk-net": draw_disk_net,
        "fps": draw_fps,
        "none": lambda *x: x # noop
    }


def build_app_kwargs(app):
    if 'args' in app and app['args']:
        # Args must be hashable, to support function caching, so convert list values to tuples
        return {k: tuple(v) if isinstance(v, list) else v for k, v in app['args'].items()}
    return {}


def render_panel(grid, panel_apps, app_functions, foreground_value, background_value, panel='left'):
    """Draw a panel's active apps into grid (9 x 34).

    panel_apps is [owner] when one app has the whole panel (scope: panel), otherwise [top, bottom].
    Returns (persistent_draw, animate): persistent_draw means an app draws to the panel itself
    (the equalizer), so the grid must not be sent; animate is the last drawn app's animate setting.
    """
    persistent_draw = False
    animate = False
    for j, arg in enumerate(panel_apps):
        arg_name = arg['name']
        if arg.get("display", True) == False: continue
        if arg.get('persistent-draw', False):
            persistent_draw = True
        if j == 0:
            idx = 0
            loc = 'top'
        else:
            idx = 16
            loc = 'bottom'
        kwargs = build_app_kwargs(arg)
        try:
            func = app_functions[arg_name]
            func(arg_name, grid, foreground_value, idx, **kwargs)
            animate = arg.get("animate", False)
        except KeyError:
            log.error(f"Unrecognized app {arg_name} for {loc} {panel}")
        except Exception as e:
            log.error(f"Error {e} with app {arg_name} for {loc} {panel}")
        # Single border draw for mem and bat together
        if arg_name == 'mem-bat': arg_name = 'mem'
        if kwargs.get('border', True) and arg.get("scope", None) != "panel":
            try:
                draw_app_border(arg_name, grid, background_value, idx)
            except KeyError:
                pass
    return persistent_draw, animate
