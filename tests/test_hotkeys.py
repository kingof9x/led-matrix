# Windows keyboard shortcuts through RegisterHotKey (no keyboard hook)
import sys
import threading

import pytest

from led_mon import platform_utils

windows_only = pytest.mark.skipif(not platform_utils.IS_WINDOWS, reason="Windows hotkey API")


@windows_only
def test_hotkey_message_calls_its_callback():
    import ctypes
    from led_mon import hotkeys
    pressed = threading.Event()
    thread = hotkeys.start({"n": pressed.set})
    try:
        assert thread.registered == ["N"]
        # What Windows posts when the shortcut is pressed
        ctypes.windll.user32.PostThreadMessageW(thread._thread_id, hotkeys.WM_HOTKEY, 1, 0)
        assert pressed.wait(5)
    finally:
        thread.stop()
        thread.join(5)
    assert not thread.is_alive()


@windows_only
def test_monitor_does_not_load_pynput_on_windows():
    pytest.importorskip("numpy")
    try:
        from led_mon import led_system_monitor
    except Exception as e:
        pytest.skip(f"full dependencies not installed: {e}")
    assert not led_system_monitor.PYNPUT_AVAILABLE
    assert not any(name == "pynput" or name.startswith("pynput.") for name in sys.modules)
