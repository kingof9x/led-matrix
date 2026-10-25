# Opening LED panel serial ports.
#
# On Linux several handles can be open on the same /dev/ttyACM* device, and the app relies on that
# (the equalizer opens the panel a second time). Windows COM ports are exclusive, so there every
# open() of the same port returns a handle onto one shared pyserial object, with writes serialised.

# Built In Dependencies
import threading
import logging

# Internal Dependencies
from led_mon.platform_utils import IS_WINDOWS

# External Dependencies
import serial # pyserial

log = logging.getLogger(__name__)

BAUD_RATE = 115200


class _SharedPort:
    def __init__(self, port_name):
        self.port_name = port_name
        self.serial = serial.Serial(port_name, BAUD_RATE)
        self.lock = threading.RLock()
        self.refs = 0
        self.broken = False

    def close(self):
        try:
            self.serial.close()
        except Exception as e:
            log.debug(f"Error closing {self.port_name}: {e}")


class SharedSerial:
    """A handle onto a shared port. Has the subset of the pyserial API the app uses."""

    def __init__(self, entry):
        self._entry = entry
        self._closed = False
        entry.refs += 1

    @property
    def port(self):
        return self._entry.port_name

    @property
    def lock(self):
        return self._entry.lock

    @property
    def is_open(self):
        return not self._closed and not self._entry.broken and self._entry.serial.is_open

    def write(self, data):
        if self._closed:
            raise serial.SerialException(f"{self.port} handle is closed")
        with self._entry.lock:
            if self._entry.broken:
                raise serial.SerialException(f"{self.port} needs to be reopened")
            try:
                return self._entry.serial.write(data)
            except Exception:
                # e.g. the COM handle was invalidated by sleep: every user must reopen
                self._entry.broken = True
                raise

    def read(self, size=1):
        with self._entry.lock:
            return self._entry.serial.read(size)

    def close(self):
        if self._closed:
            return
        self._closed = True
        _release(self._entry)


_ports = {}
_ports_guard = threading.Lock()


def _release(entry):
    with _ports_guard:
        entry.refs -= 1
        if entry.refs <= 0:
            if _ports.get(entry.port_name.lower()) is entry:
                del _ports[entry.port_name.lower()]
            entry.close()


def open_port(port_name):
    """Open an LED panel port. Shared on Windows, a plain pyserial object elsewhere."""
    if not IS_WINDOWS:
        return serial.Serial(port_name, BAUD_RATE)
    key = port_name.lower()
    with _ports_guard:
        entry = _ports.get(key)
        if entry is not None and entry.broken:
            # Drop the dead handle so the port can be opened again; holders still get errors and reconnect
            entry.close()
            del _ports[key]
            entry = None
        if entry is None:
            entry = _SharedPort(port_name)
            _ports[key] = entry
        return SharedSerial(entry)


def port_lock(s):
    """Lock to hold while sending a multi-command sequence (e.g. a full frame) to a port."""
    lock = getattr(s, 'lock', None)
    return lock if lock is not None else _no_lock


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_no_lock = _NoLock()


def close_all():
    """Close every shared port (used on Quit so both COM ports are released)."""
    with _ports_guard:
        entries = list(_ports.values())
        _ports.clear()
    for entry in entries:
        entry.close()
