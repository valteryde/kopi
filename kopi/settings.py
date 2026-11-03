import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# Development only. The image keeps Django's session key in a file on the data volume.
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only")
DEBUG = False
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

INSTALLED_APPS = [
    "kopi.apps.KopiConfig",
]

MIDDLEWARE = []

ROOT_URLCONF = "kopi.urls"
WSGI_APPLICATION = "kopi.wsgi.application"

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
    return BASE_DIR / "devdata" / "work"


DATA_DIR = data_directory()
DATA_DIR.mkdir(parents=True, exist_ok=True)

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
