"""
Passenger entry point for cPanel's "Setup Python App".

cPanel/Passenger looks for a module named ``passenger_wsgi`` at the application
root and serves the module-level ``application`` callable. This file only wires
up the path and settings module; the real WSGI app lives in ``config/wsgi.py``.

The Application Root configured in cPanel must be the directory containing this
file (the ``backend/`` folder), and the Application Startup File must be
``passenger_wsgi.py``.

Passenger does not read ``.env`` for you — but ``config.settings.base`` calls
python-dotenv's ``load_dotenv`` on ``backend/.env``, so secrets placed there are
picked up. Variables set in cPanel's own "Environment variables" panel take
precedence over the file.
"""

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# Passenger starts the process from an arbitrary cwd; make sure the project
# package (config, core, accounts, ...) is importable.
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.prod")

from config.wsgi import application  # noqa: E402  (must follow the path setup)

# Passenger looks up this exact name.
__all__ = ["application"]
