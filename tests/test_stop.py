import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import tests.bootstrap  # noqa: F401
from tests.bootstrap import DATA_DIR
from django.db import connection
from django.utils import timezone
from ward import raises, test

from kopi.child import reconcile
from kopi.identity import encrypt_secret, generate_identity
from kopi.models import Link, Run, Source, Target
from kopi.pipeline import marker_path
from kopi.storage import FakeStorage
from kopi.worker import serve

ROOT = Path(__file__).resolve().parents[1]
MANAGE = [sys.executable, str(ROOT / "manage.py"), "work"]


class _Stop(Exception):
    pass


def _reset() -> None:
    Run.objects.all().delete()
    Link.objects.all().delete()
    Source.objects.all().delete()
    Target.objects.all().delete()


def _link(*, post: str = "") -> Link:
    source = Source.objects.create(
        name=f"src-{uuid.uuid4().hex[:8]}",
        kind=Source.Kind.FOLDER,
        root_path=f"/data/{uuid.uuid4().hex[:8]}",
    )
    target = Target.objects.create(name=f"tgt-{uuid.uuid4().hex[:8]}", bucket="kopi")
    return Link.objects.create(
        source=source,
        target=target,
        cadence="0 3 * * *",
        timezone="Europe/Copenhagen",
        retention_kind=Link.Retention.COUNT,
        retention_count=3,
        post_script_path=post,
    )


def _running(link: Link, phase: str) -> Run:
    run = Run.objects.create(link=link, status=Run.Status.RUNNING, phase=phase)
    Run.objects.filter(pk=run.pk).update(started_at=timezone.now())
    run.refresh_from_db()
    return run


def _marker(ciphertext: Path, run_id: int, payload: bytes) -> None:
    ciphertext.parent.mkdir(parents=True)
    ciphertext.write_bytes(payload)
    body = {
        "run_id": str(run_id),
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    marker_path(ciphertext).write_text(json.dumps(body, sort_keys=True) + "\n")


def _serve(reconcile_ids) -> None:
    # A due link with no run is claimed after reconciliation. Raising there
    # ends the loop without replacing time.sleep, which the post script uses.
    def claimed(run):
        raise _Stop

    with raises(_Stop):
        serve(
            claimed,
            sleep_seconds=30,
            now=timezone.now,
            reconcile=reconcile_ids,
        )


@test("the next start uploads a marked file and does not start tar")
def _():
    _reset()
    kept = _running(_link(), Run.Phase.UPLOAD)
    other = _link()
    work = DATA_DIR / f"work-{uuid.uuid4().hex[:8]}"
    payload = b"marked-ciphertext"
    ciphertext = work / str(kept.pk) / "payload.age"
    _marker(ciphertext, kept.pk, payload)
    identity = generate_identity(DATA_DIR / f"id-{uuid.uuid4().hex[:8]}")
    storage = FakeStorage()
    seen = []

    def reconcile_ids(run_ids):
        seen.append(list(run_ids))
        for run_id in run_ids:
            reconcile(run_id, identity=identity, storage=storage, work_dir=work)

    with patch("kopi.child.start_folder_producer") as started:
        _serve(reconcile_ids)
        assert started.call_count == 0
    connection.close()
    kept.refresh_from_db()
    assert seen[0] == [kept.pk]
    assert kept.status == Run.Status.SUCCEEDED
    assert [item.key for item in storage.list()] == [kept.object_name]
    assert not ciphertext.exists()
    assert Run.objects.filter(link=other, status=Run.Status.RUNNING).exists()


@test("a partial with no marker is deleted and the post script runs when pre succeeded")
def _():
    _reset()
    flag = DATA_DIR / f"post-{uuid.uuid4().hex[:8]}"
    script = DATA_DIR / f"post-{uuid.uuid4().hex[:8]}.sh"
    script.write_text(f"#!/bin/bash\ntouch {flag}\n")
    link = _link(post=str(script))
    _link()
    run = _running(link, Run.Phase.ARCHIVE)
    work = DATA_DIR / f"work-{uuid.uuid4().hex[:8]}"
    partial = work / str(run.pk) / "payload.age"
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"partial")
    identity = generate_identity(DATA_DIR / f"id-{uuid.uuid4().hex[:8]}")

    def reconcile_ids(run_ids):
        for run_id in run_ids:
            reconcile(run_id, identity=identity, storage=FakeStorage(), work_dir=work)

    with patch("kopi.child.start_folder_producer") as started:
        _serve(reconcile_ids)
        assert started.call_count == 0
    connection.close()
    run.refresh_from_db()
    assert run.status == Run.Status.FAILED
    assert not partial.exists()
    assert flag.is_file()


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
            if name in {"tar", "zstd", "age"}:
                found.add(name)
    return found


def _wait(predicate, timeout: float = 15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(0.05)
    raise AssertionError("timed out")


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


@test("SIGTERM during an archive leaves no tar, zstd, or age")
def _():
    _reset()
    key = DATA_DIR / "secret.key"
    if key.is_symlink() or key.exists():
        key.unlink()
    identity = generate_identity(key)
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
    target = Target.objects.create(
        name=f"tgt-{uuid.uuid4().hex[:8]}",
        bucket="kopi",
        endpoint_url="http://127.0.0.1:9",
        region="garage",
        path_style=True,
        access_key_id=encrypt_secret(identity, "access-key"),
        secret_access_key=encrypt_secret(identity, "secret-key"),
    )
    link = Link.objects.create(
        source=source,
        target=target,
        cadence="0 3 * * *",
        timezone="Europe/Copenhagen",
        retention_kind=Link.Retention.COUNT,
        retention_count=3,
    )
    work = DATA_DIR / f"work-{uuid.uuid4().hex[:8]}"
    work.mkdir()
    env = os.environ.copy()
    env["KOPI_WORKER_SLEEP"] = "30"
    env["KOPI_WORK_DIR"] = str(work)
    env["PATH"] = f"{shims}{os.pathsep}{env.get('PATH', '')}"
    env.pop("KOPI_PARENT_FD", None)
    worker = subprocess.Popen(
        MANAGE,
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        def running_tools():
            if worker.poll() is not None:
                raise AssertionError(f"worker exited {worker.returncode}")
            rows = _descendants(worker.pid, _processes())
            found = {name: pid for pid, command in rows for name in _tools([command])}
            if {"tar", "zstd", "age"} <= set(found):
                return found
            return None

        tools = _wait(running_tools)
        worker.send_signal(signal.SIGTERM)
        worker.wait(timeout=8)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=5)

    def gone():
        rows = _processes()
        return True if all(not any(row[0] == pid for row in rows) for pid in tools.values()) else None

    assert _wait(gone)
    connection.close()
    run = Run.objects.get(link=link)
    assert run.status == Run.Status.RUNNING
    assert run.started_at is not None
