import tests.bootstrap  # noqa: F401

from django.test import Client
from django.urls import reverse
from ward import test


def _get(path):
    return Client().get(path, HTTP_HOST="localhost")


def _bytes(response):
    return b"".join(response.streaming_content)


@test("the runs URL returns the shell, the Kopi brand, and the Runs heading")
def _():
    response = _get(reverse("runs"))
    assert response.status_code == 200
    body = response.content.decode()
    assert 'id="sidebar"' in body
    assert 'id="brand-name">Kopi' in body
    assert "Runs</h1>" in body
    assert 'class="lede"' in body
    assert 'id="run-rows"' in body
    assert "<td" not in body
    for label in ("Sources", "Targets", "Links", "Mail", "Audit", "Users"):
        assert f">{label}<" not in body


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
