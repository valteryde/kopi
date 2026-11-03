import os
import stat
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from ward import raises, test

from kopi.identity import (
    IdentityError,
    decrypt,
    decrypt_secret,
    encrypt,
    encrypt_secret,
    generate_identity,
    load_identity,
)


def _secret_line(path: Path) -> bytes:
    for line in path.read_bytes().splitlines():
        if line.startswith(b"AGE-SECRET-KEY-"):
            return line
    raise AssertionError("identity file has no secret key")


@test("an age identity is mode 0600 and decrypts what it encrypted")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "secret.key"
        previous = os.umask(0)
        try:
            identity = generate_identity(path)
        finally:
            os.umask(previous)

        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert identity.path == path
        assert identity.recipient.startswith("age1")
        secret = _secret_line(path)
        assert secret not in identity.recipient.encode()

        plaintext = b"p@ss word\x00\xff"
        ciphertext = encrypt(identity, plaintext)
        assert ciphertext != plaintext
        assert ciphertext.startswith(b"age-encryption.org/v1\n")
        assert secret not in ciphertext
        assert decrypt(identity, ciphertext) == plaintext

        armored = encrypt_secret(identity, "db-password")
        assert armored.startswith("-----BEGIN AGE ENCRYPTED FILE-----")
        assert "db-password" not in armored
        assert secret.decode() not in armored
        assert decrypt_secret(identity, armored) == "db-password"

        loaded = load_identity(path)
        assert loaded == identity


@test("a missing age identity fails")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "secret.key"
        with raises(IdentityError):
            load_identity(missing)


@test("a bad age identity fails")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        garbage = Path(tmp) / "garbage.key"
        garbage.write_text("not a key\n")
        os.chmod(garbage, 0o600)
        with raises(IdentityError):
            load_identity(garbage)

        loose = generate_identity(Path(tmp) / "loose.key")
        os.chmod(loose.path, 0o644)
        with raises(IdentityError):
            load_identity(loose.path)

        good = generate_identity(Path(tmp) / "good.key")
        other = generate_identity(Path(tmp) / "other.key")
        ciphertext = encrypt(good, b"secret")
        with raises(IdentityError):
            decrypt(other, ciphertext)
        with raises(IdentityError):
            decrypt(good, b"not age ciphertext")


@test("age commands keep the secret key off argv and out of the environment")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        identity = generate_identity(Path(tmp) / "secret.key")
        secret = _secret_line(identity.path)
        ciphertext = encrypt(identity, b"hello")
        with patch("kopi.identity.subprocess.run", wraps=subprocess.run) as run:
            assert decrypt(identity, ciphertext) == b"hello"
            encrypt(identity, b"hello")
        assert run.call_count == 2
        for call in run.call_args_list:
            argv = " ".join(call.args[0]).encode()
            assert secret not in argv
            assert b"AGE-SECRET-KEY-" not in argv
            assert set(call.kwargs["env"]) <= {"PATH", "LANG", "LC_ALL", "LC_CTYPE"}
