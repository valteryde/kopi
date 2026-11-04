import json
import uuid

import tests.bootstrap  # noqa: F401

from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from ward import test

from kopi.models import Link, Run, Source, Target

_BYTES = 3 * 1024 * 1024 + 512 * 1024
_SIZE = 842 * 1024 * 1024


def _client():
    return Client(HTTP_HOST="localhost")


def _bytes(response):
    return b"".join(response.streaming_content)


def _user(name: str) -> User:
    user = User.objects.create_user(name, password="correct-password")
    user.groups.add(Group.objects.get(name="viewer"))
    return user


def _link(token: str) -> tuple[Source, Target, Link]:
    source = Source.objects.create(
        name=f"uploads-{token}",
        kind=Source.Kind.FOLDER,
        root_path=f"/srv/{token}",
    )
    target = Target.objects.create(name=f"kopi-eu-{token}", bucket=f"b-{token}")
    link = Link.objects.create(
        source=source,
        target=target,
        cadence="0 2 * * *",
        timezone="Europe/Copenhagen",
        retention_kind=Link.Retention.COUNT,
        retention_count=14,
    )
    return source, target, link


@test("the status URL returns the bytes on the row, and the runs page includes the live card for a running run")
def _():
    token = uuid.uuid4().hex[:8]
    source, target, link = _link(token)
    checksum = uuid.uuid4().hex
    object_name = f"{source.name}/2026-11-03T160200Z/{checksum}"
    log = "age writing ciphertext\nmode 0600"
    viewer = _user(f"runs-{token}")

    Run.objects.create(link=link, status=Run.Status.PENDING)
    done = Run.objects.create(
        link=link,
        status=Run.Status.SUCCEEDED,
        size=_SIZE,
        checksum=checksum,
        object_name=object_name,
        log=log,
        bytes_moved=99,
    )
    running = Run.objects.create(
        link=link,
        status=Run.Status.RUNNING,
        phase=Run.Phase.ARCHIVE,
        bytes_moved=_BYTES,
        started_at=timezone.now(),
    )

    anonymous = Client(HTTP_HOST="localhost")
    assert anonymous.get(reverse("run", args=[running.pk])).status_code == 302
    assert anonymous.get(reverse("run-status", args=[running.pk])).status_code == 302

    client = _client()
    client.force_login(viewer)

    home = client.get(reverse("runs"))
    newest = Run.objects.order_by("-created_at", "-pk").first()
    assert newest.pk == running.pk
    assert home.status_code == 302
    assert home.url == reverse("run", args=[running.pk])

    missing = client.get(reverse("run-status", args=[running.pk + 1]))
    assert missing.status_code == 404
    refused = client.post(reverse("run-status", args=[running.pk]))
    assert refused.status_code == 405

    status = client.get(reverse("run-status", args=[running.pk]))
    assert status.status_code == 200
    assert status["Content-Type"].startswith("application/json")
    assert status["Cache-Control"] == "no-store"
    assert json.loads(status.content) == {
        "phase": "archive",
        "status": "running",
        "bytes_moved": _BYTES,
    }
    done_status = client.get(reverse("run-status", args=[done.pk]))
    assert json.loads(done_status.content) == {
        "phase": "",
        "status": "succeeded",
        "bytes_moved": 99,
    }

    page = client.get(reverse("run", args=[running.pk]))
    assert page.status_code == 200
    body = page.content.decode()
    assert 'class="panel live span-2"' in body
    assert reverse("run-status", args=[running.pk]) in body
    assert body.count("poll.js") == 1
    assert "live.css" in body
    assert "filter.js" in body
    assert body.count("data-mb>3.5<") == 3
    assert 'class="phase done" data-phase="pre"' in body
    assert 'class="phase now" data-phase="archive"' in body
    assert 'class="phase" data-phase="upload"' in body
    assert 'class="phase" data-phase="post"' in body
    assert 'class="phase" data-phase="retain"' in body
    assert 'data-caption="Applying the retention rule"' in body
    assert 'data-phases="pre archive upload post retain"' in body
    assert 'data-label="Encrypt"' not in body
    assert "Moved while archiving" in body
    assert f"<strong>{source.name}</strong>" in body
    assert f"<strong>{target.name}</strong>" in body
    assert "Europe/Copenhagen" in body
    assert "Started" in body
    assert "Not written yet." in body
    assert "Queued" in body
    assert "842 MB" in body
    assert f'class="is-on"' in body
    assert 'aria-current="true"' in body
    running_total = Run.objects.filter(status=Run.Status.RUNNING).count()
    badge = f'class="navcount">{running_total}<'
    assert body.index(">Runs<") < body.index(badge) < body.index(">Sources<")
    audit = client.get(reverse("audit"))
    assert badge in audit.content.decode()

    finished = client.get(reverse("run", args=[done.pk]))
    assert finished.status_code == 200
    finished_body = finished.content.decode()
    assert "poll.js" not in finished_body
    assert "data-status-url" not in finished_body
    assert "data-phase=" not in finished_body
    assert "data-mb" not in finished_body
    assert "panel live" not in finished_body
    assert ">Result<" in finished_body
    assert "Succeeded" in finished_body
    assert ">Size<" in finished_body
    assert "842 MB" in finished_body
    assert ">Checksum<" in finished_body
    assert f"sha256 {checksum}" in finished_body
    assert ">Object<" in finished_body
    assert object_name in finished_body
    assert ">Log<" in finished_body
    assert "age writing ciphertext" in finished_body
    assert "mode 0600" in finished_body
    assert "Not written yet." not in finished_body
    assert badge in finished_body
    assert "restore.js" not in finished_body

    script = _bytes(client.get("/static/js/poll.js"))
    assert b"bytes_moved" in script
    assert b"reload" in script
    assert b"data-phase" in script
    assert b'redirect: "manual"' in script
