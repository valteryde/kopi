import re
import uuid
from datetime import datetime, timezone

import tests.bootstrap  # noqa: F401

from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse
from ward import test

from kopi.models import AuditEvent, Link, Run, Source, Target
from kopi.schedule import is_due


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


def _source(name: str) -> Source:
    return Source.objects.create(name=name, kind=Source.Kind.FOLDER, root_path=f"/srv/{name}")


def _target(name: str) -> Target:
    return Target.objects.create(name=name, bucket="kopi")


def _link(source: Source, target: Target) -> Link:
    return Link.objects.create(
        source=source,
        target=target,
        cadence="0 3 * * *",
        timezone="Europe/Copenhagen",
        retention_kind=Link.Retention.COUNT,
        retention_count=2,
    )


@test("an admin removes an unused source and target, and a link keeps its history when retired")
def _():
    token = uuid.uuid4().hex[:8]
    admin = _user(f"rm-admin-{token}", "admin")
    viewer = _user(f"rm-viewer-{token}", "viewer")
    source = _source(f"rm-src-{token}")
    target = _target(f"rm-tgt-{token}")
    link = _link(source, target)
    Run.objects.create(link=link, status=Run.Status.SUCCEEDED, size=12, checksum="ab")

    client = _client()
    client.force_login(admin)
    source_page = client.get(reverse("source", args=[source.pk]))
    source_body = source_page.content.decode()
    assert "Remove" in source_body
    blocked = client.post(
        reverse("source-remove", args=[source.pk]),
        {"csrfmiddlewaretoken": _csrf(source_body)},
    )
    assert "Remove the links that use this source first." in blocked.content.decode()
    assert Source.objects.filter(pk=source.pk).exists()

    viewer_client = _client()
    viewer_client.force_login(viewer)
    readable = viewer_client.get(reverse("runs"), follow=True)
    assert (
        viewer_client.post(
            reverse("source-remove", args=[source.pk]),
            {"csrfmiddlewaretoken": _csrf(readable.content.decode())},
        ).status_code
        == 403
    )

    link_page = client.get(reverse("link", args=[link.pk]))
    link_body = link_page.content.decode()
    assert "Retire" in link_body
    assert 'name="action" value="remove"' not in link_body
    retired = client.post(
        reverse("link-remove", args=[link.pk]),
        {"csrfmiddlewaretoken": _csrf(link_body), "action": "retire"},
        follow=True,
    )
    retired_body = retired.content.decode()
    assert "Retired" in retired_body
    assert "Resume" in retired_body
    link.refresh_from_db()
    assert link.enabled is False
    assert Run.objects.filter(link=link).count() == 1
    assert is_due(link, datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)) is False
    assert AuditEvent.objects.filter(event=f"Link retired · {source.name} → {target.name}").exists()

    resumed = client.post(
        reverse("link-remove", args=[link.pk]),
        {"csrfmiddlewaretoken": _csrf(retired_body), "action": "resume"},
        follow=True,
    )
    resumed_body = resumed.content.decode()
    assert "Retire" in resumed_body
    assert "Resume" not in resumed_body
    link.refresh_from_db()
    assert link.enabled is True

    link.enabled = True
    link.save(update_fields=["enabled"])
    Run.objects.filter(link=link).delete()
    empty = client.get(reverse("link", args=[link.pk]))
    removed = client.post(
        reverse("link-remove", args=[link.pk]),
        {"csrfmiddlewaretoken": _csrf(empty.content.decode()), "action": "remove"},
        follow=True,
    )
    assert removed.status_code == 200
    assert not Link.objects.filter(pk=link.pk).exists()

    source_page = client.get(reverse("source", args=[source.pk]))
    client.post(
        reverse("source-remove", args=[source.pk]),
        {"csrfmiddlewaretoken": _csrf(source_page.content.decode())},
        follow=True,
    )
    target_page = client.get(reverse("target", args=[target.pk]))
    client.post(
        reverse("target-remove", args=[target.pk]),
        {"csrfmiddlewaretoken": _csrf(target_page.content.decode())},
        follow=True,
    )
    assert not Source.objects.filter(pk=source.pk).exists()
    assert not Target.objects.filter(pk=target.pk).exists()
    assert AuditEvent.objects.filter(event=f"Source removed · {source.name}").exists()
    assert AuditEvent.objects.filter(event=f"Target removed · {target.name}").exists()


@test("an admin can disable another user, and that session ends")
def _():
    token = uuid.uuid4().hex[:8]
    admin = _user(f"off-admin-{token}", "admin")
    other = _user(f"off-other-{token}", "admin")
    viewer = _user(f"off-viewer-{token}", "viewer")

    client = _client()
    client.force_login(admin)
    page = client.get(reverse("users"))
    body = page.content.decode()
    assert f">{viewer.get_username()}<" in body
    assert "Disable" in body

    others = list(
        User.objects.filter(is_active=True, groups__name="admin").exclude(pk=admin.pk)
    )
    for user in others:
        user.is_active = False
        user.save(update_fields=["is_active"])
    try:
        self_post = client.post(
            reverse("user-active", args=[admin.pk]),
            {"csrfmiddlewaretoken": _csrf(body), "active": "0"},
        )
        assert "The last admin stays active." in self_post.content.decode()
        admin.refresh_from_db()
        assert admin.is_active is True
    finally:
        for user in others:
            user.is_active = True
            user.save(update_fields=["is_active"])

    viewer_client = _client()
    viewer_client.force_login(viewer)
    assert viewer_client.get(reverse("runs"), follow=True).status_code == 200
    client.post(
        reverse("user-active", args=[viewer.pk]),
        {"csrfmiddlewaretoken": _csrf(body), "active": "0"},
    )
    viewer.refresh_from_db()
    assert viewer.is_active is False
    gone = viewer_client.get(reverse("runs"))
    assert gone.status_code == 302
    assert reverse("login") in gone.url
    login = _client()
    form = login.get(reverse("login"))
    refused = login.post(
        reverse("login"),
        {
            "csrfmiddlewaretoken": _csrf(form.content.decode()),
            "name": viewer.get_username(),
            "password": "correct-password",
        },
    )
    assert refused.status_code == 403
    assert "This account is disabled." in refused.content.decode()

    client.post(
        reverse("user-active", args=[viewer.pk]),
        {"csrfmiddlewaretoken": _csrf(client.get(reverse("users")).content.decode()), "active": "1"},
    )
    viewer.refresh_from_db()
    assert viewer.is_active is True
    assert AuditEvent.objects.filter(event=f"User disabled · {viewer.get_username()}").exists()
    assert AuditEvent.objects.filter(event=f"User enabled · {viewer.get_username()}").exists()

    other_client = _client()
    other_client.force_login(other)
    other_page = other_client.get(reverse("users"))
    kept = other_client.post(
        reverse("user-active", args=[other.pk]),
        {"csrfmiddlewaretoken": _csrf(other_page.content.decode()), "active": "0"},
    )
    assert "You cannot disable this account." in kept.content.decode()
    other.refresh_from_db()
    assert other.is_active is True
    client.post(
        reverse("user-active", args=[other.pk]),
        {"csrfmiddlewaretoken": _csrf(client.get(reverse("users")).content.decode()), "active": "0"},
    )
    other.refresh_from_db()
    assert other.is_active is False
