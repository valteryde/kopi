"""Admin and viewer roles. Group names match the migration that creates them."""

from functools import wraps

from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.urls import reverse

from kopi.models import Run

ADMIN = "admin"
VIEWER = "viewer"


def is_admin(user) -> bool:
    return bool(
        user is not None
        and user.is_authenticated
        and user.groups.filter(name=ADMIN).exists()
    )


def display_name(user) -> str:
    name = user.first_name.strip()
    if name:
        return name
    return user.get_username()


def role_of(user) -> tuple[str, str]:
    names = set(user.groups.values_list("name", flat=True))
    if ADMIN in names:
        return "Admin", "shield-check"
    if VIEWER in names:
        return "Viewer", "eye"
    return "No role", "user"


def require_admin(view):
    """Sources, targets, links, users, Run now, and restore accept an admin only."""

    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not is_admin(request.user):
            raise PermissionDenied
        return view(request, *args, **kwargs)

    return wrapped


def ensure_roles():
    for name in (ADMIN, VIEWER):
        Group.objects.get_or_create(name=name)


def operator(request):
    context = {"nav": _nav(request)}
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return context
    role, icon = role_of(user)
    context["who"] = {
        "name": display_name(user),
        "role": role,
        "role_icon": icon,
        "logout_url": reverse("logout"),
    }
    return context


def _nav(request):
    match = getattr(request, "resolver_match", None)
    current = match.url_name if match else ""
    running = Run.objects.filter(status=Run.Status.RUNNING).count()
    items = [
        ("runs", "clock-counter-clockwise", "Runs", {"runs", "run", "run-restore"}, running),
        ("sources", "database", "Sources", {"sources", "source", "source-new"}, 0),
        ("targets", "cloud", "Targets", {"targets", "target", "target-new"}, 0),
        ("links", "link", "Links", {"links", "link", "link-new", "link-run"}, 0),
        ("mail", "envelope", "Mail", {"mail"}, 0),
        ("audit", "scroll", "Audit", {"audit"}, 0),
    ]
    user = getattr(request, "user", None)
    if is_admin(user):
        items.append(("users", "users", "Users", {"users"}, 0))
    return [
        {
            "href": reverse(name),
            "icon": icon,
            "label": label,
            "current": current in names,
            "count": count,
        }
        for name, icon, label, names, count in items
    ]
