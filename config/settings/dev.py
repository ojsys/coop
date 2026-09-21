"""
Development settings — local machines only.

Enabled by default via manage.py and the test runner. Optimised for a fast,
forgiving local loop: DEBUG on, SQLite, emails printed to the console. None of
the values here are safe for a public deployment.
"""

import os

from .base import *  # noqa: F401,F403
from .base import BASE_DIR

# SECURITY WARNING: this key is public and for local dev only. Production reads
# its key from the environment (see prod.py).
SECRET_KEY = os.environ.get(
    'DJANGO_SECRET_KEY',
    'django-insecure-sy_m)ntw3=9s#e!-%@-6lqq6#jco)-3gob1$@zydhqcp@j)wtn',
)

# SECURITY WARNING: never run with debug turned on in production!
DEBUG = True

ALLOWED_HOSTS = ['localhost', '127.0.0.1', '0.0.0.0']

# Local SQLite database.
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# CORS — the React admin console / member PWA run on a separate origin in dev.
CORS_ALLOWED_ORIGINS = [
    'http://localhost:5173',
    'http://127.0.0.1:5173',
]

# Print emails to stdout instead of sending them.
EMAIL_BACKEND = os.environ.get(
    'EMAIL_BACKEND', 'django.core.mail.backends.console.EmailBackend',
)
