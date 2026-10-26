# Built In Dependencies
import time
import math
import threading
import importlib.util
import sys
import os
import re
import json
import logging

# Internal Dependencies
from led_mon.commands import Commands, send_command, do_animate
from led_mon.patterns import lightning_bolt_bot, lightning_bolt_top, lookup_table, id_patterns, symbols, numerals, icons
from led_mon.platform_utils import package_dir, plugins_dir
from led_mon.serial_ports import open_port, port_lock
from led_mon.shared_state import is_led_matrix_port

# External Dependencies
import numpy as np
from serial.tools import list_ports

log = logging.getLogger(__name__)

# Correct table orientation for visual orientation when drawn
for i in range(lookup_table.shape[0]):
    lookup_table[i] = lookup_table[i].T


def spiral_index(fill_ratio):
    return int(round(fill_ratio * 9.999999 - 0.5))

## App Draw Functions ##

# Takes up 15 rows, 7 columns, starting at y,1
# For bottom segment, the 16th row will be empty
def draw_spiral_vals(grid, cpu_values, fill_value, y):
    y += 1
    for i, v in enumerate(cpu_values):
        column_number = i % 2
        row_number = i // 2
        fill_grid = lookup_table[spiral_index(v)]
        grid[1+column_number*4:4+column_number*4, y+row_number*4:y+3+row_number*4] = fill_grid * fill_value

# Takes up 2 rows, 7 columns, starting at y, 1
def draw_memory(grid, memory_ratio, fill_value, y):
    lit_pixels = 7 * 2 * memory_ratio
    pixels_bottom = int(round(lit_pixels / 2))
    pixels_top = int(round((lit_pixels - 0.49) / 2))
    grid[1:1+pixels_top,y+1] = fill_value
    grid[1:1+pixels_bottom,y+2] = fill_value

# Takes up 12 (top segment) or 13 (bottom segment) rows, 7 columns, starting at y,1
def draw_battery(grid, battery_ratio, battery_plugged, fill_value, y,
        battery_low_thresh = 0.07, battery_low_flash_time = 2, charging_pulse_time = 3):
    if y == 19: # Placement on bottom
        bot = 33
        num_rows = 13
        lightning_bolt = lightning_bolt_bot
    else: # Placement on top (y == 3)
        bot = 16
        num_rows = 12
        lightning_bolt = lightning_bolt_top
    bat_top = y + 1
    bat_bot = bat_top + num_rows
    lit_pixels = int(round(num_rows * 7 * battery_ratio))
    pixels_base = lit_pixels // 7
    remainder = lit_pixels % 7
    if battery_ratio <= battery_low_thresh and not battery_plugged:
        if time.time() % battery_low_flash_time * 2 < battery_low_flash_time: # This will flash the battery indicator if too low
            return
    for i in range(7):
        pixels_col = pixels_base
        if i < remainder:
            pixels_col += 1
        grid[i+1,bot-pixels_col:bot] = fill_value
    if battery_plugged:
        pulse_amount = math.sin(time.time() / charging_pulse_time)
        grid[1:8,bat_top:bat_bot][lightning_bolt] -= np.rint(fill_value + 10 * pulse_amount).astype(int)
        indices = grid[1:8,bat_top:bat_bot] < 0
        grid[1:8,bat_top:bat_bot][indices] = -grid[1:8,bat_top:bat_bot][indices]
    
# Takes up 16 (top segment) or 17 (bottom segment) rows, 3 columns, starting at y,1
def draw_bar(grid, bar_ratio, bar_value, bar_x_offset = 1, y=0):
    bar_width = 3
    bar_height = 16
    lit_pixels = int(round(bar_height * bar_width * bar_ratio))
    pixels_base = lit_pixels // bar_width
    remainder = lit_pixels % bar_width
    for i in range(bar_width):
        pixels_col = pixels_base
        if i < remainder:
            pixels_col += 1
        if y == 16:
            grid[bar_x_offset+i,33-pixels_col:33] = bar_value
        else:
            grid[bar_x_offset+i,1:1+pixels_col] = bar_value
            
# Rows inside a half panel's border: 15 for the top half (rows 1-15), 16 for the bottom (17-32)
def half_rows(y):
    return range(y + 1, 16) if y == 0 else range(y + 1, 33)

# Lights a horizontal bar from column 1, up to 7 pixels long; a partly filled last pixel is dimmer
def draw_hbar(grid, ratio, fill_value, row, width = 7):
    length = max(0.0, min(1.0, ratio)) * width
    full = int(length)
    grid[1:1+full, row] = fill_value
    if full < width:
        grid[1+full, row] = int(round(fill_value * (length - full)))

# One horizontal bar per core, for CPUs with more cores than the eight spirals fit.
# More cores than rows (not on any Framework 16 CPU) are averaged in pairs, and so on.
def draw_core_bars(grid, core_values, fill_value, y):
    rows = list(half_rows(y))
    per_row = -(-len(core_values) // len(rows)) # Ceiling division
    values = [sum(core_values[i:i+per_row]) / len(core_values[i:i+per_row])
              for i in range(0, len(core_values), per_row)]
    for row, v in zip(rows, values):
        draw_hbar(grid, v, fill_value, row)

# The cpu app: a spiral per core when there are up to eight, otherwise a bar per core
cpu_core_count = 0
def draw_cpu_vals(grid, cpu_values, fill_value, y):
    global cpu_core_count
    cpu_core_count = len(cpu_values)
    if len(cpu_values) <= 8:
        draw_spiral_vals(grid, cpu_values, fill_value, y)
    else:
        draw_core_bars(grid, cpu_values, fill_value, y)

# Scrolling graph filling a half panel: one row per reading, newest at the top,
# each row a bar as long as the reading (None draws an empty row)
def draw_history_graph(grid, values, fill_value, y):
    for row, v in zip(half_rows(y), values):
        if v is not None:
            draw_hbar(grid, v, fill_value, row)

# Two short bars that grow upwards from row bottom, 3 columns wide at bar_x_offset
def draw_short_bar(grid, bar_ratio, bar_value, bar_x_offset, top, bottom):
    bar_width = 3
    bar_height = bottom - top + 1
    lit_pixels = int(round(bar_height * bar_width * max(0.0, min(1.0, bar_ratio))))
    pixels_base = lit_pixels // bar_width
    remainder = lit_pixels % bar_width
    for i in range(bar_width):
        pixels_col = pixels_base
        if i < remainder:
            pixels_col += 1
        if pixels_col:
            grid[bar_x_offset+i, bottom+1-pixels_col:bottom+1] = bar_value

# Disk on the upper part of the half panel (read left, write right), network on the
# lower part (upload left, download right), like the separate disk and net apps
def draw_disk_net(grid, disk_values, net_values, fill_value, y):
    rows = list(half_rows(y))
    disk_top, disk_bottom = rows[0], rows[6]
    net_top, net_bottom = rows[8], rows[-1]
    draw_short_bar(grid, disk_values[0], fill_value, 1, disk_top, disk_bottom)
    draw_short_bar(grid, disk_values[1], fill_value, 5, disk_top, disk_bottom)
    draw_short_bar(grid, net_values[0], fill_value, 1, net_top, net_bottom)
    draw_short_bar(grid, net_values[1], fill_value, 5, net_top, net_bottom)

# The fps app, on the whole panel: the FPS as up to three digits stacked from the top,
# and below them a graph of the last 8 seconds, newest at the top, each row's length
# relative to the highest FPS shown. A "?" means FPS can't be measured (see the log).
def draw_fps(grid, fps, history, available, fill_value):
    char_map = {**numerals, **symbols}
    rows = grid.T
    if not available:
        rows[13:19, :] = char_map["?"] * fill_value
        return
    text = str(min(999, int(round(fps or 0)))).rjust(3)
    for i, ch in enumerate(text):
        if ch != " ":
            rows[1+i*8:8+i*8, :] = char_map[ch] * fill_value
    top = max([v for v in history if v] + [1.0])
    for row, v in zip(range(26, 34), history):
        if v is not None:
            draw_hbar(grid, v / top, fill_value, row)

warned = set()
def draw_snapshot(grid, fill_value, **kwargs):
    global warned
    path = kwargs.get('path', None)
    panel = kwargs.get('panel', None)
    file = kwargs.get('file', None)
    current_dir = package_dir()
    path = os.path.join(current_dir, path)
    subdirs = [ f.name for f in os.scandir(path) if f.is_dir() and f.name in ['left', 'right']]
    subdir = panel if panel in subdirs else ''
    try:
        with open(os.path.join(path, subdir, file)) as f:
            snap = json.load(f)
            grid[:,:] = np.array(snap).T * fill_value
    except FileNotFoundError as e:
        if not file in warned:
            print(f"File {file} not found")
            warned.add(file)

def draw_chars_list(grid: np.ndarray, chars: list[str], fill_value: int, y: int):
    char_map = {**numerals, **symbols, **icons}
    grid = grid.T
    for char in chars:
        if char in char_map:
            try:
                l = len(char_map[char])
                # For three-digit temps (e.g. with Kelvin) only part of the condition icon can be rendered
                grid[y:y+l, :] = char_map[char][:34-y] * fill_value
                y += l + 1 # +1 for spacing
            except KeyError:
                raise Exception(f"Character {char} not found in numerals, symbols, or icons dictionary")
            except Exception as e:
                raise e
    grid = grid.T
    
    
## Border Draw Functions ##
    
# Draws a border around a 16 (top segment) or a 17 (bottom segment)
# x 9 grid, divided into a 2 x 4 grid. For the bottom segment,
# the last grid will have an extra row
def draw_8_x_8_grid(grid, border_value, y):
    height = 16 if y == 0 else 17
    grid[:, y] = border_value # Top
    grid[:, y+height] = border_value # Bottom
    
    grid[0, y:y+height] = border_value # Left
    grid[8, y:y+height] = border_value # Right
    grid[4, y:y+height] = border_value # Middle
    
    # Horizontal grid borders
    grid[:, y+4] = border_value
    grid[:, y+8] = border_value
    grid[:, y+12] = border_value
    
# Draws a border around a 16 (top segment) or a 17 (bottom segment)
# x 9 grid, split horizontally into two sections at the specified column
def draw_2_x_1_horiz_grid(grid, border_value, y, x_split_idx=4):
    height = 16 if y == 0 else 17
    grid[:, y] = border_value # Top
    grid[:, y+height] = border_value # Bottom

    grid[0, y:y+height] = border_value # Left
    grid[8, y:y+height] = border_value # Right
    grid[x_split_idx, y:y+height] = border_value # Middle
    
# Draws a border around a 16 (top segment) or a 17 (bottom segment),
# row section, split vertically into two sections at the specified row
def draw_1_x_2_vert_grid(grid, border_value, y, y_split_idx = 3):
    height = 16 if y == 0 else 17
    grid[:, y] = border_value # Top
    grid[:, y+height] = border_value # Bottom
    grid[:, y+y_split_idx] = border_value # Middle

    grid[0, y:y+height] = border_value # Left
    grid[8, y:y+height] = border_value # Right
    
# Draws a plain border around a 16 (top segment) or a 17 (bottom segment) row section
def draw_half_outline(grid, border_value, y):
    height = 16 if y == 0 else 17
    grid[:, y] = border_value # Top
    grid[:, y+height] = border_value # Bottom
    grid[0, y:y+height] = border_value # Left
    grid[8, y:y+height] = border_value # Right

# The cpu app's border: the 2 x 4 grid around spirals, or a plain outline around core bars
def draw_cpu_border(grid, border_value, y):
    if cpu_core_count <= 8:
        draw_8_x_8_grid(grid, border_value, y)
    else:
        draw_half_outline(grid, border_value, y)

# The disk-net app's border: split into left and right bars, and into a disk and a network part
def draw_disk_net_border(grid, border_value, y):
    draw_2_x_1_horiz_grid(grid, border_value, y)
    grid[:, y+8] = border_value

# Draws a border around the entire panel, split
# vertically into two equal segments
def draw_outline_border(grid, border_value):
    grid[:, 0] = border_value # Top
    grid[:, 16] = border_value # Middle
    grid[:, 33] = border_value # Bottom
    grid[0, :] = border_value # Left
    grid[8, :] = border_value # Right
    
# Maps an app arg value to abstract app and border draw functions
direct_draw_funcs = {
    "cpu": {
        "fn": draw_cpu_vals,
        "border": draw_cpu_border
    },
    "cpu-graph": {
        "fn": draw_history_graph,
        "border": draw_half_outline
    },
    "gpu-graph": {
        "fn": draw_history_graph,
        "border": draw_half_outline
    },
    "disk-net": {
        "fn": draw_disk_net,
        "border": draw_disk_net_border
    },
    "disk": {
        "fn": draw_bar,
        "border": draw_2_x_1_horiz_grid
    },
    "net": {
        "fn": draw_bar,
        "border": draw_2_x_1_horiz_grid
    },
    "mem": {
        "fn": draw_memory,
        "border": draw_1_x_2_vert_grid
    },
    "bat": {
        "fn": draw_battery,
        "border": draw_1_x_2_vert_grid
    },
    "fps": {
        "fn": draw_fps,
        "border": lambda *x: None # Whole panel, no border
    },
    "snap": {
        "fn": draw_snapshot,
        "border": lambda *x: None # No border
    },
    #noop
    "none": {
        "fn": lambda *x: x,
        "border": lambda *x: x
    }
}

# Draws the app for the specified arg value
def draw_app(app, *arguments, **kwargs):
    direct_draw_funcs[app].get('fn')(*arguments, **kwargs)
    
# Draws the border for the specified arg value
def draw_app_border(app, *arguments):
    direct_draw_funcs[app].get('border')(*arguments)
            
# Draw the IDs of apps currently assigned to the top and bottom of a panel
def draw_ids(grid, top, bottom, fill_value, targs=None, bargs=None):
    if isinstance(targs, list):
        t_merged_dict = {k: v for d in targs if isinstance(d, dict) for k, v in d.items()}
        id_override = t_merged_dict.get('id_key_override', None)
        if id_override:
            if t_merged_dict.get(id_override[0], False):
                top = id_override[1]
            else:
                top = id_override[2]
        b_merged_dict = {k: v for d in bargs if isinstance(d, dict) for k, v in d.items()}
        id_override = b_merged_dict.get('id_key_override', None)
        if id_override:
            if b_merged_dict.get(id_override[0], False):
                bottom = id_override[1]
            else:
                bottom = id_override[2]
    fill_grid_top = id_patterns[top]
    fill_grid_bot = id_patterns[bottom]
    grid[1:8, 1:16] = fill_grid_top * fill_value
    grid[1:8, 18:-1] = fill_grid_bot * fill_value
    
# Draw the ID of the app currently assigned to the full panel
def draw_id(grid, id, fill_value, args=None):
    id_override = args.get('id_key_override', None)
    if id_override:
        if args.get(id_override[0], False):
            id = id_override[1]
        else:
            id = id_override[2]
    fill_grid = id_patterns[id]
    grid[:,:] = fill_grid * fill_value

def draw_to_LEDs(s, grid):
    # Ensure all values are valid bytes before sending
    safe_grid = np.clip(grid, 0, 255).astype(np.uint8)
    # Hold the port for the whole frame so another thread sharing it can't interleave commands
    with port_lock(s):
        for i in range(safe_grid.shape[0]):
            params = bytearray([i]) + bytearray(safe_grid[i, :].tolist())
            send_command(s, Commands.StageCol, parameters=params)
        send_command(s, Commands.FlushCols)


def init_device(location = "1-3.2", fatal=True):
    # location is a USB location prefix (e.g. 1-3.2), or a port name (e.g. COM4) when the
    # location is unknown or the port was pinned with left-port / right-port in the config
    try:
        device_list = list_ports.comports()
        for device in device_list:
            if device.device.lower() == str(location).lower():
                return open_port(device.device)
        for device in device_list:
            if device.location and device.location.startswith(location) and is_led_matrix_port(device):
                return open_port(device.device)
        raise RuntimeError(f"No LED panel device found for location prefix '{location}'")
    except Exception as e:
        if fatal:
            log.error(f"Error getting comm ports for LED panel USB devices: {e}")
            sys.exit(1)
        raise


class DrawingThread(threading.Thread):
    def __init__(self, port_location, input_queue):
        super().__init__()
        self.daemon = True
        self.port_location = port_location
        self.serial_port = init_device(self.port_location)
        self.input_queue = input_queue
        self.animate_active= False
        self._reconnect_backoff_sec = 0.5
        self._max_reconnect_backoff_sec = 8.0
        self._next_reconnect_time = 0.0

    def set_animate(self, animate):
        do_animate(self.serial_port, animate=animate)

    def _close_serial_port(self):
        if self.serial_port is None:
            return
        try:
            self.serial_port.close()
        except Exception as e:
            log.debug(f"Error closing serial port during reconnect: {e}")
        finally:
            self.serial_port = None

    def _attempt_reconnect(self, force=False):
        now = time.monotonic()
        if not force and now < self._next_reconnect_time:
            return False
        try:
            self.serial_port = init_device(self.port_location, fatal=False)
            do_animate(self.serial_port, animate=self.animate_active)
            self._reconnect_backoff_sec = 0.5
            self._next_reconnect_time = 0.0
            log.info(f"Reconnected LED panel at location {self.port_location}")
            return True
        except Exception as e:
            self._next_reconnect_time = now + self._reconnect_backoff_sec
            self._reconnect_backoff_sec = min(self._reconnect_backoff_sec * 2, self._max_reconnect_backoff_sec)
            log.warning(f"Unable to reconnect LED panel at {self.port_location}: {e}")
            return False
    
    def run(self):
        while True:
            try:
                item = self.input_queue.get()
                if item is None:  # Sentinel to exit cleanly
                    break
                grid, animate = item

                if animate is not None:
                    self.animate_active = animate

                if self.serial_port is None and not self._attempt_reconnect():
                    continue

                if not self.animate_active:
                    draw_to_LEDs(self.serial_port, grid)
                if animate is not None:
                    do_animate(self.serial_port, animate)

            except Exception as e:
                log.error(f"Error in DrawingThread: {e}")
                self._close_serial_port()
                self._attempt_reconnect(force=True)
                time.sleep(1.0)
                continue
            
        # Clean shutdown
        self._close_serial_port()
        log.debug("DrawingThread exited cleanly")
                
###############################################################
###           Load metrics functions from plugins           ###
###############################################################
# Keep this at the end of the module to avoid circular imports
if not re.search(r"--disable-plugins|-dp", str(sys.argv)):
    # Try to find plugins directory - either in current dir or installed location
    _plugins_dir = plugins_dir()
    for file in os.listdir(_plugins_dir):
        if file.endswith('_plugin.py'):
            module_name = re.sub("_plugin.py", "", file)
            file_path = os.path.join(_plugins_dir, file)
            spec = importlib.util.spec_from_file_location(module_name, file_path)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)

            for k,v in module.direct_draw_funcs.items():
                direct_draw_funcs[k] = v
                
            from led_mon.drawing import id_patterns
            for k,v in module.id_patterns.items():
                id_patterns[k] = v
            
################################################################

