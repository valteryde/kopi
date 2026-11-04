"""The source list and the inspect column.

`/sources/` selects the first source. An admin gets the form. A viewer gets
the key-value list. The password is age ciphertext and is not rendered.
"""

from pathlib import Path

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse

from kopi.access import is_admin, require_admin
from kopi.audit import note_source_changed, note_source_created
from kopi.forms.sources import FolderSourceForm, PostgresSourceForm
from kopi.identity import IDENTITY_FILENAME, IdentityError, encrypt_secret, load_identity
from kopi.models import Source
from kopi.postgres import find_dump_binaries


def sources(request):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    first = Source.objects.order_by("name", "pk").first()
    if first is not None:
        return redirect("source", first.pk)
    return _render(request, selected=None, source=None, kind="", form=None, binaries=[])


def source_detail(request, pk):
    row = get_object_or_404(Source, pk=pk)
    if request.method == "POST":
        if not is_admin(request.user):
            raise PermissionDenied
        return _save(request, row)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    binaries = _binaries(row)
    form = _blank_form(row, binaries=binaries) if is_admin(request.user) else None
    return _render(request, selected=row.pk, source=row, kind=row.kind, form=form, binaries=binaries)


@require_admin
def new_source(request):
    if request.method == "POST":
        return _save(request, None)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    kind = _kind(request.GET.get("kind"))
    binaries = _binaries(None)
    return _render(
        request,
        selected=None,
        source=None,
        kind=kind,
        form=_blank_form(None, kind, binaries),
        binaries=binaries,
    )


def _save(request, source: Source | None):
    kind = source.kind if source is not None else _kind(request.POST.get("kind"))
    binaries = _binaries(source)
    form = _bound_form(kind, request.POST, source, binaries)
    if form.is_valid():
        saved = _store(request, source, kind, form)
        if saved is not None:
            return redirect("source", saved.pk)
    return _render(
        request,
        selected=None if source is None else source.pk,
        source=source,
        kind=kind,
        form=form,
        binaries=binaries,
    )


def _store(request, source: Source | None, kind: str, form):
    cleaned = form.cleaned_data
    password = ""
    if kind == Source.Kind.POSTGRES and cleaned["pg_password"]:
        password = _ciphertext(cleaned["pg_password"])
        if password is None:
            form.add_error(None, "Could not read secret.key.")
            return None
    creating = source is None
    row = source or Source()
    before = None if creating else _values(row)
    _apply(row, kind, cleaned, password)
    changed = before is not None and _values(row) != before
    try:
        with transaction.atomic():
            row.save()
            if creating:
                note_source_created(request.user, row.name)
            elif changed:
                note_source_changed(request.user, row.name)
    except IntegrityError:
        form.add_error("name", "A source already has this name.")
        return None
    return row


def _apply(source: Source, kind: str, cleaned: dict, password: str) -> None:
    source.name = cleaned["name"]
    source.kind = kind
    if kind == Source.Kind.FOLDER:
        source.root_path = cleaned["root_path"]
        source.include_rules = cleaned["include_rules"]
        source.exclude_rules = cleaned["exclude_rules"]
        source.pg_host = ""
        source.pg_port = 5432
        source.pg_database = ""
        source.pg_user = ""
        source.pg_password = ""
        source.pg_dump_binary = ""
        return
    source.root_path = ""
    source.include_rules = ""
    source.exclude_rules = ""
    source.pg_host = cleaned["pg_host"]
    source.pg_port = cleaned["pg_port"]
    source.pg_database = cleaned["pg_database"]
    source.pg_user = cleaned["pg_user"]
    source.pg_dump_binary = cleaned["pg_dump_binary"]
    if password:
        source.pg_password = password


def _ciphertext(secret: str) -> str | None:
    try:
        identity = load_identity(Path(settings.DATA_DIR) / IDENTITY_FILENAME)
    except IdentityError:
        return None
    try:
        return encrypt_secret(identity, secret)
    except IdentityError:
        return None


def _render(request, *, selected, source, kind, form, binaries):
    labels = dict(binaries)
    picks = [_pick(row, selected, labels) for row in Source.objects.order_by("name", "pk")]
    mode = "empty"
    detail = None
    if form is not None:
        mode = "new" if source is None else "edit"
    elif source is not None:
        mode = "detail"
        detail = _detail(source, labels)
    return render(
        request,
        "sources.jinja",
        {
            "picks": picks,
            "can_edit": is_admin(request.user),
            "mode": mode,
            "kind": kind,
            "form": form,
            "source": source,
            "detail": detail,
            "binaries": binaries,
            "password_set": bool(source and source.pg_password),
        },
    )


def _pick(source: Source, selected, labels: dict) -> dict:
    if source.kind == Source.Kind.FOLDER:
        icon, sub_icon, sub = "folder", "folder-open", source.root_path
    else:
        icon, sub_icon = "database", "hard-drives"
        version = _version(labels.get(source.pg_dump_binary, ""))
        sub = f"Postgres {version} · {source.pg_host}" if version else f"Postgres · {source.pg_host}"
    return {
        "href": reverse("source", args=[source.pk]),
        "icon": icon,
        "name": source.name,
        "sub": sub,
        "sub_icon": sub_icon,
        "current": source.pk == selected,
    }


def _detail(source: Source, labels: dict) -> dict:
    if source.kind == Source.Kind.FOLDER:
        return {
            "icon": "folder",
            "title": source.name,
            "rows": [
                _row("folder", "Kind", "Folder", False),
                _row("folder-open", "Root", source.root_path, True),
                _row(
                    "plus",
                    "Include",
                    source.include_rules or "Everything under the root",
                    bool(source.include_rules),
                ),
                _row("minus", "Exclude", source.exclude_rules or "None", bool(source.exclude_rules)),
            ],
            "hint_icon": "key",
            "hint": "secret.key is skipped if this tree would include it. Symlinks are stored as symlinks.",
        }
    label = labels.get(source.pg_dump_binary, source.pg_dump_binary)
    password = "Set. Not shown." if source.pg_password else "Not set."
    return {
        "icon": "database",
        "title": source.name,
        "rows": [
            _row("database", "Kind", "Postgres", False),
            _row("globe", "Host", source.pg_host, True),
            _row("plugs", "Port", str(source.pg_port), False),
            _row("table", "Database", source.pg_database, False),
            _row("user", "User", source.pg_user, False),
            _row("lock-key", "Password", password, False, marked=bool(source.pg_password)),
            _row("terminal-window", "Dump binary", label, True),
        ],
        "hint_icon": "info",
        "hint": "The backup is a logical dump, one database.",
    }


def _row(icon: str, key: str, value: str, mono: bool, marked: bool = False) -> dict:
    return {"icon": icon, "key": key, "value": value, "mono": mono, "marked": marked}


def _version(label: str) -> str:
    prefix = "pg_dump "
    if label.startswith(prefix):
        return label.removeprefix(prefix)
    return ""


def _blank_form(source: Source | None, kind: str = "", binaries=None):
    kind = source.kind if source is not None else kind
    binaries = list(binaries or [])
    if kind == Source.Kind.FOLDER:
        initial = {}
        if source is not None:
            initial = {
                "name": source.name,
                "root_path": source.root_path,
                "include_rules": source.include_rules,
                "exclude_rules": source.exclude_rules,
            }
        return FolderSourceForm(initial=initial, source=source)
    initial = {"pg_port": 5432}
    if binaries:
        initial["pg_dump_binary"] = binaries[0][0]
    if source is not None:
        initial = {
            "name": source.name,
            "pg_host": source.pg_host,
            "pg_port": source.pg_port,
            "pg_database": source.pg_database,
            "pg_user": source.pg_user,
            "pg_dump_binary": source.pg_dump_binary,
        }
    return PostgresSourceForm(initial=initial, source=source, binaries=binaries)


def _bound_form(kind: str, data, source: Source | None, binaries):
    if kind == Source.Kind.FOLDER:
        return FolderSourceForm(data, source=source)
    return PostgresSourceForm(data, source=source, binaries=binaries)


def _binaries(source: Source | None):
    found = find_dump_binaries()
    paths = {path for path, _label in found}
    if source is not None and source.pg_dump_binary and source.pg_dump_binary not in paths:
        return [(source.pg_dump_binary, source.pg_dump_binary), *found]
    return found


def _kind(value: str | None) -> str:
    if value == Source.Kind.FOLDER:
        return Source.Kind.FOLDER
    return Source.Kind.POSTGRES


def _values(source: Source) -> tuple:
    return (
        source.name,
        source.kind,
        source.root_path,
        source.include_rules,
        source.exclude_rules,
        source.pg_host,
        source.pg_port,
        source.pg_database,
        source.pg_user,
        source.pg_password,
        source.pg_dump_binary,
    )


urlpatterns = [
    path("sources/", sources, name="sources"),
    path("sources/new/", new_source, name="source-new"),
    path("sources/<int:pk>/", source_detail, name="source"),
]
