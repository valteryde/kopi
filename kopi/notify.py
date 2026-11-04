"""Failure mail and the status digest.

The worker calls `notify` from a tick. The child does not open an SMTP
connection. A send error leaves the run or the digest due, and the next
tick tries again. The password is decrypted only for the connection.
"""

from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.mail import EmailMessage, get_connection
from django.db import transaction
from django.utils import timezone

from kopi.audit import format_size, note_failure_mail
from kopi.identity import IDENTITY_FILENAME, decrypt_secret, load_identity
from kopi.models import Channel, Link, Run
from kopi.schedule import digest_due, next_digest_at
from kopi.settings import data_directory

Sender = Callable[[Channel, str, str], None]

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

DIGEST_SUBJECT = "Kopi status digest"


def notify(now: datetime, *, send: Sender | None = None) -> None:
    """Send failure mail, then the digest when it is due."""
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be timezone-aware")
    channel = Channel.objects.order_by("pk").first()
    if channel is None:
        return
    sender = send or deliver
    _failure_mail(channel, now, sender)
    _digest(channel, now, sender)


def deliver(channel: Channel, subject: str, body: str) -> None:
    """Open one SMTP connection and send `body`. The password stays out of both."""
    password = _password(channel)
    connection = get_connection(
        backend="django.core.mail.backends.smtp.EmailBackend",
        host=channel.host,
        port=channel.port,
        username=channel.username or None,
        password=password or None,
        use_tls=channel.port == 587,
        use_ssl=channel.port == 465,
        timeout=30,
        fail_silently=False,
    )
    message = EmailMessage(
        subject=subject,
        body=body,
        from_email=channel.from_address,
        to=recipient_list(channel.recipients),
        connection=connection,
    )
    message.send(fail_silently=False)


def failure_message(run: Run, channel: Channel) -> tuple[str, str]:
    source = run.link.source.name
    target = run.link.target.name
    when = run.finished_at or timezone.now()
    lines = [
        f"Source: {source}",
        f"Target: {target}",
        f"Time: {format_when(when, channel.timezone)}",
        f"Reason: {_reason(run)}",
    ]
    if run.object_name:
        lines.append(f"Object: {run.object_name}")
    else:
        lines.append("Nothing was uploaded.")
    return f"Kopi backup failed · {source}", "\n".join(lines) + "\n"


def digest_message(channel: Channel) -> tuple[str, str]:
    rows = link_digests()
    if not rows:
        return DIGEST_SUBJECT, "There are no links.\n"
    blocks = []
    for row in rows:
        success = format_when(row.success_at, channel.timezone) if row.success_at else "—"
        failure = format_when(row.failure_at, channel.timezone) if row.failure_at else "—"
        size = format_size(row.success_size) if row.success_size is not None else "—"
        blocks.append(
            "\n".join(
                [
                    f"{row.source} → {row.target}",
                    f"Last success: {success}",
                    f"Last failure: {failure}",
                    f"Last size: {size}",
                ]
            )
        )
    return DIGEST_SUBJECT, "\n\n".join(blocks) + "\n"


def digest_label(kind: str, hour: int) -> str:
    clock = f"{int(hour):02d}:00"
    if kind == Channel.Digest.DAILY:
        return f"Daily at {clock}"
    if kind == Channel.Digest.WEEKLY:
        return f"Weekly on Monday at {clock}"
    return f"Monthly on the 1st at {clock}"


def digest_summary(channel: Channel, now: datetime) -> str:
    zone = channel.timezone
    if channel.last_digest_at is None:
        sent = "Nothing sent yet."
    else:
        sent = f"Sent {format_when(channel.last_digest_at, zone)}."
    nxt = next_digest_at(
        channel.digest_kind,
        channel.digest_hour,
        zone,
        channel.last_digest_at,
        now,
    )
    if nxt is None:
        return sent
    return f"{sent} Next one {format_when(nxt, zone)}."


def format_when(moment: datetime, zone_name: str) -> str:
    local = moment.astimezone(_zone(zone_name))
    return f"{local.day} {_MONTHS[local.month - 1]} {local.strftime('%H:%M')}"


class LinkDigest:
    def __init__(self, link: Link, success: Run | None, failure: Run | None) -> None:
        self.source = link.source.name
        self.target = link.target.name
        self.kind = link.source.kind
        self.success_at = success.finished_at if success is not None else None
        self.success_size = success.size if success is not None else None
        self.failure_at = failure.finished_at if failure is not None else None


def link_digests() -> list[LinkDigest]:
    found = []
    links = Link.objects.select_related("source", "target").order_by(
        "source__name", "target__name", "pk"
    )
    for link in links:
        success = (
            Run.objects.filter(link=link, status=Run.Status.SUCCEEDED)
            .order_by("-finished_at", "-pk")
            .first()
        )
        failure = (
            Run.objects.filter(link=link, status=Run.Status.FAILED)
            .order_by("-finished_at", "-pk")
            .first()
        )
        found.append(LinkDigest(link, success, failure))
    return found


def recipient_list(stored: str) -> list[str]:
    return [part.strip() for part in stored.split(",") if part.strip()]


def _failure_mail(channel: Channel, now: datetime, send: Sender) -> None:
    runs = list(
        Run.objects.select_related("link__source", "link__target")
        .filter(
            status=Run.Status.FAILED,
            failure_notified_at__isnull=True,
            finished_at__gte=channel.created_at,
        )
        .order_by("finished_at", "pk")
    )
    for run in runs:
        try:
            subject, body = failure_message(run, channel)
            send(channel, subject, body)
        except Exception:
            continue
        with transaction.atomic():
            updated = Run.objects.filter(pk=run.pk, failure_notified_at__isnull=True).update(
                failure_notified_at=now
            )
            if updated:
                note_failure_mail(run.link.source.name, run.link.target.name)


def _digest(channel: Channel, now: datetime, send: Sender) -> None:
    if not digest_due(
        channel.digest_kind,
        channel.digest_hour,
        channel.timezone,
        channel.last_digest_at,
        now,
    ):
        return
    try:
        subject, body = digest_message(channel)
        send(channel, subject, body)
    except Exception:
        return
    Channel.objects.filter(pk=channel.pk).update(last_digest_at=now)


def _reason(run: Run) -> str:
    text = (run.log or "").strip()
    if not text:
        return "failed"
    if len(text) > 8000:
        return text[:8000]
    return text


def _password(channel: Channel) -> str:
    if not channel.password:
        return ""
    identity = load_identity(data_directory() / IDENTITY_FILENAME)
    return decrypt_secret(identity, channel.password)


def _zone(zone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        return ZoneInfo("UTC")
