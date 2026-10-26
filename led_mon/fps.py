# Frames per second of a game or other app, for the fps app. Windows only.
#
# Windows has no simple "FPS of this app" reading, so this runs Intel's PresentMon console tool
# (MIT licence, https://github.com/GameTechDev/PresentMon), which records every frame apps
# present, and reads its CSV output. The Windows build bundles PresentMon.exe next to led_mon.exe.
#
# PresentMon needs administrator rights, or the Windows account in the "Performance Log Users"
# group (see the README). Without either it exits with "access denied", and the app shows a "?".

# Built In Dependencies
import os
import sys
import csv
import time
import shutil
import logging
import atexit
import threading
import subprocess
from collections import deque

# Internal Dependencies
from led_mon import platform_utils
from led_mon.platform_utils import IS_WINDOWS

log = logging.getLogger(__name__)

EXE_NAME = "PresentMon.exe"
# One ETW session per program, so the layout editor's preview doesn't stop the monitor's capture
SESSION_NAME = "led_mon_fps"
# Stop PresentMon when no fps app has been drawn for this long
IDLE_SECONDS = 15
# Wait this long before trying again after PresentMon failed to start
RETRY_SECONDS = 30
# A process that presented nothing for this long shows 0 FPS
STALE_SECONDS = 2.0

# Status values for the drawing
OK = "ok"
NOT_AVAILABLE = "not available"   # not Windows, or PresentMon.exe not found
NO_PERMISSION = "no permission"   # PresentMon couldn't start its trace session


def find_presentmon():
    """Path of PresentMon.exe: bundled with led_mon, next to the exe, or on the PATH."""
    candidates = []
    if platform_utils.is_frozen():
        candidates.append(os.path.join(sys._MEIPASS, EXE_NAME))
        candidates.append(os.path.join(os.path.dirname(sys.executable), EXE_NAME))
    candidates.append(os.path.join(platform_utils.package_dir(), EXE_NAME))
    for path in candidates:
        if os.path.isfile(path):
            return path
    return shutil.which("PresentMon") or shutil.which("PresentMon.exe")


def foreground_process_id():
    """Process ID of the window in front, or None."""
    if not IS_WINDOWS:
        return None
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value or None


_job = None


def _kill_with_us(process):
    """Put the process in a job that Windows closes when led_mon exits, even if it crashes."""
    global _job
    try:
        import win32job
        import win32api
        import win32con
        if _job is None:
            job = win32job.CreateJobObject(None, "")
            info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
            info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
            _job = job
        handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, process.pid)
        win32job.AssignProcessToJobObject(_job, handle)
    except Exception as e:
        log.debug(f"FPS app: couldn't tie PresentMon to led_mon's lifetime: {e}")


class FrameStats:
    """Recent frames per (process, swap chain), from PresentMon's CSV rows."""

    def __init__(self, window=1.0):
        self.window = window
        self.frames = {}     # (pid, swapchain) -> deque of (present time in seconds, ms between presents)
        self.names = {}      # pid -> exe name
        self.last_seen = {}  # pid -> time.monotonic() of its newest row
        self.lock = threading.Lock()
        self.columns = None

    def add_header(self, row):
        self.columns = {name: i for i, name in enumerate(row)}

    def add_row(self, row, now=None):
        """Add one CSV row; returns False if it isn't a frame row."""
        c = self.columns
        if not c or "ProcessID" not in c or "MsBetweenPresents" not in c:
            return False
        time_col = c.get("CPUStartTime", c.get("TimeInSeconds"))
        try:
            pid = int(row[c["ProcessID"]])
            ms = float(row[c["MsBetweenPresents"]])
            t = float(row[time_col]) if time_col is not None else time.monotonic()
        except (ValueError, IndexError, TypeError):
            return False
        swapchain = row[c["SwapChainAddress"]] if "SwapChainAddress" in c and c["SwapChainAddress"] < len(row) else ""
        now = time.monotonic() if now is None else now
        with self.lock:
            frames = self.frames.setdefault((pid, swapchain), deque())
            frames.append((t, ms))
            while frames and frames[0][0] < t - self.window:
                frames.popleft()
            if "Application" in c:
                self.names[pid] = row[c["Application"]]
            self.last_seen[pid] = now
        return True

    def fps(self, pid, now=None):
        """FPS of the process's busiest swap chain over the last second, 0 if it stopped presenting."""
        now = time.monotonic() if now is None else now
        with self.lock:
            if now - self.last_seen.get(pid, -1e9) > STALE_SECONDS:
                return 0.0
            best = 0.0
            for (p, _), frames in self.frames.items():
                if p != pid or not frames:
                    continue
                gaps = [ms for _, ms in frames if ms > 0]
                if gaps:
                    best = max(best, 1000.0 / (sum(gaps) / len(gaps)))
            return best

    def pids_named(self, name):
        name = name.lower()
        with self.lock:
            return [pid for pid, n in self.names.items() if n.lower() == name]

    def forget_old(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            for pid in [p for p, seen in self.last_seen.items() if now - seen > 60]:
                self.last_seen.pop(pid, None)
                self.names.pop(pid, None)
                for key in [k for k in self.frames if k[0] == pid]:
                    del self.frames[key]


class PresentMonCapture:
    """Runs PresentMon for one target (an exe name, or every process) while the app is shown."""

    def __init__(self, process_name=None):
        self.process_name = process_name or None
        self.stats = FrameStats()
        self.status = OK
        self.process = None
        self.last_used = 0.0
        self.next_start = 0.0
        self.lock = threading.Lock()
        self.exe = None

    def command(self, exe):
        cmd = [exe, "--output_stdout", "--no_console_stats", "--session_name", SESSION_NAME,
               "--stop_existing_session", "--no_track_input", "--no_track_gpu"]
        if self.process_name:
            cmd += ["--process_name", self.process_name]
        return cmd

    def touch(self):
        """Called when the app is drawn: starts PresentMon if it isn't running."""
        now = time.monotonic()
        self.last_used = now
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return
            if now < self.next_start:
                return
            self.next_start = now + RETRY_SECONDS
            exe = find_presentmon() if IS_WINDOWS else None
            if exe is None:
                if self.status != NOT_AVAILABLE:
                    log.warning("FPS app: PresentMon.exe wasn't found, so FPS can't be measured" if IS_WINDOWS
                                else "FPS app: measuring FPS is only supported on Windows")
                self.status = NOT_AVAILABLE
                return
            try:
                self.stats = FrameStats()
                self.process = subprocess.Popen(
                    self.command(exe), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, bufsize=1, creationflags=platform_utils.subprocess_flags())
            except OSError as e:
                log.warning(f"FPS app: couldn't start PresentMon: {e}")
                self.status = NOT_AVAILABLE
                return
            self.status = OK
            process = self.process
            self.exe = exe
        _kill_with_us(process)
        threading.Thread(target=self._read_output, args=(process,), daemon=True).start()
        threading.Thread(target=self._read_errors, args=(process,), daemon=True).start()
        threading.Thread(target=self._stop_when_idle, args=(process,), daemon=True).start()

    def _read_output(self, process):
        reader = csv.reader(process.stdout)
        header_seen = False
        for row in reader:
            if not row:
                continue
            if not header_seen and "ProcessID" in row:
                self.stats.add_header(row)
                header_seen = True
                continue
            self.stats.add_row(row)

    def _read_errors(self, process):
        text = process.stderr.read()
        process.wait()
        if self.process is process and process.returncode not in (0, None):
            if "access denied" in text.lower() or "access is denied" in text.lower():
                if self.status != NO_PERMISSION:
                    log.warning("FPS app: Windows refused PresentMon access to frame timing. Add your account to "
                                "the 'Performance Log Users' group (see the README), then sign out and back in.")
                self.status = NO_PERMISSION
            else:
                log.warning(f"FPS app: PresentMon stopped (exit code {process.returncode}): {text.strip()[-300:]}")

    def _stop_when_idle(self, process):
        while process.poll() is None:
            time.sleep(1.0)
            self.stats.forget_old()
            if time.monotonic() - self.last_used > IDLE_SECONDS:
                self.stop()
                return

    def stop(self):
        with self.lock:
            process, self.process = self.process, None
            # A fresh start may happen straight away next time the app is shown
            self.next_start = 0.0
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
            # Terminating skips PresentMon's own cleanup, so end its trace session the proper way
            try:
                subprocess.run([self.exe, "--terminate_existing_session", "--session_name", SESSION_NAME],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=5, creationflags=platform_utils.subprocess_flags())
            except (OSError, subprocess.SubprocessError):
                pass

    def get(self):
        """(fps or None, status). fps is None when nothing is being measured."""
        self.touch()
        if self.status != OK:
            return None, self.status
        if self.process_name:
            pids = self.stats.pids_named(self.process_name)
            return (max((self.stats.fps(p) for p in pids), default=0.0), OK)
        pid = foreground_process_id()
        return (self.stats.fps(pid) if pid else 0.0), OK


_captures = {}
_captures_lock = threading.Lock()


def capture_for(process_name=None):
    """The shared capture for an exe name (blank: whichever app is in front)."""
    key = (process_name or "").strip().lower()
    with _captures_lock:
        if key not in _captures:
            _captures[key] = PresentMonCapture(key or None)
        return _captures[key]


def stop_all():
    with _captures_lock:
        captures = list(_captures.values())
    for capture in captures:
        capture.stop()


atexit.register(stop_all)
