import uuid
from datetime import datetime, timedelta, timezone

import tests.bootstrap  # noqa: F401
from ward import test

from kopi.models import Link, Run, Source, Target
from kopi.schedule import is_due

# Europe/Copenhagen is UTC+1 on these November dates. Clocks are UTC so the
# check has to apply the link timezone itself.


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


def _run_at(link: Link, when: datetime) -> Run:
    run = Run.objects.create(link=link, status=Run.Status.PENDING)
    Run.objects.filter(pk=run.pk).update(created_at=when)
    run.refresh_from_db()
    return run


@test("a fixed clock in a non-UTC zone")
def _():
    # 02:30 Copenhagen. Yesterday's 03:00 local (02:00 UTC) is the latest instant.
    covered = _link()
    _run_at(covered, datetime(2026, 11, 2, 2, 0, tzinfo=timezone.utc))
    assert is_due(covered, datetime(2026, 11, 3, 1, 30, tzinfo=timezone.utc)) is False

    # 03:30 Copenhagen is 02:30 UTC. The latest instant is 03:00 local
    # (02:00 UTC), after the run at 04:30 local the day before.
    owed = _link()
    _run_at(owed, datetime(2026, 11, 2, 3, 30, tzinfo=timezone.utc))
    assert is_due(owed, datetime(2026, 11, 3, 2, 30, tzinfo=timezone.utc)) is True


@test("a link that already ran for this instant is not due")
def _():
    link = _link()
    # 03:00:30 in Copenhagen. The instant is 03:00 local, stored as 02:00 UTC.
    instant = datetime(2026, 11, 3, 2, 0, tzinfo=timezone.utc)
    now = datetime(2026, 11, 3, 2, 0, 30, tzinfo=timezone.utc)
    assert is_due(link, now) is True

    run = _run_at(link, instant)
    assert run.started_at is None
    assert is_due(link, now) is False

    early = _link()
    _run_at(early, instant - timedelta(seconds=1))
    assert is_due(early, now) is True

    untouched = _link()
    assert is_due(untouched, now) is True


@test("a run inserted by hand counts")
def _():
    link = _link()
    # 10:00 Copenhagen, pending, never started. That covers 03:00 the same day.
    run = _run_at(link, datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc))
    assert run.status == Run.Status.PENDING
    assert run.started_at is None
    assert is_due(link, datetime(2026, 11, 3, 14, 0, tzinfo=timezone.utc)) is False
    # 03:00 the next day is a later instant than the inserted run.
    assert is_due(link, datetime(2026, 11, 4, 2, 0, tzinfo=timezone.utc)) is True


@test("three missed instants are one due result")
def _():
    link = _link(cadence="0 * * * *")
    # Last run at 01:00 Copenhagen. 02:00, 03:00, and 04:00 have all passed.
    _run_at(link, datetime(2026, 11, 3, 0, 0, tzinfo=timezone.utc))
    now = datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc)
    assert is_due(link, now) is True

    # One run at the latest instant covers the earlier misses too.
    _run_at(link, now)
    assert is_due(link, now) is False


@test("a bad cadence is not due")
def _():
    now = datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc)
    for cadence in ("not a cron", "", "61 3 * * *"):
        assert is_due(_link(cadence=cadence), now) is False


@test("an unknown timezone is not due")
def _():
    now = datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc)
    for zone in ("Not/AZone", "", "Europe/Copenhagn"):
        assert is_due(_link(zone=zone), now) is False
