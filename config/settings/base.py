"""
Base Django settings shared by every environment.

Do NOT use this module directly as DJANGO_SETTINGS_MODULE. Point at one of the
environment modules instead, which import everything here and then override the
bits that differ:

    config.settings.dev   – local development (DEBUG on, SQLite, console email)
    config.settings.prod  – production (DEBUG off, secrets from env, hardened)

manage.py and the test runner default to `dev`; wsgi/asgi default to `prod`.
"""

from pathlib import Path

from dotenv import load_dotenv

# Build paths inside the project like this: BASE_DIR / 'subdir'.
# base.py lives at backend/config/settings/base.py, so three parents up is the
# backend/ project root (where manage.py, db.sqlite3 and .env live).
BASE_DIR = Path(__file__).resolve().parent.parent.parent

# Load secrets (Paystack keys, etc.) from a local .env file if present.
load_dotenv(BASE_DIR / '.env')


# Application definition

INSTALLED_APPS = [
    # Replaces 'django.contrib.admin' to install the CooperativeOS admin site,
    # which restricts entry to platform admins (see core/admin_site.py).
    'core.admin_config.CooperativeOSAdminConfig',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',

    # Third-party
    'rest_framework',
    'rest_framework.authtoken',
    'corsheaders',

    # CooperativeOS apps
    'core',
    'tenants',
    'accounts',
    'ledger',
    'contributions',
    'payments',
    'audit',
    'governance',
    'communications',
    'reports',
    'platform_admin',
    'savings',
    'dividends',
    'loans',
    'approvals',
]

MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',

    # Resolves the active tenant (cooperative) for the request and binds it
    # to a thread-local so tenant-scoped querysets filter automatically.
    'core.middleware.CurrentTenantMiddleware',
]

# Custom user (phone/email login, NDPA-relevant PII fields).
AUTH_USER_MODEL = 'accounts.User'

# --- Django REST Framework -------------------------------------------------
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'rest_framework.authentication.TokenAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'DEFAULT_PAGINATION_CLASS':
        'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 25,
    # Logs every API failure before returning DRF's normal response. Without
    # this, 4xx responses never reach any logger (see core/exception_handler).
    'EXCEPTION_HANDLER': 'core.exception_handler.logging_exception_handler',
}

# HTTP header the client sends to select the active cooperative (tenant).
TENANT_HEADER = 'HTTP_X_COOPERATIVE_ID'

# Platform-wide default currency (Naira). Individual coops may override.
DEFAULT_CURRENCY = 'NGN'

# --- Payment Service Providers --------------------------------------------
# Secrets used to verify inbound webhook signatures. Real values come from the
# environment in staging/production; never commit live keys.
import os  # noqa: E402

PAYSTACK_SECRET_KEY = os.environ.get('PAYSTACK_SECRET_KEY', 'sk_test_dev')
PAYSTACK_PUBLIC_KEY = os.environ.get('PAYSTACK_PUBLIC_KEY', '')
PAYSTACK_BASE_URL = os.environ.get('PAYSTACK_BASE_URL', 'https://api.paystack.co')
# Where Paystack returns the payer after checkout (the webhook remains a backup
# source of truth). Empty → Paystack uses the dashboard default.
PAYSTACK_CALLBACK_URL = os.environ.get('PAYSTACK_CALLBACK_URL', '')
# Split settlement to each coop's own subaccount. Off by default so you can test
# with just a key; turn on once real subaccount codes are stored on ProviderAccount.
PAYSTACK_USE_SUBACCOUNT = (
    os.environ.get('PAYSTACK_USE_SUBACCOUNT', 'false').lower() == 'true'
)
FLUTTERWAVE_SECRET_HASH = os.environ.get(
    'FLUTTERWAVE_SECRET_HASH', 'flw_test_hash',
)

# --- White-label -----------------------------------------------------------
# CNAME target cooperatives point their custom domains at.
WHITE_LABEL_CNAME_TARGET = os.environ.get(
    'WHITE_LABEL_CNAME_TARGET', 'tenants.cooperativeos.africa',
)

# --- Messaging -------------------------------------------------------------
# The email *backend* is chosen per environment (console in dev, SMTP in prod).
DEFAULT_FROM_EMAIL = os.environ.get(
    'DEFAULT_FROM_EMAIL', 'CooperativeOS <no-reply@cooperativeos.africa>',
)

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        # Project templates win over app templates — needed to override
        # admin/base_site.html, since the admin app is listed first and
        # APP_DIRS would otherwise find its copy before ours.
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'


# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]


# Internationalization
# https://docs.djangoproject.com/en/5.2/topics/i18n/

LANGUAGE_CODE = 'en-us'

TIME_ZONE = 'UTC'

USE_I18N = True

USE_TZ = True


# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/5.2/howto/static-files/

STATIC_URL = 'static/'

# Uploaded media (member photos, KYC documents). Local filesystem in dev;
# swap DEFAULT_FILE_STORAGE for S3/GCS in production.
MEDIA_URL = 'media/'
MEDIA_ROOT = BASE_DIR / 'media'

# The Vite build of the React app. Django serves this shell for non-API routes
# on single-origin deployments (see core/views_web.spa_index).
#
# Defaults to the sibling frontend/dist of a checked-out repo. Override with the
# FRONTEND_DIST env var when the server's layout differs — e.g. when only the
# contents of backend/ were uploaded, so no sibling frontend/ exists.
def _default_frontend_dist(base_dir):
    """Locate the SPA build across the layouts we actually deploy.

    A full checkout keeps backend/ and frontend/ as siblings. Shared hosting
    often gets only the contents of backend/ uploaded to the app root, with the
    build dropped alongside as frontend_dist/. Probe for index.html rather than
    the directory, so a stale empty folder doesn't win.
    """
    candidates = (
        base_dir.parent / 'frontend' / 'dist',   # full checkout
        base_dir / 'frontend_dist',              # backend-only upload (cPanel)
    )
    for candidate in candidates:
        if (candidate / 'index.html').is_file():
            return candidate
    return candidates[0]


FRONTEND_DIST = Path(
    os.environ.get('FRONTEND_DIST') or _default_frontend_dist(BASE_DIR)
)

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# --- Logging ---------------------------------------------------------------
# Django's defaults log almost nothing once DEBUG is False: errors go to
# mail_admins, ADMINS is empty, and the record is lost. DRF compounds it by
# converting exceptions into responses before django.request sees them, so API
# 4xx failures leave no trace at all — which makes a rejected upload impossible
# to diagnose from the server.
#
# Everything lands in a rotating file alongside the app, and on stderr (which
# Passenger captures). Override the directory with DJANGO_LOG_DIR.
LOG_DIR = Path(os.environ.get('DJANGO_LOG_DIR') or BASE_DIR / 'logs')
LOG_DIR.mkdir(parents=True, exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{asctime} {levelname} {name} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': LOG_DIR / 'app.log',
            'maxBytes': 5 * 1024 * 1024,
            'backupCount': 5,
            'formatter': 'verbose',
            'encoding': 'utf-8',
        },
    },
    'loggers': {
        # 500s and Django-level request failures.
        'django.request': {
            'handlers': ['console', 'file'],
            'level': 'WARNING',
            'propagate': False,
        },
        # Every API error, including validation 400s.
        'api.errors': {
            'handlers': ['console', 'file'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}
