"""First-run setup, and the login pause stored as failure rows."""

from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.db import transaction
from django.utils import timezone

from kopi.access import ADMIN, ensure_roles
from kopi.identity import IDENTITY_FILENAME, generate_identity
from kopi.models import LoginFailure

# Five refusals inside this window pause the account until the window moves on.
LOGIN_FAILURE_LIMIT = 5
LOGIN_PAUSE = timedelta(minutes=15)

OFFER_SECRET_KEY = "offer_secret_key"


class AccountExists(Exception):
    pass


def create_admin(name: str, password: str) -> User:
    """Create the only admin and `secret.key`. Rolls back the user if the key fails."""
    path = Path(settings.DATA_DIR) / IDENTITY_FILENAME
    with transaction.atomic():
        if User.objects.exists():
            raise AccountExists
        ensure_roles()
        user = User.objects.create_user(username=name, password=password)
        user.groups.add(Group.objects.get(name=ADMIN))
        generate_identity(path)
    return user


def is_paused(username: str) -> bool:
    since = timezone.now() - LOGIN_PAUSE
    recent = LoginFailure.objects.filter(username=username, created_at__gte=since).count()
    return recent >= LOGIN_FAILURE_LIMIT


def record_failure(username: str) -> None:
    LoginFailure.objects.create(username=username)


def clear_failures(username: str) -> None:
    LoginFailure.objects.filter(username=username).delete()
