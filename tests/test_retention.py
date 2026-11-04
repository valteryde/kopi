import hashlib
import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import tests.bootstrap  # noqa: F401

from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import Client
from django.urls import reverse
from django.utils import timezone as dj_tz
from unittest.mock import patch
from ward import test

from kopi.audit import format_size
from kopi.child import execute, reconcile
from kopi.identity import generate_identity
from kopi.models import Link, Run, Source, Target
from kopi.pipeline import _write_marker
from kopi.retention import apply_retention
from kopi.storage import FakeStorage, StoredObject, object_key


def _name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@contextmanager
def _tmpdir():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _put(storage, directory: Path, source: str, when: datetime, body: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(body).hexdigest()
    key = object_key(source, when, digest)
    path = directory / digest
    path.write_bytes(body)
    storage.put(key, path, checksum=digest)
    return key, digest


def _succeeded(link: Link, key: str, digest: str, size: int, when: datetime) -> Run:
    return Run.objects.create(
        link=link,
        status=Run.Status.SUCCEEDED,
        size=size,
        checksum=digest,
        object_name=key,
        started_at=when,
        finished_at=when,
    )


def _pair(kind: str, *, count: int | None = None, days: int | None = None) -> tuple[Source, Link]:
    source = Source.objects.create(
        name=_name("src"),
        kind=Source.Kind.FOLDER,
        root_path=f"/data/{uuid.uuid4().hex[:8]}",
    )
    target = Target.objects.create(name=_name("tgt"), bucket="kopi")
    link = Link.objects.create(
        source=source,
        target=target,
        cadence="0 3 * * *",
        timezone="UTC",
        retention_kind=kind,
        retention_count=count,
        retention_days=days,
    )
    return source, link


class _Counting(FakeStorage):
    def __init__(self) -> None:
        super().__init__()
        self.lists = 0
        self.deletes: list[str] = []

    def list(self, prefix: str = ""):
        self.lists += 1
        return super().list(prefix)

    def delete(self, key: str) -> None:
        self.deletes.append(key)
        super().delete(key)


def _script(path: Path, body: str) -> str:
    path.write_text(body)
    os.chmod(path, 0o700)
    return str(path)


def _live_run(tmp: Path, *, count: int | None = None, days: int | None = None, post: str = "") -> Run:
    source_dir = tmp / "source"
    source_dir.mkdir()
    (source_dir / "hello.txt").write_text("hello")
    kind = Link.Retention.AGE if days is not None else Link.Retention.COUNT
    source = Source.objects.create(
        name=_name("src"),
        kind=Source.Kind.FOLDER,
        root_path=str(source_dir),
    )
    target = Target.objects.create(name=_name("tgt"), bucket="kopi")
    link = Link.objects.create(
        source=source,
        target=target,
        cadence="0 3 * * *",
        timezone="Europe/Copenhagen",
        retention_kind=kind,
        retention_count=count,
        retention_days=days,
        post_script_path=post,
    )
    return Run.objects.create(link=link)


def _refresh(run: Run) -> Run:
    connection.close()
    run.refresh_from_db()
    return run


@test("keep-last-2 deletes the older object and hides its restore")
def _():
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    source, link = _pair(Link.Retention.COUNT, count=2)
    storage = FakeStorage()
    with _tmpdir() as tmp:
        oldest_key, oldest_sum = _put(storage, tmp, source.name, now - timedelta(days=60), b"oldest")
        middle_key, _middle_sum = _put(storage, tmp, source.name, now - timedelta(days=30), b"middle")
        newest_key, newest_sum = _put(storage, tmp, source.name, now, b"newest")
    oldest = _succeeded(link, oldest_key, oldest_sum, len(b"oldest"), now - timedelta(days=60))
    _succeeded(link, middle_key, _middle_sum, len(b"middle"), now - timedelta(days=30))
    newest = _succeeded(link, newest_key, newest_sum, len(b"newest"), now)
    apply_retention(newest.pk, storage, now=now)
    oldest.refresh_from_db()
    newest.refresh_from_db()
    assert oldest.removed_by_retention is True
    assert oldest.size == len(b"oldest")
    assert oldest.checksum == oldest_sum
    assert newest.removed_by_retention is False
    assert [item.key for item in storage.list()] == [middle_key, newest_key]

    admin = User.objects.create_user(_name("adm"), password="correct-password")
    admin.groups.add(Group.objects.get(name="admin"))
    client = Client(HTTP_HOST="localhost")
    client.force_login(admin)
    page = client.get(reverse("run", args=[oldest.pk]))
    body = page.content.decode()
    inspect = body.split('class="panel inspect"', 1)[1]
    assert page.status_code == 200
    assert "The retention rule removed this object." in inspect
    assert f"sha256 {oldest_sum}" in inspect
    assert format_size(len(b"oldest")) in inspect
    assert oldest_key not in inspect
    assert "data-open-restore" not in inspect
    kept = client.get(reverse("run", args=[newest.pk]))
    kept_inspect = kept.content.decode().split('class="panel inspect"', 1)[1]
    assert "data-open-restore" in kept_inspect
    assert newest_key in kept_inspect


@test("delete-older-than uses the timestamp in the object name")
def _():
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    source, link = _pair(Link.Retention.AGE, days=30)
    storage = FakeStorage()
    with _tmpdir() as tmp:
        old_key, old_sum = _put(storage, tmp, source.name, now - timedelta(days=40), b"stale")
        fresh_key, fresh_sum = _put(storage, tmp, source.name, now - timedelta(days=10), b"fresh")
    old = _succeeded(link, old_key, old_sum, len(b"stale"), now)
    fresh = _succeeded(link, fresh_key, fresh_sum, len(b"fresh"), datetime(2020, 1, 1, tzinfo=timezone.utc))
    apply_retention(fresh.pk, storage, now=now)
    old.refresh_from_db()
    fresh.refresh_from_db()
    assert old.removed_by_retention is True
    assert fresh.removed_by_retention is False
    assert [item.key for item in storage.list()] == [fresh_key]


@test("a key the listing cannot parse is left alone")
def _():
    now = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
    source, link = _pair(Link.Retention.COUNT, count=1)
    digest = "ab" * 32
    older = object_key(source.name, now - timedelta(days=2), digest)
    newer = object_key(source.name, now, "cd" * 32)
    odd = StoredObject(key=f"{source.name}/not-a-key", size=1, checksum="nope")
    kept = []

    class _Mixed:
        def list(self, prefix: str = ""):
            return [
                odd,
                StoredObject(key=older, size=1, checksum=digest),
                StoredObject(key=newer, size=1, checksum="cd" * 32),
            ]

        def delete(self, key: str) -> None:
            kept.append(key)

    current = _succeeded(link, newer, "cd" * 32, 1, now)
    apply_retention(current.pk, _Mixed(), now=now)
    assert kept == [older]
    assert odd.key not in kept


@test("a failed upload does not list or delete")
def _():
    with _tmpdir() as tmp:
        run = _live_run(tmp, count=1)
        storage = _Counting()
        planted, _digest = _put(
            storage,
            tmp,
            run.link.source.name,
            dj_tz.now() - timedelta(days=3),
            b"already-there",
        )
        storage.plans.clear()
        storage.fail_next_put()
        storage.fail_next_put()
        execute(
            run.pk,
            identity=generate_identity(tmp / "secret.key"),
            storage=storage,
            work_dir=tmp / "work",
        )
        run = _refresh(run)
        assert run.status == Run.Status.FAILED
        assert storage.lists == 0
        assert storage.deletes == []
        assert list(storage._objects) == [planted]


@test("a post-script failure still prunes")
def _():
    with _tmpdir() as tmp:
        post = _script(tmp / "post.sh", "#!/bin/bash\nexit 1\n")
        run = _live_run(tmp, count=1, post=post)
        storage = FakeStorage()
        planted, planted_sum = _put(
            storage,
            tmp,
            run.link.source.name,
            dj_tz.now() - timedelta(days=5),
            b"previous",
        )
        previous = _succeeded(
            run.link,
            planted,
            planted_sum,
            len(b"previous"),
            dj_tz.now() - timedelta(days=5),
        )
        execute(
            run.pk,
            identity=generate_identity(tmp / "secret.key"),
            storage=storage,
            work_dir=tmp / "work",
        )
        run = _refresh(run)
        previous.refresh_from_db()
        assert run.status == Run.Status.FAILED
        assert previous.removed_by_retention is True
        assert [item.key for item in storage.list()] == [run.object_name]
        assert run.removed_by_retention is False


@test("a delete error fails the run and leaves the new object")
def _():
    with _tmpdir() as tmp:
        run = _live_run(tmp, count=1)
        storage = FakeStorage()
        planted, planted_sum = _put(
            storage,
            tmp,
            run.link.source.name,
            dj_tz.now() - timedelta(days=5),
            b"previous",
        )
        _succeeded(
            run.link,
            planted,
            planted_sum,
            len(b"previous"),
            dj_tz.now() - timedelta(days=5),
        )
        storage.fail_next_delete()
        execute(
            run.pk,
            identity=generate_identity(tmp / "secret.key"),
            storage=storage,
            work_dir=tmp / "work",
        )
        run = _refresh(run)
        assert run.status == Run.Status.FAILED
        assert "delete failed" in run.log
        assert run.removed_by_retention is False
        assert {item.key for item in storage.list()} == {planted, run.object_name}


@test("a marked file reconciled after a crash is not archived again")
def _():
    with _tmpdir() as tmp:
        post_flag = tmp / "post"
        post = _script(tmp / "post.sh", f"#!/bin/bash\ntouch {post_flag}\n")
        run = _live_run(tmp, count=1, post=post)
        storage = FakeStorage()
        planted, planted_sum = _put(
            storage,
            tmp,
            run.link.source.name,
            dj_tz.now() - timedelta(days=9),
            b"previous",
        )
        previous = _succeeded(
            run.link,
            planted,
            planted_sum,
            len(b"previous"),
            dj_tz.now() - timedelta(days=9),
        )
        storage.plans.clear()
        work = tmp / "work"
        blob = b"marked-ciphertext"
        digest = hashlib.sha256(blob).hexdigest()
        ciphertext = work / str(run.pk) / "payload.age"
        ciphertext.parent.mkdir(parents=True)
        ciphertext.write_bytes(blob)
        os.chmod(ciphertext, 0o600)
        _write_marker(ciphertext, str(run.pk), len(blob), digest)
        Run.objects.filter(pk=run.pk).update(
            status=Run.Status.RUNNING,
            phase=Run.Phase.ARCHIVE,
            started_at=dj_tz.now(),
        )
        with patch("kopi.child.start_folder_producer") as started:
            reconcile(
                run.pk,
                identity=generate_identity(tmp / "secret.key"),
                storage=storage,
                work_dir=work,
            )
            assert started.call_count == 0
        run = _refresh(run)
        previous.refresh_from_db()
        assert run.status == Run.Status.SUCCEEDED
        assert len(storage.plans) == 1
        assert previous.removed_by_retention is True
        assert [item.key for item in storage.list()] == [run.object_name]
        assert post_flag.is_file()
        assert not ciphertext.exists()


@test("a recorded upload is not uploaded again, and retention still runs")
def _():
    with _tmpdir() as tmp:
        post_flag = tmp / "post"
        post = _script(tmp / "post.sh", f"#!/bin/bash\ntouch {post_flag}\n")
        run = _live_run(tmp, count=1, post=post)
        storage = FakeStorage()
        source = run.link.source.name
        current_key, current_sum = _put(storage, tmp, source, dj_tz.now(), b"current")
        planted, planted_sum = _put(
            storage,
            tmp,
            source,
            dj_tz.now() - timedelta(days=9),
            b"previous",
        )
        previous = _succeeded(
            run.link,
            planted,
            planted_sum,
            len(b"previous"),
            dj_tz.now() - timedelta(days=9),
        )
        storage.plans.clear()
        Run.objects.filter(pk=run.pk).update(
            status=Run.Status.RUNNING,
            phase=Run.Phase.POST,
            object_name=current_key,
            checksum=current_sum,
            size=len(b"current"),
            started_at=dj_tz.now(),
        )
        with patch("kopi.child.start_folder_producer") as started:
            reconcile(
                run.pk,
                identity=generate_identity(tmp / "secret.key"),
                storage=storage,
                work_dir=tmp / "work",
            )
            assert started.call_count == 0
        run = _refresh(run)
        previous.refresh_from_db()
        assert run.status == Run.Status.SUCCEEDED
        assert storage.plans == []
        assert post_flag.is_file()
        assert previous.removed_by_retention is True
        assert [item.key for item in storage.list()] == [current_key]
