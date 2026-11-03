from pathlib import Path

from django.conf import settings
from django.contrib.auth import authenticate
from django.contrib.auth import login as auth_login
from django.contrib.auth import logout as auth_logout
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.models import User
from django.http import FileResponse, Http404, HttpResponseNotAllowed
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from kopi.accounts import (
    OFFER_SECRET_KEY,
    AccountExists,
    clear_failures,
    create_admin,
    is_paused,
    record_failure,
)
from kopi.forms.accounts import LoginForm, SetupForm, first_error
from kopi.identity import IDENTITY_FILENAME, IdentityError, load_identity


@login_not_required
def account(request):
    """Empty user table is setup. After that, this URL is login."""
    if request.user.is_authenticated:
        if request.session.get(OFFER_SECRET_KEY):
            return render(request, "key.jinja")
        return redirect("runs")
    if not User.objects.exists():
        return _setup(request)
    return _login(request)


def _setup(request):
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    error = ""
    name = ""
    if request.method == "POST":
        form = SetupForm(request.POST)
        name = request.POST.get("name", "")
        if form.is_valid():
            name = form.cleaned_data["name"]
            password = form.cleaned_data["password"]
            try:
                create_admin(name, password)
            except AccountExists:
                return _login(request)
            except IdentityError:
                error = "Could not generate secret.key."
            else:
                user = authenticate(request, username=name, password=password)
                if user is None:
                    error = "Could not sign in."
                else:
                    auth_login(request, user)
                    request.session[OFFER_SECRET_KEY] = True
                    return redirect("login")
        else:
            error = first_error(form)
    return render(request, "setup.jinja", {"error": error, "name": name})


def _login(request):
    if request.method not in ("GET", "POST"):
        return HttpResponseNotAllowed(["GET", "POST"])
    error = ""
    name = ""
    status = 200
    next_url = _safe_next(request)
    if request.method == "POST":
        form = LoginForm(request.POST)
        name = request.POST.get("name", "")
        if form.is_valid():
            name = form.cleaned_data["name"]
            password = form.cleaned_data["password"]
            if is_paused(name):
                error = "This account is paused."
                status = 403
            else:
                user = authenticate(request, username=name, password=password)
                if user is None:
                    record_failure(name)
                    if is_paused(name):
                        error = "This account is paused."
                        status = 403
                    else:
                        error = "Name or password is wrong."
                else:
                    clear_failures(name)
                    auth_login(request, user)
                    return redirect(next_url or reverse("runs"))
        else:
            error = first_error(form)
    response = render(
        request,
        "login.jinja",
        {"error": error, "name": name, "next": next_url},
    )
    response.status_code = status
    return response


def _safe_next(request) -> str:
    if request.method == "POST":
        candidate = request.POST.get("next", "")
    else:
        candidate = request.GET.get("next", "")
    if candidate and url_has_allowed_host_and_scheme(
        candidate,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return candidate
    return ""


@require_POST
def secret_key(request):
    """The age identity, offered once in the session that created it."""
    if not request.session.get(OFFER_SECRET_KEY):
        raise Http404
    path = Path(settings.DATA_DIR) / IDENTITY_FILENAME
    try:
        identity = load_identity(path)
    except IdentityError as exc:
        raise Http404 from exc
    del request.session[OFFER_SECRET_KEY]
    response = FileResponse(
        identity.path.open("rb"),
        as_attachment=True,
        filename=IDENTITY_FILENAME,
        content_type="application/octet-stream",
    )
    response["Cache-Control"] = "no-store"
    return response


@require_POST
def sign_out(request):
    auth_logout(request)
    return redirect("login")


urlpatterns = [
    path("login/", account, name="login"),
    path("login/secret.key", secret_key, name="secret-key"),
    path("logout/", sign_out, name="logout"),
]
