"""The user table, the role cards, and the form that adds an account.

An admin adds a name, a login, a password, and a role, and can disable
another user. Replacing secret.key lives here too. Viewers are refused.
"""

from django.contrib.auth.models import Group, User
from django.contrib.sessions.models import Session
from django.db import IntegrityError, transaction
from django.http import HttpResponseNotAllowed
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path
from django.views.decorators.http import require_POST

from kopi.access import ADMIN, display_name, ensure_roles, require_admin, role_of
from kopi.accounts import OFFER_SECRET_KEY
from kopi.audit import note_key_replaced, note_user_created, note_user_disabled, note_user_enabled
from kopi.forms.users import UserForm
from kopi.identity import IdentityError
from kopi.rekey import RekeyError, replace_identity
from kopi.toast import set_toast


@require_admin
def users(request):
    if request.method == "POST":
        return _create(request)
    if request.method != "GET":
        return HttpResponseNotAllowed(["GET", "POST"])
    return _render(request, UserForm())


def _create(request):
    form = UserForm(request.POST)
    if form.is_valid():
        saved = _store(request, form)
        if saved is not None:
            set_toast(request, f"User {saved.get_username()} added.", "user-plus")
            return redirect("users")
    return _render(request, form)


def _store(request, form: UserForm):
    cleaned = form.cleaned_data
    try:
        with transaction.atomic():
            ensure_roles()
            user = User.objects.create_user(
                username=cleaned["login"],
                password=cleaned["password"],
                first_name=cleaned["name"],
            )
            user.groups.set([Group.objects.get(name=cleaned["role"])])
            label, _icon = role_of(user)
            note_user_created(request.user, user.get_username(), label)
    except IntegrityError:
        form.add_error("login", "A user already has this login.")
        return None
    return user


@require_admin
@require_POST
def set_active(request, pk):
    row = get_object_or_404(User, pk=pk)
    enable = request.POST.get("active") == "1"
    error = _set_active(request, row, enable)
    if error:
        return _render(request, UserForm(), error=error)
    state = "enabled" if enable else "disabled"
    icon = "check" if enable else "prohibit"
    set_toast(request, f"User {row.get_username()} {state}.", icon)
    return redirect("users")


@require_admin
@require_POST
def replace_key(request):
    if request.POST.get("kept") != "yes":
        return _render(request, UserForm(), key_error="Keep a copy of the current key first.")
    try:
        replace_identity()
    except (RekeyError, IdentityError):
        return _render(request, UserForm(), key_error="Could not replace secret.key.")
    note_key_replaced(request.user)
    request.session[OFFER_SECRET_KEY] = True
    return redirect("login")


def _set_active(request, row, enable: bool) -> str:
    if not enable and _last_active_admin(row):
        return "The last admin stays active."
    if not enable and row.pk == request.user.pk:
        return "You cannot disable this account."
    if row.is_active == enable:
        return ""
    row.is_active = enable
    row.save(update_fields=["is_active"])
    if enable:
        note_user_enabled(request.user, row.get_username())
    else:
        _drop_sessions(row.pk)
        note_user_disabled(request.user, row.get_username())
    return ""


def _last_active_admin(row) -> bool:
    if not row.groups.filter(name=ADMIN).exists():
        return False
    others = User.objects.filter(is_active=True, groups__name=ADMIN).exclude(pk=row.pk)
    return not others.exists()


def _drop_sessions(user_id: int) -> None:
    for session in Session.objects.all():
        try:
            data = session.get_decoded()
        except Exception:
            continue
        if str(data.get("_auth_user_id")) == str(user_id):
            session.delete()


def _render(request, form: UserForm, error="", key_error=""):
    current = request.user.pk
    rows = []
    for user in User.objects.all():
        label, icon = role_of(user)
        rows.append(
            {
                "pk": user.pk,
                "name": display_name(user),
                "login": user.get_username(),
                "role": label,
                "role_icon": icon,
                "you": user.pk == current,
                "active": user.is_active,
            }
        )
    rows.sort(key=lambda row: (row["name"].casefold(), row["login"].casefold()))
    return render(
        request,
        "users.jinja",
        {"form": form, "rows": rows, "error": error, "key_error": key_error},
    )


urlpatterns = [
    path("users/", users, name="users"),
    path("users/key/", replace_key, name="user-key"),
    path("users/<int:pk>/active/", set_active, name="user-active"),
]
