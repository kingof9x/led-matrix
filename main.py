# Built In Dependencies
import os
import sys
import logging

# Internal Dependencies
from led_mon import platform_utils

if __name__ == "__main__":
    # Set up logging before the app (and its plugins) are imported, so import errors reach the log file on Windows
    platform_utils.load_env_file()
    platform_utils.setup_logging()
    from led_mon.led_system_monitor import main
    try:
        main(sys.argv)
    except Exception:
        logging.getLogger(__name__).exception("led_mon stopped with an error")
        raise
