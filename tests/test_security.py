import tests.bootstrap  # noqa: F401

from django.conf import settings
from django.core.exceptions import DisallowedHost
from django.test import Client, override_settings
from ward import test

from kopi.settings import security_settings


@test("the plain-HTTP opt-in turns off secure cookies and the redirect")
def _():
    got = security_settings({"KOPI_ALLOW_HTTP": "1"})
    assert got["DEBUG"] is False
    assert got["SESSION_COOKIE_HTTPONLY"] is True
    assert got["CSRF_COOKIE_HTTPONLY"] is True
    assert got["SESSION_COOKIE_SAMESITE"] == "Lax"
    assert got["CSRF_COOKIE_SAMESITE"] == "Lax"
    assert got["SESSION_COOKIE_SECURE"] is False
    assert got["CSRF_COOKIE_SECURE"] is False
    assert got["SECURE_SSL_REDIRECT"] is False
    assert got["ALLOWED_HOSTS"] == ["localhost", "127.0.0.1"]


@test("without the opt-in, cookies are secure and HTTP redirects to HTTPS")
def _():
    got = security_settings({})
    assert got["DEBUG"] is False
    assert got["SESSION_COOKIE_SECURE"] is True
    assert got["CSRF_COOKIE_SECURE"] is True
    assert got["SECURE_SSL_REDIRECT"] is True
    assert got["SESSION_COOKIE_HTTPONLY"] is True
    assert got["CSRF_COOKIE_SAMESITE"] == "Lax"
    replaced = security_settings({"KOPI_ALLOWED_HOSTS": "kopi.example, backup.internal"})
    assert replaced["ALLOWED_HOSTS"] == ["kopi.example", "backup.internal"]
    assert settings.DEBUG is False


@test("a request whose Host is outside the configured list is refused")
def _():
    client = Client(HTTP_HOST="evil.example")
    try:
        response = client.get("/runs/")
    except DisallowedHost:
        return
    assert response.status_code == 400


@test("the development process stays on HTTP, and the redirect flag still works")
def _():
    plain = Client(HTTP_HOST="localhost").get("/runs/")
    assert plain.status_code == 302
    assert plain["Location"].startswith("/login/")
    with override_settings(SECURE_SSL_REDIRECT=True):
        redirected = Client(HTTP_HOST="localhost").get("/runs/")
    assert redirected.status_code == 301
    assert redirected["Location"].startswith("https://")
