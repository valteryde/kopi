"""Logical Postgres dumps through the same age pipeline as a folder archive.

`pg_dump -Fc` never sees the password on its command line. The child writes
a mode 0600 pgpass file and deletes it before returning. A source names the
server itself, never a connection pooler; `pooler_reason` is what refuses it.
"""

import os
import re
import subprocess
from pathlib import Path

from kopi.identity import Identity, _cli_env
from kopi.pdeath import popen_kwargs
from kopi.pipeline import Marker, PipelineError, Producer, archive, read_marker


# `pg_dump` empties `search_path` for the rest of its own session:
#
#     SELECT pg_catalog.set_config('search_path', '', false);
#
# On a server that is harmless, because the backend goes away with the dump. A
# connection pooler keeps that backend and hands it to the next client, which
# then finds none of its own tables. Every name and port below is a way an
# operator reaches a pooler by accident instead of the server.
POOLER_PORTS = {6432: "PgBouncer", 9999: "PgPool-II", 5000: "Odyssey"}
POOLER_MARKS = ("bouncer", "pooler", "pgpool", "pgcat", "pgdog", "odyssey", "pgproxy")
POOLER_EXPLAINED = (
    "A dump empties search_path for its own session, and a pooler hands that "
    "backend to the next client. Its tables then read as missing."
)


def pooler_reason(host: str, port: int | str | None) -> str | None:
    """Return why this host and port look like a pooler, or None.

    The name wins over the port: an operator who typed a pooler service name
    means that host, whatever port they paired it with. The port is read
    defensively because an uncleaned form value can arrive as text.
    """
    name = str(host or "").strip().lower()
    for mark in POOLER_MARKS:
        if mark in name:
            return f"host '{host}' looks like a connection pooler"
    try:
        number = int(port or 0)
    except (TypeError, ValueError):
        number = 0
    if number in POOLER_PORTS:
        return f"port {number} is the {POOLER_PORTS[number]} default"
    return None


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


def find_dump_binaries() -> list[tuple[str, str]]:
    """Executable `pg_dump` names on `PATH`, as `(path, label)`."""
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        if not directory:
            continue
        root = Path(directory)
        if not root.is_dir():
            continue
        try:
            names = sorted(path.name for path in root.iterdir())
        except OSError:
            continue
        for name in names:
            if not _is_dump_name(name):
                continue
            path = root / name
            if len(os.fspath(path)) > 255 or not _is_executable(path):
                continue
            real = os.path.realpath(path)
            if real in seen:
                continue
            seen.add(real)
            found.append((os.fspath(path), _dump_label(path)))
    return found


def _is_dump_name(name: str) -> bool:
    if name == "pg_dump":
        return True
    if not name.startswith("pg_dump"):
        return False
    rest = name.removeprefix("pg_dump")
    return rest.startswith("-") or rest[:1].isdigit()


def _is_executable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _dump_label(path: Path) -> str:
    try:
        result = subprocess.run(
            [os.fspath(path), "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired):
        return path.name
    match = re.search(r"(\d+)", result.stdout)
    if match is None:
        return path.name
    return f"pg_dump {match.group(1)}"


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
        **popen_kwargs(),
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
            **popen_kwargs(),
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
            **popen_kwargs(),
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
