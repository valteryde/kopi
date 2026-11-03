"""The host age identity, `secret.key`.

Encrypt with the public recipient so the secret key stays off the command
line. Decrypt passes `age` the identity path. Credential columns store the
armored form. The pipeline stores the binary form.
"""

import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

IDENTITY_FILENAME = "secret.key"


class IdentityError(Exception):
    pass


@dataclass(frozen=True)
class Identity:
    path: Path
    recipient: str


def generate_identity(path: Path) -> Identity:
    path = Path(path)
    if path.is_symlink() or path.exists():
        raise IdentityError("identity file already exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    result = _run_cli(["age-keygen", "-o", os.fspath(path)])
    if result.returncode != 0:
        if path.exists() and not path.is_symlink():
            path.unlink()
        raise IdentityError("could not generate an identity")
    os.chmod(path, 0o600)
    return load_identity(path)


def load_identity(path: Path) -> Identity:
    path = Path(path)
    if path.is_symlink():
        raise IdentityError("bad identity")
    if not path.exists():
        raise IdentityError("identity file is missing")
    if not path.is_file():
        raise IdentityError("bad identity")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise IdentityError("identity file mode is too open")
    result = _run_cli(["age-keygen", "-y", os.fspath(path)])
    if result.returncode != 0:
        raise IdentityError("bad identity")
    lines = [line.strip() for line in result.stdout.decode().splitlines() if line.strip()]
    if len(lines) != 1 or not lines[0].startswith("age1") or " " in lines[0]:
        raise IdentityError("bad identity")
    return Identity(path=path, recipient=lines[0])


def encrypt(identity: Identity, plaintext: bytes) -> bytes:
    """Binary age ciphertext. The pipeline writes this to the work volume."""
    return _transform(
        ["age", "--encrypt", "--recipient", identity.recipient],
        plaintext,
    )


def decrypt(identity: Identity, ciphertext: bytes) -> bytes:
    return _transform(
        ["age", "--decrypt", "--identity", os.fspath(identity.path)],
        ciphertext,
    )


def encrypt_secret(identity: Identity, secret: str) -> str:
    """Armored ciphertext for a credential column."""
    ciphertext = _transform(
        ["age", "--encrypt", "--armor", "--recipient", identity.recipient],
        secret.encode(),
    )
    return ciphertext.decode()


def decrypt_secret(identity: Identity, stored: str) -> str:
    return decrypt(identity, stored.encode()).decode()


def _transform(argv: list[str], payload: bytes) -> bytes:
    if not isinstance(payload, bytes):
        raise TypeError("age payload must be bytes")
    result = _run_cli(argv, payload)
    if result.returncode != 0:
        raise IdentityError("age failed")
    return result.stdout


def _run_cli(argv: list[str], payload: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            argv,
            input=payload,
            capture_output=True,
            env=_cli_env(),
            check=False,
        )
    except FileNotFoundError as exc:
        raise IdentityError("age is not installed") from exc


def _cli_env() -> dict[str, str]:
    env = {}
    for name in ("PATH", "LANG", "LC_ALL", "LC_CTYPE"):
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    return env
