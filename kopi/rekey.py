"""Replace `secret.key` and re-encrypt credential columns with the new key.

Objects already in the bucket stay readable only with the previous key.
`secret.key.previous` and `secret.key.next` are crash markers. The next open
finishes a swap that stopped between the database write and the rename, and
drops a swap that stopped before the database write.
"""

import os
import stat
from pathlib import Path

from django.db import transaction

from kopi.identity import (
    IDENTITY_FILENAME,
    Identity,
    IdentityError,
    decrypt_secret,
    encrypt_secret,
    generate_identity,
    load_identity,
)
from kopi.settings import data_directory

PREVIOUS_FILENAME = "secret.key.previous"
PENDING_FILENAME = "secret.key.next"


class RekeyError(Exception):
    pass


def open_identity(directory: Path | None = None) -> Identity:
    """The identity on the data volume, after any interrupted replacement."""
    root = Path(directory) if directory is not None else data_directory()
    recover(root)
    return load_identity(root / IDENTITY_FILENAME)


def replace_identity(directory: Path | None = None) -> Identity:
    """Install a new key and re-encrypt stored passwords. The old key file is removed."""
    root = Path(directory) if directory is not None else data_directory()
    recover(root)
    path = root / IDENTITY_FILENAME
    previous = root / PREVIOUS_FILENAME
    pending = root / PENDING_FILENAME
    if _present(previous) or _present(pending):
        raise RekeyError("could not replace secret.key")
    old = load_identity(path)
    _copy_private(path, previous)
    try:
        new = generate_identity(pending)
        _rewrite(old, new)
        os.replace(pending, path)
        os.chmod(path, 0o600)
    except Exception as exc:
        _abort(root)
        recover(root)
        try:
            installed = load_identity(path)
        except IdentityError as identity_error:
            raise RekeyError("could not replace secret.key") from identity_error
        if installed.recipient == old.recipient:
            raise RekeyError("could not replace secret.key") from exc
        return installed
    previous.unlink(missing_ok=True)
    return load_identity(path)


def recover(directory: Path) -> None:
    """Finish or drop a replacement that stopped halfway."""
    root = Path(directory)
    path = root / IDENTITY_FILENAME
    previous = root / PREVIOUS_FILENAME
    pending = root / PENDING_FILENAME
    if not _present(previous) and not _present(pending):
        return
    opener = _opener(path, pending, previous)
    if opener is None:
        if not _has_secrets() and _present(pending):
            pending.unlink(missing_ok=True)
            previous.unlink(missing_ok=True)
        elif not _has_secrets() and _present(previous) and not _present(pending):
            previous.unlink(missing_ok=True)
        return
    if opener != path:
        os.replace(opener, path)
        os.chmod(path, 0o600)
    if _present(pending) and pending.resolve() != path.resolve():
        pending.unlink()
    if _present(previous) and previous.resolve() != path.resolve():
        previous.unlink()


def _opener(path: Path, pending: Path, previous: Path) -> Path | None:
    for candidate in (path, pending, previous):
        if not _present(candidate):
            continue
        try:
            identity = load_identity(candidate)
        except IdentityError:
            continue
        if _secrets_open(identity):
            return candidate
    return None


def _abort(root: Path) -> None:
    """Drop a new key when the database still opens with the installed file.

    When the database already uses the new key, the markers stay so `recover`
    can finish the rename.
    """
    path = root / IDENTITY_FILENAME
    pending = root / PENDING_FILENAME
    previous = root / PREVIOUS_FILENAME
    try:
        current = load_identity(path)
    except IdentityError:
        return
    if _secrets_open(current):
        pending.unlink(missing_ok=True)
        previous.unlink(missing_ok=True)


def _rewrite(old: Identity, new: Identity) -> None:
    from kopi.models import Channel, Source, Target

    with transaction.atomic():
        for source in Source.objects.exclude(pg_password=""):
            source.pg_password = encrypt_secret(new, decrypt_secret(old, source.pg_password))
            source.save(update_fields=["pg_password"])
        for target in Target.objects.all():
            fields = []
            if target.access_key_id:
                target.access_key_id = encrypt_secret(
                    new, decrypt_secret(old, target.access_key_id)
                )
                fields.append("access_key_id")
            if target.secret_access_key:
                target.secret_access_key = encrypt_secret(
                    new, decrypt_secret(old, target.secret_access_key)
                )
                fields.append("secret_access_key")
            if fields:
                target.save(update_fields=fields)
        for channel in Channel.objects.exclude(password=""):
            channel.password = encrypt_secret(new, decrypt_secret(old, channel.password))
            channel.save(update_fields=["password"])


def _secrets_open(identity: Identity) -> bool:
    for stored in _secret_values():
        try:
            decrypt_secret(identity, stored)
        except IdentityError:
            return False
    return True


def _has_secrets() -> bool:
    return any(True for _stored in _secret_values())


def _secret_values():
    from kopi.models import Channel, Source, Target

    yield from Source.objects.exclude(pg_password="").values_list("pg_password", flat=True)
    yield from Target.objects.exclude(access_key_id="").values_list("access_key_id", flat=True)
    yield from Target.objects.exclude(secret_access_key="").values_list(
        "secret_access_key", flat=True
    )
    yield from Channel.objects.exclude(password="").values_list("password", flat=True)


def _present(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _copy_private(source: Path, dest: Path) -> None:
    if dest.exists() or dest.is_symlink():
        raise RekeyError("could not replace secret.key")
    payload = source.read_bytes()
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(dest, 0o600)
    if stat.S_IMODE(dest.stat().st_mode) & 0o077:
        dest.unlink()
        raise RekeyError("could not replace secret.key")
