"""Start Django against a throwaway data directory.

Import this before any other Kopi module that loads settings. Every test
process shares one directory so SQLite is migrated once.
"""

import os
import tempfile
from pathlib import Path

_TMP = tempfile.TemporaryDirectory(prefix="kopi-tests-")
os.environ["KOPI_DATA_DIR"] = _TMP.name
# Before Django starts, so the client tests stay on HTTP.
os.environ["KOPI_ALLOW_HTTP"] = "1"
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kopi.settings")

import django

django.setup()

from django.conf import settings

# Production keeps PBKDF2. Tests only need a hash that authenticate() accepts.
settings.PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.MD5PasswordHasher",
]
from django.contrib.auth.hashers import get_hashers, get_hashers_by_algorithm

get_hashers.cache_clear()
get_hashers_by_algorithm.cache_clear()

from django.core.management import call_command

call_command("migrate", verbosity=0, interactive=False)

DATA_DIR = Path(_TMP.name)

from kopi.identity import IDENTITY_FILENAME, generate_identity, load_identity


def ensure_identity():
    """The age identity every test that stores a secret reads and writes."""
    path = DATA_DIR / IDENTITY_FILENAME
    return generate_identity(path) if not path.exists() else load_identity(path)
