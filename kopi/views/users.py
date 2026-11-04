"""The user table, the role cards, and the form that adds an account.

An admin adds a name, a login, a password, and a role. Viewers are refused.
"""

from django.contrib.auth.models import Group, User
from django.db import IntegrityError, transaction
from django.http import HttpResponseNotAllowed
from django.shortcuts import redirect, render
from django.urls import path

from kopi.access import display_name, ensure_roles, require_admin, role_of
from kopi.audit import note_user_created
from kopi.forms.users import UserForm


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


def _render(request, form: UserForm):
    current = request.user.pk
    rows = []
    for user in User.objects.all():
        label, icon = role_of(user)
        rows.append(
            {
                "name": display_name(user),
                "login": user.get_username(),
                "role": label,
                "role_icon": icon,
                "you": user.pk == current,
            }
        )
    rows.sort(key=lambda row: (row["name"].casefold(), row["login"].casefold()))
    return render(request, "users.jinja", {"form": form, "rows": rows})


urlpatterns = [
    path("users/", users, name="users"),
]
