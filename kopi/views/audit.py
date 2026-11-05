from django.shortcuts import render
from django.urls import path
from django.utils import timezone

from kopi.audit import ICONS
from kopi.models import AuditEvent
from kopi.schedule import stamp


def audit(request):
    rows = [_row(event) for event in AuditEvent.objects.order_by("-created_at", "-pk")]
    return render(request, "audit.jinja", {"rows": rows})


def _row(event: AuditEvent) -> dict:
    local = timezone.localtime(event.created_at)
    who_icon = "cpu" if event.actor == AuditEvent.Actor.WORKER else "user"
    return {
        "when": stamp(local),
        "who": event.name,
        "who_icon": who_icon,
        "icon": ICONS.get(event.kind, "scroll"),
        "event": event.event,
    }


urlpatterns = [
    path("audit/", audit, name="audit"),
]
