import os
from pathlib import Path

import tests.bootstrap  # noqa: F401  — sets the throwaway data dir before Django starts
from tests.bootstrap import DATA_DIR

from django.core.management import call_command
from django.db import IntegrityError, connection
from ward import raises, test

from kopi.identity import decrypt_secret, encrypt_secret, generate_identity
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
    assert Path(connection.settings_dict["NAME"]).is_relative_to(DATA_DIR)


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


@test("credential columns store age ciphertext")
def _():
    call_command("migrate", verbosity=0, interactive=False)
    identity = generate_identity(DATA_DIR / "secret.key")
    source = Source.objects.create(
        name="prod",
        kind=Source.Kind.POSTGRES,
        pg_host="127.0.0.1",
        pg_database="kopi",
        pg_user="kopi",
        pg_password=encrypt_secret(identity, "db-password"),
    )
    target = Target.objects.create(
        name="creds",
        bucket="kopi",
        access_key_id=encrypt_secret(identity, "GKACCESS"),
        secret_access_key=encrypt_secret(identity, "s3-secret"),
    )

    stored_source = Source.objects.get(pk=source.pk)
    stored_target = Target.objects.get(pk=target.pk)
    assert "db-password" not in stored_source.pg_password
    assert "GKACCESS" not in stored_target.access_key_id
    assert "s3-secret" not in stored_target.secret_access_key
    assert decrypt_secret(identity, stored_source.pg_password) == "db-password"
    assert decrypt_secret(identity, stored_target.access_key_id) == "GKACCESS"
    assert decrypt_secret(identity, stored_target.secret_access_key) == "s3-secret"
