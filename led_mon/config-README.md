# LED System Monitor Configuration

Control the behavior of the app by settings in `config.yaml`. This file has default settings that are delivered as part of the codebase. To customise the behvaior, copy the settings to another file named `config-local.yaml` and make any desirfeed changes. If this file is present, it will be used instead of `config.yaml`.

On Windows you can also use the layout editor (tray menu *Edit layout*) instead of editing this file; see the Windows section of the main `README.md`. It writes `config-local.yaml` for you.

Configuration is specified for each of four quadrants, the top and bottom of the left and right LED Matrix panels. Each quadrant is a list containing one or more `app` items. These apps will be displayed in the quadrant in sequence, for a time specified by the `duration` parameter, cycling through the configured apps indefinitely.

Here is an explanation of the parameters that can (or must) be specified for each app.

**name** (required): The name of the app, which must match a key in `app_funcs`, as explained in `plugin-README.md`. Names must be unique within a quadrant.

**duration** (optional): The number of seconds the app will be displayed before switching to the next one. The same app can have a different duration in each quadrant. If there is only one app in the quadrant, this parameter will have no effect. This paramater is optional only if a global `duration` value is specified.

**scope** (optional): If set to `panel`, the app will be displayed on the entire panel instead of a single quandrant. The scheduler now enforces panel ownership: while a panel-scope app is active in either quadrant, the sibling quadrant on that side is suppressed for rendering and app rotation. If both top and bottom apps on the same side are active and both set `scope: panel`, the top quadrant app takes precedence and a warning is logged. For predictable behavior, configure one panel-scope app per side and set the sibling quadrant app to `display:false`.

**animate** (optional): If set to true, the displayed pattern will scroll vertically.

**persistent-draw** (optional): If set to true, the app function is responsible to render pixels to the matrix panel for the entire duration time of the app. Otherwise (if ommitted or set to false), the app function is invoked repeatedly and renders a single pixel snapshot each time. Most apps do not use persistent-draw, but an example of one that does is the equalizer (see `equalizer_plugin.py`).

**dispose-fn** (optional): For an app with `persistent-draw:true`, you will need to make the app stop rendering when its duration period is complete and the next app is displayed. You can specifiy a dispose function with this parameter, which cancls the app rendering when called. This function should be specified in `app_funcs`, with the key value that matches the specified parameter value.

**display** (optional): If set to `false`, the app will not render any pixels during its configured time slice. Set it to `true` or omit it, for normal rendering.

**args** (optional): This is a mapping containing key-value pairs to be passed to the app, to configure app-specific behavior. App arguments and their meaning are described for each app in the main `README.md` file.


Parameters specified in the global scope of the file apply to every app, unless overriden in a particular app. The global parameters recognized are:

**duration**: default number of seconds each app is displayed.

**left-port** / **right-port** (optional): pin the left or right panel to a serial port, such as `COM4` on Windows or `/dev/ttyACM0` on Linux, instead of detecting it automatically. Useful if the panels are detected in the wrong order. On Windows, the port names are shown in Device Manager under *Ports (COM & LPT)*.

**left-location** / **right-location** (optional): pin the left or right panel to a USB position, such as `1-4.2`. Unlike a COM port number, this stays the same when Windows renumbers the ports. The layout editor's *Port* boxes save this setting. Run `led_mon --diagnose` to see each panel's USB position. If no panel is found at that position, the panel is detected automatically. If both a port and a location are set for the same side, the port is used.

On Windows the config files live in `%APPDATA%\led_mon\` (the default `config.yaml` is copied there on first run).