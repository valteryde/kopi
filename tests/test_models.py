import os
import tempfile
from pathlib import Path

# Point Django at a throwaway directory before settings are imported, so a test
# run does not migrate the development database under devdata/data.
_TMP = tempfile.TemporaryDirectory(prefix="kopi-models-")
os.environ["KOPI_DATA_DIR"] = _TMP.name
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kopi.settings")

import django

django.setup()

from django.core.management import call_command
from django.db import IntegrityError, connection
from ward import raises, test

from kopi.models import Link, Run, Source, Target
from kopi.settings import BASE_DIR, SQLITE_BUSY_TIMEOUT, data_directory

ROOT = Path(__file__).resolve().parents[1]


@test("the development database is devdata/data when KOPI_DATA_DIR is unset")
def _():
    saved = os.environ.pop("KOPI_DATA_DIR", None)
    try:
        assert data_directory() == BASE_DIR / "devdata" / "data"
        assert data_directory() == ROOT / "devdata" / "data"
    finally:
        if saved is not None:
            os.environ["KOPI_DATA_DIR"] = saved


@test("a folder source, link, and run round-trip through a migrated sqlite database")
def _():
    call_command("migrate", verbosity=0, interactive=False)

    source = Source.objects.create(
        name="notes",
        kind=Source.Kind.FOLDER,
        root_path="/data/notes",
        include_rules="docs/**",
        exclude_rules="*.tmp",
    )
    target = Target.objects.create(
        name="garage",
        bucket="kopi",
        endpoint_url="http://127.0.0.1:3900",
        region="garage",
        path_style=True,
        access_key_id="opaque-access-key",
        secret_access_key="opaque-secret",
    )
    link = Link.objects.create(
        source=source,
        target=target,
        cadence="0 3 * * *",
        timezone="Europe/Copenhagen",
        retention_kind=Link.Retention.COUNT,
        retention_count=7,
        pre_script_path="/hooks/pre.sh",
        post_script_path="/hooks/post.sh",
    )
    run = Run.objects.create(
        link=link,
        status=Run.Status.RUNNING,
        phase=Run.Phase.ARCHIVE,
        bytes_moved=1024,
        size=2048,
        checksum="a" * 64,
        object_name="notes/2026-11-03T17:00:00Z/" + "a" * 64,
        log="archived\n",
    )

    stored = Run.objects.select_related("link__source", "link__target").get(pk=run.pk)
    assert stored.link.source.name == "notes"
    assert stored.link.source.kind == Source.Kind.FOLDER
    assert stored.link.source.root_path == "/data/notes"
    assert stored.link.source.include_rules == "docs/**"
    assert stored.link.source.exclude_rules == "*.tmp"
    assert stored.link.cadence == "0 3 * * *"
    assert stored.link.timezone == "Europe/Copenhagen"
    assert stored.link.retention_kind == Link.Retention.COUNT
    assert stored.link.retention_count == 7
    assert stored.link.retention_days is None
    assert stored.link.pre_script_path == "/hooks/pre.sh"
    assert stored.link.post_script_path == "/hooks/post.sh"
    assert stored.link.pre_script_timeout == 300
    assert stored.link.post_script_timeout == 300
    assert stored.link.target.bucket == "kopi"
    assert stored.link.target.secret_access_key == "opaque-secret"
    assert stored.status == Run.Status.RUNNING
    assert stored.phase == Run.Phase.ARCHIVE
    assert stored.bytes_moved == 1024
    assert stored.size == 2048
    assert stored.checksum == "a" * 64
    assert stored.object_name.endswith(stored.checksum)
    assert stored.log == "archived\n"
    assert stored.started_at is None
    assert stored.finished_at is None
    assert stored.created_at is not None
    assert stored.updated_at is not None

    with connection.cursor() as cursor:
        cursor.execute("PRAGMA journal_mode")
        mode = cursor.fetchone()[0]
        cursor.execute("PRAGMA busy_timeout")
        timeout_ms = cursor.fetchone()[0]
    assert mode.lower() == "wal"
    assert timeout_ms == SQLITE_BUSY_TIMEOUT * 1000
    assert Path(connection.settings_dict["NAME"]).is_relative_to(Path(_TMP.name))


@test("a folder source needs a root path and retention fields have to match the kind")
def _():
    call_command("migrate", verbosity=0, interactive=False)
    with raises(IntegrityError):
        Source.objects.create(name="empty-folder", kind=Source.Kind.FOLDER, root_path="")

    source = Source.objects.create(
        name="notes-constraint",
        kind=Source.Kind.FOLDER,
        root_path="/data/notes",
    )
    target = Target.objects.create(name="garage-constraint", bucket="kopi")
    with raises(IntegrityError):
        Link.objects.create(
            source=source,
            target=target,
            cadence="0 3 * * *",
            timezone="Europe/Copenhagen",
            retention_kind=Link.Retention.COUNT,
        )
