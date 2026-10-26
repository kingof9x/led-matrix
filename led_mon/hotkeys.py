# Global keyboard shortcuts on Windows, through the Windows hotkey API (RegisterHotKey).
#
# Windows tells the app only when one of its registered shortcuts is pressed; the app never sees
# any other keystroke. Watching every key instead (as pynput's keyboard hook does) looks like a
# keylogger to antivirus scanners, and a windowless app that starts at login and hooks the
# keyboard is flagged as malware.

# Built In Dependencies
import logging
import threading

log = logging.getLogger(__name__)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

# Ctrl+Alt+Shift, so the shortcuts don't take Alt+F (File menus) and the like from other programs
MODIFIERS = MOD_CONTROL | MOD_ALT | MOD_SHIFT
MODIFIERS_TEXT = "Ctrl+Alt+Shift"


class HotkeyThread(threading.Thread):
    """Registers {letter: callback} shortcuts and calls the callback when one is pressed."""

    def __init__(self, bindings):
        super().__init__(daemon=True, name="led_mon-hotkeys")
        self.bindings = dict(bindings)
        self.registered = []
        self.ready = threading.Event()
        self._thread_id = None

    def run(self):
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        # Hotkeys belong to the thread that registers them, and arrive in its message queue
        self._thread_id = kernel32.GetCurrentThreadId()
        callbacks = {}
        for hotkey_id, (letter, callback) in enumerate(self.bindings.items(), start=1):
            if user32.RegisterHotKey(None, hotkey_id, MODIFIERS | MOD_NOREPEAT, ord(letter.upper())):
                callbacks[hotkey_id] = callback
                self.registered.append(letter.upper())
            else:
                log.warning(f"Could not register {MODIFIERS_TEXT}+{letter.upper()}; another program may be using it")
        self.ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY and msg.wParam in callbacks:
                    try:
                        callbacks[msg.wParam]()
                    except Exception as e:
                        log.error(f"Error handling hotkey: {e}")
        finally:
            for hotkey_id in callbacks:
                user32.UnregisterHotKey(None, hotkey_id)

    def stop(self):
        if self._thread_id is not None:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)


def start(bindings):
    """Start listening for the shortcuts. Returns the thread (call stop() to end it)."""
    thread = HotkeyThread(bindings)
    thread.start()
    thread.ready.wait(5)
    if thread.registered:
        log.info(f"Keyboard shortcuts: {', '.join(MODIFIERS_TEXT + '+' + k for k in thread.registered)}")
    return thread
