"""Decide whether a link is due.

A cadence is a cron expression evaluated in the link's timezone. Instants are
compared in UTC. The link is due when the latest instant is at or before now
and no run for that link was created at or after that instant. Several missed
instants are one due result. A run counts by `created_at`, including one
inserted by hand. An unknown timezone, or a cadence croniter rejects, is not
due.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import CroniterError, croniter

from kopi.models import Link, Run


_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def zone_of(zone_name: str):
    """The zone by that name, or UTC when the name is not one croniter can step."""
    try:
        return ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return timezone.utc


def stamp(local: datetime) -> str:
    """One local instant the way an operator reads it: `5 Nov 14:08`."""
    return f"{local.day} {_MONTHS[local.month - 1]} {local.strftime('%H:%M')}"


def known_timezone(zone_name: str) -> bool:
    """Whether `zone_name` is a zone the scheduler can evaluate."""
    try:
        ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return False
    return True


def known_cadence(cadence: str, zone_name: str = "UTC") -> bool:
    """Whether croniter can step `cadence` the way `is_due` does."""
    if not known_timezone(zone_name):
        zone_name = "UTC"
    start = datetime(2026, 1, 1, tzinfo=ZoneInfo(zone_name))
    try:
        previous = croniter(cadence, start).get_prev(datetime)
        croniter(cadence, previous).get_next(datetime)
    except (CroniterError, TypeError, ValueError, OverflowError):
        return False
    return True


def is_due(link: Link, now: datetime) -> bool:
    """Return whether `link` owes a run for the latest instant at or before `now`.

    `now` is timezone-aware. The caller supplies the clock.
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be timezone-aware")
    if not link.enabled:
        return False

    instant = _latest_instant(link.cadence, link.timezone, now)
    if instant is None:
        return False
    return not Run.objects.filter(link=link, created_at__gte=instant).exists()


def digest_cron(kind: str, hour: int) -> str:
    """Cron for a digest. Monthly is the 1st, weekly is Monday, at `hour`."""
    clock = int(hour)
    if kind == "daily":
        return f"0 {clock} * * *"
    if kind == "weekly":
        return f"0 {clock} * * 1"
    return f"0 {clock} 1 * *"


def digest_due(kind: str, hour: int, zone_name: str, last_at: datetime | None, now: datetime) -> bool:
    """Whether the digest owes its latest instant. Several missed instants are one."""
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be timezone-aware")
    instant = _latest_instant(digest_cron(kind, hour), zone_name, now)
    if instant is None:
        return False
    return last_at is None or last_at < instant


def next_digest_at(
    kind: str,
    hour: int,
    zone_name: str,
    last_at: datetime | None,
    now: datetime,
) -> datetime | None:
    """When the next digest is due, in UTC. A due digest returns the instant it owes."""
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be timezone-aware")
    cadence = digest_cron(kind, hour)
    instant = _latest_instant(cadence, zone_name, now)
    if instant is None:
        return None
    if last_at is None or last_at < instant:
        return instant
    if not known_timezone(zone_name):
        return None
    local = instant.astimezone(ZoneInfo(zone_name))
    try:
        following = croniter(cadence, local).get_next(datetime)
    except (CroniterError, TypeError, ValueError, OverflowError):
        return None
    return following.astimezone(timezone.utc)


def _latest_instant(cadence: str, zone_name: str, now: datetime) -> datetime | None:
    """The latest cron instant at or before `now`, expressed in UTC."""
    if not known_timezone(zone_name):
        return None

    local_now = now.astimezone(ZoneInfo(zone_name))
    try:
        # The previous step sits strictly before an exact match. Step forward
        # once so an instant equal to `local_now` still counts.
        previous = croniter(cadence, local_now).get_prev(datetime)
        following = croniter(cadence, previous).get_next(datetime)
    except (CroniterError, TypeError, ValueError, OverflowError):
        return None

    chosen = following if following <= local_now else previous
    if chosen > local_now:
        return None
    return chosen.astimezone(timezone.utc)
