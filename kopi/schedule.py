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


def is_due(link: Link, now: datetime) -> bool:
    """Return whether `link` owes a run for the latest instant at or before `now`.

    `now` is timezone-aware. The caller supplies the clock.
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be timezone-aware")

    instant = _latest_instant(link.cadence, link.timezone, now)
    if instant is None:
        return False
    return not Run.objects.filter(link=link, created_at__gte=instant).exists()


def _latest_instant(cadence: str, zone_name: str, now: datetime) -> datetime | None:
    """The latest cron instant at or before `now`, expressed in UTC."""
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return None

    local_now = now.astimezone(zone)
    try:
        # The previous step sits strictly before an exact match. Step forward
        # once so an instant equal to `local_now` still counts.
        previous = croniter(cadence, local_now).get_prev(datetime)
        following = croniter(cadence, previous).get_next(datetime)
    except (CroniterError, TypeError):
        return None

    chosen = following if following <= local_now else previous
    if chosen > local_now:
        return None
    return chosen.astimezone(timezone.utc)
