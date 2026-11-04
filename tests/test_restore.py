import json
import os
import stat
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

import tests.bootstrap  # noqa: F401

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from ward import test

from kopi.audit import RESTORE, format_size
from kopi.identity import (
    IDENTITY_FILENAME,
    encrypt_secret,
    generate_identity,
    load_identity,
)
from kopi.models import AuditEvent, Link, Run, Source, Target
from kopi.pipeline import archive_folder, marker_path
from kopi.storage import FakeStorage, object_key


def _client():
    return Client(HTTP_HOST="localhost")


def _text(response) -> str:
    if getattr(response, "streaming", False):
        return b"".join(response.streaming_content).decode()
    return response.content.decode()


def _user(name: str, role: str) -> User:
    user = User.objects.create_user(name, password="correct-password")
    user.groups.add(Group.objects.get(name=role))
    return user


def _link(source: Source, token: str) -> Link:
    target = Target.objects.create(name=f"kopi-{source.name}", bucket=f"b-{token}-{source.pk}")
    return Link.objects.create(
        source=source,
        target=target,
        cadence="0 2 * * *",
        timezone="Europe/Copenhagen",
        retention_kind=Link.Retention.COUNT,
        retention_count=14,
    )


def _identity():
    path = Path(settings.DATA_DIR) / IDENTITY_FILENAME
    if path.exists():
        return load_identity(path)
    return generate_identity(path)


@test("a marked folder object is restored to the named directory, and a viewer POST is refused")
def _():
    token = uuid.uuid4().hex[:8]
    identity = _identity()
    password = f"secret-db-{token}"
    folder_name = f"uploads-{token}"
    db_name = f"bill{token}"
    database = f"{db_name}_restore"

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        source_dir = root / "source"
        nested = source_dir / "nested"
        nested.mkdir(parents=True)
        note = source_dir / "note.txt"
        note.write_text(f"hello restore {token}\n")
        os.chmod(note, 0o644)
        inner = nested / "inner.txt"
        inner.write_text("inside\n")
        os.chmod(nested, 0o755)
        ciphertext = root / "payload.age"
        marker = archive_folder(source_dir, identity, ciphertext, "restore-test")
        payload = ciphertext.read_bytes()
        storage = FakeStorage()

        folder = Source.objects.create(
            name=folder_name,
            kind=Source.Kind.FOLDER,
            root_path=str(source_dir),
        )
        database_source = Source.objects.create(
            name=f"billing-{token}",
            kind=Source.Kind.POSTGRES,
            pg_host="db.internal",
            pg_port=5432,
            pg_database=db_name,
            pg_user="kopi",
            pg_password=encrypt_secret(identity, password),
            pg_dump_binary="/usr/lib/postgresql/16/bin/pg_dump",
        )
        assert database_source.pg_password != password
        assert password not in database_source.pg_password
        folder_link = _link(folder, token)
        database_link = _link(database_source, token)
        when = timezone.now()
        folder_key = object_key(folder.name, when, marker.sha256)
        database_key = object_key(database_source.name, when, marker.sha256)
        storage.put(folder_key, ciphertext, checksum=marker.sha256)
        storage.put(database_key, ciphertext, checksum=marker.sha256)
        marker.path.unlink()
        assert not marker_path(ciphertext).exists()

        folder_run = Run.objects.create(
            link=folder_link,
            status=Run.Status.SUCCEEDED,
            size=marker.size,
            checksum=marker.sha256,
            object_name=folder_key,
            log="uploaded",
        )
        database_run = Run.objects.create(
            link=database_link,
            status=Run.Status.FAILED,
            size=marker.size,
            checksum=marker.sha256,
            object_name=database_key,
            log="post script timed out",
        )
        running = Run.objects.create(
            link=folder_link,
            status=Run.Status.RUNNING,
            phase=Run.Phase.ARCHIVE,
            bytes_moved=1024,
            started_at=when,
        )

        destination = root / "restored"
        refused = root / "viewer-dest"
        not_yet = root / "not-yet"
        leaked = Path(settings.DATA_DIR) / f"leak-{token}"
        relative_name = f"not-absolute-{token}"
        assert destination.resolve().is_relative_to(root.resolve())
        assert not destination.resolve().is_relative_to(Path(settings.DATA_DIR).resolve())

        viewer = _user(f"restore-viewer-{token}", "viewer")
        admin = _user(f"restore-admin-{token}", "admin")
        work = root / "work"
        previous_work = os.environ.get("KOPI_WORK_DIR")
        os.environ["KOPI_WORK_DIR"] = str(work)

        def open_storage(target, loaded):
            assert loaded.path == identity.path
            return storage

        try:
            with patch("kopi.views.runs._open_storage", side_effect=open_storage):
                viewer_client = _client()
                viewer_client.force_login(viewer)
                anonymous = Client(HTTP_HOST="localhost")
                assert anonymous.post(
                    reverse("run-restore", args=[folder_run.pk]),
                    {"destination": str(refused)},
                ).status_code == 302
                denied = viewer_client.post(
                    reverse("run-restore", args=[folder_run.pk]),
                    {"destination": str(refused)},
                )
                assert denied.status_code == 403
                assert not refused.exists()
                assert not destination.exists()
                assert not AuditEvent.objects.filter(
                    kind=RESTORE, event__contains=str(refused)
                ).exists()

                viewer_page = viewer_client.get(reverse("run", args=[folder_run.pk]))
                viewer_body = viewer_page.content.decode()
                assert viewer_page.status_code == 200
                assert "restore.js" not in viewer_body
                assert "modal.css" not in viewer_body
                assert "data-open-restore" not in viewer_body
                assert 'id="restore-form"' not in viewer_body

                admin_client = _client()
                admin_client.force_login(admin)
                assert admin_client.get(reverse("run-restore", args=[folder_run.pk])).status_code == 405

                folder_action = reverse("run-restore", args=[folder_run.pk])
                database_action = reverse("run-restore", args=[database_run.pk])
                folder_button = (
                    f'data-action="{folder_action}" data-kind="folder" '
                    f'data-name="{folder.name}" data-object="{folder_key}" '
                    f'data-size="{format_size(marker.size)}" data-sum="sha256 {marker.sha256}" '
                    f'data-dest="/restore/{folder.name}"'
                )
                database_button = (
                    f'data-action="{database_action}" data-kind="database" '
                    f'data-name="{database_source.name}" data-object="{database_key}" '
                    f'data-size="{format_size(marker.size)}" data-sum="sha256 {marker.sha256}" '
                    f'data-dest="{database}"'
                )

                running_page = admin_client.get(reverse("run", args=[running.pk]))
                running_body = running_page.content.decode()
                assert "Restore stays closed until the upload finishes." in running_body
                assert "poll.js" in running_body
                assert "restore.js" in running_body
                assert running_body.count(folder_button) == 1
                assert running_body.count(database_button) == 1
                assert reverse("run-restore", args=[running.pk]) not in running_body

                page = admin_client.get(reverse("run", args=[folder_run.pk]))
                body = page.content.decode()
                assert page.status_code == 200
                assert "modal.css" in body
                assert body.count("restore.js") == 1
                assert 'id="restore-form"' in body
                assert 'method="post"' in body
                assert 'name="destination"' in body
                assert 'type="submit"' in body
                assert 'id="restore-go"' in body
                assert body.count(folder_button) == 2
                assert body.count(database_button) == 1
                assert password not in body

                early = admin_client.post(
                    reverse("run-restore", args=[running.pk]),
                    {"destination": str(not_yet)},
                    follow=True,
                )
                assert "This run has no object to restore." in early.content.decode()
                assert not not_yet.exists()

                relative = admin_client.post(
                    reverse("run-restore", args=[folder_run.pk]),
                    {"destination": relative_name},
                    follow=True,
                )
                assert "Name an absolute path." in relative.content.decode()
                assert not Path(relative_name).exists()

                inside = admin_client.post(
                    reverse("run-restore", args=[folder_run.pk]),
                    {"destination": str(leaked)},
                    follow=True,
                )
                inside_body = inside.content.decode()
                assert "That path is inside the Kopi data directory." in inside_body
                assert 'class="action-note"' not in inside_body
                assert not leaked.exists()
                assert not AuditEvent.objects.filter(kind=RESTORE, event__contains=str(leaked)).exists()

                restored = admin_client.post(
                    reverse("run-restore", args=[folder_run.pk]),
                    {"destination": str(destination)},
                    follow=True,
                )
                restored_body = restored.content.decode()
                assert restored.status_code == 200
                assert f"Restored {folder.name} to {destination}." in restored_body
                assert 'class="action-note"' in restored_body
                assert password not in restored_body
                assert (destination / "note.txt").read_text() == f"hello restore {token}\n"
                assert (destination / "nested" / "inner.txt").read_text() == "inside\n"
                assert stat.S_IMODE((destination / "note.txt").stat().st_mode) == 0o600
                assert stat.S_IMODE((destination / "nested").stat().st_mode) == 0o700
                assert stat.S_IMODE(destination.stat().st_mode) == 0o700
                assert stat.S_IMODE(note.stat().st_mode) == 0o644
                names = sorted(
                    path.relative_to(destination).as_posix() for path in destination.rglob("*")
                )
                assert names == ["nested", "nested/inner.txt", "note.txt"]

                seen = {}

                def accept(downloaded, loaded, **kwargs):
                    assert downloaded.is_file()
                    assert downloaded.read_bytes() == payload
                    assert downloaded.resolve().is_relative_to(work.resolve())
                    written = marker_path(downloaded)
                    assert written.is_file()
                    assert stat.S_IMODE(written.stat().st_mode) == 0o600
                    assert json.loads(written.read_text(encoding="utf-8")) == {
                        "run_id": str(database_run.pk),
                        "sha256": marker.sha256,
                        "size": marker.size,
                    }
                    assert loaded.path == identity.path
                    seen.update(kwargs)

                with patch("kopi.views.runs.restore_database", side_effect=AssertionError("unexpected")):
                    bad_name = admin_client.post(
                        reverse("run-restore", args=[database_run.pk]),
                        {"destination": f"{database} extra"},
                        follow=True,
                    )
                assert "Name a database." in bad_name.content.decode()
                assert seen == {}

                with patch("kopi.views.runs.restore_database", side_effect=accept):
                    dumped = admin_client.post(
                        reverse("run-restore", args=[database_run.pk]),
                        {"destination": database},
                        follow=True,
                    )
                dumped_body = dumped.content.decode()
                assert seen["database"] == database
                assert seen["password"] == password
                assert seen["host"] == "db.internal"
                assert seen["port"] == 5432
                assert seen["user"] == "kopi"
                assert seen["binary"] == "/usr/lib/postgresql/16/bin/pg_dump"
                assert f"Restored {database_source.name} to {database}." in dumped_body
                assert "The run is failed. The ciphertext is still there to restore." in dumped_body
                assert password not in dumped_body
                assert not Path(database).exists()
                assert not (root / database).exists()
        finally:
            if previous_work is None:
                os.environ.pop("KOPI_WORK_DIR", None)
            else:
                os.environ["KOPI_WORK_DIR"] = previous_work

        assert list(work.rglob("*")) == []
        folder_event = AuditEvent.objects.get(kind=RESTORE, event=f"Restore · {folder.name} → {destination}")
        assert folder_event.user_id == admin.pk
        assert folder_event.actor == AuditEvent.Actor.USER
        assert folder_event.name == admin.get_username()
        database_event = AuditEvent.objects.get(
            kind=RESTORE,
            event=f"Restore · {database_source.name} → {database}",
        )
        assert database_event.user_id == admin.pk
        assert password not in database_event.event
        audit_page = _client()
        audit_page.force_login(admin)
        audit_body = audit_page.get(reverse("audit")).content.decode()
        assert folder_event.event in audit_body
        assert database_event.event in audit_body
        assert "ph-arrow-counter-clockwise" in audit_body

    script = _text(_client().get("/static/js/restore.js"))
    assert 'setAttribute("action"' in script
    assert "Folder path" in script
    assert "Database name" in script
    assert "Files are mode 0600. Directories are mode 0700." in script
    assert "The dump is restored into this database." in script
    assert "[data-open-restore]" in script
