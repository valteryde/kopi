"""Admin and viewer roles. Group names match the migration that creates them."""

from functools import wraps

from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.urls import reverse

ADMIN = "admin"
VIEWER = "viewer"


def is_admin(user) -> bool:
    return user.is_authenticated and user.groups.filter(name=ADMIN).exists()


def require_admin(view):
    """Sources, targets, links, Run now, and restore accept an admin only."""

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
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    names = set(user.groups.values_list("name", flat=True))
    if ADMIN in names:
        role, icon = "Admin", "shield"
    elif VIEWER in names:
        role, icon = "Viewer", "eye"
    else:
        role, icon = "No role", "user"
    return {
        "who": {
            "name": user.get_username(),
            "role": role,
            "role_icon": icon,
            "logout_url": reverse("logout"),
        }
    }
