"""Run history, the live card, and restore.

`/runs/` selects the newest run. The inspect column shows the result, size,
checksum, object, and log. A running selection also shows the live card.
`poll.js` is included only then. A finished run that has an object offers
restore to an admin. The dialog posts, and plaintext is written only at the
destination the admin named.
"""

import os
import re
import tempfile
from pathlib import Path

from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from kopi.access import is_admin, require_admin
from kopi.audit import format_size, note_restore
from kopi.child import _storage
from kopi.identity import IdentityError, decrypt_secret
from kopi.rekey import open_identity
from kopi.models import Run, Source
from kopi.pipeline import PipelineError, _write_marker, restore_folder
from kopi.postgres import restore_database
from kopi.settings import data_directory, work_directory
from kopi.storage import StorageError, parse_object_key

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

# The phases a run row stores. `poll.js` reads the keys from the page.
_PHASES = (
    ("pre", "Pre", "terminal-window", "Moved during the pre script"),
    ("archive", "Archive", "archive", "Moved while archiving"),
    ("upload", "Upload", "cloud-arrow-up", "Moved while uploading"),
    ("post", "Post", "file-code", "Moved during the post script"),
    ("retain", "Retain", "hourglass", "Applying the retention rule"),
)

_NOTE = "restore_note"
_ERROR = "restore_error"
_CHECKSUM = re.compile(r"[0-9a-f]{64}")
_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")
_DATABASE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,62}")


class RestoreError(Exception):
    pass


def runs(request):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    first = _runs().first()
    if first is not None:
        return redirect("run", first.pk)
    return _render(request, selected=None)


def run_detail(request, pk):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    get_object_or_404(_runs(), pk=pk)
    return _render(request, selected=pk)


def run_status(request, pk):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    row = get_object_or_404(Run.objects.only("phase", "status", "bytes_moved"), pk=pk)
    response = JsonResponse(
        {"phase": row.phase, "status": row.status, "bytes_moved": row.bytes_moved}
    )
    response["Cache-Control"] = "no-store"
    return response


@require_admin
@require_POST
def restore_run(request, pk):
    run = get_object_or_404(_runs(), pk=pk)
    if not _restorable(run):
        request.session[_ERROR] = "This run has no object to restore."
        return redirect("run", run.pk)
    try:
        destination = _perform(run, request.POST.get("destination", ""))
    except RestoreError as exc:
        request.session[_ERROR] = str(exc)
        return redirect("run", run.pk)
    note_restore(request.user, run.link.source.name, destination)
    request.session[_NOTE] = f"Restored {run.link.source.name} to {destination}."
    return redirect("run", run.pk)


def _render(request, selected):
    admin = is_admin(request.user)
    chosen = None
    rows = []
    for run in _runs():
        if run.pk == selected:
            chosen = run
        rows.append(_table_row(run, selected, admin))
    live = _live(chosen) if chosen is not None and chosen.status == Run.Status.RUNNING else None
    return render(
        request,
        "runs.jinja",
        {
            "rows": rows,
            "detail": _detail(chosen, admin) if chosen is not None else None,
            "live": live,
            "dialog": any(row["restore"] for row in rows),
            "note": request.session.pop(_NOTE, ""),
            "error": request.session.pop(_ERROR, ""),
        },
    )


def _runs():
    return Run.objects.select_related("link__source", "link__target").order_by("-created_at", "-pk")


def _table_row(run: Run, selected, admin: bool) -> dict:
    label, icon, tone = _result(run)
    running = run.status == Run.Status.RUNNING
    return {
        "href": reverse("run", args=[run.pk]),
        "current": run.pk == selected,
        "when": _when(run.started_at or run.created_at),
        "source_icon": _source_icon(run.link.source),
        "source": run.link.source.name,
        "target": run.link.target.name,
        "running": running,
        "megabytes": _megabytes(run.bytes_moved) if running else "",
        "size": "" if running else _size(run),
        "result": label,
        "result_icon": icon,
        "tone": tone,
        "spin": running,
        "restore": _restore_button(run) if admin and _restorable(run) else None,
    }


def _detail(run: Run, admin: bool) -> dict:
    label, icon, tone = _result(run)
    running = run.status == Run.Status.RUNNING
    return {
        "title": run.link.source.name,
        "icon": _source_icon(run.link.source),
        "log": run.log,
        "rows": _detail_rows(run, label, icon, tone, running),
        "restore": _restore_button(run) if admin and _restorable(run) else None,
        "closed": run.status in {Run.Status.PENDING, Run.Status.RUNNING},
        "failed_kept": run.status == Run.Status.FAILED and _restorable(run),
    }


def _live(run: Run) -> dict:
    label, icon, caption = _phase_meta(run.phase)
    return {
        "status_url": reverse("run-status", args=[run.pk]),
        "phase_keys": " ".join(key for key, *_rest in _PHASES),
        "source_icon": _source_icon(run.link.source),
        "source": run.link.source.name,
        "target": run.link.target.name,
        "started_label": "Started" if run.started_at else "Queued",
        "started": _when(run.started_at or run.created_at),
        "timezone": run.link.timezone,
        "megabytes": _megabytes(run.bytes_moved),
        "label": label,
        "caption": caption,
        "caption_icon": icon,
        "phases": [
            {
                "key": key,
                "label": name,
                "icon": phase_icon,
                "caption": phase_caption,
                "state": _phase_state(run.phase, key),
            }
            for key, name, phase_icon, phase_caption in _PHASES
        ],
    }


def _restore_button(run: Run) -> dict:
    source = run.link.source
    kind = "folder" if source.kind == Source.Kind.FOLDER else "database"
    return {
        "action": reverse("run-restore", args=[run.pk]),
        "kind": kind,
        "name": source.name,
        "object": run.object_name,
        "size": _size(run),
        "sum": f"sha256 {run.checksum}",
        "dest": _suggested(source),
    }


def _detail_rows(run: Run, label: str, icon: str, tone: str, running: bool) -> list[dict]:
    rows = [
        _cell("Result", icon, label, tone=tone, spin=running),
        _cell(
            "Size",
            "hard-drive",
            "" if running else _size(run),
            megabytes=_megabytes(run.bytes_moved) if running else "",
        ),
        _marked("Checksum", "fingerprint", run.checksum, prefix="sha256 "),
        _object_cell(run),
    ]
    if run.failure_notified_at is not None:
        rows.append(_cell("Mail", "envelope", "Failure mail sent"))
    return rows


def _object_cell(run: Run) -> dict:
    if run.removed_by_retention:
        return _cell("Object", "file-lock", "The retention rule removed this object.")
    return _marked("Object", "file-lock", run.object_name)


def _restorable(run: Run) -> bool:
    if run.removed_by_retention:
        return False
    if run.status not in {Run.Status.SUCCEEDED, Run.Status.FAILED}:
        return False
    if run.size is None or not run.object_name:
        return False
    return _CHECKSUM.fullmatch(run.checksum or "") is not None


def _suggested(source: Source) -> str:
    if source.kind == Source.Kind.FOLDER and _SEGMENT.fullmatch(source.name):
        return f"/restore/{source.name}"
    if source.kind != Source.Kind.POSTGRES:
        return ""
    name = f"{source.pg_database}_restore"
    if _DATABASE.fullmatch(source.pg_database) and _DATABASE.fullmatch(name):
        return name
    return ""


def _open_storage(target, identity):
    return _storage(target, identity)


def _perform(run: Run, raw: str) -> str:
    source = run.link.source
    destination = _destination(source, raw)
    _require_object(run)
    identity = _load_identity()
    try:
        storage = _open_storage(run.link.target, identity)
    except IdentityError as exc:
        raise RestoreError("Could not read the target credentials.") from exc
    root = work_directory()
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="restore-", dir=root))
    try:
        ciphertext = directory / "payload.age"
        try:
            storage.get(run.object_name, ciphertext)
        except StorageError as exc:
            raise RestoreError("Could not download the object.") from exc
        try:
            # The upload stores the ciphertext and deletes the marker.
            _write_marker(ciphertext, str(run.pk), int(run.size), run.checksum)
            if source.kind == Source.Kind.FOLDER:
                restore_folder(ciphertext, identity, destination)
            elif source.kind == Source.Kind.POSTGRES:
                _restore_database(source, identity, ciphertext, str(destination), directory / "tmp")
            else:
                raise RestoreError("Could not restore this backup.")
        except RestoreError:
            raise
        except (PipelineError, OSError) as exc:
            raise RestoreError("Could not restore this backup.") from exc
    finally:
        _remove_tree(directory)
    return os.fspath(destination)


def _restore_database(source, identity, ciphertext, database: str, tmpdir: Path) -> None:
    try:
        password = decrypt_secret(identity, source.pg_password)
    except IdentityError as exc:
        raise RestoreError("Could not read the database password.") from exc
    tmpdir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(tmpdir, 0o700)
    restore_database(
        ciphertext,
        identity,
        binary=source.pg_dump_binary,
        host=source.pg_host,
        port=source.pg_port,
        user=source.pg_user,
        database=database,
        password=password,
        tmpdir=tmpdir,
    )


def _destination(source: Source, raw: str):
    text = raw.strip()
    if source.kind == Source.Kind.POSTGRES:
        if _DATABASE.fullmatch(text) is None:
            raise RestoreError("Name a database.")
        return text
    if source.kind != Source.Kind.FOLDER:
        raise RestoreError("Could not restore this backup.")
    return _folder_destination(text)


def _folder_destination(text: str) -> Path:
    if not text or any(ord(char) < 32 for char in text):
        raise RestoreError("Name a folder path.")
    path = Path(text)
    if not path.is_absolute() or path.is_symlink():
        raise RestoreError("Name an absolute path." if not path.is_absolute() else "Name a directory.")
    try:
        resolved = path.resolve()
    except OSError as exc:
        raise RestoreError("Name a folder path.") from exc
    if resolved == Path("/") or (resolved.exists() and not resolved.is_dir()):
        raise RestoreError("Name a directory.")
    data = data_directory().resolve()
    work = work_directory().resolve()
    if _within(resolved, data) or _within(resolved, work):
        raise RestoreError("That path is inside the Kopi data directory.")
    return path


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _require_object(run: Run) -> None:
    try:
        parsed = parse_object_key(run.object_name)
    except StorageError as exc:
        raise RestoreError("Could not restore this backup.") from exc
    if parsed.checksum != run.checksum:
        raise RestoreError("Could not restore this backup.")


def _load_identity():
    try:
        return open_identity()
    except IdentityError as exc:
        raise RestoreError("Could not read secret.key.") from exc


def _remove_tree(path: Path) -> None:
    try:
        if path.is_symlink() or not path.is_dir():
            path.unlink(missing_ok=True)
            return
        for child in list(path.iterdir()):
            _remove_tree(child)
        path.rmdir()
    except OSError:
        return


def _cell(key, icon, value, *, tone="", mono=False, spin=False, megabytes=""):
    return {
        "key": key,
        "icon": icon,
        "value": value,
        "tone": tone,
        "mono": mono,
        "spin": spin,
        "megabytes": megabytes,
    }


def _marked(key, icon, value, *, prefix=""):
    if not value:
        return _cell(key, icon, "Not written yet.")
    return _cell(key, icon, f"{prefix}{value}", mono=True)


def _result(run: Run) -> tuple[str, str, str]:
    if run.status == Run.Status.RUNNING:
        label, icon, _caption = _phase_meta(run.phase)
        return label, icon, "run"
    if run.status == Run.Status.SUCCEEDED:
        return "Succeeded", "check", "ok"
    if run.status == Run.Status.FAILED:
        return "Failed", "x", "bad"
    return "Queued", "clock", ""


def _phase_meta(phase: str) -> tuple[str, str, str]:
    for key, label, icon, caption in _PHASES:
        if key == phase:
            return label, icon, caption
    return "Running", "circle-notch", "Moved"


def _phase_state(current: str, key: str) -> str:
    order = [item[0] for item in _PHASES]
    if current not in order or key not in order:
        return ""
    at = order.index(key)
    now = order.index(current)
    if at < now:
        return "done"
    if at == now:
        return "now"
    return ""


def _size(run: Run) -> str:
    if run.size is None:
        return "—"
    return format_size(run.size)


def _megabytes(amount: int) -> str:
    return f"{amount / (1024 * 1024):,.1f}"


def _source_icon(source: Source) -> str:
    if source.kind == Source.Kind.FOLDER:
        return "folder"
    return "database"


def _when(moment) -> str:
    local = timezone.localtime(moment)
    return f"{local.day} {_MONTHS[local.month - 1]} {local.strftime('%H:%M')}"


urlpatterns = [
    path("runs/", runs, name="runs"),
    path("runs/<int:pk>/status/", run_status, name="run-status"),
    path("runs/<int:pk>/restore/", restore_run, name="run-restore"),
    path("runs/<int:pk>/", run_detail, name="run"),
]
