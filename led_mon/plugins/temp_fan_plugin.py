# Built In Dependencies

# Internal dependencies
from led_mon.patterns import letters_small
from led_mon import drawing
from led_mon.platform_utils import IS_WINDOWS

# External dependenciees
import psutil
import numpy as np

# Reference for fractional measure of sensor temps (in degrees Celcius)
TEMP_REF = 120

# Reference for fractional measure of fan speeds (in rpm)
MAX_FAN_SPEED = 6_000

#### Implement monitor functions ####

class TemperatureMonitor:
    @staticmethod
    def named():
        """[(sensor name, C)] for every sensor that has a reading, most important first. On Windows they come
        from the source chosen in the layout editor (psutil can't read sensors there)."""
        from led_mon.sensor_sources import named_temps
        return named_temps()

    @staticmethod
    def get(sensors=None, unit="C"):
        """The three temperatures to show (None where there is no reading), in the chosen unit."""
        from led_mon.sensor_sources import pick, to_unit
        values = pick(TemperatureMonitor.named(), sensors or [], 3)
        return [None if v is None else to_unit(v, unit) for v in values]

class FanSpeedMonitor:
    @staticmethod
    def get():
        if IS_WINDOWS:
            from led_mon.sensor_sources import windows_readings
            speeds = windows_readings()[1]
            # Always two values, so missing readings draw empty bars
            speeds = (list(speeds) + [0, 0])[:2]
            return [min(1.0, s / MAX_FAN_SPEED) for s in speeds]
        fans = psutil.sensors_fans()
        speeds = []
        for _, entries in fans.items():
            for entry in entries:
                speeds.append(entry.current)
        # We can handle up to two fan speeds on the matrix display
        return list(map(lambda x: x / MAX_FAN_SPEED, speeds))[:2]
    
temperature_monitor = TemperatureMonitor()
fan_speed_monitor = FanSpeedMonitor()

#### Implement high-level drawing functions to be called by app functions below ####

draw_app = getattr(drawing, 'draw_app')

def draw_temps(arg, grid, foreground_value, idx, sensor_1="", sensor_2="", sensor_3="", unit="C", **kwargs):
    temp_values = temperature_monitor.get([sensor_1, sensor_2, sensor_3], unit)
    draw_app(arg, grid, temp_values, foreground_value, idx)
        
def draw_fans(arg, grid, foreground_value, idx, **kwargs):
    fan_speeds = fan_speed_monitor.get()
    draw_app(arg, grid, fan_speeds[0], foreground_value, bar_x_offset=1, y=idx)
    draw_app(arg, grid, fan_speeds[1], foreground_value, bar_x_offset=5, y=idx)
    

draw_spiral_vals = getattr(drawing, 'draw_spiral_vals')
from led_mon.patterns import digits_3_x_5


def number_columns(value):
    """The 3x5 digits of a temperature and their columns, centred on the 9-wide panel.
    100-199 draws the 1 as a single column so three digits fit; None draws "--"."""
    if value is None:
        return [("-", 1), ("-", 5)]
    value = max(0, min(199, int(round(value))))
    if value >= 100:
        return [("|", 0), (str(value // 10 % 10), 2), (str(value % 10), 6)]
    if value >= 10:
        return [(str(value // 10), 1), (str(value % 10), 5)]
    return [(str(value), 3)]


def draw_temp_numbers(grid, temp_values, fill_value, y):
    """Three temperatures as numbers, one under the other, in the top (y=0) or bottom (y=16) half.
    The middle one is dimmer, so the numbers stay apart where they sit close together."""
    tops = (0, 6, 11) if y == 0 else (17, 23, 29)
    for i, (top, value) in enumerate(zip(tops, list(temp_values)[:3])):
        brightness = fill_value if i != 1 else int(round(fill_value * 0.55))
        for ch, x in number_columns(value):
            if ch == "|":
                grid[x, top:top + 5] = brightness
            else:
                grid[x:x + 3, top:top + 5] = digits_3_x_5[ch].T * brightness
draw_8_x_8_grid = getattr(drawing, 'draw_8_x_8_grid')
draw_bar = getattr(drawing, 'draw_bar')
draw_2_x_1_horiz_grid = getattr(drawing, 'draw_2_x_1_horiz_grid')

#### Implement low-level drawing functions ####
# These functions will be dynamically imported by drawing.py and called by their correcsponding app function

direct_draw_funcs = {
    "temp": {
        "fn": draw_temp_numbers,
        "border": lambda *x: None  # numbers use the whole half; no room for a border
    },
    "fan": {
        "fn": draw_bar,
        "border": draw_2_x_1_horiz_grid
    }
}

# Implement app functions that call your direct_draw functions
# These functions will be dynamically imported by led_system_monitor.py. They call the direct_draw_funcs
# defined above, providing additional capabilities that can be targeted to panel quadrants

app_funcs = [
    {
        "name": "temp",
        "fn": draw_temps
    },
    {
        "name": "fan",
        "fn":   draw_fans
    }
]

# Provide id patterns that identify your apps
# These items will be dynamically imported by drawing.py

id_patterns = {
    "temp": np.concatenate((
        letters_small["T"],
        np.zeros((1,7)),
        letters_small["M"],
        np.zeros((1,7)),
        letters_small["P"])).T,
    "fan": np.concatenate((
        letters_small["F"],
        np.zeros((1,7)),
        letters_small["A"],
        np.zeros((1,7)),
        letters_small["N"],
        np.zeros((1,7)))).T,
}