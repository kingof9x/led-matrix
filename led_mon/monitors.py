# Built In Dependencies
import time
import os

# External Dependencies
import psutil

# Reference for fractional measure of sensor temps (in degrees Celcius)
TEMP_REF = 120

# Reference for fractional measure of fan speeds (in rpm)
MAX_FAN_SPEED = 6_000

if os.name == 'nt':
    import wmi


def _read_backlight_ratio(device):
    """Return a normalized brightness in [0.0, 1.0] for the given backlight device."""
    with open(f'/sys/class/backlight/{device}/max_brightness', 'r') as f:
        max_b = int(f.read())
    with open(f'/sys/class/backlight/{device}/brightness', 'r') as f:
        b = int(f.read())

    if max_b <= 0:
        return 1.0

    r = b / max_b
    if r < 0.0:
        return 0.0
    if r > 1.0:
        return 1.0
    return r

class DiskMonitor:
    def __init__(self, hysterisis_time = 20):
        self.read_usage_history = [0]
        self.write_usage_history = [0]
        self.history_times = [0]
        self.highest_read_rate = 0.00001
        self.highest_write_rate = 0.00001
        self.max_history_size = hysterisis_time

    def get(self):
        try:
            disk_io = psutil.disk_io_counters()
            read_usage = disk_io.read_bytes
            write_usage = disk_io.write_bytes
            self.read_usage_history.append(read_usage)
            self.write_usage_history.append(write_usage)
            self.history_times.append(time.time())
            if len(self.read_usage_history) > self.max_history_size:
                self.read_usage_history = self.read_usage_history[-self.max_history_size:]
                self.write_usage_history = self.write_usage_history[-self.max_history_size:]
                self.history_times = self.history_times[-self.max_history_size:]

            read_diff = self.read_usage_history[-1] - self.read_usage_history[0]
            write_diff = self.write_usage_history[-1] - self.write_usage_history[0]
            time_diff = self.history_times[-1] - self.history_times[0]
            read_rate = read_diff / time_diff
            write_rate = write_diff / time_diff
            self.highest_read_rate = max(self.highest_read_rate, read_rate)
            self.highest_write_rate = max(self.highest_write_rate, write_rate)
            read_percent = min(1.0, read_rate / self.highest_read_rate)
            write_percent = min(1.0, write_rate / self.highest_write_rate)
            return read_percent, write_percent
        except Exception as e:
            print(f"Error in DiskMonitor.get(): {e}")
            return 0, 0

class NetworkMonitor:
    def __init__(self, hysterisis_time = 20):
        self.sent_usage_history = [0]
        self.recv_usage_history = [0]
        self.history_times = [0]
        self.highest_sent_rate = 0.00001
        self.highest_recv_rate = 0.00001
        self.max_history_size = hysterisis_time

    def get(self):
        try:
            net_io = psutil.net_io_counters()
            sent_usage = net_io.bytes_sent
            recv_usage = net_io.bytes_recv
            self.sent_usage_history.append(sent_usage)
            self.recv_usage_history.append(recv_usage)
            self.history_times.append(time.time())
            if len(self.sent_usage_history) > self.max_history_size:
                self.sent_usage_history = self.sent_usage_history[-self.max_history_size:]
                self.recv_usage_history = self.recv_usage_history[-self.max_history_size:]
                self.history_times = self.history_times[-self.max_history_size:]

            sent_diff = self.sent_usage_history[-1] - self.sent_usage_history[0]
            recv_diff = self.recv_usage_history[-1] - self.recv_usage_history[0]
            time_diff = self.history_times[-1] - self.history_times[0]
            sent_rate = sent_diff / time_diff
            recv_rate = recv_diff / time_diff
            self.highest_sent_rate = max(self.highest_sent_rate, sent_rate)
            self.highest_recv_rate = max(self.highest_recv_rate, recv_rate)
            sent_percent = min(1.0, sent_rate / self.highest_sent_rate)
            recv_percent = min(1.0, recv_rate / self.highest_recv_rate)
            return sent_percent, recv_percent
        except Exception as e:
            print(f"Error in NetworkMonitor.get(): {e}")
            return 0, 0

def _linux_core_groups(logical_count):
    """Logical CPUs grouped by physical core, from sysfs (AMD numbers a core's threads N and N+8, not N and N+1)."""
    groups = []
    seen = set()
    for cpu in range(logical_count):
        if cpu in seen:
            continue
        try:
            with open(f'/sys/devices/system/cpu/cpu{cpu}/topology/thread_siblings_list') as f:
                text = f.read().strip()
        except OSError:
            return None
        members = set()
        for part in text.split(','):
            if '-' in part:
                a, b = part.split('-')
                members.update(range(int(a), int(b) + 1))
            elif part:
                members.add(int(part))
        members = sorted(m for m in members if m < logical_count) or [cpu]
        seen.update(members)
        groups.append(members)
    return groups


def cpu_core_groups():
    """Indexes into psutil.cpu_percent(percpu=True), one list per physical core."""
    logical = psutil.cpu_count() or 1
    physical = psutil.cpu_count(logical=False) or logical
    groups = None
    if os.name != 'nt':
        groups = _linux_core_groups(logical)
    if not groups:
        # Windows numbers a core's threads next to each other
        per_core = max(1, logical // physical)
        groups = [list(range(i, min(i + per_core, logical))) for i in range(0, logical, per_core)]
    return groups


class CPUMonitor:
    def __init__(self, hysterisis_time = 10):
        self.core_groups = cpu_core_groups()
        self.cpu_count = len(self.core_groups)
        self.cpu_usage_history = [[] for _ in range(self.cpu_count)]
        self.history_times = []
        self.max_history_size = hysterisis_time

    def get(self):
        try:
            cpu_usage = psutil.cpu_percent(percpu=True)
            for i, group in enumerate(self.core_groups):
                # Combine logical cores
                useage = len(group) * max(cpu_usage[j] for j in group)
                if useage > 100:
                    useage = 100
                self.cpu_usage_history[i].append(useage / 100.0)
            self.history_times.append(time.time())
            if len(self.cpu_usage_history[0]) > self.max_history_size:
                for i in range(self.cpu_count):
                    self.cpu_usage_history[i] = self.cpu_usage_history[i][-self.max_history_size:]
                    self.history_times = self.history_times[-self.max_history_size:]
            cpu_percentages = [sum(core_history) / self.max_history_size for core_history in self.cpu_usage_history]
            # Somehow cpu_percentages can have values greater than 1 so we clamp them
            cpu_percentages = [min(1.0, max(0.0, v)) for v in cpu_percentages]
            return cpu_percentages
        except Exception as e:
            print(f"Error in CPUMonitor.get(): {e}")
            return [0] * self.cpu_count

class HistoryMonitor:
    """Keeps the last few readings of a value (0.0-1.0, unless clamp is off) for a scrolling graph, newest first.

    read() is called at most once per interval seconds; it may return None when there is no
    reading (for example no GPU found), which shows as an empty row.
    """
    def __init__(self, read, interval = 1.0, size = 16, clamp = True):
        self.read = read
        self.clamp = clamp
        self.interval = interval
        self.size = size
        self.samples = []
        self.last_time = None

    def get(self):
        now = time.monotonic()
        if self.last_time is None or now - self.last_time >= self.interval:
            self.last_time = now
            try:
                value = self.read()
            except Exception as e:
                print(f"Error in HistoryMonitor.get(): {e}")
                value = None
            if value is not None and self.clamp:
                value = min(1.0, max(0.0, value))
            self.samples = ([value] + self.samples)[:self.size]
        return list(self.samples)

def overall_cpu_ratio():
    # Not per-CPU, so it doesn't disturb the CPU app's own percpu readings
    return psutil.cpu_percent() / 100.0

class MemoryMonitor:
    @staticmethod
    def get():
        return psutil.virtual_memory().percent / 100.0
    

class BatteryMonitor:
    @staticmethod
    def get():
        battery = psutil.sensors_battery()
        if battery is not None:
            battery_percentage = battery.percent / 100.0
            if os.name == "nt":
                battery_plugged = battery.power_plugged
            else:
                bat_status = open('/sys/class/power_supply/BAT1/status', 'r').read().strip()
                battery_plugged = (bat_status != 'Discharging')
            return battery_percentage, battery_plugged

_wmi_brightness_cache = (0.0, 1.0)  # (monotonic time, value)

def _get_wmi_brightness():
    # A WMI query takes tens of ms and the main loop runs at 10 Hz, so cache it briefly
    global _wmi_brightness_cache
    now = time.monotonic()
    if now - _wmi_brightness_cache[0] < 2.0:
        return _wmi_brightness_cache[1]
    try:
        value = wmi.WMI(namespace='wmi').WmiMonitorBrightness()[0].CurrentBrightness / 100.0
    except Exception:
        value = 1.0
    _wmi_brightness_cache = (now, value)
    return value

def get_monitor_brightness():
    try:
        if os.name == 'nt':
            return _get_wmi_brightness()
        else:
            # On Linux, use max_brightness to normalize into [0.0, 1.0]
            try:
                return _read_backlight_ratio('amdgpu_bl2')
            except Exception:
                return _read_backlight_ratio('amdgpu_bl1')
    except Exception:
        # Reasonable default if anything goes wrong
        return 1.0

if __name__ == "__main__":
    print(get_monitor_brightness())