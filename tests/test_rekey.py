import re
import uuid
from pathlib import Path

import tests.bootstrap  # noqa: F401

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse
from ward import raises, test

from kopi.identity import (
    IDENTITY_FILENAME,
    IdentityError,
    decrypt_secret,
    encrypt_secret,
    generate_identity,
    load_identity,
)
from kopi.models import AuditEvent, Channel, Source, Target
from kopi.rekey import (
    PENDING_FILENAME,
    PREVIOUS_FILENAME,
    _rewrite,
    recover,
    replace_identity,
)


def _client():
    return Client(enforce_csrf_checks=True, HTTP_HOST="localhost")


def _csrf(body: str) -> str:
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', body)
    assert match, body
    return match.group(1)


def _identity():
    path = Path(settings.DATA_DIR) / IDENTITY_FILENAME
    if path.exists() and not path.is_symlink():
        return load_identity(path)
    return generate_identity(path)


@test("replacing the key re-encrypts stored passwords and offers the new file once")
def _():
    token = uuid.uuid4().hex[:8]
    old = _identity()
    password = f"pw-{token}"
    secret = f"secret-{token}"
    source = Source.objects.create(
        name=f"rekey-src-{token}",
        kind=Source.Kind.POSTGRES,
        pg_host="db.internal",
        pg_database="billing",
        pg_user="kopi",
        pg_password=encrypt_secret(old, password),
    )
    target = Target.objects.create(
        name=f"rekey-tgt-{token}",
        bucket="kopi",
        access_key_id=encrypt_secret(old, f"AKIA{token}"),
        secret_access_key=encrypt_secret(old, secret),
    )
    channel = Channel.objects.create(
        host="smtp.internal",
        from_address="kopi@internal",
        recipients="ops@internal",
        password=encrypt_secret(old, f"mail-{token}"),
    )
    admin = User.objects.create_user(f"rekey-admin-{token}", password="correct-password")
    admin.groups.add(Group.objects.get(name="admin"))
    viewer = User.objects.create_user(f"rekey-viewer-{token}", password="correct-password")
    viewer.groups.add(Group.objects.get(name="viewer"))

    client = _client()
    client.force_login(admin)
    page = client.get(reverse("users"))
    body = page.content.decode()
    assert "I have a copy of the current key" in body
    refused = client.post(
        reverse("user-key"),
        {"csrfmiddlewaretoken": _csrf(body)},
    )
    assert "Keep a copy of the current key first." in refused.content.decode()
    assert load_identity(old.path).recipient == old.recipient

    viewer_client = _client()
    viewer_client.force_login(viewer)
    readable = viewer_client.get(reverse("runs"), follow=True)
    hidden = viewer_client.post(
        reverse("user-key"),
        {"csrfmiddlewaretoken": _csrf(readable.content.decode()), "kept": "yes"},
    )
    assert hidden.status_code == 403

    replaced = client.post(
        reverse("user-key"),
        {"csrfmiddlewaretoken": _csrf(body), "kept": "yes"},
        follow=True,
    )
    assert replaced.status_code == 200
    offer = replaced.content.decode()
    assert "Download secret.key" in offer
    assert "AGE-SECRET-KEY-" not in offer
    downloaded = client.post(
        reverse("secret-key"),
        {"csrfmiddlewaretoken": _csrf(offer)},
    )
    payload = b"".join(downloaded.streaming_content)
    new = load_identity(old.path)
    assert payload == new.path.read_bytes()
    assert new.recipient != old.recipient
    assert not (Path(settings.DATA_DIR) / PREVIOUS_FILENAME).exists()
    assert not (Path(settings.DATA_DIR) / PENDING_FILENAME).exists()

    source.refresh_from_db()
    target.refresh_from_db()
    channel.refresh_from_db()
    assert decrypt_secret(new, source.pg_password) == password
    assert decrypt_secret(new, target.secret_access_key) == secret
    assert decrypt_secret(new, target.access_key_id) == f"AKIA{token}"
    assert decrypt_secret(new, channel.password) == f"mail-{token}"
    event = AuditEvent.objects.filter(user=admin, kind="key_replaced").latest("pk")
    assert "previous key" in event.event
    again = client.post(
        reverse("secret-key"),
        {"csrfmiddlewaretoken": _csrf(offer)},
    )
    assert again.status_code == 404


@test("an interrupted replacement finishes on the next open")
def _():
    token = uuid.uuid4().hex[:8]
    root = Path(settings.DATA_DIR)
    old = _identity()
    password = f"crash-{token}"
    source = Source.objects.create(
        name=f"rekey-crash-{token}",
        kind=Source.Kind.POSTGRES,
        pg_host="db.internal",
        pg_database="billing",
        pg_user="kopi",
        pg_password=encrypt_secret(old, password),
    )
    previous = root / PREVIOUS_FILENAME
    pending = root / PENDING_FILENAME
    previous.write_bytes(old.path.read_bytes())
    previous.chmod(0o600)
    new = generate_identity(pending)
    _rewrite(old, new)
    source.refresh_from_db()
    with raises(IdentityError):
        decrypt_secret(load_identity(old.path), source.pg_password)
    recover(root)
    installed = load_identity(old.path)
    source.refresh_from_db()
    assert installed.recipient == new.recipient
    assert decrypt_secret(installed, source.pg_password) == password
    assert not previous.exists()
    assert not pending.exists()
