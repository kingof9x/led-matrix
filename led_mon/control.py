# Local control channel between the running monitor and the layout editor.
#
# The monitor listens on a named pipe (Windows) or a Unix socket (Linux) that only this user can reach.
# Only one program can use a panel's serial port at a time on Windows, so while the monitor runs the
# editor asks it to reload the config or show a preview frame instead of opening the ports itself.
# Messages are JSON objects, one per connection; the reply is a JSON object with "ok".

# Built In Dependencies
import os
import json
import secrets
import logging
import threading
import getpass
import tempfile
from multiprocessing.connection import Listener, Client

# Internal Dependencies
from led_mon import platform_utils
from led_mon.platform_utils import IS_WINDOWS

log = logging.getLogger(__name__)


def _user_tag():
    try:
        name = getpass.getuser()
    except Exception:
        name = "user"
    return "".join(c for c in name if c.isalnum() or c in "-_") or "user"


def address():
    if IS_WINDOWS:
        return rf"\\.\pipe\led_mon-{_user_tag()}"
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or tempfile.gettempdir()
    return os.path.join(runtime_dir, f"led_mon-{_user_tag()}.sock")


def _key_file():
    return os.path.join(platform_utils.user_log_dir(), "control.key")


def _read_key():
    with open(_key_file(), "rb") as f:
        return f.read()


def _new_key():
    key = secrets.token_bytes(32)
    path = _key_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Readable only by this user on Linux; %LOCALAPPDATA% is already per-user on Windows
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def serve(handler):
    """Start a daemon thread answering requests with handler(message) -> dict. Returns the thread, or None."""
    try:
        key = _new_key()
        addr = address()
        if not IS_WINDOWS and os.path.exists(addr):
            os.unlink(addr)
        listener = Listener(addr, authkey=key)
    except Exception as e:
        log.warning(f"Layout editor control channel not available: {e}")
        return None

    def loop():
        while True:
            try:
                conn = listener.accept()
            except Exception as e:
                # A client that fails the key check ends up here; keep serving
                log.debug(f"Control connection refused: {e}")
                continue
            try:
                message = json.loads(conn.recv_bytes(16 * 1024 * 1024).decode("utf-8"))
                try:
                    reply = handler(message) or {}
                    reply.setdefault("ok", True)
                except Exception as e:
                    log.error(f"Error handling control request {message.get('cmd')}: {e}")
                    reply = {"ok": False, "error": str(e)}
                conn.send_bytes(json.dumps(reply).encode("utf-8"))
            except Exception as e:
                log.debug(f"Control connection error: {e}")
            finally:
                conn.close()

    thread = threading.Thread(target=loop, daemon=True, name="led_mon-control")
    thread.start()
    log.info(f"Control channel listening on {addr}")
    return thread


def request(message, timeout=3.0):
    """Send one request to the running monitor. Returns its reply, or None if no monitor is running."""
    try:
        key = _read_key()
    except OSError:
        return None
    result = {}

    def run():
        try:
            conn = Client(address(), authkey=key)
        except Exception:
            return
        try:
            conn.send_bytes(json.dumps(message).encode("utf-8"))
            result["reply"] = json.loads(conn.recv_bytes().decode("utf-8"))
        except Exception as e:
            log.debug(f"Control request failed: {e}")
        finally:
            conn.close()

    # Client() has no timeout of its own, and a stale pipe or socket must not hang the editor
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    return result.get("reply")
