"""Django's signing key, `session.key` on the data volume.

A different file from the age identity `secret.key`.
"""

import os
import secrets
import stat
from pathlib import Path

FILENAME = "session.key"


class SessionKeyError(Exception):
    pass


def load_session_key(directory: Path) -> str:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / FILENAME
    if path.is_symlink():
        raise SessionKeyError("bad session key")
    if path.exists():
        return _read(path)
    return _create(path)


def _create(path: Path) -> str:
    key = secrets.token_urlsafe(64)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        return _read(path)
    try:
        os.write(fd, key.encode())
        os.fchmod(fd, 0o600)
    except Exception:
        os.close(fd)
        if path.exists() and not path.is_symlink():
            path.unlink()
        raise
    os.close(fd)
    return key


def _read(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise SessionKeyError("bad session key")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise SessionKeyError("session key mode is too open")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        raw = os.read(fd, 257)
    finally:
        os.close(fd)
    if not raw or len(raw) > 256:
        raise SessionKeyError("bad session key")
    return raw.decode()
