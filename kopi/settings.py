import os
from pathlib import Path

from kopi.sessionkey import load_session_key

BASE_DIR = Path(__file__).resolve().parent.parent

DEBUG = False
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.staticfiles",
    "kopi.apps.KopiConfig",
]

MIDDLEWARE = [
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
]

ROOT_URLCONF = "kopi.urls"
WSGI_APPLICATION = "kopi.wsgi.application"

STATIC_URL = "/static/"
# DEBUG is off, so WhiteNoise would not use finders on its own.
WHITENOISE_USE_FINDERS = True

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.jinja2.Jinja2",
        "DIRS": [BASE_DIR / "kopi" / "jinja"],
        "APP_DIRS": False,
        "OPTIONS": {
            "environment": "kopi.jinja.environment",
            "context_processors": [
                "kopi.access.operator",
            ],
        },
    },
]

# Seconds. sqlite3.connect uses this, and the connection hook sets the same pragma.
SQLITE_BUSY_TIMEOUT = 5

# Seconds the worker sleeps after each tick. The tick itself does not sleep.
WORKER_SLEEP_SECONDS = float(os.environ.get("KOPI_WORKER_SLEEP", "5"))


def data_directory():
    override = os.environ.get("KOPI_DATA_DIR")
    if override:
        return Path(override)
    return BASE_DIR / "devdata" / "data"


def work_directory():
    """Ciphertext staged for upload. Development keeps it at ./devdata/work."""
    override = os.environ.get("KOPI_WORK_DIR")
    if override:
        return Path(override)
    return BASE_DIR / "devdata" / "work"


DATA_DIR = data_directory()
# `session.key` on the data volume. A different file from `secret.key`.
SECRET_KEY = load_session_key(DATA_DIR)

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "runs"

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": DATA_DIR / "kopi.sqlite3",
        "OPTIONS": {
            "timeout": SQLITE_BUSY_TIMEOUT,
        },
    }
}

TIME_ZONE = "UTC"
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
