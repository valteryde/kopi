import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import tests.bootstrap  # noqa: F401
from tests.bootstrap import DATA_DIR
from django.db import connection
from ward import test

from kopi.identity import encrypt_secret, generate_identity
from kopi.models import Link, Run, Source, Target

ROOT = Path(__file__).resolve().parents[1]
MANAGE = [sys.executable, str(ROOT / "manage.py"), "work"]
SECRET = "s3-secret-should-not-leak"
PASSWORD = "db-password-should-not-leak"


def _reset() -> None:
    Run.objects.all().delete()
    Link.objects.all().delete()
    Source.objects.all().delete()
    Target.objects.all().delete()


def _identity():
    path = DATA_DIR / "secret.key"
    if path.is_symlink() or path.exists():
        path.unlink()
    return generate_identity(path)


def _target(identity, name: str, endpoint: str = "http://127.0.0.1:9") -> Target:
    return Target.objects.create(
        name=name,
        bucket="kopi",
        endpoint_url=endpoint,
        region="garage",
        path_style=True,
        access_key_id=encrypt_secret(identity, "access-key"),
        secret_access_key=encrypt_secret(identity, SECRET),
    )


def _link(source: Source, target: Target) -> Link:
    return Link.objects.create(
        source=source,
        target=target,
        cadence="0 3 * * *",
        timezone="Europe/Copenhagen",
        retention_kind=Link.Retention.COUNT,
        retention_count=3,
    )


def _env(work: Path, path: str | None = None) -> dict[str, str]:
    env = os.environ.copy()
    env["KOPI_WORKER_SLEEP"] = "30"
    env["KOPI_WORK_DIR"] = str(work)
    if path is not None:
        env["PATH"] = path
    env.pop("KOPI_PARENT_FD", None)
    return env


class _Worker:
    def __init__(self, env: dict[str, str]):
        self.proc = subprocess.Popen(
            MANAGE,
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)

    def kill(self) -> None:
        if self.proc.poll() is None:
            os.kill(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=5)


def _processes():
    result = subprocess.run(
        ["ps", "-ax", "-o", "pid=,ppid=,command="],
        capture_output=True,
        text=True,
        check=False,
    )
    rows = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        rows.append((int(parts[0]), int(parts[1]), parts[2]))
    return rows


def _descendants(root: int, rows):
    children: dict[int, list[tuple[int, str]]] = {}
    for pid, ppid, command in rows:
        children.setdefault(ppid, []).append((pid, command))
    found = []

    def walk(pid: int) -> None:
        for child, command in children.get(pid, []):
            found.append((child, command))
            walk(child)

    walk(root)
    return found


def _tools(commands: list[str]) -> set[str]:
    found = set()
    for command in commands:
        for token in command.split():
            name = Path(token).name
            if name == "gtar":
                name = "tar"
            if name in {"tar", "zstd", "age", "pg_dump"}:
                found.add(name)
    return found


def _wait(predicate, timeout: float = 15):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.05)
    raise AssertionError("timed out")


def _alive(pid: int, rows) -> bool:
    return any(row[0] == pid for row in rows)


def _tar_shim(directory: Path) -> None:
    body = (
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        "if '--version' in sys.argv:\n"
        "    sys.stdout.buffer.write(b'tar (GNU tar) 1.35\\n')\n"
        "    raise SystemExit(0)\n"
        "time.sleep(120)\n"
    )
    for name in ("gtar", "tar"):
        path = directory / name
        path.write_text(body)
        path.chmod(0o755)


@test("a claimed link runs the phase 1 child")
def _():
    _reset()
    identity = _identity()
    folder = DATA_DIR / f"notes-{uuid.uuid4().hex[:8]}"
    folder.mkdir()
    (folder / "hello.txt").write_text("hello")
    source = Source.objects.create(
        name=f"src-{uuid.uuid4().hex[:8]}",
        kind=Source.Kind.FOLDER,
        root_path=str(folder),
    )
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    link = _link(
        source,
        _target(identity, f"tgt-{uuid.uuid4().hex[:8]}", f"http://127.0.0.1:{port}"),
    )
    work = DATA_DIR / f"work-{uuid.uuid4().hex[:8]}"
    work.mkdir()
    worker = _Worker(_env(work))
    try:
        def started():
            connection.close()
            run = Run.objects.filter(link=link).first()
            if run is None or run.phase not in {Run.Phase.UPLOAD, Run.Phase.POST}:
                return None
            rows = _descendants(worker.proc.pid, _processes())
            child = next((row for row in rows if f"runchild {run.pk}" in row[1]), None)
            return None if child is None else (run, child)

        run, child = _wait(started)
        assert SECRET not in child[1]
        assert PASSWORD not in child[1]
        assert f"runchild {run.pk}" in child[1]
        child_pid = child[0]
        worker.kill()
        assert _wait(lambda: None if _alive(child_pid, _processes()) else True)
    finally:
        listener.close()
        worker.stop()


@test("killing the worker during a folder archive leaves no tar, zstd, or age")
def _():
    _reset()
    identity = _identity()
    folder = DATA_DIR / f"tree-{uuid.uuid4().hex[:8]}"
    folder.mkdir()
    (folder / "hello.txt").write_text("hello")
    shims = DATA_DIR / f"bin-{uuid.uuid4().hex[:8]}"
    shims.mkdir()
    _tar_shim(shims)
    source = Source.objects.create(
        name=f"src-{uuid.uuid4().hex[:8]}",
        kind=Source.Kind.FOLDER,
        root_path=str(folder),
    )
    _link(source, _target(identity, f"tgt-{uuid.uuid4().hex[:8]}"))
    work = DATA_DIR / f"work-{uuid.uuid4().hex[:8]}"
    work.mkdir()
    path = f"{shims}{os.pathsep}{os.environ.get('PATH', '')}"
    worker = _Worker(_env(work, path))
    try:
        def running_tools():
            if worker.proc.poll() is not None:
                raise AssertionError(f"worker exited {worker.proc.returncode}")
            rows = _descendants(worker.proc.pid, _processes())
            found = {name: pid for pid, command in rows for name in _tools([command])}
            if {"tar", "zstd", "age"} <= set(found):
                return found
            return None

        tools = _wait(running_tools)
    finally:
        worker.kill()

    def gone():
        rows = _processes()
        return True if all(not _alive(pid, rows) for pid in tools.values()) else None

    assert _wait(gone)


@test("killing the worker while pg_dump is in progress leaves no pg_dump")
def _():
    _reset()
    identity = _identity()
    binary = DATA_DIR / f"pg_dump-{uuid.uuid4().hex[:8]}"
    binary.write_text("#!/bin/sh\nsleep 120\n")
    binary.chmod(0o755)
    source = Source.objects.create(
        name=f"pg-{uuid.uuid4().hex[:8]}",
        kind=Source.Kind.POSTGRES,
        pg_host="127.0.0.1",
        pg_database="kopi",
        pg_user="kopi",
        pg_password=encrypt_secret(identity, PASSWORD),
        pg_dump_binary=str(binary),
    )
    _link(source, _target(identity, f"tgt-{uuid.uuid4().hex[:8]}"))
    work = DATA_DIR / f"work-{uuid.uuid4().hex[:8]}"
    work.mkdir()
    worker = _Worker(_env(work))
    try:
        def dumping():
            if worker.proc.poll() is not None:
                raise AssertionError(f"worker exited {worker.proc.returncode}")
            rows = _descendants(worker.proc.pid, _processes())
            for pid, command in rows:
                if str(binary) in command or Path(command.split()[0]).name == binary.name:
                    assert PASSWORD not in command
                    return pid
            return None

        pid = _wait(dumping)
    finally:
        worker.kill()

    assert _wait(lambda: None if _alive(pid, _processes()) else True)
