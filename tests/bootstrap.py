"""Start Django against a throwaway data directory.

Import this before any other Kopi module that loads settings. Every test
process shares one directory so SQLite is migrated once.
"""

import os
import tempfile
from pathlib import Path

_TMP = tempfile.TemporaryDirectory(prefix="kopi-tests-")
os.environ["KOPI_DATA_DIR"] = _TMP.name
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kopi.settings")

import django

django.setup()

from django.core.management import call_command

call_command("migrate", verbosity=0, interactive=False)

DATA_DIR = Path(_TMP.name)
