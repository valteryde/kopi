import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import tests.bootstrap  # noqa: F401
from django.db import connection
from ward import test

from kopi.models import Channel, Link, Run, Source, Target
from kopi.schedule import is_due
from kopi.settings import BASE_DIR, work_directory
from kopi.worker import tick

# 13:00 in Copenhagen on 3 Nov 2026 (UTC+1). The day's 03:00 instant is 02:00 UTC.
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
INSTANT = datetime(2026, 11, 3, 2, 0, tzinfo=timezone.utc)


def _reset() -> None:
    Channel.objects.all().delete()
    Run.objects.all().delete()
    Link.objects.all().delete()
    Source.objects.all().delete()
    Target.objects.all().delete()


def _link(*, cadence: str = "0 3 * * *", zone: str = "Europe/Copenhagen") -> Link:
    source = Source.objects.create(
        name=f"src-{uuid.uuid4().hex[:8]}",
        kind=Source.Kind.FOLDER,
        root_path="/data/notes",
    )
    target = Target.objects.create(name=f"tgt-{uuid.uuid4().hex[:8]}", bucket="kopi")
    return Link.objects.create(
        source=source,
        target=target,
        cadence=cadence,
        timezone=zone,
        retention_kind=Link.Retention.COUNT,
        retention_count=7,
    )


def _run_at(
    link: Link,
    when: datetime,
    *,
    status: str = Run.Status.PENDING,
    started_at: datetime | None = None,
) -> Run:
    run = Run.objects.create(link=link, status=status, started_at=started_at)
    Run.objects.filter(pk=run.pk).update(created_at=when)
    run.refresh_from_db()
    return run


def _record(seen: list[int]):
    def child(run: Run) -> None:
        assert not connection.in_atomic_block
        stored = Run.objects.get(pk=run.pk)
        assert stored.status == Run.Status.RUNNING
        seen.append(stored.pk)

    return child


@test("the development work directory is devdata/work")
def _():
    assert work_directory() == BASE_DIR / "devdata" / "work"


@test("a due link is claimed")
def _():
    _reset()
    link = _link()
    seen: list[int] = []
    with tempfile.TemporaryDirectory() as tmp:
        tick(NOW, child=_record(seen), slots=2, work_dir=Path(tmp))
        tick(NOW, child=_record(seen), slots=2, work_dir=Path(tmp))

    runs = list(Run.objects.filter(link=link))
    assert len(runs) == 1
    assert runs[0].status == Run.Status.RUNNING
    assert runs[0].started_at == NOW
    assert seen == [runs[0].pk, runs[0].pk]


@test("a second link waits for a free slot")
def _():
    _reset()
    _link()
    _link()
    seen: list[int] = []
    with tempfile.TemporaryDirectory() as tmp:
        tick(NOW, child=_record(seen), work_dir=Path(tmp))

    oldest = Run.objects.order_by("created_at", "pk").first()
    waiting = Run.objects.exclude(pk=oldest.pk).get()
    assert oldest.status == Run.Status.RUNNING
    assert oldest.started_at == NOW
    assert waiting.status == Run.Status.PENDING
    assert waiting.started_at is None
    assert seen == [oldest.pk]


@test("the same link does not overlap")
def _():
    _reset()
    link = _link()
    started = datetime(2026, 11, 3, 2, 5, tzinfo=timezone.utc)
    running = _run_at(
        link,
        datetime(2026, 11, 2, 2, 0, tzinfo=timezone.utc),
        status=Run.Status.RUNNING,
        started_at=started,
    )
    pending = _run_at(link, datetime(2026, 11, 3, 1, 0, tzinfo=timezone.utc))
    assert is_due(link, NOW) is True

    seen: list[int] = []
    with tempfile.TemporaryDirectory() as tmp:
        tick(NOW, child=_record(seen), slots=2, work_dir=Path(tmp))

    assert Run.objects.filter(link=link).count() == 2
    assert Run.objects.get(pk=running.pk).status == Run.Status.RUNNING
    assert Run.objects.get(pk=running.pk).started_at == started
    assert Run.objects.get(pk=pending.pk).status == Run.Status.PENDING
    assert seen == [running.pk]


@test("a pending row runs off cadence")
def _():
    _reset()
    link = _link()
    _run_at(link, INSTANT, status=Run.Status.SUCCEEDED)
    pending = _run_at(link, datetime(2026, 11, 3, 10, 0, tzinfo=timezone.utc))
    assert is_due(link, NOW) is False

    seen: list[int] = []
    with tempfile.TemporaryDirectory() as tmp:
        tick(NOW, child=_record(seen), work_dir=Path(tmp))

    assert Run.objects.filter(link=link).count() == 2
    claimed = Run.objects.get(pk=pending.pk)
    assert claimed.status == Run.Status.RUNNING
    assert claimed.started_at == NOW
    assert seen == [pending.pk]


@test("the floor refuses a new claim")
def _():
    _reset()
    held = _link()
    due = _link()
    started = datetime(2026, 11, 3, 2, 5, tzinfo=timezone.utc)
    running = _run_at(
        held,
        datetime(2026, 11, 2, 2, 0, tzinfo=timezone.utc),
        status=Run.Status.RUNNING,
        started_at=started,
    )
    seen: list[int] = []
    with tempfile.TemporaryDirectory() as tmp:
        floor = shutil.disk_usage(tmp).free + 1
        work = Path(tmp)
        tick(NOW, child=_record(seen), slots=2, disk_floor=floor, work_dir=work)
        tick(NOW, child=_record(seen), slots=2, disk_floor=floor, work_dir=work)

    waiting = Run.objects.get(link=due)
    assert waiting.status == Run.Status.PENDING
    assert waiting.started_at is None
    assert Run.objects.filter(link=due).count() == 1
    assert Run.objects.get(pk=running.pk).status == Run.Status.RUNNING
    assert seen == [running.pk, running.pk]


@test("a running row is taken before a pending one")
def _():
    _reset()
    held = _link()
    older = _link()
    newer = _link()
    started = datetime(2026, 11, 3, 2, 5, tzinfo=timezone.utc)
    running = _run_at(
        held,
        datetime(2026, 11, 2, 2, 0, tzinfo=timezone.utc),
        status=Run.Status.RUNNING,
        started_at=started,
    )
    first = _run_at(older, datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc))
    second = _run_at(newer, datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc))

    seen: list[int] = []
    with tempfile.TemporaryDirectory() as tmp:
        tick(NOW, child=_record(seen), slots=2, work_dir=Path(tmp))

    assert seen == [running.pk, first.pk]
    assert Run.objects.get(pk=running.pk).started_at == started
    assert Run.objects.get(pk=first.pk).status == Run.Status.RUNNING
    assert Run.objects.get(pk=first.pk).started_at == NOW
    assert Run.objects.get(pk=second.pk).status == Run.Status.PENDING


@test("a retired link is not due and a pending row for it is not claimed")
def _():
    _reset()
    link = _link()
    link.enabled = False
    link.save(update_fields=["enabled"])
    pending = _run_at(link, datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc))
    seen: list[int] = []
    with tempfile.TemporaryDirectory() as tmp:
        tick(NOW, child=_record(seen), work_dir=Path(tmp))
    assert is_due(link, NOW) is False
    assert Run.objects.get(pk=pending.pk).status == Run.Status.PENDING
    assert seen == []
