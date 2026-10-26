# Overall GPU utilization, per GPU, for the gpu-graph app.
#
# Windows: the "GPU Engine" performance counters, read the same way Task Manager does: for each
# GPU, add up the utilization of every process on each engine type (3D, Copy, Video Decode, ...)
# and report the busiest engine type. GPU names come from DXGI, matched by the adapter LUID that
# appears in the counter instance names.
# Linux: amdgpu's gpu_busy_percent in sysfs, and nvidia-smi for NVIDIA cards.

# Built In Dependencies
import os
import re
import glob
import time
import shutil
import logging
import subprocess

# Internal Dependencies
from led_mon.platform_utils import IS_WINDOWS, subprocess_flags

log = logging.getLogger(__name__)

# Seconds a list of GPUs or a reading is reused, so callers can ask every frame
CACHE_SECONDS = 1.0


def unique_names(names):
    """Number repeated names (two identical GPUs) so each one can be picked: 'X', 'X #2'."""
    seen = {}
    result = []
    for name in names:
        seen[name] = seen.get(name, 0) + 1
        result.append(name if seen[name] == 1 else f"{name} #{seen[name]}")
    return result


#### Windows ####

def _dxgi_adapters():
    """[(name, (luid_high, luid_low))] for the hardware GPUs, in DXGI order (the main one first)."""
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    class LUID(ctypes.Structure):
        _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]

    class DXGI_ADAPTER_DESC1(ctypes.Structure):
        _fields_ = [("Description", ctypes.c_wchar * 128), ("VendorId", wintypes.UINT),
                    ("DeviceId", wintypes.UINT), ("SubSysId", wintypes.UINT), ("Revision", wintypes.UINT),
                    ("DedicatedVideoMemory", ctypes.c_size_t), ("DedicatedSystemMemory", ctypes.c_size_t),
                    ("SharedSystemMemory", ctypes.c_size_t), ("AdapterLuid", LUID), ("Flags", wintypes.UINT)]

    DXGI_ADAPTER_FLAG_SOFTWARE = 2
    # IDXGIFactory1 {770aae78-f26f-4dba-a829-253c83d1b387}
    iid = GUID(0x770aae78, 0xf26f, 0x4dba, (ctypes.c_ubyte * 8)(0xa8, 0x29, 0x25, 0x3c, 0x83, 0xd1, 0xb3, 0x87))

    def method(obj, index, *argtypes):
        # COM call by vtable slot: IUnknown 0-2, IDXGIObject 3-6, then the interface's own methods
        vtable = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        prototype = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
        return lambda *args: prototype(vtable[index])(obj, *args)

    factory = ctypes.c_void_p()
    hr = ctypes.WinDLL("dxgi").CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory))
    if hr != 0 or not factory:
        raise OSError(f"CreateDXGIFactory1 failed ({hr & 0xffffffff:#x})")
    adapters = []
    try:
        index = 0
        while True:
            adapter = ctypes.c_void_p()
            # IDXGIFactory1::EnumAdapters1; fails with DXGI_ERROR_NOT_FOUND after the last one
            if method(factory, 12, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))(index, ctypes.byref(adapter)) != 0:
                break
            try:
                desc = DXGI_ADAPTER_DESC1()
                # IDXGIAdapter1::GetDesc1
                if method(adapter, 10, ctypes.POINTER(DXGI_ADAPTER_DESC1))(ctypes.byref(desc)) == 0:
                    if not desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE:
                        luid = (desc.AdapterLuid.HighPart & 0xffffffff, desc.AdapterLuid.LowPart)
                        adapters.append((desc.Description.strip(), luid))
            finally:
                method(adapter, 2)()  # Release
            index += 1
    finally:
        method(factory, 2)()  # Release
    return adapters


_LUID_RE = re.compile(r"luid_0x([0-9a-f]+)_0x([0-9a-f]+)_phys_(\d+).*engtype_(.*)$", re.IGNORECASE)


def busiest_engines(counter_values):
    """Task Manager's GPU %: {(luid_high, luid_low): percent} from {instance name: utilization}.

    Instance names look like pid_1234_luid_0x00000000_0x0000D1F2_phys_0_eng_0_engtype_3D.
    """
    per_engine_type = {}
    for instance, value in counter_values.items():
        match = _LUID_RE.search(instance)
        if not match:
            continue
        luid = (int(match.group(1), 16), int(match.group(2), 16))
        key = (luid, match.group(4))
        per_engine_type[key] = per_engine_type.get(key, 0.0) + max(0.0, float(value))
    result = {}
    for (luid, _), total in per_engine_type.items():
        result[luid] = min(100.0, max(result.get(luid, 0.0), total))
    return result


class WindowsGpuReader:
    COUNTER = r"\GPU Engine(*)\Utilization Percentage"

    def __init__(self):
        import win32pdh
        self._pdh = win32pdh
        self._query = win32pdh.OpenQuery()
        # The English name works whatever language Windows is in. The wildcard picks up processes
        # that start using the GPU later.
        self._counter = win32pdh.AddEnglishCounter(self._query, self.COUNTER)
        win32pdh.CollectQueryData(self._query)
        self._adapters = None
        self._adapters_time = 0.0

    def gpus(self):
        """[(name, luid)], refreshed now and then in case an external GPU is plugged in."""
        now = time.monotonic()
        if self._adapters is None or now - self._adapters_time > 60:
            adapters = _dxgi_adapters()
            names = unique_names([name for name, _ in adapters])
            self._adapters = [(name, luid) for name, (_, luid) in zip(names, adapters)]
            self._adapters_time = now
        return self._adapters

    def utilization(self):
        """{luid: percent} since the previous call."""
        pdh = self._pdh
        pdh.CollectQueryData(self._query)
        try:
            values = pdh.GetFormattedCounterArray(self._counter, pdh.PDH_FMT_DOUBLE | pdh.PDH_FMT_NOCAP100)
        except pdh.error:
            # No GPU work yet (no instances), or only one sample so far
            return {}
        return busiest_engines(values)


#### Linux ####

def _read(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


class LinuxGpuReader:
    """amdgpu cards through sysfs, NVIDIA cards through nvidia-smi."""

    def __init__(self):
        self._nvidia_smi = shutil.which("nvidia-smi")

    def _amd_cards(self):
        cards = []
        for busy in sorted(glob.glob("/sys/class/drm/card*/device/gpu_busy_percent")):
            device = os.path.dirname(busy)
            name = _read(os.path.join(device, "product_name"))
            if not name:
                card = os.path.basename(os.path.dirname(device))
                name = f"AMD GPU ({card}, {os.path.basename(os.path.realpath(device))})"
            cards.append((name, busy))
        return cards

    def _nvidia(self):
        if not self._nvidia_smi:
            return []
        try:
            out = subprocess.run([self._nvidia_smi, "--query-gpu=name,utilization.gpu",
                                  "--format=csv,noheader,nounits"], capture_output=True, text=True,
                                 timeout=2, creationflags=subprocess_flags()).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        rows = []
        for line in out.splitlines():
            name, _, value = line.rpartition(",")
            try:
                rows.append((name.strip(), float(value)))
            except ValueError:
                pass
        return rows

    def read_all(self):
        """[(name, percent or None)]"""
        rows = []
        for name, busy in self._amd_cards():
            value = _read(busy)
            rows.append((name, float(value) if value and value.isdigit() else None))
        rows += self._nvidia()
        names = unique_names([name for name, _ in rows])
        return [(name, value) for name, (_, value) in zip(names, rows)]


#### Shared ####

class GpuMonitor:
    """GPU names and utilization (0.0-1.0), on either platform. Never raises."""

    def __init__(self):
        self._reader = None
        self._failed = False
        self._last = (0.0, [])  # (monotonic time, [(name, ratio or None)])

    def _get_reader(self):
        if self._reader is None and not self._failed:
            try:
                self._reader = WindowsGpuReader() if IS_WINDOWS else LinuxGpuReader()
            except Exception as e:
                log.warning(f"GPU utilization isn't available: {e}")
                self._failed = True
        return self._reader

    def read_all(self):
        """[(name, ratio or None)] for every GPU found, cached for CACHE_SECONDS."""
        now = time.monotonic()
        if now - self._last[0] < CACHE_SECONDS:
            return self._last[1]
        rows = []
        reader = self._get_reader()
        try:
            if isinstance(reader, WindowsGpuReader):
                usage = reader.utilization()
                rows = [(name, usage.get(luid, 0.0) / 100.0) for name, luid in reader.gpus()]
            elif reader is not None:
                rows = [(name, None if v is None else min(1.0, max(0.0, v / 100.0))) for name, v in reader.read_all()]
        except Exception as e:
            log.debug(f"Error reading GPU utilization: {e}")
        self._last = (now, rows)
        return rows

    def names(self):
        return [name for name, _ in self.read_all()]

    def get(self, gpu=None):
        """Utilization of the named GPU (the first one found if gpu is blank or not found), or None."""
        rows = self.read_all()
        for name, value in rows:
            if name == gpu:
                return value
        return rows[0][1] if rows else None


_shared = None


def shared_monitor():
    global _shared
    if _shared is None:
        _shared = GpuMonitor()
    return _shared


def gpu_names():
    """Names of the GPUs on this computer, for the layout editor's GPU picker."""
    return shared_monitor().names()


if __name__ == "__main__":
    m = GpuMonitor()
    m.read_all()
    time.sleep(1.1)
    for name, value in m.read_all():
        print(f"{name}: {value}")
