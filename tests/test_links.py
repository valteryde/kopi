import re
import uuid

import tests.bootstrap  # noqa: F401

from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse
from ward import test

from kopi.models import AuditEvent, Link, Run, Source, Target


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


def _pair(token: str) -> tuple[Source, Target]:
    source = Source.objects.create(
        name=f"src-{token}",
        kind=Source.Kind.FOLDER,
        root_path=f"/srv/{token}",
    )
    target = Target.objects.create(name=f"tgt-{token}", bucket=f"b-{token}")
    return source, target


def _fields(source: Source, target: Target, **overrides) -> dict:
    data = {
        "source": str(source.pk),
        "target": str(target.pk),
        "cadence": "0 2 * * *",
        "timezone": "Europe/Copenhagen",
        "retention": "count",
        "retention_count": "14",
        "retention_days": "90",
        "pre_script_path": "",
        "pre_script_timeout": "120",
        "post_script_path": "/hooks/thaw.sh",
        "post_script_timeout": "45",
    }
    data.update(overrides)
    return data


@test("a link row matches the form, and a bad cadence is refused")
def _():
    token = uuid.uuid4().hex[:8]
    source, target = _pair(token)
    admin = _user(f"lnk-admin-{token}", "admin")
    viewer = _user(f"lnk-viewer-{token}", "viewer")
    created_event = f"Link created · {source.name} → {target.name}"
    changed_event = f"Link changed · {source.name} → {target.name}"

    anonymous = Client(HTTP_HOST="localhost")
    assert anonymous.get(reverse("links")).status_code == 302

    client = _client()
    client.force_login(admin)
    new = client.get(reverse("link-new"))
    assert new.status_code == 200
    body = new.content.decode()
    csrf = _csrf(body)
    assert "New link" in body
    assert "Keep the last" in body
    assert "Delete older than" in body
    assert 'id="find-links"' in body
    assert "filter.js" in body
    assert 'value="300"' in body
    assert f'value="{source.pk}"' in body
    assert f">{source.name}<" in body
    assert f">{target.name}<" in body

    fields = _fields(source, target)
    refused = _post(client, reverse("link-new"), csrf, {**fields, "cadence": "not a cron"})
    assert "Enter a valid cron expression." in refused.content.decode()
    assert not Link.objects.filter(source=source).exists()
    assert not AuditEvent.objects.filter(user=admin, event=created_event).exists()

    zone = _post(client, reverse("link-new"), csrf, {**fields, "timezone": "Europe/Copenhagn"})
    assert "Unknown timezone." in zone.content.decode()
    assert not Link.objects.filter(source=source).exists()

    created = _post(
        client,
        reverse("link-new"),
        csrf,
        {**fields, "retention_days": "nope", "pre_script_path": "  "},
    )
    assert created.status_code == 200
    link = Link.objects.get(source=source, target=target)
    assert link.cadence == "0 2 * * *"
    assert link.timezone == "Europe/Copenhagen"
    assert link.retention_kind == Link.Retention.COUNT
    assert link.retention_count == 14
    assert link.retention_days is None
    assert link.pre_script_path == ""
    assert link.pre_script_timeout == 120
    assert link.post_script_path == "/hooks/thaw.sh"
    assert link.post_script_timeout == 45
    assert created.request["PATH_INFO"] == reverse("link", args=[link.pk])
    page = created.content.decode()
    assert "0 2 * * *" in page
    assert "Europe/Copenhagen" in page
    assert "Keep 14" in page
    assert f'class="pick is-on" href="{reverse("link", args=[link.pk])}"' in page
    assert AuditEvent.objects.filter(user=admin, event=created_event).exists()

    missing_days = _post(
        client,
        reverse("link", args=[link.pk]),
        _csrf(page),
        {**fields, "retention": "age", "retention_days": ""},
    )
    assert "Enter how many days to keep." in missing_days.content.decode()
    link.refresh_from_db()
    assert link.retention_kind == Link.Retention.COUNT
    assert link.retention_count == 14
    assert not AuditEvent.objects.filter(user=admin, event=changed_event).exists()

    edited = _post(
        client,
        reverse("link", args=[link.pk]),
        _csrf(missing_days.content.decode()),
        {**fields, "retention": "age", "retention_count": "nope", "retention_days": "90"},
    )
    assert edited.status_code == 200
    link.refresh_from_db()
    assert link.retention_kind == Link.Retention.AGE
    assert link.retention_count is None
    assert link.retention_days == 90
    assert link.cadence == "0 2 * * *"
    edited_body = edited.content.decode()
    assert "90 days" in edited_body
    assert AuditEvent.objects.filter(user=admin, event=changed_event).count() == 1

    same = _post(
        client,
        reverse("link", args=[link.pk]),
        _csrf(edited_body),
        {**fields, "retention": "age", "retention_days": "90"},
    )
    assert same.status_code == 200
    assert AuditEvent.objects.filter(user=admin, event=changed_event).count() == 1

    home = client.get(reverse("links"))
    assert home.status_code == 302
    first = Link.objects.order_by("source__name", "target__name", "pk").first()
    assert home.url == reverse("link", args=[first.pk])

    viewer_client = _client()
    viewer_client.force_login(viewer)
    seen_page = viewer_client.get(reverse("link", args=[link.pk]))
    assert seen_page.status_code == 200
    seen = seen_page.content.decode()
    assert "Delete objects older than 90 days" in seen
    assert "0 2 * * *" in seen
    assert "Europe/Copenhagen" in seen
    assert "/hooks/thaw.sh · 45s" in seen
    assert "Not set. The backup starts immediately." in seen
    assert "The worker applies this rule after each run." in seen
    assert 'name="cadence"' not in seen
    assert 'name="retention"' not in seen
    assert "Run now" not in seen
    assert "New link" not in seen
    assert 'href="/links/"' in seen
    assert 'aria-current="page"' in seen
    refused_new = viewer_client.post(
        reverse("link-new"),
        {"csrfmiddlewaretoken": _csrf(seen), **fields},
    )
    assert refused_new.status_code == 403
    assert "CSRF" not in refused_new.content.decode()
    assert viewer_client.get(reverse("link-new")).status_code == 403
    assert Link.objects.filter(source=source).count() == 1


@test("Run now inserts one pending run")
def _():
    token = uuid.uuid4().hex[:8]
    source, target = _pair(f"run-{token}")
    link = Link.objects.create(
        source=source,
        target=target,
        cadence="0 2 * * *",
        timezone="Europe/Copenhagen",
        retention_kind=Link.Retention.COUNT,
        retention_count=7,
    )
    admin = _user(f"run-admin-{token}", "admin")
    viewer = _user(f"run-viewer-{token}", "viewer")
    queued = f"Run queued · {source.name} → {target.name}"

    viewer_client = _client()
    viewer_client.force_login(viewer)
    viewer_page = viewer_client.get(reverse("link", args=[link.pk]))
    refused = viewer_client.post(
        reverse("link-run", args=[link.pk]),
        {"csrfmiddlewaretoken": _csrf(viewer_page.content.decode())},
    )
    assert refused.status_code == 403
    assert Run.objects.filter(link=link).count() == 0
    assert not AuditEvent.objects.filter(event=queued).exists()

    client = _client()
    client.force_login(admin)
    page = client.get(reverse("link", args=[link.pk]))
    body = page.content.decode()
    assert "Run now" in body
    assert "disabled" not in body

    started = _post(client, reverse("link-run", args=[link.pk]), _csrf(body), {})
    assert started.status_code == 200
    assert Run.objects.filter(link=link, status=Run.Status.PENDING).count() == 1
    assert AuditEvent.objects.filter(user=admin, event=queued).count() == 1
    queued_body = started.content.decode()
    assert "Queued at" in queued_body
    assert "slot is free" in queued_body
    assert "disabled" in queued_body
    assert "Queued" in queued_body

    again = _post(client, reverse("link-run", args=[link.pk]), _csrf(queued_body), {})
    assert again.status_code == 200
    assert Run.objects.filter(link=link).count() == 1
    assert AuditEvent.objects.filter(user=admin, event=queued).count() == 1

    Run.objects.filter(link=link).update(status=Run.Status.RUNNING)
    running = client.get(reverse("link", args=[link.pk]))
    running_body = running.content.decode()
    assert "will not start a second run" in running_body
    assert "Queued at" not in running_body
    assert "disabled" in running_body
    assert "Running" in running_body
    held = _post(client, reverse("link-run", args=[link.pk]), _csrf(running_body), {})
    assert held.status_code == 200
    assert Run.objects.filter(link=link).count() == 1
    assert Run.objects.get(link=link).status == Run.Status.RUNNING
    assert AuditEvent.objects.filter(user=admin, event=queued).count() == 1
