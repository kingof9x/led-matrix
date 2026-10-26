# The layout editor window (tkinter, which ships with Python and needs no extra packages).

# Built In Dependencies
import os
import copy
import queue
import logging
import threading
import tkinter as tk
from tkinter import ttk, messagebox

# Internal Dependencies
from led_mon import platform_utils, app_settings
from led_mon.editor import catalog, layout as layout_mod
from led_mon.editor.layout import Page, SIDES
from led_mon.editor import preview
from led_mon.brightness_popup import BrightnessControl

log = logging.getLogger(__name__)

CELL = 12          # pixels per LED in the panel preview
GAP = 2
MARGIN = 10
LED_OFF = (34, 34, 34)
LED_ON = (250, 246, 232)
PANEL_BG = "#111111"
SLOT_OUTLINE = "#3a3a3a"
SELECTED_OUTLINE = "#4c9aff"
DROP_OUTLINE = "#7bd88f"
PREVIEW_SECONDS = 30
AUTO_PORT = "Automatic"


def led_color(value):
    # Grid values are LED brightness (0-255); the brightest app pixels are about DEFAULT_FOREGROUND
    level = max(0.0, min(1.0, value / 160.0)) ** 0.6 if value > 0 else 0.0
    rgb = [int(off + (on - off) * level) for off, on in zip(LED_OFF, LED_ON)]
    return "#%02x%02x%02x" % tuple(rgb)


def led_ports():
    """[(port, USB location or None), ...] of the connected LED panels."""
    from serial.tools import list_ports
    from led_mon.shared_state import is_led_matrix_port
    try:
        # The location's interface part (":1.0") is dropped; the rest stays the same when COM numbers change
        return [(p.device, p.location.split(":")[0] if p.location else None)
                for p in list_ports.comports() if is_led_matrix_port(p)]
    except Exception:
        return []


def build_port_choices(ports, port_setting=None, location_setting=None):
    """The Port box's entries for one panel, as {label: (port, location)}, and the label to show now.

    Picking a panel whose USB location is known saves the location (left-location / right-location),
    which stays the same when Windows renumbers the COM ports; otherwise the port name is saved.
    """
    choices = {AUTO_PORT: (None, None)}
    current = AUTO_PORT
    for port, location in ports:
        label = f"{port} (USB {location})" if location else port
        choices[label] = (port, location)
        if location_setting and location and location.startswith(location_setting) and not port_setting:
            current = label
        elif port_setting and port.lower() == str(port_setting).lower():
            current = label
            # Keep what the config says (the port), so choosing it again changes nothing
            choices[label] = (port, None)
    if current == AUTO_PORT and port_setting:
        current = str(port_setting)
        choices[current] = (current, None)
    elif current == AUTO_PORT and location_setting:
        current = f"USB {location_setting} (not connected)"
        choices[current] = (None, location_setting)
    return choices, current


class PanelView(ttk.Frame):
    """One LED panel: its pages, a live preview of the selected page, and drop zones for apps."""

    def __init__(self, master, editor, side):
        super().__init__(master, padding=(8, 0))
        self.editor = editor
        self.side = side
        self.selected_page = 0
        self.drop_hint = None
        self.leds = []

        header = ttk.Frame(self)
        header.pack(fill="x")
        ttk.Label(header, text=f"{side.capitalize()} panel", font=("Segoe UI", 11, "bold")).pack(side="left")
        self.port_var = tk.StringVar(value=AUTO_PORT)
        self.port_box = ttk.Combobox(header, textvariable=self.port_var, width=20, state="readonly")
        self.port_box.pack(side="right")
        ttk.Label(header, text="Port").pack(side="right", padx=(0, 4))
        self.port_box.bind("<<ComboboxSelected>>", lambda e: editor.on_port_changed(side, self.port_var.get()))

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, pady=(6, 0))

        width = MARGIN * 2 + 9 * (CELL + GAP) - GAP
        height = MARGIN * 2 + 34 * (CELL + GAP) - GAP
        self.canvas = tk.Canvas(body, width=width, height=height, bg=PANEL_BG, highlightthickness=0, cursor="hand2")
        self.canvas.pack(side="left", anchor="n")
        for x in range(9):
            column = []
            for y in range(34):
                x0 = MARGIN + x * (CELL + GAP)
                y0 = MARGIN + y * (CELL + GAP)
                column.append(self.canvas.create_oval(x0, y0, x0 + CELL, y0 + CELL, fill=led_color(0), outline=""))
            self.leds.append(column)
        self.last_frame = None
        # Outlines of the top and bottom halves, and of the whole panel
        self.outline_top = self.canvas.create_rectangle(0, 0, 0, 0, outline=SLOT_OUTLINE, width=2, dash=(4, 3))
        self.outline_bottom = self.canvas.create_rectangle(0, 0, 0, 0, outline=SLOT_OUTLINE, width=2, dash=(4, 3))
        self.text_top = self.canvas.create_text(width / 2, height * 0.25, text="", fill="#9a9a9a",
                                                font=("Segoe UI", 9), width=width - 16, justify="center")
        self.text_bottom = self.canvas.create_text(width / 2, height * 0.75, text="", fill="#9a9a9a",
                                                   font=("Segoe UI", 9), width=width - 16, justify="center")
        self.canvas.bind("<Button-1>", self.on_canvas_click)

        side_panel = ttk.Frame(body)
        side_panel.pack(side="left", fill="both", expand=True, padx=(8, 0))
        ttk.Label(side_panel, text="Pages (shown in turn)").pack(anchor="w")
        self.page_list = tk.Listbox(side_panel, height=8, width=26, exportselection=False, activestyle="none")
        self.page_list.pack(fill="both", expand=True)
        self.page_list.bind("<<ListboxSelect>>", self.on_page_selected)

        buttons = ttk.Frame(side_panel)
        buttons.pack(fill="x", pady=(4, 0))
        ttk.Button(buttons, text="Add", width=6, command=self.add_page).pack(side="left")
        ttk.Button(buttons, text="Remove", width=8, command=self.remove_page).pack(side="left", padx=2)
        ttk.Button(buttons, text="▲", width=3, command=lambda: self.move_page(-1)).pack(side="left")
        ttk.Button(buttons, text="▼", width=3, command=lambda: self.move_page(1)).pack(side="left", padx=2)

        duration_row = ttk.Frame(side_panel)
        duration_row.pack(fill="x", pady=(8, 0))
        ttk.Label(duration_row, text="Show page for").pack(side="left")
        self.duration_var = tk.StringVar()
        self.duration_box = ttk.Spinbox(duration_row, from_=1, to=3600, width=6, textvariable=self.duration_var,
                                        command=self.on_duration_changed)
        self.duration_box.pack(side="left", padx=4)
        self.duration_box.bind("<KeyRelease>", lambda e: self.on_duration_changed())
        ttk.Label(duration_row, text="seconds").pack(side="left")

        ttk.Button(side_panel, text="Clear selected slot", command=self.clear_slot).pack(anchor="w", pady=(8, 0))

    # Geometry
    def slot_at(self, y):
        """'top' or 'bottom' for a canvas y coordinate (the page's whole-panel app counts as either)."""
        boundary = MARGIN + 16.5 * (CELL + GAP)
        return "top" if y < boundary else "bottom"

    def slot_box(self, slot):
        width = int(self.canvas["width"])
        height = int(self.canvas["height"])
        middle = MARGIN + 16.5 * (CELL + GAP) - GAP / 2
        if slot == "top":
            return (3, 3, width - 3, middle)
        if slot == "bottom":
            return (3, middle, width - 3, height - 3)
        return (3, 3, width - 3, height - 3)

    @property
    def pages(self):
        return self.editor.layout.pages[self.side]

    def page(self):
        if not self.pages:
            return None
        self.selected_page = max(0, min(self.selected_page, len(self.pages) - 1))
        return self.pages[self.selected_page]

    # Refresh
    def refresh(self):
        self.page_list.delete(0, "end")
        for i, page in enumerate(self.pages, start=1):
            if page.full is not None:
                what = catalog.label(page.full)
            elif page.top is None and page.bottom is None:
                what = "(empty)"
            else:
                what = f"{catalog.label(page.top)} / {catalog.label(page.bottom)}"
            self.page_list.insert("end", f"{i}. {what}  ({page.duration} s)")
        page = self.page()
        if page is not None:
            self.page_list.selection_clear(0, "end")
            self.page_list.selection_set(self.selected_page)
            self.page_list.see(self.selected_page)
            if self.duration_var.get() != str(page.duration):
                self.duration_var.set(str(page.duration))
        self.refresh_outlines()

    def refresh_outlines(self):
        page = self.page()
        selected = self.editor.selection
        mine = selected is not None and selected[0] == self.side and selected[1] == self.selected_page
        if page is not None and page.full is not None:
            self.canvas.coords(self.outline_top, *self.slot_box("full"))
            self.canvas.coords(self.outline_bottom, 0, 0, 0, 0)
            self.canvas.itemconfigure(self.outline_top, outline=SELECTED_OUTLINE if mine else SLOT_OUTLINE)
            self.canvas.itemconfigure(self.text_top, text="")
            self.canvas.itemconfigure(self.text_bottom, text="")
        else:
            self.canvas.coords(self.outline_top, *self.slot_box("top"))
            self.canvas.coords(self.outline_bottom, *self.slot_box("bottom"))
            for slot, outline, text in (("top", self.outline_top, self.text_top),
                                        ("bottom", self.outline_bottom, self.text_bottom)):
                is_selected = mine and selected[2] == slot
                self.canvas.itemconfigure(outline, outline=SELECTED_OUTLINE if is_selected else SLOT_OUTLINE)
                empty = page is None or getattr(page, slot) is None
                self.canvas.itemconfigure(text, text="Drop an app here" if empty else "")
        if self.drop_hint is not None:
            target = self.outline_top if self.drop_hint in ("top", "full") or (page and page.full) else self.outline_bottom
            if self.drop_hint == "full":
                self.canvas.coords(self.outline_top, *self.slot_box("full"))
                self.canvas.coords(self.outline_bottom, 0, 0, 0, 0)
            self.canvas.itemconfigure(target, outline=DROP_OUTLINE)
        for item in (self.outline_top, self.outline_bottom, self.text_top, self.text_bottom):
            self.canvas.tag_raise(item)

    def draw_frame(self, grid):
        if self.last_frame is not None and (self.last_frame == grid).all():
            return
        for x in range(9):
            for y in range(34):
                value = int(grid[x][y])
                if self.last_frame is None or int(self.last_frame[x][y]) != value:
                    self.canvas.itemconfigure(self.leds[x][y], fill=led_color(value))
        self.last_frame = grid.copy()

    # Events
    def on_canvas_click(self, event):
        page = self.page()
        if page is None:
            return
        slot = "full" if page.full is not None else self.slot_at(event.y)
        self.editor.select(self.side, self.selected_page, slot)

    def on_page_selected(self, event=None):
        chosen = self.page_list.curselection()
        if not chosen:
            return
        self.selected_page = chosen[0]
        page = self.page()
        slot = "full" if page.full is not None else "top"
        self.editor.select(self.side, self.selected_page, slot)

    def on_duration_changed(self):
        page = self.page()
        try:
            value = int(self.duration_var.get())
        except ValueError:
            return
        if page is not None and value >= 1 and value != page.duration:
            page.duration = value
            self.editor.changed()

    def add_page(self):
        self.pages.insert(self.selected_page + 1, Page(duration=self.page().duration if self.page() else 30))
        self.selected_page += 1 if len(self.pages) > 1 else 0
        self.editor.select(self.side, self.selected_page, "top")
        self.editor.changed()

    def remove_page(self):
        if len(self.pages) <= 1:
            self.pages[0] = Page(duration=self.pages[0].duration)
        else:
            del self.pages[self.selected_page]
        self.selected_page = min(self.selected_page, len(self.pages) - 1)
        self.editor.select(self.side, self.selected_page, "top")
        self.editor.changed()

    def move_page(self, step):
        i = self.selected_page
        j = i + step
        if 0 <= j < len(self.pages):
            self.pages[i], self.pages[j] = self.pages[j], self.pages[i]
            self.selected_page = j
            sel = self.editor.selection
            self.editor.select(self.side, j, sel[2] if sel and sel[0] == self.side else "top")
            self.editor.changed()

    def clear_slot(self):
        sel = self.editor.selection
        page = self.page()
        if page is None:
            return
        if sel is None or sel[0] != self.side:
            slot = "full" if page.full is not None else "top"
        else:
            slot = sel[2]
        if slot == "full":
            page.full = None
        else:
            setattr(page, slot, None)
        self.editor.select(self.side, self.selected_page, "top")
        self.editor.changed()

    def place(self, app_name, slot):
        """Put a new app from the palette in this panel's selected page."""
        page = self.page()
        if page is None:
            return
        entry = catalog.new_app(app_name, self.side)
        if catalog.get(app_name).size == catalog.FULL:
            page.full, page.top, page.bottom = entry, None, None
            slot = "full"
        else:
            if page.full is not None:
                page.full = None
            if slot == "full":
                slot = "top"
            setattr(page, slot, entry)
        self.editor.select(self.side, self.selected_page, slot)
        self.editor.changed()


class SettingsPane(ttk.Frame):
    """Settings of the selected app."""

    def __init__(self, master, editor):
        super().__init__(master, padding=(12, 0, 0, 0))
        self.editor = editor
        self.title = ttk.Label(self, text="", font=("Segoe UI", 11, "bold"), wraplength=240, justify="left")
        self.title.pack(anchor="w")
        self.description = ttk.Label(self, text="", wraplength=240, justify="left", foreground="#555555")
        self.description.pack(anchor="w", pady=(2, 8))
        self.form = ttk.Frame(self)
        self.form.pack(fill="x", anchor="n")
        self.variables = []

    def show(self, entry, where, side):
        for child in self.form.winfo_children():
            child.destroy()
        self.variables = []
        if entry is None:
            self.title.configure(text="No app selected" if where is None else "Empty slot")
            self.description.configure(text="Drag an app from the list on the left onto a panel, "
                                            "or select a slot and double-click an app." if where is None else
                                            f"{where}. Double-click an app on the left, or drag it here.")
            return
        info = catalog.get(entry["name"])
        self.title.configure(text=info.label)
        self.description.configure(text=f"{info.description}\n{where}")
        if not info.settings:
            ttk.Label(self.form, text="This app has no settings.").pack(anchor="w")
            return
        for setting in info.settings:
            self.add_field(entry, setting, side)

    def _get(self, entry, setting):
        if setting.target == catalog.APP:
            return entry.get(setting.key, setting.default)
        return (entry.get("args") or {}).get(setting.key, setting.default)

    def _set(self, entry, setting, value):
        if setting.target == catalog.APP:
            target = entry
        else:
            target = entry.setdefault("args", {})
        if value in (None, "", []) and setting.kind in ("text", "list", "gpu"):
            target.pop(setting.key, None)
        else:
            target[setting.key] = value
        self.editor.changed(refresh_settings=False)

    def add_field(self, entry, setting, side):
        row = ttk.Frame(self.form)
        row.pack(fill="x", pady=3)
        value = self._get(entry, setting)
        if setting.kind == "bool":
            var = tk.BooleanVar(value=bool(value))
            ttk.Checkbutton(row, text=setting.label, variable=var,
                            command=lambda: self._set(entry, setting, var.get())).pack(anchor="w")
        elif setting.kind in ("choice", "snapshot"):
            ttk.Label(row, text=setting.label).pack(anchor="w")
            options = catalog.snapshot_files(side) if setting.kind == "snapshot" else list(setting.options)
            labels = [str(o) for o in options]
            var = tk.StringVar(value=str(value) if value is not None else "")
            box = ttk.Combobox(row, textvariable=var, values=labels, state="readonly", width=28)
            box.pack(anchor="w")

            def chosen(event=None, options=options, labels=labels, var=var):
                if var.get() in labels:
                    self._set(entry, setting, options[labels.index(var.get())])
            box.bind("<<ComboboxSelected>>", chosen)
        elif setting.kind == "gpu":
            ttk.Label(row, text=setting.label).pack(anchor="w")
            options = catalog.gpu_options()
            values = [v for v, _ in options]
            labels = [l for _, l in options]
            if value and value not in values:
                # Picked on this computer before, but not found now
                values.append(value)
                labels.append(f"{value} (not found)")
            var = tk.StringVar(value=labels[values.index(value or "")])
            box = ttk.Combobox(row, textvariable=var, values=labels, state="readonly", width=28)
            box.pack(anchor="w")

            def chosen(event=None, values=values, labels=labels, var=var):
                if var.get() in labels:
                    self._set(entry, setting, values[labels.index(var.get())])
            box.bind("<<ComboboxSelected>>", chosen)
            if len(options) <= 2:
                ttk.Label(row, text="Only one GPU was found." if len(options) == 2 else "No GPU usage could be read on this computer.",
                          wraplength=240, foreground="#777777", font=("Segoe UI", 8)).pack(anchor="w")
        elif setting.kind == "multi":
            ttk.Label(row, text=setting.label).pack(anchor="w")
            current = list(value or [])
            for option in setting.options:
                var = tk.BooleanVar(value=option in current)

                def toggled(option=option, var=var):
                    selected = list(self._get(entry, setting) or [])
                    if var.get() and option not in selected:
                        selected.append(option)
                    elif not var.get() and option in selected:
                        selected.remove(option)
                    # Keep the order of the options
                    self._set(entry, setting, [o for o in setting.options if o in selected])
                ttk.Checkbutton(row, text=option.replace("_", " "), variable=var, command=toggled).pack(anchor="w", padx=(12, 0))
                self.variables.append(var)
        else:
            ttk.Label(row, text=setting.label).pack(anchor="w")
            if setting.kind == "list":
                text = ", ".join(str(v) for v in value) if value else ""
            else:
                text = "" if value is None else str(value)
            var = tk.StringVar(value=text)
            field = ttk.Entry(row, textvariable=var, width=30)
            field.pack(anchor="w")

            def edited(*_):
                raw = var.get().strip()
                if setting.kind == "int":
                    try:
                        self._set(entry, setting, int(raw))
                    except ValueError:
                        pass
                elif setting.kind == "list":
                    self._set(entry, setting, [_number_or_text(p.strip()) for p in raw.split(",") if p.strip()])
                else:
                    self._set(entry, setting, raw)
            var.trace_add("write", edited)
        if setting.kind != "multi":
            self.variables.append(var)
        if setting.help:
            ttk.Label(row, text=setting.help, wraplength=240, foreground="#777777",
                      font=("Segoe UI", 8)).pack(anchor="w")


def _number_or_text(text):
    try:
        return float(text) if "." in text else text
    except ValueError:
        return text


class LayoutEditor:
    def __init__(self, root, args):
        self.root = root
        self.args = args
        self.selection = None    # (side, page index, 'top' | 'bottom' | 'full')
        self.dirty = False
        self.monitor = None
        self.drag = None
        self.renderer = None
        self.frames = queue.Queue()
        # Work from background threads for the Tk thread (tkinter must only be used from one thread)
        self.ui_calls = queue.Queue()
        self.snapshot_lock = threading.Lock()
        self.snapshot = {}
        self.stop = threading.Event()

        self.load_path, self.save_path = self.config_paths()
        self.layout, warnings = self.read_layout()

        root.title("LED Matrix Layout Editor")
        root.minsize(980, 640)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.build()
        self.refresh_all()
        if warnings:
            messagebox.showinfo("Layout editor", "\n\n".join(warnings), parent=root)

        threading.Thread(target=self.preview_loop, daemon=True).start()
        self.root.after(100, self.poll_frames)
        self.root.after(200, self.check_monitor)
        self.load_brightness()

    # Files
    def config_paths(self):
        load = platform_utils.resolve_config_file(getattr(self.args, "config_file", None))
        if getattr(self.args, "config_file", None):
            save = self.args.config_file
        elif os.environ.get("CONFIG_FILE"):
            save = os.environ["CONFIG_FILE"]
        else:
            save = os.path.join(platform_utils.user_config_dir(), "config-local.yaml")
        return load, save

    def read_layout(self):
        try:
            return layout_mod.from_config(layout_mod.load_file(self.load_path))
        except Exception as e:
            log.error(f"Could not read {self.load_path}: {e}")
            return layout_mod.Layout(), [f"Could not read {self.load_path} ({e}). Starting with an empty layout."]

    # Window
    def build(self):
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(1, weight=1)
        outer.columnconfigure(2, weight=1)
        outer.rowconfigure(0, weight=1)

        palette = ttk.Frame(outer)
        palette.grid(row=0, column=0, sticky="nsew")
        ttk.Label(palette, text="Apps", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.app_list = ttk.Treeview(palette, columns=("size",), show="tree headings", height=14, selectmode="browse")
        self.app_list.heading("#0", text="App")
        self.app_list.heading("size", text="Size")
        self.app_list.column("#0", width=150)
        self.app_list.column("size", width=95)
        self.app_list.pack(fill="y", expand=True)
        self.app_list.bind("<<TreeviewSelect>>", self.on_app_selected)
        self.app_list.bind("<Double-1>", self.on_app_double_click)
        self.app_list.bind("<ButtonPress-1>", self.on_drag_start)
        self.app_list.bind("<B1-Motion>", self.on_drag_motion)
        self.app_list.bind("<ButtonRelease-1>", self.on_drag_end)
        self.app_help = ttk.Label(palette, text="Drag an app onto a panel.\nHalf-panel apps go in the top "
                                  "or bottom half;\nwhole-panel apps fill the page.", wraplength=230,
                                  justify="left", foreground="#555555")
        self.app_help.pack(anchor="w", pady=(6, 0))
        # Panel brightness: changes the running monitor straight away, no Apply needed
        state = app_settings.Brightness().get()
        self.brightness_pending = None
        self.brightness = BrightnessControl(palette, state["follow_screen"], state["level"], self.on_brightness_changed,
                                            length=230)
        self.brightness.pack(anchor="w", fill="x", pady=(12, 0))

        self.panels = {}
        for column, side in enumerate(SIDES, start=1):
            view = PanelView(outer, self, side)
            view.grid(row=0, column=column, sticky="nsew")
            self.panels[side] = view

        self.settings = SettingsPane(outer, self)
        self.settings.grid(row=0, column=3, sticky="nsew")

        bar = ttk.Frame(self.root, padding=(12, 0, 12, 12))
        bar.pack(fill="x")
        self.status = ttk.Label(bar, text="", foreground="#555555")
        self.status.pack(side="top", anchor="w", pady=(0, 6))
        ttk.Button(bar, text="Identify panels", command=self.identify).pack(side="left")
        ttk.Label(bar, text="  Send preview to").pack(side="left")
        self.send_target = tk.StringVar(value="Both panels")
        ttk.Combobox(bar, textvariable=self.send_target, values=("Both panels", "Left panel", "Right panel"),
                     state="readonly", width=12).pack(side="left", padx=4)
        ttk.Button(bar, text="Send preview", command=self.send_preview).pack(side="left")
        self.resume_button = ttk.Button(bar, text="Resume monitor", command=self.resume)
        self.resume_button.pack(side="left", padx=4)
        self.apply_button = ttk.Button(bar, text="Apply to panels", command=self.apply)
        self.apply_button.pack(side="right")
        ttk.Button(bar, text="Revert", command=self.revert).pack(side="right", padx=4)
        presets = ttk.Menubutton(bar, text="Ready-made layouts")
        presets_menu = tk.Menu(presets, tearoff=False)
        for name in layout_mod.PRESETS:
            presets_menu.add_command(label=name, command=lambda name=name: self.use_preset(name))
        presets["menu"] = presets_menu
        presets.pack(side="right", padx=4)
        self.start_button = ttk.Button(bar, text="Start monitor", command=self.start_monitor)

        self.fill_app_list()
        self.fill_ports()

    def fill_app_list(self, plugin_names=()):
        self.app_list.delete(*self.app_list.get_children())
        for info in catalog.available(plugin_names):
            size = "Whole panel" if info.size == catalog.FULL else "Half panel"
            self.app_list.insert("", "end", iid=info.name, text=info.label, values=(size,))

    def fill_ports(self):
        ports = led_ports()
        self.port_choices = {}
        for side, view in self.panels.items():
            choices, current = build_port_choices(ports, self.layout.settings.get(f"{side}-port"),
                                            self.layout.settings.get(f"{side}-location"))
            self.port_choices[side] = choices
            view.port_box.configure(values=list(choices))
            view.port_var.set(current)

    def refresh_all(self):
        for view in self.panels.values():
            view.refresh()
        self.refresh_settings()
        self.update_title()

    def refresh_settings(self):
        sel = self.selection
        if sel is None:
            self.settings.show(None, None, None)
            return
        side, index, slot = sel
        pages = self.layout.pages[side]
        if index >= len(pages):
            self.settings.show(None, None, None)
            return
        page = pages[index]
        entry = getattr(page, slot)
        where = {"full": "whole panel", "top": "top half", "bottom": "bottom half"}[slot]
        self.settings.show(entry, f"{side.capitalize()} panel, page {index + 1}, {where}", side)

    def update_title(self):
        self.root.title(("* " if self.dirty else "") + "LED Matrix Layout Editor")

    def set_status(self, text):
        self.status.configure(text=text)

    # Model changes
    def select(self, side, index, slot):
        self.selection = (side, index, slot)
        self.panels[side].selected_page = index
        for view in self.panels.values():
            view.refresh()
        self.refresh_settings()

    def changed(self, refresh_settings=True):
        self.dirty = True
        for view in self.panels.values():
            view.refresh()
        if refresh_settings:
            self.refresh_settings()
        self.update_title()

    def on_port_changed(self, side, value):
        port, location = self.port_choices.get(side, {}).get(value, (None, None))
        self.layout.settings.pop(f"{side}-port", None)
        self.layout.settings.pop(f"{side}-location", None)
        if location:
            self.layout.settings[f"{side}-location"] = location
        elif port:
            self.layout.settings[f"{side}-port"] = port
        self.changed(refresh_settings=False)
        self.set_status("Port changes take effect the next time the monitor starts.")

    # Palette
    def on_app_selected(self, event=None):
        chosen = self.app_list.selection()
        if chosen:
            info = catalog.get(chosen[0])
            size = "whole panel" if info.size == catalog.FULL else "top or bottom half"
            self.app_help.configure(text=f"{info.description}\nGoes in: {size}.")

    def on_app_double_click(self, event):
        item = self.app_list.identify_row(event.y)
        if not item:
            return
        if self.selection is None:
            self.select("left", self.panels["left"].selected_page, "top")
        side, index, slot = self.selection
        self.panels[side].place(item, slot)

    def on_drag_start(self, event):
        item = self.app_list.identify_row(event.y)
        self.drag = {"item": item, "x": event.x_root, "y": event.y_root, "window": None} if item else None

    def on_drag_motion(self, event):
        if not self.drag:
            return
        if self.drag["window"] is None:
            if abs(event.x_root - self.drag["x"]) + abs(event.y_root - self.drag["y"]) < 6:
                return
            window = tk.Toplevel(self.root)
            window.overrideredirect(True)
            window.attributes("-topmost", True)
            tk.Label(window, text=catalog.get(self.drag["item"]).label, bg="#4c9aff", fg="white",
                     padx=8, pady=3).pack()
            self.drag["window"] = window
        self.drag["window"].geometry(f"+{event.x_root + 12}+{event.y_root + 8}")
        target = self.drop_target(event.x_root, event.y_root, self.drag["item"])
        for view in self.panels.values():
            hint = target[1] if target and target[0] is view else None
            if view.drop_hint != hint:
                view.drop_hint = hint
                view.refresh_outlines()

    def on_drag_end(self, event):
        drag, self.drag = self.drag, None
        if not drag or drag["window"] is None:
            return
        drag["window"].destroy()
        target = self.drop_target(event.x_root, event.y_root, drag["item"])
        for view in self.panels.values():
            view.drop_hint = None
            view.refresh_outlines()
        if target:
            view, slot = target
            view.place(drag["item"], slot)

    def drop_target(self, x_root, y_root, item):
        widget = self.root.winfo_containing(x_root, y_root)
        for view in self.panels.values():
            if widget is view.canvas:
                if catalog.get(item).size == catalog.FULL:
                    return view, "full"
                return view, view.slot_at(y_root - view.canvas.winfo_rooty())
        return None

    # Preview
    def current_pages(self):
        return {side: copy.deepcopy(view.page()) for side, view in self.panels.items()}

    def preview_loop(self):
        platform_utils.init_com_for_thread()
        try:
            self.renderer = preview.PreviewRenderer()
            names = self.renderer.plugin_names()
            self.call_soon(lambda: self.fill_app_list(names))
        except Exception as e:
            log.exception("Preview renderer failed to start")
            message = f"The live preview isn't available: {e}"
            self.call_soon(lambda: self.set_status(message))
            return
        while not self.stop.is_set():
            with self.snapshot_lock:
                pages = dict(self.snapshot)
            for side, page in pages.items():
                try:
                    entries = None
                    if page is not None:
                        entries = {k: catalog.finalize(getattr(page, k), side)
                                   for k in ("full", "top", "bottom") if getattr(page, k) is not None}
                    self.frames.put((side, self.renderer.render(page, side, entries)))
                except Exception as e:
                    log.error(f"Preview of the {side} panel failed: {e}")
            self.stop.wait(0.2)

    def call_soon(self, fn):
        self.ui_calls.put(fn)

    def poll_frames(self):
        while True:
            try:
                fn = self.ui_calls.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception as e:
                log.error(f"Layout editor update failed: {e}")
        with self.snapshot_lock:
            self.snapshot = self.current_pages()
        latest = {}
        while True:
            try:
                side, grid = self.frames.get_nowait()
            except queue.Empty:
                break
            latest[side] = grid
        for side, grid in latest.items():
            self.panels[side].draw_frame(grid)
        self.root.after(150, self.poll_frames)

    def check_monitor(self):
        def run():
            status = preview.monitor_status()
            self.call_soon(lambda: self.show_monitor_status(status))
        threading.Thread(target=run, daemon=True).start()
        self.root.after(5000, self.check_monitor)

    def show_monitor_status(self, status):
        was_running = self.monitor is not None
        self.monitor = status
        if status:
            ports = ", ".join(status.get("ports", [])) or "no panels"
            text = f"Monitor is running ({ports})."
            self.start_button.pack_forget()
            self.resume_button.state(["!disabled"])
        else:
            text = "The monitor isn't running. Apply saves the layout; Start monitor shows it on the panels."
            if not self.start_button.winfo_ismapped():
                self.start_button.pack(side="right", padx=4)
            self.resume_button.state(["disabled"])
        if not self.status.cget("text") or was_running != (status is not None) or self.status.cget("text").startswith(("Monitor is", "The monitor isn't")):
            self.set_status(text)

    def load_brightness(self):
        def run():
            state = preview.brightness()
            self.call_soon(lambda: self.brightness.set_state(state["follow_screen"], state["level"]))
        threading.Thread(target=run, daemon=True).start()

    def on_brightness_changed(self, follow_screen, level):
        # The slider sends many changes while dragging; send the latest one a moment after it stops
        first = self.brightness_pending is None
        self.brightness_pending = (follow_screen, level)
        if first:
            self.root.after(150, self.send_brightness)

    def send_brightness(self):
        follow_screen, level = self.brightness_pending
        self.brightness_pending = None

        def run():
            preview.set_brightness(follow_screen, level)
        threading.Thread(target=run, daemon=True).start()

    # Actions
    def preview_frames(self):
        target = self.send_target.get()
        sides = {"Left panel": ["left"], "Right panel": ["right"]}.get(target, list(SIDES))
        frames = {}
        for side in sides:
            frame = self.panels[side].last_frame
            if frame is not None:
                frames[side] = frame
        return frames

    def send_preview(self):
        frames = self.preview_frames()
        if not frames:
            self.set_status("The preview isn't ready yet.")
            return
        self.run_action(lambda: preview.send_frames(frames, PREVIEW_SECONDS), "Sending the preview...")

    def resume(self):
        self.run_action(lambda: "The monitor is showing your layout again." if preview.resume_monitor()
                        else "The monitor isn't running.", "")

    def identify(self):
        self.run_action(lambda: "The panels show their app names for a few seconds." if preview.identify()
                        else "Identify needs the monitor to be running.", "")

    def run_action(self, action, busy_text):
        if busy_text:
            self.set_status(busy_text)

        def run():
            try:
                message = action()
            except Exception as e:
                log.error(f"Layout editor action failed: {e}")
                message = f"That didn't work: {e}"
            self.call_soon(lambda: self.set_status(message))
        threading.Thread(target=run, daemon=True).start()

    def save(self):
        problems = layout_mod.validate(self.layout)
        if problems:
            messagebox.showerror("Can't save the layout", "\n\n".join(problems), parent=self.root)
            return False
        try:
            layout_mod.save(self.layout, self.save_path)
        except Exception as e:
            messagebox.showerror("Can't save the layout", f"Could not write {self.save_path}:\n{e}", parent=self.root)
            return False
        self.load_path = self.save_path
        self.dirty = False
        self.update_title()
        return True

    def apply(self):
        if not self.save():
            return

        def run():
            if preview.apply_config():
                return "Saved. The panels switch to the new layout in a moment."
            return f"Saved to {self.save_path}. The monitor isn't running; click Start monitor to show it."
        self.run_action(run, "Saving...")

    def start_monitor(self):
        if self.dirty and not self.save():
            return
        try:
            preview.start_monitor(getattr(self.args, "config_file", None))
            self.set_status("Starting the monitor...")
            self.root.after(3000, self.check_monitor)
        except Exception as e:
            self.set_status(f"Could not start the monitor: {e}")

    def use_preset(self, name):
        if not messagebox.askyesno("Ready-made layout", f"Replace both panels' pages with the {name} layout?\n\n"
                                   "Nothing is saved until you press Apply to panels.", parent=self.root):
            return
        self.layout.pages = layout_mod.preset_pages(name)
        for view in self.panels.values():
            view.selected_page = 0
        self.selection = None
        self.changed()
        self.set_status(f"Using the {name} layout. Press Apply to panels to show it.")

    def revert(self):
        if self.dirty and not messagebox.askyesno("Revert", "Discard your changes and reload the saved layout?",
                                                  parent=self.root):
            return
        self.layout, warnings = self.read_layout()
        self.selection = None
        self.dirty = False
        self.fill_ports()
        self.refresh_all()
        self.set_status("Reloaded the saved layout.")

    def on_close(self):
        if self.dirty:
            answer = messagebox.askyesnocancel("Layout editor", "Apply your changes before closing?", parent=self.root)
            if answer is None:
                return
            if answer:
                if not self.save():
                    return
                preview.apply_config()
        self.stop.set()
        self.root.destroy()


class _OncePerMessage(logging.Filter):
    """The preview redraws several times a second; log each distinct error only once."""

    def __init__(self):
        super().__init__()
        self.seen = set()

    def filter(self, record):
        message = record.getMessage()
        if message in self.seen:
            return False
        self.seen.add(message)
        return True


def run_editor(args):
    platform_utils.attach_parent_console()
    logging.getLogger("led_mon.rendering").addFilter(_OncePerMessage())
    platform_utils.enable_dpi_awareness()
    root = tk.Tk()
    try:
        from led_mon.tray import make_icon_image
        from PIL import ImageTk
        icon = ImageTk.PhotoImage(make_icon_image(64))
        root.iconphoto(True, icon)
    except Exception:
        pass
    editor = LayoutEditor(root, args)
    if os.environ.get("LED_MON_EDITOR_SMOKE_TEST"):
        # CI: check that the window opens and the preview starts, then close
        def finish():
            editor.stop.set()
            root.destroy()
        root.after(8000, finish)
    root.mainloop()
