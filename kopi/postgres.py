"""Logical Postgres dumps through the same age pipeline as a folder archive.

`pg_dump -Fc` never sees the password on its command line. The child writes
a mode 0600 pgpass file and deletes it before returning.
"""

import os
import subprocess
from pathlib import Path

from kopi.identity import Identity, _cli_env
from kopi.pipeline import Marker, PipelineError, Producer, archive, read_marker


def dump_argv(binary: str, *, host: str, port: int, user: str, database: str) -> list[str]:
    return [
        binary or "pg_dump",
        "-Fc",
        "--no-password",
        "-h",
        host,
        "-p",
        str(port),
        "-U",
        user,
        "-d",
        database,
    ]


def restore_argv(binary: str, *, host: str, port: int, user: str, database: str) -> list[str]:
    return [
        binary or "pg_restore",
        "--no-password",
        "--exit-on-error",
        "--no-owner",
        "-h",
        host,
        "-p",
        str(port),
        "-U",
        user,
        "-d",
        database,
    ]


def restore_binary_for(dump_binary: str) -> str:
    if not dump_binary:
        return "pg_restore"
    sibling = Path(dump_binary).with_name("pg_restore")
    if sibling.is_file():
        return os.fspath(sibling)
    return "pg_restore"


def write_pgpass(
    directory: Path,
    *,
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
) -> Path:
    """Create a mode 0600 pgpass file. The caller deletes it."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "pgpass"
    line = ":".join(
        _escape(part) for part in (host, str(port), database, user, password)
    ) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, line.encode())
    except Exception:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    os.close(fd)
    os.chmod(path, 0o600)
    return path


def start_dump(
    *,
    binary: str,
    host: str,
    port: int,
    user: str,
    database: str,
    pgpass: Path,
    tmpdir: Path,
) -> Producer:
    env = _tool_env(tmpdir)
    env["PGPASSFILE"] = os.fspath(pgpass)
    process = subprocess.Popen(
        dump_argv(binary, host=host, port=port, user=user, database=database),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
        bufsize=0,
    )
    return Producer([process])


def archive_database(
    *,
    binary: str,
    host: str,
    port: int,
    user: str,
    database: str,
    password: str,
    identity: Identity,
    ciphertext: Path,
    run_id: str,
    tmpdir: Path,
    on_bytes=None,
    producer_slot=None,
) -> Marker:
    """Dump one database into age. The pgpass file is gone when this returns."""
    pgpass = write_pgpass(
        tmpdir,
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
    )
    try:
        producer = start_dump(
            binary=binary,
            host=host,
            port=port,
            user=user,
            database=database,
            pgpass=pgpass,
            tmpdir=tmpdir,
        )
        if producer_slot is not None:
            producer_slot.append(producer)
        return archive(
            producer,
            identity,
            ciphertext,
            run_id,
            on_bytes=on_bytes,
            tmpdir=tmpdir,
        )
    finally:
        if producer_slot is not None:
            producer_slot.clear()
        pgpass.unlink(missing_ok=True)


def restore_database(
    ciphertext: Path,
    identity: Identity,
    *,
    binary: str,
    host: str,
    port: int,
    user: str,
    database: str,
    password: str,
    tmpdir: Path,
) -> None:
    """Decrypt one marked dump and load it into `database`."""
    if read_marker(ciphertext) is None:
        raise PipelineError("archive has no valid marker")
    pgpass = write_pgpass(
        tmpdir,
        host=host,
        port=port,
        database=database,
        user=user,
        password=password,
    )
    source = Path(ciphertext).open("rb")
    processes: list[subprocess.Popen[bytes]] = []
    try:
        env = _tool_env(tmpdir)
        env["PGPASSFILE"] = os.fspath(pgpass)
        age = subprocess.Popen(
            ["age", "--decrypt", "--identity", os.fspath(identity.path)],
            stdin=source,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            bufsize=0,
        )
        processes.append(age)
        assert age.stdout is not None
        restore = subprocess.Popen(
            restore_argv(
                restore_binary_for(binary),
                host=host,
                port=port,
                user=user,
                database=database,
            ),
            stdin=age.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=env,
        )
        processes.append(restore)
        age.stdout.close()
        error = restore.stderr.read() if restore.stderr is not None else b""
        codes = [process.wait() for process in reversed(processes)]
        if any(code != 0 for code in codes):
            detail = error.decode("utf-8", "replace").strip()
            raise PipelineError(detail or "restore failed")
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
        source.close()
        pgpass.unlink(missing_ok=True)


def _tool_env(tmpdir: Path) -> dict[str, str]:
    env = _cli_env()
    env["TMPDIR"] = os.fspath(tmpdir)
    return env


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:")
