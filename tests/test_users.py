import re
import uuid

import tests.bootstrap  # noqa: F401

from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse
from ward import test

from kopi.models import AuditEvent


def _client():
    return Client(enforce_csrf_checks=True, HTTP_HOST="localhost")


def _csrf(body: str) -> str:
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', body)
    assert match, body
    return match.group(1)


def _post(client, url, token, data):
    return client.post(url, {"csrfmiddlewaretoken": token, **data}, follow=True)


@test("an admin creates a viewer, and that viewer can open runs and cannot open the new-source form")
def _():
    token = uuid.uuid4().hex[:8]
    login = f"niels-{token}"
    name = f"Niels {token}"
    password = "correct-password"
    admin = User.objects.create_user(f"usr-admin-{token}", password="correct-password")
    admin.groups.add(Group.objects.get(name="admin"))

    anonymous = Client(HTTP_HOST="localhost")
    assert anonymous.get(reverse("users")).status_code == 302

    client = _client()
    client.force_login(admin)
    page = client.get(reverse("users"))
    assert page.status_code == 200
    body = page.content.decode()
    token_csrf = _csrf(body)
    assert "Users</h1>" in body
    assert 'class="navbtn is-on"' in body
    assert body.index(">Audit<") < body.index(">Users<")
    assert ">Mail<" not in body
    assert "New user" in body
    assert "Sources, targets, and links" in body
    assert "Run now, and restore" in body
    assert "Adding users" in body
    assert "Backup history" in body
    assert "Restores" in body
    assert "The audit log" in body
    assert "Secrets stay hidden" in body
    assert "The mail channel" not in body
    assert "Replacing secret.key" not in body
    assert "· you" in body
    assert f">{admin.get_username()}<" in body

    short = _post(
        client,
        reverse("users"),
        token_csrf,
        {"name": name, "login": login, "password": "short", "role": "viewer"},
    )
    assert short.status_code == 200
    short_body = short.content.decode()
    assert "too short" in short_body
    assert f'value="{login}"' in short_body
    assert 'value="viewer" checked' in short_body
    assert "correct-password" not in short_body
    assert not User.objects.filter(username=login).exists()
    assert not AuditEvent.objects.filter(event__contains=login).exists()

    created = _post(
        client,
        reverse("users"),
        _csrf(short_body),
        {"name": name, "login": login, "password": password, "role": "viewer"},
    )
    assert created.status_code == 200
    created_body = created.content.decode()
    assert name in created_body
    assert f'<td class="mono">{login}</td>' in created_body
    assert password not in created_body
    assert 'name="name" value=""' in created_body
    viewer = User.objects.get(username=login)
    assert viewer.check_password(password)
    assert viewer.first_name == name
    assert list(viewer.groups.values_list("name", flat=True)) == ["viewer"]
    event = f"User created · {login} · Viewer"
    assert AuditEvent.objects.filter(user=admin, event=event).count() == 1

    audit = client.get(reverse("audit"))
    assert audit.status_code == 200
    audit_body = audit.content.decode()
    assert event in audit_body
    assert "ph-user-plus" in audit_body

    duplicate = _post(
        client,
        reverse("users"),
        _csrf(created_body),
        {"name": name, "login": login, "password": password, "role": "admin"},
    )
    assert "A user already has this login." in duplicate.content.decode()
    assert User.objects.filter(username=login).count() == 1
    assert AuditEvent.objects.filter(user=admin, event=event).count() == 1
    assert list(viewer.groups.values_list("name", flat=True)) == ["viewer"]

    viewer_client = _client()
    viewer_client.force_login(viewer)
    runs = viewer_client.get(reverse("runs"), follow=True)
    assert runs.status_code == 200
    runs_body = runs.content.decode()
    assert "Runs</h1>" in runs_body
    assert f">{name}<" in runs_body
    assert ">Users<" not in runs_body
    assert ">Mail<" not in runs_body
    refused = viewer_client.get(reverse("source-new"))
    assert refused.status_code == 403
    assert "New source" not in refused.content.decode()
    hidden = viewer_client.get(reverse("users"))
    assert hidden.status_code == 403
    assert User.objects.filter(username=login).count() == 1
