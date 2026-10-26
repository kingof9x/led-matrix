# Brightness slider: a widget used by the layout editor, and a small window that pops up from the tray.

# Built In Dependencies
import gc
import queue
import logging
import threading
import tkinter as tk
from tkinter import ttk

# Internal Dependencies
from led_mon import platform_utils

log = logging.getLogger(__name__)


class BrightnessControl(ttk.Frame):
    """A 0-100 slider and a "Follow screen brightness" checkbox.

    Moving the slider turns following the screen off. on_change(follow_screen, level) runs on the Tk thread.
    """

    def __init__(self, master, follow_screen, level, on_change, length=200):
        super().__init__(master)
        self.on_change = on_change
        self._updating = False
        self.follow = tk.BooleanVar(value=follow_screen)

        top = ttk.Frame(self)
        top.pack(fill="x")
        ttk.Label(top, text="Brightness").pack(side="left")
        self.value_label = ttk.Label(top, width=6, anchor="e")
        self.value_label.pack(side="right")
        self.scale = ttk.Scale(self, from_=0, to=100, orient="horizontal", length=length, command=self._slid)
        self.scale.pack(fill="x", pady=2)
        ttk.Checkbutton(self, text="Follow screen brightness", variable=self.follow,
                        command=self._toggled).pack(anchor="w")
        self.set_state(follow_screen, level)

    def level(self):
        return int(round(float(self.scale.get())))

    def set_state(self, follow_screen, level):
        """Show a brightness chosen elsewhere, without calling on_change."""
        self._updating = True
        try:
            self.follow.set(bool(follow_screen))
            self.scale.set(level)
        finally:
            self._updating = False
        self._show_value()

    def _show_value(self):
        self.value_label.configure(text="Auto" if self.follow.get() else f"{self.level()}%")

    def _slid(self, _value):
        if self._updating:
            return
        self.follow.set(False)
        self._show_value()
        self.on_change(False, self.level())

    def _toggled(self):
        self._show_value()
        self.on_change(self.follow.get(), self.level())


def _work_area():
    """(left, top, right, bottom) of the desktop without the taskbar, or None."""
    if not platform_utils.IS_WINDOWS:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        rect = wintypes.RECT()
        SPI_GETWORKAREA = 0x0030
        if ctypes.windll.user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        pass
    return None


def popup_position(pointer, size, area):
    """Top-left corner for a window of size (w, h) that opens above and left of the pointer, inside area."""
    x, y = pointer
    width, height = size
    left, top, right, bottom = area
    x = min(max(x - width, left), right - width)
    y = min(max(y - height, top), bottom - height)
    return x, y


class TrayBrightnessPopup:
    """The tray's brightness window. Tk runs on its own thread, since the tray owns the main thread."""

    def __init__(self, brightness):
        self.brightness = brightness
        self.calls = queue.Queue()
        self.thread = None
        self.root = None
        self.window = None

    def show(self):
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, daemon=True, name="led_mon-brightness")
            self.thread.start()
        self.calls.put(self._open)

    def stop(self):
        if self.thread is not None and self.thread.is_alive():
            self.calls.put(lambda: self.root.quit())
            self.thread.join(timeout=2)

    # Everything below runs on the Tk thread
    def _run(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
            self.root.after(50, self._poll)
            self.root.mainloop()
            self.root.destroy()
        except Exception as e:
            log.error(f"Brightness window stopped: {e}")
        self.root = None
        self.window = None
        # Tk objects must be freed on the thread that made them, or Tcl aborts the program
        gc.collect()

    def _poll(self):
        while True:
            try:
                call = self.calls.get_nowait()
            except queue.Empty:
                break
            try:
                call()
            except Exception as e:
                log.error(f"Brightness window error: {e}")
        self.root.after(50, self._poll)

    def _open(self):
        if self.window is not None:
            self.window.lift()
            self.window.focus_force()
            return
        state = self.brightness.get()
        window = tk.Toplevel(self.root)
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        frame = ttk.Frame(window, padding=10, relief="solid", borderwidth=1)
        frame.pack(fill="both", expand=True)
        header = ttk.Frame(frame)
        header.pack(fill="x")
        ttk.Label(header, text="LED Matrix Monitor", font=("Segoe UI", 9, "bold")).pack(side="left")
        ttk.Button(header, text="✕", width=3, command=self._close).pack(side="right")
        control = BrightnessControl(frame, state["follow_screen"], state["level"],
                                    lambda follow, level: self.brightness.set(follow, level, save=False))
        control.pack(fill="x", pady=(6, 0))
        window.bind("<Escape>", lambda e: self._close())
        # Close when the user clicks somewhere else, like the volume slider does
        window.bind("<FocusOut>", lambda e: window.after(200, self._close_if_unfocused))
        self.window = window

        window.update_idletasks()
        size = (window.winfo_reqwidth(), window.winfo_reqheight())
        area = _work_area() or (0, 0, window.winfo_screenwidth(), window.winfo_screenheight())
        x, y = popup_position(window.winfo_pointerxy(), size, area)
        window.geometry(f"+{x}+{y}")
        window.after(50, window.focus_force)

    def _close_if_unfocused(self):
        try:
            focused = self.root.focus_get()
        except Exception:
            focused = None
        if self.window is not None and focused is None:
            self._close()

    def _close(self):
        if self.window is None:
            return
        self.brightness.save()
        self.window.destroy()
        self.window = None
        gc.collect()
