import os
import re
import stat
import tempfile
from pathlib import Path

import tests.bootstrap  # noqa: F401

from django.conf import settings
from django.contrib.auth import SESSION_KEY
from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse
from ward import raises, test

from kopi.accounts import LOGIN_FAILURE_LIMIT
from kopi.identity import IDENTITY_FILENAME
from kopi.models import LoginFailure, Source
from kopi.sessionkey import FILENAME as SESSION_FILENAME
from kopi.sessionkey import SessionKeyError, load_session_key


def _client():
    return Client(enforce_csrf_checks=True, HTTP_HOST="localhost")


def _csrf(body: str) -> str:
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', body)
    assert match, body
    return match.group(1)


def _post(client, path, data, *, follow=False):
    page = client.get(path)
    payload = {"csrfmiddlewaretoken": _csrf(page.content.decode()), **data}
    return client.post(path, payload, follow=follow)


def _reset():
    LoginFailure.objects.all().delete()
    User.objects.all().delete()
    path = Path(settings.DATA_DIR) / IDENTITY_FILENAME
    if path.is_symlink() or path.exists():
        path.unlink()


@test("setup creates the admin and a mode 0600 identity")
def _():
    _reset()
    client = _client()
    response = _post(
        client,
        reverse("login"),
        {"name": "ada", "password": "correct-password"},
        follow=True,
    )
    assert response.status_code == 200
    body = response.content.decode()
    assert "Download secret.key" in body
    assert "AGE-SECRET-KEY-" not in body

    user = User.objects.get(username="ada")
    assert user.check_password("correct-password")
    assert list(user.groups.values_list("name", flat=True)) == ["admin"]
    assert int(client.session[SESSION_KEY]) == user.pk

    identity = Path(settings.DATA_DIR) / IDENTITY_FILENAME
    session_key = Path(settings.DATA_DIR) / SESSION_FILENAME
    assert stat.S_IMODE(identity.stat().st_mode) == 0o600
    assert stat.S_IMODE(session_key.stat().st_mode) == 0o600
    assert settings.SECRET_KEY == session_key.read_text()
    assert session_key.read_text() not in identity.read_text()

    downloaded = client.post(
        reverse("secret-key"),
        {"csrfmiddlewaretoken": _csrf(body)},
    )
    assert downloaded.status_code == 200
    assert downloaded["Cache-Control"] == "no-store"
    payload = b"".join(downloaded.streaming_content)
    assert payload == identity.read_bytes()
    assert b"AGE-SECRET-KEY-" in payload

    runs = client.get(reverse("runs"))
    assert runs.status_code == 200
    runs_body = runs.content.decode()
    assert "ada" in runs_body
    assert "Admin" in runs_body
    again = client.post(
        reverse("secret-key"),
        {"csrfmiddlewaretoken": _csrf(runs_body)},
    )
    assert again.status_code == 404

    logged_out = client.post(
        reverse("logout"),
        {"csrfmiddlewaretoken": _csrf(runs_body)},
    )
    assert logged_out.status_code == 302
    assert logged_out.url == reverse("login")
    assert SESSION_KEY not in client.session


@test("a second setup request is login")
def _():
    _reset()
    first = _client().get(reverse("login"))
    first_body = first.content.decode()
    assert "Create the admin" in first_body
    assert "Sign in" not in first_body
    created = _post(
        _client(),
        reverse("login"),
        {"name": "ada", "password": "correct-password"},
    )
    assert created.status_code == 302
    assert created.url == reverse("login")

    anonymous = _client()
    page = anonymous.get(reverse("login"))
    body = page.content.decode()
    assert page.status_code == 200
    assert "Sign in" in body
    assert "Create the admin" not in body

    signed_in = _post(
        anonymous,
        reverse("login"),
        {"name": "ada", "password": "correct-password"},
        follow=True,
    )
    assert signed_in.status_code == 200
    assert "Runs</h1>" in signed_in.content.decode()


@test("a paused account is refused")
def _():
    _reset()
    User.objects.create_user("ada", password="correct-password")
    client = _client()
    for _attempt in range(LOGIN_FAILURE_LIMIT - 1):
        refused = _post(
            client,
            reverse("login"),
            {"name": "ada", "password": "wrong-password"},
        )
        assert refused.status_code == 200
        assert "wrong" in refused.content.decode()

    signed_in = _post(
        client,
        reverse("login"),
        {"name": "ada", "password": "correct-password"},
        follow=True,
    )
    assert "Runs</h1>" in signed_in.content.decode()
    assert LoginFailure.objects.filter(username="ada").count() == 0
    client.post(
        reverse("logout"),
        {"csrfmiddlewaretoken": _csrf(signed_in.content.decode())},
    )

    for _attempt in range(LOGIN_FAILURE_LIMIT):
        _post(client, reverse("login"), {"name": "ada", "password": "wrong-password"})
    paused = _post(
        client,
        reverse("login"),
        {"name": "ada", "password": "correct-password"},
    )
    assert paused.status_code == 403
    assert "paused" in paused.content.decode()
    assert SESSION_KEY not in client.session
    assert LoginFailure.objects.filter(username="ada").count() == LOGIN_FAILURE_LIMIT


@test("a viewer POST to create a source is refused")
def _():
    _reset()
    viewer = User.objects.create_user("vera", password="viewer-password")
    viewer.groups.add(Group.objects.get(name="viewer"))
    admin = User.objects.create_user("ada", password="correct-password")
    admin.groups.add(Group.objects.get(name="admin"))
    before = Source.objects.count()

    viewer_client = _client()
    viewer_client.force_login(viewer)
    runs = viewer_client.get(reverse("runs"))
    assert runs.status_code == 200
    assert "Runs</h1>" in runs.content.decode()
    refused = viewer_client.post(
        reverse("source-new"),
        {"csrfmiddlewaretoken": _csrf(runs.content.decode()), "name": "docs"},
    )
    assert refused.status_code == 403
    assert "CSRF" not in refused.content.decode()
    assert Source.objects.count() == before

    admin_client = _client()
    admin_client.force_login(admin)
    page = admin_client.get(reverse("runs"))
    accepted = admin_client.post(
        reverse("source-new"),
        {"csrfmiddlewaretoken": _csrf(page.content.decode()), "name": "docs"},
    )
    assert accepted.status_code != 403
    assert Source.objects.count() == before


@test("session.key is created mode 0600 and a looser file is refused")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        previous = os.umask(0)
        try:
            key = load_session_key(directory)
        finally:
            os.umask(previous)
        path = directory / SESSION_FILENAME
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert key == path.read_text()
        assert load_session_key(directory) == key
        os.chmod(path, 0o644)
        with raises(SessionKeyError):
            load_session_key(directory)
