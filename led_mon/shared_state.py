id_key_press_active = False
foreground_value = 0
# Optional {'left': 'COM4', 'right': 'COM5'} from the left-port / right-port config keys
port_overrides = {}
# Optional {'left': '1-4.2', 'right': '1-3.3'} from the left-location / right-location config keys
location_overrides = {}

from serial.tools import list_ports
import re
import logging

log = logging.getLogger(__name__)

# USB IDs of the Framework LED Matrix Input Module (same as inputmodule-rs)
LED_MATRIX_VID = 0x32AC
LED_MATRIX_PID = 0x0020

def is_led_matrix_port(port):
    if getattr(port, 'vid', None) == LED_MATRIX_VID and getattr(port, 'pid', None) == LED_MATRIX_PID:
        return True
    return 'LED Matrix Input Module' in str(port)

def location_sort_key(entry):
    location, device = entry
    #location is of form: <bus>-<port>[-<port>]  (port is of form x.y:n.m)
    # Example: 1-3.3:1.0 (right device) , 1-3.2:1.0 (left device)
    # Sort by y:n.m to get the devices in left-right order
    if location:
        return (0, re.sub(r'^\d+\-\d+\.', '', location), device)
    # No location reported (can happen on Windows): fall back to the port name
    return (1, '', device)

def set_overrides_from_config(config):
    """Read the optional left-port / right-port and left-location / right-location config keys."""
    global port_overrides, location_overrides
    config = config or {}
    port_overrides = {side: str(config[f'{side}-port'])
                      for side in ('left', 'right') if config.get(f'{side}-port')}
    location_overrides = {side: str(config[f'{side}-location'])
                          for side in ('left', 'right') if config.get(f'{side}-location')}

def find_port_at_location(device_list, location):
    """The LED panel port whose USB location starts with location (e.g. 1-4.2), or None."""
    for device in device_list:
        if device.location and device.location.startswith(location) and is_led_matrix_port(device):
            return device.device
    return None

def discover_led_devices(device_list=None, overrides=None, locations_pinned=None):
    """Return [(location, device), ...] for the LED panels, in left/right order.

    location may be None. overrides ({'left': 'COM4', 'right': 'COM5'}) pins a side to a port.
    locations_pinned ({'left': '1-4.2'}) pins a side to a USB location, which stays the same when
    Windows renumbers the COM ports; a side whose location isn't found is detected automatically.
    A port pin wins over a location pin for the same side.
    """
    locations = []
    if overrides is None:
        overrides = port_overrides
    if locations_pinned is None:
        locations_pinned = location_overrides
    try:
        if device_list is None:
            device_list = list_ports.comports()
        for device in device_list:
            if is_led_matrix_port(device):
                locations.append((device.location, device.device))
        found = sorted(locations, key=location_sort_key)
        overrides = dict(overrides)
        for side, location in locations_pinned.items():
            if not overrides.get(side):
                pinned_port = find_port_at_location(device_list, location)
                if pinned_port:
                    overrides[side] = pinned_port
                else:
                    log.warning(f"No LED panel found at USB location {location} ({side}-location); detecting it automatically")
        left, right = overrides.get('left'), overrides.get('right')
        if not left and not right:
            return found
        location_by_port = {d.device.lower(): d.location for d in device_list}
        def pinned(port):
            return (location_by_port.get(port.lower()), port)
        pinned_ports = {p.lower() for p in (left, right) if p}
        remaining = [x for x in found if x[1].lower() not in pinned_ports]
        result = []
        result.append(pinned(left) if left else (remaining.pop(0) if remaining else None))
        result.append(pinned(right) if right else (remaining.pop(0) if remaining else None))
        return [x for x in result if x is not None]
    except Exception as e:
        print(f"An Exception occured while trying to locate LED Matrix devices. {e}")
