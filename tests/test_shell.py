import tests.bootstrap  # noqa: F401

from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse
from ward import test

from kopi.models import Run


def _client():
    return Client(HTTP_HOST="localhost")


def _get(path):
    return _client().get(path)


def _bytes(response):
    return b"".join(response.streaming_content)


@test("the runs URL returns the shell, the Kopi brand, and the Runs heading")
def _():
    Run.objects.all().delete()
    user, _ = User.objects.get_or_create(username="shell")
    group, _ = Group.objects.get_or_create(name="viewer")
    user.groups.add(group)
    client = _client()
    client.force_login(user)
    response = client.get(reverse("runs"))
    assert response.status_code == 200
    body = response.content.decode()
    assert 'id="sidebar"' in body
    assert 'id="brand-name">Kopi' in body
    assert "Runs</h1>" in body
    assert 'class="lede"' in body
    assert 'id="run-rows"' in body
    assert "<td" not in body
    assert "poll.js" not in body
    assert "navcount" not in body
    assert ">shell<" in body
    assert "Viewer" in body
    assert 'id="sign-out"' in body
    assert (
        body.index(">Runs<")
        < body.index(">Sources<")
        < body.index(">Targets<")
        < body.index(">Links<")
        < body.index(">Mail<")
        < body.index(">Audit<")
    )
    assert ">Users<" not in body


@test("whitenoise serves the shell stylesheets, icon, and fonts")
def _():
    for path in (
        "/static/css/tokens.css",
        "/static/css/shell.css",
        "/static/css/controls.css",
        "/static/css/panel.css",
        "/static/css/icons.css",
        "/static/js/filter.js",
        "/static/icon.png",
    ):
        response = _get(path)
        assert response.status_code == 200, path
    tokens = _bytes(_get("/static/css/tokens.css"))
    assert b"--lime" in tokens
    assert b"Instrument Sans" in tokens
    for path in (
        "/static/fonts/instrument-sans-400.woff2",
        "/static/fonts/instrument-sans-700.woff2",
        "/static/fonts/ibm-plex-mono-400.woff2",
        "/static/fonts/ibm-plex-mono-500.woff2",
        "/static/fonts/Phosphor.woff2",
    ):
        response = _get(path)
        assert response.status_code == 200, path
        assert _bytes(response)[:4] == b"wOF2"
