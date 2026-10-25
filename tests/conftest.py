import os
import sys

# Make `import led_mon` work when pytest is run from the repo root without installing
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
