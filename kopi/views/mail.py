"""The mail screen.

`/mail/` is one channel. An admin gets the form. A viewer gets the
key-value list. Both see the last digest. A blank password keeps the
stored ciphertext.
"""

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponseNotAllowed
from django.shortcuts import redirect, render
from django.urls import path
from django.utils import timezone

from kopi.access import is_admin
from kopi.audit import format_size, note_mail_changed
from kopi.forms.mail import MailForm
from kopi.identity import IdentityError, encrypt_secret
from kopi.models import Channel, Source
from kopi.notify import digest_label, digest_summary, format_when, link_digests
from kopi.rekey import open_identity
from kopi.toast import set_toast


def mail(request):
    channel = Channel.objects.order_by("pk").first()
    if request.method == "POST":
        if not is_admin(request.user):
            raise PermissionDenied
        return _save(request, channel)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    form = MailForm(initial=_initial(channel)) if is_admin(request.user) else None
    return _render(request, channel, form)


def _save(request, channel: Channel | None):
    form = MailForm(request.POST)
    if form.is_valid():
        saved = _store(request, channel, form)
        if saved is not None:
            set_toast(request, "Mail settings saved.", "check")
            return redirect("mail")
    return _render(request, channel, form)


def _store(request, channel: Channel | None, form: MailForm):
    cleaned = form.cleaned_data
    password = channel.password if channel is not None else ""
    if cleaned["password"]:
        encrypted = _ciphertext(cleaned["password"])
        if encrypted is None:
            form.add_error("password", "Could not read secret.key.")
            return None
        password = encrypted
    row = channel or Channel()
    row.host = cleaned["host"]
    row.port = cleaned["port"]
    row.from_address = cleaned["from_address"]
    row.recipients = cleaned["recipients"]
    row.username = cleaned["username"]
    row.password = password
    row.digest_kind = cleaned["digest"]
    row.digest_hour = cleaned["hour"]
    row.timezone = cleaned["timezone"]
    with transaction.atomic():
        row.save()
        note_mail_changed(request.user)
    return row


def _ciphertext(secret: str) -> str | None:
    try:
        identity = open_identity()
        return encrypt_secret(identity, secret)
    except IdentityError:
        return None


def _initial(channel: Channel | None) -> dict:
    if channel is None:
        return {"port": 587, "digest": Channel.Digest.MONTHLY, "hour": 8, "timezone": "UTC"}
    return {
        "host": channel.host,
        "port": channel.port,
        "from_address": channel.from_address,
        "recipients": channel.recipients,
        "username": channel.username,
        "digest": channel.digest_kind,
        "hour": channel.digest_hour,
        "timezone": channel.timezone,
    }


def _render(request, channel: Channel | None, form):
    return render(
        request,
        "mail.jinja",
        {
            "form": form,
            "rows": None if form is not None else _rows(channel),
            "password_set": bool(channel and channel.password),
            "digest": _digest(channel),
        },
    )


def _rows(channel: Channel | None) -> list[dict]:
    if channel is None:
        return [
            _row("globe", "Host", "Not set.", mono=False),
            _row("plugs", "Port", "Not set."),
            _row("at", "From", "Not set."),
            _row("address-book", "Recipients", "Not set."),
            _row("user", "Username", "Not set."),
            _row("lock-key", "Password", "Not set."),
            _row("bell", "Failure mail", "Off."),
            _row("sun", "Digest", "Not set."),
            _row("globe", "Timezone", "Not set."),
        ]
    password = "Set. Not shown." if channel.password else "Not set."
    return [
        _row("globe", "Host", channel.host, mono=True),
        _row("plugs", "Port", str(channel.port)),
        _row("at", "From", channel.from_address),
        _row("address-book", "Recipients", channel.recipients),
        _row("user", "Username", channel.username or "Not set."),
        _row("lock-key", "Password", password, marked=bool(channel.password)),
        _row("bell", "Failure mail", "On. One message per failed run."),
        _row("sun", "Digest", digest_label(channel.digest_kind, channel.digest_hour)),
        _row("globe", "Timezone", channel.timezone),
    ]


def _row(icon: str, key: str, value: str, *, mono: bool = False, marked: bool = False) -> dict:
    return {"icon": icon, "key": key, "value": value, "mono": mono, "marked": marked}


def _digest(channel: Channel | None) -> dict:
    now = timezone.now()
    zone = channel.timezone if channel is not None else "UTC"
    rows = []
    for item in link_digests():
        rows.append(
            {
                "icon": "folder" if item.kind == Source.Kind.FOLDER else "database",
                "source": item.source,
                "target": item.target,
                "success": format_when(item.success_at, zone) if item.success_at else "—",
                "failure": format_when(item.failure_at, zone) if item.failure_at else "—",
                "size": format_size(item.success_size) if item.success_size is not None else "—",
            }
        )
    if channel is None:
        summary = "Nothing sent yet."
    else:
        summary = digest_summary(channel, now)
    return {"summary": summary, "rows": rows}


urlpatterns = [
    path("mail/", mail, name="mail"),
]
