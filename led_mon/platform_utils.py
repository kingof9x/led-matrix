# Small helpers that hide the differences between Linux and Windows.
# Everything Windows-specific in the app should go through here (or be guarded by IS_WINDOWS),
# so the Linux/NixOS behaviour stays exactly as it was.

# Built In Dependencies
import os
import sys
import shutil
import logging
from logging.handlers import RotatingFileHandler

IS_WINDOWS = os.name == "nt"
IS_LINUX = sys.platform.startswith("linux")

APP_NAME = "led_mon"
SINGLE_INSTANCE_MUTEX = "led_mon_single_instance"

log = logging.getLogger(__name__)


def is_frozen():
    """Return True if running from a PyInstaller bundle."""
    return hasattr(sys, "_MEIPASS")


def package_dir():
    """Directory holding the led_mon package files (plugins, snapshot_files, config.yaml, ...)."""
    if is_frozen():
        return os.path.join(sys._MEIPASS, "led_mon")
    return os.path.dirname(os.path.abspath(__file__))


def plugins_dir():
    path = os.path.join(package_dir(), "plugins")
    if not os.path.exists(path):
        path = "./plugins/"
    return path


def app_dir():
    """Directory of the running program: the exe when frozen, otherwise the package directory."""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return package_dir()


def user_config_dir():
    """Where the user's config.yaml / config-local.yaml / .env live.

    Windows: %APPDATA%\\led_mon. Linux: next to the package, as before.
    """
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, APP_NAME)
    return package_dir()


def user_log_dir():
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP_NAME)


def log_file_path():
    return os.path.join(user_log_dir(), f"{APP_NAME}.log")


def ensure_user_config():
    """On Windows, copy the bundled config.yaml to %APPDATA%\\led_mon on first run."""
    if not IS_WINDOWS:
        return
    cfg_dir = user_config_dir()
    local_cfg = os.path.join(cfg_dir, "config-local.yaml")
    cfg = os.path.join(cfg_dir, "config.yaml")
    if os.path.exists(local_cfg) or os.path.exists(cfg):
        return
    bundled = os.path.join(package_dir(), "config.yaml")
    try:
        os.makedirs(cfg_dir, exist_ok=True)
        shutil.copyfile(bundled, cfg)
        log.info(f"Created default config file {cfg}")
    except OSError as e:
        log.error(f"Could not create default config file {cfg}: {e}")


def resolve_config_file(cli_config_file=None):
    """Return the config file path to use.

    Priority: --config-file, then CONFIG_FILE env var, then config-local.yaml, then config.yaml
    in user_config_dir(). On Windows the bundled config.yaml is used if the user one can't be created.
    """
    if cli_config_file:
        log.debug(f"Using config file from program arg --config-file: {cli_config_file}")
        return cli_config_file
    env_config_file = os.environ.get("CONFIG_FILE", None)
    if env_config_file:
        log.debug(f"Using config file from CONFIG_FILE env var: {env_config_file}")
        return env_config_file
    ensure_user_config()
    cfg_dir = user_config_dir()
    config_file = os.path.join(cfg_dir, "config-local.yaml")
    if os.path.exists(config_file):
        log.debug(f"Using local config file {config_file}")
        return config_file
    config_file = os.path.join(cfg_dir, "config.yaml")
    if IS_WINDOWS and not os.path.exists(config_file):
        config_file = os.path.join(package_dir(), "config.yaml")
    log.debug(f"Using default config file {config_file}")
    return config_file


def load_env_file():
    """Load the .env file with API keys.

    Linux: as before, the current-directory .env when not frozen (frozen builds get env vars from systemd).
    Windows: %APPDATA%\\led_mon\\.env, frozen or not.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    if IS_WINDOWS:
        load_dotenv(os.path.join(user_config_dir(), ".env"))
    elif not is_frozen():
        load_dotenv()


def log_level_from_env(default=logging.WARNING):
    return {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
    }.get(os.getenv("LOG_LEVEL", "").lower(), default)


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging():
    """Configure root logging. On Windows also log to a rotating file in %LOCALAPPDATA%\\led_mon."""
    if not IS_WINDOWS:
        logging.basicConfig(level=log_level_from_env(), format=LOG_FORMAT)
        return
    handlers = []
    try:
        os.makedirs(user_log_dir(), exist_ok=True)
        handlers.append(RotatingFileHandler(log_file_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    except OSError:
        pass
    # A windowless exe has no stderr
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=log_level_from_env(logging.INFO), format=LOG_FORMAT, handlers=handlers, force=True)


def attach_parent_console():
    """Let a windowless Windows exe print to the console it was started from (for --help / --list-apps)."""
    if not IS_WINDOWS or sys.stdout is not None:
        return
    try:
        import ctypes
        ATTACH_PARENT_PROCESS = -1
        if ctypes.windll.kernel32.AttachConsole(ATTACH_PARENT_PROCESS):
            sys.stdout = open("CONOUT$", "w")
            sys.stderr = open("CONOUT$", "w")
    except Exception:
        pass


_mutex_handle = None


def acquire_single_instance():
    """Create the named mutex the installer also checks. Returns False if another instance holds it."""
    global _mutex_handle
    if not IS_WINDOWS:
        return True
    import ctypes
    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX)
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        if handle:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    _mutex_handle = handle
    return True


def init_com_for_thread():
    """WMI (brightness) needs COM initialised on each thread that uses it."""
    if not IS_WINDOWS:
        return
    try:
        import pythoncom
        pythoncom.CoInitialize()
    except Exception as e:
        log.debug(f"CoInitialize failed: {e}")


def enable_dpi_awareness():
    """Sharp text on high-DPI screens (Windows); must be set before the process's first window."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def subprocess_flags():
    """creationflags that stop a console window flashing up when a windowless app runs a tool."""
    if IS_WINDOWS:
        import subprocess
        return subprocess.CREATE_NO_WINDOW
    return 0


def open_path(path):
    """Open a file or folder with the default handler (Explorer on Windows)."""
    if IS_WINDOWS:
        os.startfile(path)
    else:
        import subprocess
        subprocess.Popen(["xdg-open", path])


def launch_self(*args):
    """Start another copy of this program (the exe when frozen) with the given arguments."""
    import subprocess
    if is_frozen():
        command = [sys.executable, *args]
        cwd = None
    else:
        command = [sys.executable, "-m", "led_mon.main", *args]
        cwd = os.path.dirname(package_dir())
    return subprocess.Popen(command, cwd=cwd, creationflags=subprocess_flags())


def show_error(message, title="LED Matrix Monitor"):
    """Tell the user about a fatal problem when there is no console to print to (Windows)."""
    if not IS_WINDOWS:
        return
    try:
        import ctypes
        MB_ICONERROR = 0x10
        ctypes.windll.user32.MessageBoxW(None, message, title, MB_ICONERROR)
    except Exception:
        pass
