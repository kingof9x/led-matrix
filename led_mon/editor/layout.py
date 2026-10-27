# The layout editor's model, and its conversion to and from the config file.
#
# The config gives each of the four quadrants its own rotating list of apps. The editor shows each
# panel as a list of pages instead: a page is either one app on the whole panel, or a top app and a
# bottom app, shown together for the page's duration. Writing a page puts its top app (or the
# whole-panel app, with scope: panel) in the top quadrant's list and its bottom app (or an empty
# display: false slot) in the bottom quadrant's list, with the same duration, so the two lists
# stay in step.

# Built In Dependencies
import os
import copy
import shutil
from dataclasses import dataclass, field

# Internal Dependencies
from led_mon.editor import catalog

# External Dependencies
import yaml

SIDES = ("left", "right")
DEFAULT_DURATION = 30
HEADER = ("# Written by the LED Matrix layout editor. You can still edit this file by hand;\n"
          "# see config-README.md. The editor keeps a copy of the previous version in config-local.yaml.bak\n\n")


@dataclass
class Page:
    duration: int = DEFAULT_DURATION
    full: dict = None      # one app on the whole panel
    top: dict = None
    bottom: dict = None

    def apps(self):
        return [a for a in (self.full, self.top, self.bottom) if a is not None]

    def is_empty(self):
        return not self.apps()


@dataclass
class Layout:
    pages: dict = field(default_factory=lambda: {"left": [Page()], "right": [Page()]})
    # Everything else in the config file (duration, left-port, right-port, ...), kept as it was
    settings: dict = field(default_factory=lambda: {"duration": 10})


# Ready-made layouts: {name: {side: [(top app, bottom app), ...]}}, one page per pair
PRESETS = {
    # The same as the LEDmatrix monitor tool: CPU cores, memory and battery on the left; disk and network on the right
    "Classic monitor": {"left": [("cpu", "mem-bat")], "right": [("disk", "net")]},
}


def preset_pages(name, duration=DEFAULT_DURATION):
    """The pages of a ready-made layout, for both panels."""
    return {side: [Page(duration=duration, top=catalog.new_app(top, side), bottom=catalog.new_app(bottom, side))
                   for top, bottom in PRESETS[name][side]]
            for side in SIDES}


def _displays(app):
    return app is not None and app.get("display", True) is not False


def _clean(app):
    """An app entry as the editor holds it: without the YAML list marker and placement keys."""
    app = copy.deepcopy(app)
    app.pop("app", None)
    app.pop("scope", None)
    app.pop("duration", None)
    return app


def from_config(config):
    """Build a Layout from a parsed config. Returns (layout, warnings)."""
    config = copy.deepcopy(config or {})
    quads = config.pop("quadrants", None) or {}
    default_duration = int(config.get("duration", DEFAULT_DURATION) or DEFAULT_DURATION)
    warnings = []
    pages = {}
    for side in SIDES:
        top = quads.get(f"top-{side}") or []
        bottom = quads.get(f"bottom-{side}") or []
        if top and bottom and len(top) != len(bottom):
            warnings.append(f"The {side} panel's top and bottom lists have different lengths, so they rotated "
                            f"independently. The editor has paired them up page by page.")
        side_pages = []
        for i in range(max(len(top), len(bottom))):
            t = top[i] if i < len(top) else None
            b = bottom[i] if i < len(bottom) else None
            durations = [int(a.get("duration", default_duration)) for a in (t, b) if a is not None]
            if len(set(durations)) > 1:
                warnings.append(f"Page {i + 1} of the {side} panel had different top and bottom durations; "
                                f"the editor uses {durations[0]} seconds for both.")
            page = Page(duration=durations[0] if durations else default_duration)
            if _displays(t) and t.get("scope") == "panel":
                page.full = _clean(t)
            elif _displays(b) and b.get("scope") == "panel" and not _displays(t):
                page.full = _clean(b)
            else:
                page.top = _clean(t) if _displays(t) else None
                page.bottom = _clean(b) if _displays(b) else None
            side_pages.append(page)
        pages[side] = side_pages or [Page(duration=default_duration)]
    return Layout(pages=pages, settings=config), warnings


def validate(layout):
    """Return a list of problems that stop the layout being saved."""
    problems = []
    for side in SIDES:
        for position in ("top", "bottom"):
            seen = {}
            for i, page in enumerate(layout.pages[side], start=1):
                # A whole-panel app is written to the top quadrant's list
                if position == "top":
                    app = page.full or page.top
                else:
                    app = None if page.full else page.bottom
                if app is None:
                    continue
                name = app["name"]
                if name in seen:
                    where = "top half (or whole panel)" if position == "top" else "bottom half"
                    problems.append(f"{catalog.label(app)} is used twice in the {side} panel's {where} "
                                    f"(pages {seen[name]} and {i}). Each app can be used once there.")
                else:
                    seen[name] = i
    for side in SIDES:
        for i, page in enumerate(layout.pages[side], start=1):
            if page.duration < 1:
                problems.append(f"Page {i} of the {side} panel needs a duration of at least 1 second.")
    return problems


def _blank(side, position, index, duration):
    return {"app": None, "name": f"empty-{position}-{side}-{index}", "duration": duration, "display": False}


def _entry(app, side, duration, panel_scope=False):
    entry = catalog.finalize(app, side)
    out = {"app": None, "name": entry.pop("name"), "duration": duration}
    if panel_scope:
        out["scope"] = "panel"
    out.update(entry)
    return out


def to_config(layout):
    """The config mapping to write for a Layout."""
    config = copy.deepcopy(layout.settings)
    config.setdefault("duration", DEFAULT_DURATION)
    quadrants = {}
    for side in SIDES:
        top, bottom = [], []
        for i, page in enumerate(layout.pages[side], start=1):
            d = int(page.duration)
            if page.full is not None:
                top.append(_entry(page.full, side, d, panel_scope=True))
                bottom.append(_blank(side, "bottom", i, d))
            else:
                top.append(_entry(page.top, side, d) if page.top else _blank(side, "top", i, d))
                bottom.append(_entry(page.bottom, side, d) if page.bottom else _blank(side, "bottom", i, d))
        quadrants[f"top-{side}"] = top
        quadrants[f"bottom-{side}"] = bottom
    config["quadrants"] = quadrants
    return config


def dump(config):
    return HEADER + yaml.safe_dump(config, sort_keys=False, default_flow_style=False)


def load_file(path):
    with open(path, "r") as f:
        return yaml.safe_load(f) or {}


def save(layout, path):
    """Write the layout to path (config-local.yaml), keeping the previous file as path.bak."""
    problems = validate(layout)
    if problems:
        raise ValueError("\n".join(problems))
    text = dump(to_config(layout))
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    if os.path.exists(path):
        shutil.copyfile(path, path + ".bak")
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)
    return path
