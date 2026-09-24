"""
Production settings — single-origin deployment (cPanel / Passenger).

Enabled by default via wsgi.py / passenger_wsgi.py. Every secret and host is
read from the environment — nothing sensitive is hard-coded — and the app
refuses to start if a required value is missing.

Layout this targets: one domain serves everything. Django answers /api and
/admin, WhiteNoise serves /static plus the Vite build's root-level assets
(/assets/*, /sw.js, /manifest.webmanifest), and config.urls falls through to the
SPA shell. The React client's axios baseURL is a relative '/api/v1', so the app
and API MUST share an origin.

Database: MySQL/MariaDB (what cPanel provides). Django 5.2 requires
MySQL >= 8.0.11 or MariaDB >= 10.5. Note that the ledger's CheckConstraints
(debit XOR credit, non-negative amounts) need MySQL >= 8.0.16 — below that
Django silently omits them and the database stops enforcing double-entry.
MariaDB supports them at every version Django accepts.
"""

import os

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F401,F403
from .base import BASE_DIR, FRONTEND_DIST, MIDDLEWARE


def _require(name):
    """Read a mandatory environment variable or fail loudly at startup."""
    value = os.environ.get(name)
    if not value:
        raise ImproperlyConfigured(
            f'The {name} environment variable is required in production.'
        )
    return value


def _csv(name, default=''):
    return [v.strip() for v in os.environ.get(name, default).split(',') if v.strip()]


# --- Core security ---------------------------------------------------------
SECRET_KEY = _require('DJANGO_SECRET_KEY')

DEBUG = False

# Comma-separated list, e.g. "mycooperativeos.com,www.mycooperativeos.com"
ALLOWED_HOSTS = _csv('DJANGO_ALLOWED_HOSTS') or None
if not ALLOWED_HOSTS:
    raise ImproperlyConfigured(
        'The DJANGO_ALLOWED_HOSTS environment variable is required in production.'
    )

# Django requires the scheme here for POSTs behind a TLS-terminating proxy
# (the admin login will 403 without it). Defaults to https:// for each host.
CSRF_TRUSTED_ORIGINS = _csv('CSRF_TRUSTED_ORIGINS') or [
    f'https://{host}' for host in ALLOWED_HOSTS if host != '*'
]

# --- Database (MySQL / MariaDB) --------------------------------------------
# PyMySQL stands in for mysqlclient: it is pure Python, so it installs on shared
# hosting with no compiler or libmysqlclient headers. Swap to mysqlclient if the
# host provides it. This shim must run before Django loads the MySQL backend.
import pymysql  # noqa: E402

pymysql.install_as_MySQLdb()

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        # cPanel prefixes database and user names, e.g. "cpaneluser_coopdb".
        'NAME': _require('MYSQL_DB'),
        'USER': _require('MYSQL_USER'),
        'PASSWORD': _require('MYSQL_PASSWORD'),
        'HOST': os.environ.get('MYSQL_HOST', 'localhost'),
        'PORT': os.environ.get('MYSQL_PORT', '3306'),
        'CONN_MAX_AGE': int(os.environ.get('MYSQL_CONN_MAX_AGE', '60')),
        'OPTIONS': {
            # utf8mb4 so Yorùbá/Igbo/Hausa diacritics and emoji round-trip.
            'charset': 'utf8mb4',
            # Refuse silent truncation and invalid dates. A money column must
            # never quietly round; it must raise.
            'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
        },
    }
}

# --- Static & SPA assets ---------------------------------------------------
STATIC_ROOT = BASE_DIR / 'staticfiles'

# WhiteNoise serves static files from the app process, so no Apache alias or
# .htaccess rules are needed — which is what makes this portable across hosts.
MIDDLEWARE = list(MIDDLEWARE)
MIDDLEWARE.insert(
    MIDDLEWARE.index('django.middleware.security.SecurityMiddleware') + 1,
    'whitenoise.middleware.WhiteNoiseMiddleware',
)

# Serve the Vite build's root-level files (/assets/*, /sw.js, /favicon.svg,
# /manifest.webmanifest) at the URL root, exactly as index.html references them.
WHITENOISE_ROOT = FRONTEND_DIST
WHITENOISE_MAX_AGE = int(os.environ.get('WHITENOISE_MAX_AGE', '3600'))

# The line above is not enough on its own, and used to claim otherwise.
# WhiteNoise only treats a file as immutable when it sits under STATIC_URL, and
# the SPA build is served from the URL root — so every file in it, including
# sw.js, was getting max-age=3600. A cached service worker keeps serving its
# precached shell (pointing at the previous bundle), which is why a redeploy
# could appear to change nothing. This hook runs last and settles both ends:
# never cache the files that choose the build, cache the hashed ones forever.
from core.static_headers import add_spa_cache_headers  # noqa: E402

WHITENOISE_ADD_HEADERS_FUNCTION = add_spa_cache_headers

STORAGES = {
    'default': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
    },
    'staticfiles': {
        # Compressed but NOT hashed: a manifest storage fails the whole deploy
        # if any referenced asset is missing, which is a poor trade on a pilot.
        'BACKEND': 'whitenoise.storage.CompressedStaticFilesStorage',
    },
}

# --- CORS ------------------------------------------------------------------
# Empty on a single-origin deployment; set only if the SPA moves to its own host.
CORS_ALLOWED_ORIGINS = _csv('CORS_ALLOWED_ORIGINS')

# --- HTTPS / hardening -----------------------------------------------------
# Trust the X-Forwarded-Proto header set by the TLS-terminating proxy/load
# balancer so Django knows the original request was HTTPS.
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = os.environ.get('SECURE_SSL_REDIRECT', 'true').lower() == 'true'

SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

SECURE_HSTS_SECONDS = int(os.environ.get('SECURE_HSTS_SECONDS', '31536000'))
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_CONTENT_TYPE_NOSNIFF = True

# --- Email (SMTP) ----------------------------------------------------------
EMAIL_BACKEND = os.environ.get(
    'EMAIL_BACKEND', 'django.core.mail.backends.smtp.EmailBackend',
)
EMAIL_HOST = os.environ.get('EMAIL_HOST', '')
EMAIL_PORT = int(os.environ.get('EMAIL_PORT', '587'))
EMAIL_HOST_USER = os.environ.get('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.environ.get('EMAIL_HOST_PASSWORD', '')
EMAIL_USE_TLS = os.environ.get('EMAIL_USE_TLS', 'true').lower() == 'true'
