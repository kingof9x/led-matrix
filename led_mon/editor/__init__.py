# Layout editor: a window to place apps on the panels, preview them and send them to the panels.
# Started with `led_mon --edit-layout` or from the tray menu.


def run_editor(args):
    from led_mon.editor.window import run_editor as run
    run(args)
