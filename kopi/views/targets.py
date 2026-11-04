"""The target list and the inspect column.

`/targets/` selects the first target. An admin gets the form. A viewer gets
the key-value list. Both credentials are age ciphertext and are not rendered.
"""

from pathlib import Path

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse

from kopi.access import is_admin, require_admin
from kopi.audit import note_target_changed, note_target_created
from kopi.forms.targets import TargetForm
from kopi.identity import IDENTITY_FILENAME, IdentityError, encrypt_secret, load_identity
from kopi.models import Target

_HINT = "Object names stay readable. They carry the source, the time, and a checksum."


def targets(request):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    first = Target.objects.order_by("name", "pk").first()
    if first is not None:
        return redirect("target", first.pk)
    return _render(request, selected=None, target=None, form=None)


def target_detail(request, pk):
    row = get_object_or_404(Target, pk=pk)
    if request.method == "POST":
        if not is_admin(request.user):
            raise PermissionDenied
        return _save(request, row)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    form = _blank_form(row) if is_admin(request.user) else None
    return _render(request, selected=row.pk, target=row, form=form)


@require_admin
def new_target(request):
    if request.method == "POST":
        return _save(request, None)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    return _render(request, selected=None, target=None, form=_blank_form(None))


def _save(request, target: Target | None):
    form = TargetForm(request.POST, target=target)
    if form.is_valid():
        saved = _store(request, target, form)
        if saved is not None:
            return redirect("target", saved.pk)
    return _render(
        request,
        selected=None if target is None else target.pk,
        target=target,
        form=form,
    )


def _store(request, target: Target | None, form: TargetForm):
    cleaned = form.cleaned_data
    access = _ciphertext(cleaned["access_key"]) if cleaned["access_key"] else ""
    secret = _ciphertext(cleaned["secret"]) if cleaned["secret"] else ""
    if (cleaned["access_key"] and access is None) or (cleaned["secret"] and secret is None):
        form.add_error(None, "Could not read secret.key.")
        return None
    creating = target is None
    row = target or Target()
    before = None if creating else _values(row)
    _apply(row, cleaned, access, secret)
    changed = before is not None and _values(row) != before
    try:
        with transaction.atomic():
            row.save()
            if creating:
                note_target_created(request.user, row.name)
            elif changed:
                note_target_changed(request.user, row.name)
    except IntegrityError:
        form.add_error("name", "A target already has this name.")
        return None
    return row


def _apply(target: Target, cleaned: dict, access: str, secret: str) -> None:
    target.name = cleaned["name"]
    target.endpoint_url = cleaned["endpoint"]
    target.region = cleaned["region"]
    target.bucket = cleaned["bucket"]
    target.path_style = cleaned["addressing"] == "path"
    if access:
        target.access_key_id = access
    if secret:
        target.secret_access_key = secret


def _ciphertext(secret: str) -> str | None:
    try:
        identity = load_identity(Path(settings.DATA_DIR) / IDENTITY_FILENAME)
        return encrypt_secret(identity, secret)
    except IdentityError:
        return None


def _render(request, *, selected, target, form):
    picks = [_pick(row, selected) for row in Target.objects.order_by("name", "pk")]
    if form is not None:
        mode = "new" if target is None else "edit"
        detail = None
    elif target is not None:
        mode = "detail"
        detail = _detail(target)
    else:
        mode = "empty"
        detail = None
    return render(
        request,
        "targets.jinja",
        {
            "picks": picks,
            "can_edit": is_admin(request.user),
            "mode": mode,
            "form": form,
            "target": target,
            "detail": detail,
            "hint": _HINT,
            "access_set": bool(target and target.access_key_id),
            "secret_set": bool(target and target.secret_access_key),
        },
    )


def _pick(target: Target, selected) -> dict:
    if target.path_style:
        host = _host(target.endpoint_url)
        sub = f"{host} · path-style" if host else "path-style"
        sub_icon = "hard-drives"
    elif target.region:
        sub = f"{target.region} · {target.bucket}"
        sub_icon = "globe"
    else:
        sub = target.bucket
        sub_icon = "globe"
    return {
        "href": reverse("target", args=[target.pk]),
        "icon": "cloud",
        "name": target.name,
        "sub": sub,
        "sub_icon": sub_icon,
        "current": target.pk == selected,
    }


def _detail(target: Target) -> dict:
    access = "Set. Not shown." if target.access_key_id else "Not set."
    secret = "Set. Not shown." if target.secret_access_key else "Not set."
    return {
        "title": target.name,
        "rows": [
            _row("globe", "Endpoint", target.endpoint_url or "Not set.", bool(target.endpoint_url)),
            _row("map-pin", "Region", target.region or "Not set.", False),
            _row("archive", "Bucket", target.bucket, False),
            _row("path", "Addressing", "Path-style" if target.path_style else "Virtual-hosted", False),
            _row("identification-badge", "Access key", access, False, marked=bool(target.access_key_id)),
            _row("lock-key", "Secret", secret, False, marked=bool(target.secret_access_key)),
        ],
    }


def _row(icon: str, key: str, value: str, mono: bool, marked: bool = False) -> dict:
    return {"icon": icon, "key": key, "value": value, "mono": mono, "marked": marked}


def _blank_form(target: Target | None) -> TargetForm:
    if target is None:
        return TargetForm(initial={"addressing": "virtual"})
    return TargetForm(
        initial={
            "name": target.name,
            "endpoint": target.endpoint_url,
            "region": target.region,
            "bucket": target.bucket,
            "addressing": "path" if target.path_style else "virtual",
        },
        target=target,
    )


def _host(endpoint: str) -> str:
    text = endpoint.strip()
    if "://" in text:
        text = text.split("://", 1)[1]
    return text.split("/", 1)[0]


def _values(target: Target) -> tuple:
    return (
        target.name,
        target.endpoint_url,
        target.region,
        target.bucket,
        target.path_style,
        target.access_key_id,
        target.secret_access_key,
    )


urlpatterns = [
    path("targets/", targets, name="targets"),
    path("targets/new/", new_target, name="target-new"),
    path("targets/<int:pk>/", target_detail, name="target"),
]
