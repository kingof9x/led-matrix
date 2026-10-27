# Built In dependencies
import logging
import sys

# Internal dependencies
from led_mon.led_system_monitor import main
from led_mon import platform_utils

platform_utils.setup_logging()

log = logging.getLogger(__name__)

if __name__ == "__main__":
    log.debug("Calling main")
    main(sys.argv)