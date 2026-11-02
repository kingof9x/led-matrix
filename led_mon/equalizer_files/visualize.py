
# Built-in Dependencies
import time
import os
import threading
import argparse
import logging
import signal
import sys
from led_mon import shared_state
from led_mon.drawing import DrawingThread

# Internal Dependencies
from led_mon.shared_state import discover_led_devices
from led_mon.patterns import id_patterns
from led_mon.commands import Commands, send_command
from led_mon.serial_ports import open_port
import queue

# External Dependencies
import numpy as np
from scipy.signal import butter, sosfiltfilt
try:
    # WASAPI loopback capture of the default output device
    import soundcard as sc
except Exception:
    # Lets the tests import this module where soundcard can't load
    sc = None


level = logging.WARNING
if os.getenv("LOG_LEVEL", "").lower() == "debug":
    level = logging.DEBUG
elif os.getenv("LOG_LEVEL", "").lower() == "error":
    level = logging.ERROR
elif os.getenv("LOG_LEVEL", "").lower() == "info":
    level = logging.INFO

logging.basicConfig(
    level=level,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

log = logging.getLogger(__name__)

# How often to re-check the default output device, and when captured audio counts as stale
DEFAULT_DEVICE_CHECK_SEC = 3
STALE_AUDIO_SEC = 0.25
DEFAULT_ZERO_FRAME_NOTIFY_DELAY_SEC = 4.0
DEFAULT_SILENT_PULSE_AFTER_SEC = 12.0
DEFAULT_SILENT_PULSE_PERIOD_SEC = 3.8
DEFAULT_SILENT_PULSE_REVEAL_SEC = 5.0

# Configuration
SAMPLE_RATE = 48000
CHUNK_SIZE = 1024
UPDATE_RATE = 0.03 # 33 fps

# 9 frequency bands (If you use an EasyEffects filter, match the centers as closely as possible)
BAND_CENTERS = [31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000]  # Hz
Q = 1.414
# Pre-compute bandpass filters (used in python file mode)
filters = []
for fc in BAND_CENTERS:
    low = fc / Q
    high = fc * Q
    sos = butter(4, [low, high], btype='band', fs=SAMPLE_RATE, output='sos')
    filters.append(sos)

# Scale RMS to 0–34 range for --eq
def scale_rms(rms, min_db=-60, max_db=0):
    db = 20 * np.log10(rms + 1e-10)
    normalized = np.clip((db - min_db) / (max_db - min_db), 0, 1)
    return int(normalized * 34)

def clamp_positive_float(value, default):
    try:
        parsed = float(value)
        return parsed if parsed > 0 else float(default)
    except (TypeError, ValueError):
        return float(default)

def clamp_nonnegative_int(value, default):
    try:
        parsed = int(value)
        return parsed if parsed >= 0 else int(default)
    except (TypeError, ValueError):
        return int(default)

# Lock for writing to LED matrix device
device_lock = threading.Lock()
_device_write_locks = {}
_device_write_locks_guard = threading.Lock()

def get_device_write_lock(device_name):
    with _device_write_locks_guard:
        lock = _device_write_locks.get(device_name)
        if lock is None:
            lock = threading.Lock()
            _device_write_locks[device_name] = lock
        return lock

# Pattern ids from inputmodule-rs (inputmodule-control led-matrix --pattern)
PATTERN_IDS = {'gradient': 1, 'zigzag': 4, 'all-on': 5}

def get_notification_pattern(name):
    name = name.lower()
    if 'headphone' in name or 'headset' in name or 'audio expansion' in name:
        return 'zigzag'
    if 'speaker' in name:
        return 'all-on'
    return 'gradient'

def draw_source_change_cue(source):
    # Send the pattern over the already-open (shared) serial handles
    pattern = get_notification_pattern(source)
    devices = discover_led_devices() or []
    with device_lock:
        if shared_state.id_key_press_active:
            return
        for _, port_name in devices:
            try:
                s = open_port(port_name)
                try:
                    for _ in range(3):
                        send_command(s, Commands.Pattern, parameters=[PATTERN_IDS[pattern]])
                finally:
                    s.close()
            except Exception as e:
                log.debug(f"Could not draw source change cue on {port_name}: {e}")

def eq_grid(levels):
    """Same picture as `inputmodule-control led-matrix --eq`: bars growing up and down from the middle row."""
    grid = np.zeros((9, 34), dtype=int)
    row = 34 // 2
    for col, val in enumerate(levels[:9]):
        val = max(0, min(34, int(val)))
        above = val // 2
        below = val - above
        grid[col, row:row + above] = 0xFF
        grid[col, row - below:row] = 0xFF
    return grid

def _com_init_mta():
    # soundcard uses COM; each thread that touches it needs COM initialised
    try:
        import ctypes
        COINIT_MULTITHREADED = 0x0
        ctypes.windll.ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    except Exception:
        pass

class WasapiCapture():
    """Audio capture feeding Equalizer.audio_buffer.

    playback: WASAPI loopback of the default output. The default output is re-checked every few seconds,
    so switching from speakers to headphones is followed. microphone: the default (or named) microphone.
    """

    def __init__(self, equalizer, input_mode, input_device=None):
        self.eq = equalizer
        self.input_mode = input_mode
        self.input_device = input_device
        self.generation = 0
        self.stopped = threading.Event()
        self.current_id = None
        self._error_logged = False

    def __enter__(self):
        threading.Thread(target=self._supervise, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.stopped.set()
        return False

    def _active(self):
        return not self.stopped.is_set() and not self.eq.done

    def _resolve_source(self):
        if self.input_mode == 'playback':
            speaker = sc.default_speaker()
            return sc.get_microphone(speaker.id, include_loopback=True), speaker.name
        mic = sc.get_microphone(self.input_device) if self.input_device else sc.default_microphone()
        return mic, mic.name

    def _supervise(self):
        _com_init_mta()
        while self._active():
            try:
                source, name = self._resolve_source()
                if source.id != self.current_id:
                    first = self.current_id is None
                    self.current_id = source.id
                    self.generation += 1
                    log.info(f"Equalizer capturing from {name}")
                    threading.Thread(target=self._capture, args=(source, self.generation), daemon=True).start()
                    if not first and self.input_mode == 'playback':
                        draw_source_change_cue(name)
                self._error_logged = False
            except Exception as e:
                if not self._error_logged:
                    self._error_logged = True
                    log.warning(f"Equalizer could not open the {self.input_mode} audio device: {e}")
                self.current_id = None
            self.stopped.wait(DEFAULT_DEVICE_CHECK_SEC)

    def _capture(self, source, generation):
        # A capture thread can block while nothing plays, so a device change starts a new thread
        # and an outdated one exits when it next wakes up.
        _com_init_mta()
        try:
            with source.recorder(samplerate=SAMPLE_RATE, blocksize=CHUNK_SIZE) as recorder:
                while self._active() and generation == self.generation:
                    data = recorder.record(numframes=CHUNK_SIZE)
                    if generation != self.generation:
                        break
                    with self.eq.buffer_lock:
                        self.eq.audio_buffer = np.asarray(data, dtype=np.float32)
                        self.eq.audio_buffer_ts = time.monotonic()
        except Exception as e:
            log.warning(f"Equalizer audio capture stopped: {e}")
            if generation == self.generation:
                # Let the supervisor reopen the device on its next check
                self.current_id = None

class Equalizer():
    
    def __init__(self, device_location):
        self.done = False
        self.device_name = None
        self.audio_buffer = np.zeros((CHUNK_SIZE, 2), dtype=np.float32)
        self.audio_buffer_ts = time.monotonic()
        self.buffer_lock = threading.Lock()
        self.queue = queue.Queue(2)
        self.drawing_thread = DrawingThread(device_location, self.queue)
        self.drawing_thread.start()
        
    def stop(self):
        if not self.done:
            self.done = True
            try:
                self.queue.put_nowait(None)  # Sentinel to stop DrawingThread
            except queue.Full:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self.queue.put_nowait(None)
                except queue.Full:
                    pass
        device_name = self.device_name if self.device_name else "<unknown>"
        log.debug(f"Stop equalizer on device {device_name}")

    def queue_frame(self, grid, animate=False):
        frame = (grid, animate)
        try:
            self.queue.put_nowait(frame)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.queue.put_nowait(frame)
            except queue.Full:
                pass
    
    def cleanup(self, sig=None, frame=None):
        self.stop()

    def draw_eq_levels(self, levels):
        # inputmodule-control can't open a COM port this app already holds, so draw through our own handle
        self.queue_frame(eq_grid(levels), False)
        
    def draw_inverted_silence_pulse(self, elapsed_sec, pulse_period_sec, reveal_sec):
        pulse_period_sec = clamp_positive_float(pulse_period_sec, DEFAULT_SILENT_PULSE_PERIOD_SEC)
        reveal_sec = clamp_positive_float(reveal_sec, DEFAULT_SILENT_PULSE_REVEAL_SEC)
        base_foreground = max(1, int(shared_state.foreground_value))
        phase = (elapsed_sec / pulse_period_sec) % 1.0
        # Triangle wave avoids the visual "pause" at min/max that a sinusoid can create.
        wave = 1.0 - abs((2.0 * phase) - 1.0)

        x = np.arange(9, dtype=float)[:, None]
        y = np.arange(34, dtype=float)[None, :]
        center_x = 4.0
        center_y = 16.5
        norm_x = (x - center_x) / 4.0
        norm_y = (y - center_y) / 16.5
        radial = np.sqrt((norm_x ** 2) + (norm_y ** 2))
        angle = np.arctan2(norm_y, norm_x)

        # Warp radial distance into a star-like contour.
        star_scale = 1.0 + (0.28 * np.cos(4.0 * angle))
        star_radius = radial / np.clip(star_scale, 0.45, None)
        pulse_radius = 0.22 + (1.20 * wave)
        shell_width = 0.11
        shell = np.exp(-((star_radius - pulse_radius) ** 2) / (2.0 * (shell_width ** 2)))
        fill = np.clip((pulse_radius - star_radius) / max(pulse_radius, 1e-6), 0.0, 1.0)
        shape = np.clip((0.50 * fill) + (0.50 * shell), 0.0, 1.0)

        brightness = 0.30 + (0.70 * wave)
        grid = np.rint(np.clip(base_foreground * shape * brightness, 0, 255)).astype(int)
        grid = np.where(shape > 0.04, np.maximum(grid, 1), grid)

        reveal = np.clip(elapsed_sec / reveal_sec, 0.0, 1.0)
        paused_mask = (id_patterns['equalizer_paused'] > 0).astype(float)
        grid = np.rint(grid * (1.0 - (paused_mask * reveal))).astype(int)
        self.queue_frame(grid, False)

    def run(
        self,
        channel,
        external_filter,
        device_name,
        input_mode='playback',
        input_device=None,
        level_gain=1.0,
        noise_gate_level=0,
        silence_level_sum_threshold=0,
        zero_frame_notify_delay_sec=DEFAULT_ZERO_FRAME_NOTIFY_DELAY_SEC,
        silent_pulse_after_sec=DEFAULT_SILENT_PULSE_AFTER_SEC,
        silent_pulse_period_sec=DEFAULT_SILENT_PULSE_PERIOD_SEC,
        silent_pulse_reveal_sec=DEFAULT_SILENT_PULSE_REVEAL_SEC,
    ):
        self.device_name = device_name
        input_mode = str(input_mode or 'playback').strip().lower()
        if input_mode not in ('playback', 'microphone'):
            log.warning(f"Unknown equalizer input mode '{input_mode}', defaulting to playback.")
            input_mode = 'playback'

        zero_frame_notify_delay_sec = max(
            0.0,
            clamp_positive_float(zero_frame_notify_delay_sec, DEFAULT_ZERO_FRAME_NOTIFY_DELAY_SEC)
        )
        legacy_silent_pulse_after_sec = clamp_positive_float(silent_pulse_after_sec, DEFAULT_SILENT_PULSE_AFTER_SEC)
        silent_pulse_after_sec = min(zero_frame_notify_delay_sec, legacy_silent_pulse_after_sec)
        silent_pulse_period_sec = clamp_positive_float(silent_pulse_period_sec, DEFAULT_SILENT_PULSE_PERIOD_SEC)
        silent_pulse_reveal_sec = clamp_positive_float(silent_pulse_reveal_sec, DEFAULT_SILENT_PULSE_REVEAL_SEC)
        level_gain = max(0.1, clamp_positive_float(level_gain, 1.0))
        noise_gate_level = min(34, clamp_nonnegative_int(noise_gate_level, 0))
        silence_level_sum_threshold = clamp_nonnegative_int(silence_level_sum_threshold, 0)
        if input_mode == 'playback':
            activity_resume_threshold = max(18, silence_level_sum_threshold + 10)
            activity_resume_hold_sec = 0.45
        else:
            activity_resume_threshold = max(26, silence_level_sum_threshold + 12)
            activity_resume_hold_sec = 0.30

        device_write_lock = get_device_write_lock(device_name)

        # WASAPI loopback of the default output (or the microphone), following default-device changes
        stream_device = 'default speaker loopback' if input_mode == 'playback' else (input_device or 'default microphone')
        stream = WasapiCapture(self, input_mode, input_device)

        last_nonzero_frame_ts = time.monotonic()
        pulse_phase_anchor_ts = last_nonzero_frame_ts
        idle_mode = None
        idle_mode_started_ts = None
        active_candidate_started_ts = None
        
        def update_leds():
            nonlocal last_nonzero_frame_ts, pulse_phase_anchor_ts, idle_mode, idle_mode_started_ts, active_candidate_started_ts

            def render_silent_pulse(now):
                nonlocal idle_mode, idle_mode_started_ts
                if idle_mode != 'silent-pulse':
                    idle_mode = 'silent-pulse'
                    idle_mode_started_ts = now
                pulse_elapsed = now - pulse_phase_anchor_ts
                self.draw_inverted_silence_pulse(
                    elapsed_sec=pulse_elapsed,
                    pulse_period_sec=silent_pulse_period_sec,
                    reveal_sec=silent_pulse_reveal_sec,
                )

            while not self.done:
                with self.buffer_lock:
                    buffer_snapshot = self.audio_buffer.copy()
                    buffer_age = time.monotonic() - self.audio_buffer_ts
                if buffer_age > STALE_AUDIO_SEC:
                    # WASAPI loopback delivers nothing while no audio plays; don't keep showing the last chunk
                    buffer_snapshot = np.zeros_like(buffer_snapshot)
                if buffer_snapshot.ndim == 1:
                    chunk = buffer_snapshot
                else:
                    selected_channel = min(channel, max(0, buffer_snapshot.shape[1] - 1))
                    chunk = buffer_snapshot[:, selected_channel]

                levels = []

                if external_filter:
                    # EasyEffects mode: audio already EQ'd → measure energy in each band
                    for center_freq in BAND_CENTERS:
                        # Use wide-ish windows to capture EasyEffects' output without double-filtering
                        low = center_freq * 0.75
                        high = center_freq * 1.35
                        sos = butter(2, [low, high], btype='band', fs=SAMPLE_RATE, output='sos')
                        filtered = sosfiltfilt(sos, chunk)
                        rms = np.sqrt(np.mean(filtered ** 2))
                        level = scale_rms(rms)
                        levels.append(level)

                else:
                    # Python mode: apply our fixed narrow bandpass filters
                    for sos in filters:
                        filtered = sosfiltfilt(sos, chunk)
                        rms = np.sqrt(np.mean(filtered ** 2))
                        level = scale_rms(rms)
                        levels.append(level)
                boosted_levels = [min(34, int(round(level * level_gain))) for level in levels]
                levels = [0 if level < noise_gate_level else level for level in boosted_levels]

                levels_sum = sum(levels)
                now = time.monotonic()
                if not shared_state.id_key_press_active:
                    if levels_sum > silence_level_sum_threshold:
                        if idle_mode == 'silent-pulse':
                            if levels_sum >= activity_resume_threshold:
                                if active_candidate_started_ts is None:
                                    active_candidate_started_ts = now
                                if now - active_candidate_started_ts >= activity_resume_hold_sec:
                                    last_nonzero_frame_ts = now
                                    idle_mode = None
                                    idle_mode_started_ts = None
                                    active_candidate_started_ts = None
                                    with device_write_lock:
                                        self.draw_eq_levels(levels)
                                else:
                                    render_silent_pulse(now)
                            else:
                                active_candidate_started_ts = None
                                render_silent_pulse(now)
                        else:
                            active_candidate_started_ts = None
                            last_nonzero_frame_ts = now
                            idle_mode = None
                            idle_mode_started_ts = None
                            with device_write_lock:
                                self.draw_eq_levels(levels)
                    else:
                        active_candidate_started_ts = None
                        silence_sec = now - last_nonzero_frame_ts
                        if silence_sec >= silent_pulse_after_sec:
                            render_silent_pulse(now)

                time.sleep(UPDATE_RATE)

        update_thread = threading.Thread(target=update_leds, daemon=True)
        update_thread.start()

        with stream:
            log.debug(
                f"Running equalizer for {channel} channel on {device_name} "
                f"using {input_mode} input via '{stream_device}' "
                f"with {'EasyEffects' if external_filter else 'Python'} filter"
            )
            try:
                while not self.done:
                    time.sleep(0.1)
            except KeyboardInterrupt:
                self.cleanup()
        return True
            
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="LED Matrix Audio Visualizer - Single Channel")
    parser.add_argument(
        '--channel',
        choices=['left', 'right'],
        required=True,
        help="Which channel and device to process: 'left' or 'right'"
    )
    parser.add_argument(
        '--use-easyeffects',
        action='store_true',
        help="Use EasyEffects upstream processing (skip Python bandpass filters)"
    )
    parser.add_argument(
        '--serial-dev-left',
        default=None,
        help="Serial device for left channel"
    )
    parser.add_argument(
        '--serial-dev-right',
        default=None,
        help="Serial device for right channel"
    )
    args = parser.parse_args()
    
    devices: tuple[str, str] = discover_led_devices()

    if args.channel == 'left':
        channel = 0  # left = column 0 in stereo buffer
        serial_dev = args.serial_dev_left or (devices[0][1] if len(devices) > 0 else None)
        location = (devices[0][0] or devices[0][1]) if len(devices) > 0 else None
    else:
        channel = 1  # right = column 1
        serial_dev = args.serial_dev_right or (devices[1][1] if len(devices) > 1 else None)
        location = (devices[1][0] or devices[1][1]) if len(devices) > 1 else None

    use_external_filter = args.use_easyeffects
    if location is not None:
        eq = Equalizer(location)
        eq.run(channel=channel, external_filter=use_external_filter, device_name=serial_dev)
        def stop_and_exit(sig=None, frame=None):
            eq.cleanup()
            sys.exit(0)
        signal.signal(signal.SIGINT, stop_and_exit )
    else:
        log.error(f"LED matrix device {serial_dev} not found. Please check your connections and try again.")