import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import tests.bootstrap  # noqa: F401
from tests.bootstrap import DATA_DIR

from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse
from django.utils import timezone as dj_tz
from ward import test

from kopi.audit import AuditEvent
from kopi.identity import (
    IDENTITY_FILENAME,
    decrypt_secret,
    encrypt_secret,
    generate_identity,
    load_identity,
)
from kopi.models import Channel, Link, Run, Source, Target
from kopi.notify import DIGEST_SUBJECT, deliver, format_when, notify
from kopi.schedule import next_digest_at
from kopi.worker import tick

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)
PASSWORD = "mailbox-secret"


def _client():
    return Client(enforce_csrf_checks=True, HTTP_HOST="localhost")


def _csrf(body: str) -> str:
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', body)
    assert match, body
    return match.group(1)


def _user(name: str, role: str) -> User:
    user = User.objects.create_user(name, password="correct-password")
    user.groups.add(Group.objects.get(name=role))
    return user


def _identity():
    path = DATA_DIR / IDENTITY_FILENAME
    if path.exists() and not path.is_symlink():
        return load_identity(path)
    return generate_identity(path)


def _channel(**kwargs) -> Channel:
    Channel.objects.all().delete()
    fields = {
        "host": "smtp.example",
        "port": 587,
        "from_address": "kopi@example.com",
        "recipients": "ops@example.com",
        "digest_kind": Channel.Digest.MONTHLY,
        "digest_hour": 8,
        "timezone": "Europe/Copenhagen",
        "last_digest_at": NOW,
    }
    fields.update(kwargs)
    return Channel.objects.create(**fields)


def _link(name: str | None = None) -> Link:
    token = uuid.uuid4().hex[:8]
    source = Source.objects.create(
        name=name or f"src-{token}",
        kind=Source.Kind.FOLDER,
        root_path=f"/data/{token}",
    )
    target = Target.objects.create(name=f"tgt-{token}", bucket=f"b-{token}")
    return Link.objects.create(
        source=source,
        target=target,
        cadence="0 3 * * *",
        timezone="Europe/Copenhagen",
        retention_kind=Link.Retention.COUNT,
        retention_count=3,
    )


def _silence() -> None:
    Run.objects.filter(status=Run.Status.FAILED, failure_notified_at__isnull=True).update(
        failure_notified_at=NOW
    )


@test("a failed run produces one message and a second tick does not repeat it")
def _():
    _silence()
    identity = _identity()
    channel = _channel(timezone="UTC", password=encrypt_secret(identity, PASSWORD))
    Channel.objects.filter(pk=channel.pk).update(created_at=datetime(2026, 11, 1, tzinfo=timezone.utc))
    channel.refresh_from_db()
    link = _link()
    finished = datetime(2026, 11, 2, 2, 40, tzinfo=timezone.utc)
    run = Run.objects.create(
        link=link,
        status=Run.Status.FAILED,
        finished_at=finished,
        log="post script timed out",
        object_name=f"{link.source.name}/2026-11-02T02:40:00Z/" + "ab" * 32,
        checksum="ab" * 32,
        size=10,
    )
    Run.objects.filter(pk=run.pk).update(created_at=NOW)
    messages = []

    def send(_channel, subject, body):
        messages.append((subject, body))

    notify(NOW, send=send)
    notify(NOW, send=send)
    assert len(messages) == 1
    subject, body = messages[0]
    assert subject == f"Kopi backup failed · {link.source.name}"
    assert f"Source: {link.source.name}" in body
    assert f"Target: {link.target.name}" in body
    assert "Time: 2 Nov 02:40" in body
    assert "Reason: post script timed out" in body
    assert f"Object: {run.object_name}" in body
    assert PASSWORD not in body
    assert PASSWORD not in subject
    assert channel.password not in body
    assert "secret.key" not in body
    assert "secret.key" not in subject
    run.refresh_from_db()
    assert run.failure_notified_at == NOW
    event = f"Failure mail sent · {link.source.name} → {link.target.name}"
    assert AuditEvent.objects.filter(event=event).count() == 1

    viewer = _user(f"mail-see-{uuid.uuid4().hex[:8]}", "viewer")
    client = Client(HTTP_HOST="localhost")
    client.force_login(viewer)
    page = client.get(reverse("run", args=[run.pk]))
    assert "Failure mail sent" in page.content.decode()
    Channel.objects.all().delete()


@test("a run that uploaded nothing says so, and the message still has no password")
def _():
    _silence()
    identity = _identity()
    channel = _channel(timezone="UTC", password=encrypt_secret(identity, PASSWORD))
    Channel.objects.filter(pk=channel.pk).update(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    link = _link()
    Run.objects.create(
        link=link,
        status=Run.Status.FAILED,
        finished_at=NOW,
        log="script exited 1",
    )
    messages = []
    notify(NOW, send=lambda _channel, subject, body: messages.append((subject, body)))
    assert len(messages) == 1
    _subject, body = messages[0]
    assert "Nothing was uploaded." in body
    assert "Reason: script exited 1" in body
    assert PASSWORD not in body
    assert "secret.key" not in body
    assert channel.password not in body
    Channel.objects.all().delete()


@test("a send error retries on the next tick")
def _():
    _silence()
    channel = _channel(timezone="UTC")
    Channel.objects.filter(pk=channel.pk).update(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    link = _link()
    run = Run.objects.create(
        link=link,
        status=Run.Status.FAILED,
        finished_at=NOW,
        log="stalled",
    )
    calls = {"n": 0}

    def send(_channel, subject, body):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("smtp down")

    notify(NOW, send=send)
    run.refresh_from_db()
    assert run.failure_notified_at is None
    notify(NOW, send=send)
    run.refresh_from_db()
    assert run.failure_notified_at == NOW
    assert calls["n"] == 2
    Channel.objects.all().delete()


@test("a failure from before the channel existed is skipped")
def _():
    _silence()
    link = _link()
    Run.objects.create(
        link=link,
        status=Run.Status.FAILED,
        finished_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        log="stalled",
    )
    _channel(timezone="UTC")
    messages = []
    notify(NOW, send=lambda *_args: messages.append(_args))
    assert messages == []
    Channel.objects.all().delete()


@test("a succeeded run sends nothing")
def _():
    _silence()
    link = _link()
    Run.objects.create(
        link=link,
        status=Run.Status.SUCCEEDED,
        finished_at=NOW,
        log="ok",
        size=10,
    )
    _channel(timezone="UTC")
    messages = []
    notify(NOW, send=lambda *_args: messages.append(_args))
    assert messages == []
    Channel.objects.all().delete()


@test("delivery uses STARTTLS on 587, implicit TLS on 465, and keeps the password off the message")
def _():
    identity = _identity()
    channel = Channel(
        host="smtp.example",
        port=587,
        from_address="kopi@example.com",
        recipients="ops@example.com",
        username="kopi",
        password=encrypt_secret(identity, PASSWORD),
    )
    with patch("kopi.notify.get_connection") as connect, patch("kopi.notify.EmailMessage") as message:
        for port, use_tls, use_ssl in ((587, True, False), (465, False, True), (25, False, False)):
            channel.port = port
            deliver(channel, "Kopi backup failed", "Body line")
            kwargs = connect.call_args.kwargs
            assert kwargs["use_tls"] is use_tls
            assert kwargs["use_ssl"] is use_ssl
            assert kwargs["password"] == PASSWORD
            assert PASSWORD not in message.call_args.kwargs["body"]
            assert "secret.key" not in message.call_args.kwargs["body"]
            assert message.return_value.send.called


@test("a mail error does not stop claims")
def _():
    _silence()
    due = _link()
    other = _link()
    failed = Run.objects.create(
        link=other,
        status=Run.Status.FAILED,
        finished_at=NOW,
        log="stalled",
    )
    Run.objects.filter(pk=failed.pk).update(created_at=datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc))
    channel = _channel()
    Channel.objects.filter(pk=channel.pk).update(created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    seen = []
    calls = {"n": 0}

    def send(*_args):
        calls["n"] += 1
        raise OSError("smtp down")

    moment = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tick(
            moment,
            child=lambda run: seen.append(run.pk),
            send=send,
            work_dir=Path(tmp),
            slots=32,
        )
    failed.refresh_from_db()
    claimed = Run.objects.get(link=due)
    assert calls["n"] >= 1
    assert failed.failure_notified_at is None
    assert claimed.status == Run.Status.RUNNING
    assert claimed.pk in seen
    Channel.objects.all().delete()


@test("weekly is Monday, and the monthly default is due once across three missed months")
def _():
    zone = "Europe/Copenhagen"
    weekly = next_digest_at("weekly", 8, zone, None, NOW)
    local = weekly.astimezone(ZoneInfo(zone))
    assert local.strftime("%A") == "Monday"
    assert local.day == 2
    assert local.hour == 8
    monthly = next_digest_at("monthly", 8, zone, None, NOW)
    local = monthly.astimezone(ZoneInfo(zone))
    assert local.day == 1
    assert local.hour == 8

    _silence()
    channel = _channel(
        last_digest_at=datetime(2026, 8, 1, 6, 0, tzinfo=timezone.utc),
    )
    messages = []
    notify(NOW, send=lambda _c, subject, body: messages.append(subject))
    notify(NOW, send=lambda _c, subject, body: messages.append(subject))
    assert messages == [DIGEST_SUBJECT]
    channel.refresh_from_db()
    assert channel.last_digest_at == NOW
    Channel.objects.all().delete()


@test("a daily channel is not due again the same day")
def _():
    _silence()
    channel = _channel(digest_kind=Channel.Digest.DAILY, last_digest_at=None)
    messages = []
    notify(NOW, send=lambda *_args: messages.append(_args))
    notify(NOW, send=lambda *_args: messages.append(_args))
    assert len(messages) == 1
    channel.refresh_from_db()
    assert channel.last_digest_at == NOW
    Channel.objects.all().delete()


@test("an empty link table still produces a digest")
def _():
    Run.objects.all().delete()
    Link.objects.all().delete()
    _channel(last_digest_at=None)
    messages = []
    notify(NOW, send=lambda _c, subject, body: messages.append((subject, body)))
    assert len(messages) == 1
    assert messages[0][0] == DIGEST_SUBJECT
    assert messages[0][1] == "There are no links.\n"
    Channel.objects.all().delete()


@test("a quiet installation with only successes still sends the digest")
def _():
    _silence()
    Run.objects.filter(status=Run.Status.FAILED).delete()
    link = _link("billing-db")
    finished = datetime(2026, 11, 3, 1, 14, tzinfo=timezone.utc)
    Run.objects.create(
        link=link,
        status=Run.Status.SUCCEEDED,
        finished_at=finished,
        size=842 * 1024 * 1024,
    )
    _channel(last_digest_at=None)
    messages = []
    notify(NOW, send=lambda _c, subject, body: messages.append((subject, body)))
    assert len(messages) == 1
    body = messages[0][1]
    assert "billing-db →" in body
    assert "Last success: 3 Nov 02:14" in body
    assert "Last failure: —" in body
    assert "Last size: 842 MB" in body
    Channel.objects.all().delete()


@test("a digest send error stays due")
def _():
    _silence()
    channel = _channel(last_digest_at=None)
    calls = {"n": 0}

    def send(*_args):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("smtp down")

    notify(NOW, send=send)
    channel.refresh_from_db()
    assert channel.last_digest_at is None
    notify(NOW, send=send)
    channel.refresh_from_db()
    assert channel.last_digest_at == NOW
    assert calls["n"] == 2
    Channel.objects.all().delete()


@test("the mail screen saves the channel, hides the password, and refuses a viewer POST")
def _():
    Channel.objects.all().delete()
    Run.objects.all().delete()
    Link.objects.all().delete()
    token = uuid.uuid4().hex[:8]
    password = f"mailbox-{token}"
    admin = _user(f"mail-admin-{token}", "admin")
    viewer = _user(f"mail-viewer-{token}", "viewer")
    identity = _identity()
    client = _client()
    anonymous = Client(HTTP_HOST="localhost")
    assert anonymous.get(reverse("mail")).status_code == 302

    client.force_login(admin)
    page = client.get(reverse("mail"))
    assert page.status_code == 200
    body = page.content.decode()
    assert "mail.css" in body
    assert "modal.css" not in body
    assert body.index(">Links<") < body.index(">Mail<") < body.index(">Audit<")
    assert "There are no links." in body
    assert "Nothing sent yet." in body
    csrf = _csrf(body)
    refused_fields = {
        "host": "smtp.fastmail.com",
        "port": "nope",
        "from_address": "kopi@example.com",
        "recipients": "",
        "username": "kopi",
        "password": password,
        "digest": "monthly",
        "hour": "8",
        "timezone": "Not/AZone",
    }
    errors = client.post(reverse("mail"), {"csrfmiddlewaretoken": csrf, **refused_fields})
    error_body = errors.content.decode()
    assert "Enter a port from 1 to 65535." in error_body
    assert "Enter at least one recipient." in error_body
    assert "Unknown timezone." in error_body
    assert password not in error_body
    assert not Channel.objects.exists()
    assert not AuditEvent.objects.filter(user=admin, event="Notification settings changed").exists()

    saved = client.post(
        reverse("mail"),
        {
            "csrfmiddlewaretoken": _csrf(error_body),
            "host": "smtp.fastmail.com",
            "port": "587",
            "from_address": "kopi@example.com",
            "recipients": "ops@example.com, ada@example.com",
            "username": "kopi",
            "password": password,
            "digest": "monthly",
            "hour": "8",
            "timezone": "Europe/Copenhagen",
        },
        follow=True,
    )
    assert saved.status_code == 200
    channel = Channel.objects.get()
    assert channel.host == "smtp.fastmail.com"
    assert channel.port == 587
    assert channel.from_address == "kopi@example.com"
    assert channel.recipients == "ops@example.com, ada@example.com"
    assert channel.username == "kopi"
    assert channel.digest_kind == Channel.Digest.MONTHLY
    assert channel.digest_hour == 8
    assert channel.timezone == "Europe/Copenhagen"
    assert channel.password != password
    assert decrypt_secret(identity, channel.password) == password
    saved_body = saved.content.decode()
    assert password not in saved_body
    assert channel.password not in saved_body
    assert "Set. Not shown." in saved_body
    assert 'id="digest-monthly" value="monthly" checked' in saved_body

    link = _link(f"uploads-{token}")
    Run.objects.create(
        link=link,
        status=Run.Status.SUCCEEDED,
        finished_at=datetime(2026, 11, 3, 1, 14, tzinfo=timezone.utc),
        size=842 * 1024 * 1024,
    )
    listed = client.get(reverse("mail"))
    listed_body = listed.content.decode()
    assert f"uploads-{token}" in listed_body
    assert "3 Nov 02:14" in listed_body
    assert "842 MB" in listed_body
    assert "—" in listed_body

    switched = client.post(
        reverse("mail"),
        {
            "csrfmiddlewaretoken": _csrf(listed_body),
            "host": "smtp.fastmail.com",
            "port": "587",
            "from_address": "kopi@example.com",
            "recipients": "ops@example.com, ada@example.com",
            "username": "kopi",
            "password": "",
            "digest": "daily",
            "hour": "8",
            "timezone": "Europe/Copenhagen",
        },
        follow=True,
    )
    channel.refresh_from_db()
    assert channel.digest_kind == Channel.Digest.DAILY
    assert decrypt_secret(identity, channel.password) == password
    switched_body = switched.content.decode()
    assert 'id="digest-daily" value="daily" checked' in switched_body
    assert password not in switched_body
    assert channel.password not in switched_body
    assert AuditEvent.objects.filter(user=admin, event="Notification settings changed").count() == 2

    Channel.objects.filter(pk=channel.pk).update(
        last_digest_at=datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc)
    )
    channel.refresh_from_db()
    stamped = client.get(reverse("mail"))
    stamped_body = stamped.content.decode()
    assert "Sent 1 Nov 08:00." in stamped_body
    nxt = next_digest_at(
        channel.digest_kind,
        channel.digest_hour,
        channel.timezone,
        channel.last_digest_at,
        dj_tz.now(),
    )
    assert format_when(nxt, channel.timezone) in stamped_body
    runs = client.get(reverse("runs"), follow=True)
    assert "mail.css" not in runs.content.decode()

    viewer_client = _client()
    viewer_client.force_login(viewer)
    seen = viewer_client.get(reverse("mail"))
    seen_body = seen.content.decode()
    assert seen.status_code == 200
    assert "Set. Not shown." in seen_body
    assert "Daily at 08:00" in seen_body
    assert "On. One message per failed run." in seen_body
    assert "smtp.fastmail.com" in seen_body
    assert password not in seen_body
    assert channel.password not in seen_body
    assert 'name="password"' not in seen_body
    assert 'name="host"' not in seen_body
    denied = viewer_client.post(
        reverse("mail"),
        {"csrfmiddlewaretoken": _csrf(seen_body), "host": "evil.example"},
    )
    assert denied.status_code == 403
    assert "CSRF" not in denied.content.decode()
    channel.refresh_from_db()
    assert channel.host == "smtp.fastmail.com"
    Channel.objects.all().delete()
