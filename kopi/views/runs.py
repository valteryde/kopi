"""Run history, the live card, and the one-second status poll.

`/runs/` selects the newest run. The inspect column shows the result, size,
checksum, object, and log. A running selection also shows the live card.
`poll.js` is included only then, and it reads that run's status URL.
"""

from django.http import HttpResponseNotAllowed, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone

from kopi.audit import format_size
from kopi.models import Run, Source

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

# The phases a run row stores. Retain arrives with phase 4.
_PHASES = (
    ("pre", "Pre", "terminal-window", "Moved during the pre script"),
    ("archive", "Archive", "archive", "Moved while archiving"),
    ("upload", "Upload", "cloud-arrow-up", "Moved while uploading"),
    ("post", "Post", "file-code", "Moved during the post script"),
)


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


def _render(request, selected):
    chosen = None
    rows = []
    for run in _runs():
        if run.pk == selected:
            chosen = run
        rows.append(_table_row(run, selected))
    live = _live(chosen) if chosen is not None and chosen.status == Run.Status.RUNNING else None
    return render(
        request,
        "runs.jinja",
        {
            "rows": rows,
            "detail": _detail(chosen) if chosen is not None else None,
            "live": live,
        },
    )


def _runs():
    return Run.objects.select_related("link__source", "link__target").order_by("-created_at", "-pk")


def _table_row(run: Run, selected) -> dict:
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
    }


def _detail(run: Run) -> dict:
    label, icon, tone = _result(run)
    running = run.status == Run.Status.RUNNING
    return {
        "title": run.link.source.name,
        "icon": _source_icon(run.link.source),
        "log": run.log,
        "rows": [
            _cell("Result", icon, label, tone=tone, spin=running),
            _cell(
                "Size",
                "hard-drive",
                "" if running else _size(run),
                megabytes=_megabytes(run.bytes_moved) if running else "",
            ),
            _marked("Checksum", "fingerprint", run.checksum, prefix="sha256 "),
            _marked("Object", "file-lock", run.object_name),
        ],
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
    path("runs/<int:pk>/", run_detail, name="run"),
]
