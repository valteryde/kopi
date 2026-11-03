import os
import re
import tempfile
import uuid
from pathlib import Path

import tests.bootstrap  # noqa: F401

from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse
from ward import test

from kopi.child import execute
from kopi.identity import generate_identity
from kopi.models import AuditEvent, Link, Run, Source, Target
from kopi.storage import FakeStorage


def _client():
    return Client(enforce_csrf_checks=True, HTTP_HOST="localhost")


def _csrf(body: str) -> str:
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', body)
    assert match, body
    return match.group(1)


def _login(username: str, password: str) -> Client:
    client = _client()
    page = client.get(reverse("login"))
    response = client.post(
        reverse("login"),
        {
            "csrfmiddlewaretoken": _csrf(page.content.decode()),
            "name": username,
            "password": password,
        },
        follow=True,
    )
    assert response.status_code == 200
    return client


@test("a login and a finished run each add a row, and the page lists them")
def _():
    username = f"audit-{uuid.uuid4().hex[:8]}"
    password = "correct-password"
    user = User.objects.create_user(username, password=password)
    user.groups.add(Group.objects.get(name="viewer"))
    client = _login(username, password)
    assert AuditEvent.objects.filter(user=user, kind="login", event="Login").exists()

    anonymous = Client(HTTP_HOST="localhost")
    assert anonymous.get(reverse("audit")).status_code == 302

    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        source_dir = tmp / "source"
        source_dir.mkdir()
        (source_dir / "hello.txt").write_text("hello")
        token = uuid.uuid4().hex[:8]
        source = Source.objects.create(
            name=f"audit-src-{token}",
            kind=Source.Kind.FOLDER,
            root_path=str(source_dir),
        )
        target = Target.objects.create(name=f"audit-tgt-{token}", bucket="kopi")
        post = tmp / "post.sh"
        post.write_text("#!/bin/bash\necho AUDIT-STDOUT-TOKEN\n")
        os.chmod(post, 0o700)
        link = Link.objects.create(
            source=source,
            target=target,
            cadence="0 3 * * *",
            timezone="Europe/Copenhagen",
            retention_kind=Link.Retention.COUNT,
            retention_count=3,
            post_script_path=str(post),
        )
        run = Run.objects.create(link=link)
        execute(
            run.pk,
            identity=generate_identity(tmp / "secret.key"),
            storage=FakeStorage(),
            work_dir=tmp / "work",
        )
        run = Run.objects.get(pk=run.pk)
        assert run.status == Run.Status.SUCCEEDED
        assert "AUDIT-STDOUT-TOKEN" in run.log

    page = client.get(reverse("audit"))
    assert page.status_code == 200
    body = page.content.decode()
    assert "Audit</h1>" in body
    assert 'id="find-audit"' in body
    assert "filter.js" in body
    assert "Nothing in the log matches." in body
    assert username in body
    assert "Login" in body
    assert "Worker" in body
    assert f"Run started · {source.name} → {target.name}" in body
    assert f"Run finished · {source.name} succeeded" in body
    assert "AUDIT-STDOUT-TOKEN" not in body
    assert 'aria-current="page"' in body
    assert 'href="/audit/"' in body
