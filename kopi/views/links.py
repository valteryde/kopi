"""The link list and the inspect column.

`/links/` selects the first link. An admin gets the form. A viewer gets the
key-value list. Run now inserts one pending row and is refused while that
link is already pending or running. Retention is stored and not applied.
"""

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from kopi.access import is_admin, require_admin
from kopi.audit import note_link_changed, note_link_created, note_link_queued
from kopi.forms.links import LinkForm
from kopi.models import DEFAULT_SCRIPT_TIMEOUT_SECONDS, Link, Run, Source, Target

_BUSY = (Run.Status.PENDING, Run.Status.RUNNING)
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
_ORDER = ("source__name", "target__name", "pk")


def links(request):
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET"])
    first = _links().first()
    if first is not None:
        return redirect("link", first.pk)
    return _render(request, selected=None, link=None, form=None)


def link_detail(request, pk):
    row = get_object_or_404(_links(), pk=pk)
    if request.method == "POST":
        if not is_admin(request.user):
            raise PermissionDenied
        return _save(request, row)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    form = _blank_form(row) if is_admin(request.user) else None
    return _render(request, selected=row.pk, link=row, form=form)


@require_admin
def new_link(request):
    if request.method == "POST":
        return _save(request, None)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    return _render(request, selected=None, link=None, form=_blank_form(None))


@require_admin
@require_POST
def run_link(request, pk):
    row = get_object_or_404(_links(), pk=pk)
    _queue(request, row)
    return redirect("link", row.pk)


def _save(request, link: Link | None):
    form = LinkForm(request.POST)
    if form.is_valid():
        saved = _store(request, link, form)
        if saved is not None:
            return redirect("link", saved.pk)
    return _render(
        request,
        selected=None if link is None else link.pk,
        link=link,
        form=form,
    )


def _store(request, link: Link | None, form: LinkForm):
    creating = link is None
    row = link or Link()
    before = None if creating else _values(row)
    _apply(row, form.cleaned_data)
    changed = before is not None and _values(row) != before
    try:
        with transaction.atomic():
            row.save()
            if creating:
                note_link_created(request.user, row.source.name, row.target.name)
            elif changed:
                note_link_changed(request.user, row.source.name, row.target.name)
    except IntegrityError:
        form.add_error(None, "Could not save this link.")
        return None
    return row


def _apply(link: Link, cleaned: dict) -> None:
    link.source = cleaned["source"]
    link.target = cleaned["target"]
    link.cadence = cleaned["cadence"]
    link.timezone = cleaned["timezone"]
    link.retention_kind = cleaned["retention"]
    if cleaned["retention"] == Link.Retention.COUNT:
        link.retention_count = cleaned["retention_count"]
        link.retention_days = None
    else:
        link.retention_count = None
        link.retention_days = cleaned["retention_days"]
    link.pre_script_path = cleaned["pre_script_path"]
    link.post_script_path = cleaned["post_script_path"]
    link.pre_script_timeout = cleaned["pre_script_timeout"]
    link.post_script_timeout = cleaned["post_script_timeout"]


def _queue(request, link: Link) -> None:
    with transaction.atomic():
        Link.objects.select_for_update().get(pk=link.pk)
        busy = Run.objects.filter(link=link, status__in=_BUSY).exists()
        if busy:
            return
        Run.objects.create(link=link, status=Run.Status.PENDING)
        note_link_queued(request.user, link.source.name, link.target.name)


def _render(request, *, selected, link, form):
    busy = _busy_runs()
    picks = [_pick(row, selected, busy.get(row.pk)) for row in _links()]
    if form is not None:
        mode = "new" if link is None else "edit"
        detail = None
    elif link is not None:
        mode = "detail"
        detail = _detail(link)
    else:
        mode = "empty"
        detail = None
    activity = _activity(busy.get(link.pk)) if link is not None else None
    return render(
        request,
        "links.jinja",
        {
            "picks": picks,
            "can_edit": is_admin(request.user),
            "mode": mode,
            "form": form,
            "link": link,
            "detail": detail,
            "activity": activity,
            "source_choices": list(Source.objects.order_by("name", "pk")) if form is not None else [],
            "target_choices": list(Target.objects.order_by("name", "pk")) if form is not None else [],
        },
    )


def _links():
    return Link.objects.select_related("source", "target").order_by(*_ORDER)


def _busy_runs() -> dict[int, Run]:
    """The run that blocks each link. A running row wins over a pending one."""
    chosen: dict[int, Run] = {}
    for run in Run.objects.filter(status__in=_BUSY).order_by("created_at", "pk"):
        current = chosen.get(run.link_id)
        if current is None or (
            current.status != Run.Status.RUNNING and run.status == Run.Status.RUNNING
        ):
            chosen[run.link_id] = run
    return chosen


def _pick(link: Link, selected, run: Run | None) -> dict:
    icon, label = _retention_label(link)
    return {
        "href": reverse("link", args=[link.pk]),
        "source_icon": _source_icon(link.source),
        "source": link.source.name,
        "target": link.target.name,
        "cadence": link.cadence,
        "retention_icon": icon,
        "retention": label,
        "state": run.status if run is not None else "",
        "current": link.pk == selected,
    }


def _detail(link: Link) -> dict:
    icon, _label = _retention_label(link)
    return {
        "title": link.source.name,
        "source_icon": _source_icon(link.source),
        "source": link.source.name,
        "target": link.target.name,
        "cadence": link.cadence,
        "timezone": link.timezone,
        "retention_icon": icon,
        "retention": _retention_sentence(link),
        "pre": _hook(link.pre_script_path, link.pre_script_timeout, empty=True),
        "pre_mono": bool(link.pre_script_path),
        "post": _hook(link.post_script_path, link.post_script_timeout, empty=False),
        "post_mono": bool(link.post_script_path),
    }


def _activity(run: Run | None) -> dict | None:
    if run is None:
        return None
    if run.status == Run.Status.RUNNING:
        return {"kind": "running"}
    return {"kind": "pending", "when": _when(run.created_at)}


def _blank_form(link: Link | None) -> LinkForm:
    if link is None:
        return LinkForm(
            initial={
                "retention": Link.Retention.COUNT,
                "retention_count": 14,
                "retention_days": 90,
                "pre_script_timeout": DEFAULT_SCRIPT_TIMEOUT_SECONDS,
                "post_script_timeout": DEFAULT_SCRIPT_TIMEOUT_SECONDS,
            }
        )
    return LinkForm(
        initial={
            "source": link.source_id,
            "target": link.target_id,
            "cadence": link.cadence,
            "timezone": link.timezone,
            "retention": link.retention_kind,
            "retention_count": link.retention_count,
            "retention_days": link.retention_days,
            "pre_script_path": link.pre_script_path,
            "pre_script_timeout": link.pre_script_timeout,
            "post_script_path": link.post_script_path,
            "post_script_timeout": link.post_script_timeout,
        }
    )


def _values(link: Link) -> tuple:
    return (
        link.source_id,
        link.target_id,
        link.cadence,
        link.timezone,
        link.retention_kind,
        link.retention_count,
        link.retention_days,
        link.pre_script_path,
        link.post_script_path,
        link.pre_script_timeout,
        link.post_script_timeout,
    )


def _source_icon(source: Source) -> str:
    if source.kind == Source.Kind.FOLDER:
        return "folder"
    return "database"


def _retention_label(link: Link) -> tuple[str, str]:
    if link.retention_kind == Link.Retention.AGE:
        unit = "day" if link.retention_days == 1 else "days"
        return "hourglass", f"{link.retention_days} {unit}"
    return "stack", f"Keep {link.retention_count}"


def _retention_sentence(link: Link) -> str:
    if link.retention_kind == Link.Retention.AGE:
        unit = "day" if link.retention_days == 1 else "days"
        return f"Delete objects older than {link.retention_days} {unit}"
    unit = "backup" if link.retention_count == 1 else "backups"
    return f"Keep the last {link.retention_count} successful {unit}"


def _hook(path: str, timeout: int, *, empty: bool) -> str:
    if not path:
        if empty:
            return "Not set. The backup starts immediately."
        return "Not set."
    return f"{path} · {timeout}s"


def _when(moment) -> str:
    local = timezone.localtime(moment)
    return f"{local.day} {_MONTHS[local.month - 1]} {local.strftime('%H:%M')}"


urlpatterns = [
    path("links/", links, name="links"),
    path("links/new/", new_link, name="link-new"),
    path("links/<int:pk>/run/", run_link, name="link-run"),
    path("links/<int:pk>/", link_detail, name="link"),
]
