"""Apply a link's retention rule to objects already in the bucket.

The child calls this after the post script, while the run is still
running. The link row is read here. A run with no object does not list
or delete. Keys that do not parse are left alone.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from kopi.models import Link, Run
from kopi.storage import StorageError, parse_object_key


@dataclass(frozen=True)
class _Object:
    key: str
    timestamp: datetime


def apply_retention(run_id: int, storage, *, now: datetime | None = None) -> None:
    """Delete objects the link's current rule no longer keeps.

    Keep-last-N keeps the newest N names `list` returns. The timestamp in
    the name orders them, and the key breaks a tie. Delete-older-than
    removes a name whose timestamp is earlier than now minus the link's
    days. Each deleted key marks the run that stored it.
    """
    run = Run.objects.select_related("link__source").get(pk=run_id)
    if not run.object_name:
        return
    moment = timezone.now() if now is None else now
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    link = run.link
    prefix = f"{link.source.name}/"
    ranked = _ranked(storage.list(prefix))
    for item in _doomed(link, ranked, moment):
        storage.delete(item.key)
        Run.objects.filter(object_name=item.key).update(removed_by_retention=True)


def _ranked(items) -> list[_Object]:
    ranked = []
    for item in items:
        try:
            parsed = parse_object_key(item.key)
        except StorageError:
            continue
        ranked.append(_Object(key=item.key, timestamp=parsed.timestamp))
    ranked.sort(key=lambda row: (row.timestamp, row.key))
    return ranked


def _doomed(link: Link, ranked: list[_Object], now: datetime) -> list[_Object]:
    if link.retention_kind == Link.Retention.AGE:
        if not link.retention_days:
            return []
        cutoff = now - timedelta(days=link.retention_days)
        return [item for item in ranked if item.timestamp < cutoff]
    if link.retention_kind != Link.Retention.COUNT or not link.retention_count:
        return []
    return ranked[: -link.retention_count]
