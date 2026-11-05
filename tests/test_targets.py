import re
import uuid
from pathlib import Path

import tests.bootstrap  # noqa: F401
from tests.bootstrap import ensure_identity

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse
from ward import test

from kopi.identity import (
    IDENTITY_FILENAME,
    decrypt_secret,
    generate_identity,
    load_identity,
)
from kopi.models import AuditEvent, Target
from tests.bootstrap import ensure_identity


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


def _post(client, url, token, data):
    return client.post(url, {"csrfmiddlewaretoken": token, **data}, follow=True)


@test("a path-style target round-trips, and the secret is not in the HTML")
def _():
    token = uuid.uuid4().hex[:8]
    name = f"archive-{token}"
    endpoint = f"http://127.0.0.1:39{token[:2]}"
    bucket = f"kopi-{token}"
    access_key = f"GK{token}"
    secret = f"secret-{token}"
    admin = _user(f"tgt-admin-{token}", "admin")
    viewer = _user(f"tgt-viewer-{token}", "viewer")
    key = Path(settings.DATA_DIR) / IDENTITY_FILENAME
    created_key = not key.exists()
    identity = generate_identity(key) if created_key else load_identity(key)
    try:
        anonymous = Client(HTTP_HOST="localhost")
        assert anonymous.get(reverse("targets")).status_code == 302

        client = _client()
        client.force_login(admin)
        new = client.get(reverse("target-new"))
        assert new.status_code == 200
        body = new.content.decode()
        csrf = _csrf(body)
        assert "New target" in body
        assert "Virtual-hosted" in body
        assert "Path-style" in body
        assert 'id="find-targets"' in body
        assert "filter.js" in body

        missing = _post(
            client,
            reverse("target-new"),
            csrf,
            {"name": name, "bucket": bucket, "addressing": "path", "access_key": access_key},
        )
        assert "Secret is required." in missing.content.decode()
        assert secret not in missing.content.decode()
        assert not Target.objects.filter(name=name).exists()
        assert not AuditEvent.objects.filter(user=admin, event=f"Target created · {name}").exists()

        created = _post(
            client,
            reverse("target-new"),
            csrf,
            {
                "name": name,
                "endpoint": endpoint,
                "region": "garage",
                "bucket": bucket,
                "addressing": "path",
                "access_key": access_key,
                "secret": secret,
            },
        )
        assert created.status_code == 200
        target = Target.objects.get(name=name)
        assert target.path_style is True
        assert target.endpoint_url == endpoint
        assert target.region == "garage"
        assert target.bucket == bucket
        assert access_key not in target.access_key_id
        assert secret not in target.secret_access_key
        assert "BEGIN AGE" in target.access_key_id
        assert "BEGIN AGE" in target.secret_access_key
        assert decrypt_secret(identity, target.access_key_id) == access_key
        assert decrypt_secret(identity, target.secret_access_key) == secret
        assert created.request["PATH_INFO"] == reverse("target", args=[target.pk])
        page = created.content.decode()
        assert endpoint in page
        assert "garage" in page
        assert bucket in page
        assert "Path-style" in page
        assert "127.0.0.1" in page
        assert "path-style" in page
        assert secret not in page
        assert access_key not in page
        assert target.secret_access_key not in page
        assert target.access_key_id not in page
        assert "BEGIN AGE" not in page
        assert 'name="secret"' in page
        assert f'class="pick is-on" href="{reverse("target", args=[target.pk])}"' in page
        assert AuditEvent.objects.filter(user=admin, event=f"Target created · {name}").exists()

        kept_access = target.access_key_id
        kept_secret = target.secret_access_key
        edited = _post(
            client,
            reverse("target", args=[target.pk]),
            _csrf(page),
            {
                "name": name,
                "endpoint": endpoint,
                "region": "garage-2",
                "bucket": bucket,
                "addressing": "path",
                "access_key": "",
                "secret": "",
            },
        )
        assert edited.status_code == 200
        target.refresh_from_db()
        assert target.region == "garage-2"
        assert target.access_key_id == kept_access
        assert target.secret_access_key == kept_secret
        edited_body = edited.content.decode()
        assert "garage-2" in edited_body
        assert secret not in edited_body
        assert access_key not in edited_body
        assert AuditEvent.objects.filter(user=admin, event=f"Target changed · {name}").exists()

        duplicate = _post(
            client,
            reverse("target-new"),
            _csrf(edited_body),
            {
                "name": name,
                "bucket": "other",
                "addressing": "virtual",
                "access_key": access_key,
                "secret": secret,
            },
        )
        assert "already has this name" in duplicate.content.decode()
        assert secret not in duplicate.content.decode()
        assert Target.objects.filter(name=name).count() == 1

        home = client.get(reverse("targets"))
        assert home.status_code == 302
        assert home.url == reverse("target", args=[target.pk])

        viewer_client = _client()
        viewer_client.force_login(viewer)
        seen_page = viewer_client.get(reverse("target", args=[target.pk]))
        assert seen_page.status_code == 200
        seen = seen_page.content.decode()
        assert seen.count("Set. Not shown.") == 2
        assert "Path-style" in seen
        assert endpoint in seen
        assert secret not in seen
        assert access_key not in seen
        assert target.secret_access_key not in seen
        assert 'name="secret"' not in seen
        assert 'name="access_key"' not in seen
        assert "New target" not in seen
        assert 'href="/targets/"' in seen
        assert 'aria-current="page"' in seen
        refused = viewer_client.post(
            reverse("target-new"),
            {"csrfmiddlewaretoken": _csrf(seen), "name": name},
        )
        assert refused.status_code == 403
        assert "CSRF" not in refused.content.decode()
        assert viewer_client.get(reverse("target-new")).status_code == 403
        assert Target.objects.get(pk=target.pk).region == "garage-2"
    finally:
        if created_key and key.exists():
            key.unlink()


def _check(client, token: str, data: dict) -> str:
    """The lines the Test button gets back, as text."""
    response = client.post(reverse("target-test"), {"csrfmiddlewaretoken": token, **data})
    assert response.status_code == 200, response.content.decode()
    return response.content.decode()


@test("a target form tests the bucket it names, and saves nothing")
def _():
    token = uuid.uuid4().hex[:8]
    name = f"chk-{token}"
    secret = f"sek-{token}"
    admin = _user(f"chk-admin-{token}", "admin")
    viewer = _user(f"chk-viewer-{token}", "viewer")
    ensure_identity()
    fields = {
        "name": name,
        "endpoint": "http://127.0.0.1:1",
        "region": "garage",
        "bucket": f"b-{token}",
        "addressing": "path",
        "access_key": "GK0123456789abcdef01234567",
        "secret": secret,
    }

    client = _client()
    client.force_login(admin)
    page = client.get(reverse("target-new"))
    body = page.content.decode()
    csrf = _csrf(body)
    assert f'data-check="{reverse("target-test")}"' in body
    assert "data-check-button" in body
    assert "data-check-out" in body
    assert "js/checks.js" in body
    assert "css/checks.css" in body

    assert client.get(reverse("target-test")).status_code == 405

    viewer_client = _client()
    viewer_client.force_login(viewer)
    viewer_body = viewer_client.get(reverse("targets"), follow=True).content.decode()
    refused = viewer_client.post(reverse("target-test"), {"csrfmiddlewaretoken": _csrf(viewer_body), **fields})
    assert refused.status_code == 403

    answered = _check(client, csrf, fields)
    assert "The endpoint did not answer" in answered
    assert AuditEvent.objects.filter(
        user=admin, kind="checked", event=f"Target checked · {name} · not ready"
    ).exists()
    assert "data-bad" in answered
    assert "Fix the lines above before saving." in answered
    assert secret not in answered
    assert not Target.objects.filter(name=name).exists()

    # The form has to be satisfied before anything reaches for the endpoint.
    logged = AuditEvent.objects.filter(kind="checked", event__contains=f"· {name}").count()
    needs = _check(client, csrf, {**fields, "bucket": "", "secret": ""})
    assert "Bucket is required." in needs
    assert "Secret is required." in needs
    assert "The endpoint did not answer" not in needs
    assert AuditEvent.objects.filter(kind="checked", event__contains=f"· {name}").count() == logged

    saved = _post(client, reverse("target-new"), csrf, fields)
    assert saved.status_code == 200
    row = Target.objects.get(name=name)
    assert secret not in row.secret_access_key
    assert decrypt_secret(load_identity(Path(settings.DATA_DIR) / IDENTITY_FILENAME), row.secret_access_key) == secret

    edit_body = client.get(reverse("target", args=[row.pk])).content.decode()
    assert f'<input type="hidden" name="pk" value="{row.pk}">' in edit_body

    # Both boxes are blank, so the check uses the stored ciphertext. Had it
    # found nothing, the line would say so instead of naming the endpoint.
    again = _check(
        client,
        _csrf(edit_body),
        {
            "pk": row.pk,
            "name": name,
            "endpoint": row.endpoint_url,
            "region": row.region,
            "bucket": row.bucket,
            "addressing": "path" if row.path_style else "virtual",
            "access_key": "",
            "secret": "",
        },
    )
    assert "The endpoint did not answer" in again
    assert "no access key and secret" not in again
    assert secret not in again
    assert Target.objects.filter(name=name).count() == 1
