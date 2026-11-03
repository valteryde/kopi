import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import tests.bootstrap  # noqa: F401
from tests.bootstrap import DATA_DIR
from django.conf import settings
from django.db import connection
from django.utils import timezone
from ward import raises, test

from kopi.models import Link, Run, Source, Target
from kopi.worker import serve, tick, worker_lock_path

ROOT = Path(__file__).resolve().parents[1]
MANAGE = [sys.executable, str(ROOT / "manage.py"), "work"]


class _Stop(Exception):
    pass


def _reset() -> None:
    Run.objects.all().delete()
    Link.objects.all().delete()
    Source.objects.all().delete()
    Target.objects.all().delete()


def _link() -> Link:
    source = Source.objects.create(
        name=f"src-{uuid.uuid4().hex[:8]}",
        kind=Source.Kind.FOLDER,
        root_path="/data/notes",
    )
    target = Target.objects.create(name=f"tgt-{uuid.uuid4().hex[:8]}", bucket="kopi")
    return Link.objects.create(
        source=source,
        target=target,
        cadence="0 3 * * *",
        timezone="Europe/Copenhagen",
        retention_kind=Link.Retention.COUNT,
        retention_count=7,
    )


def _env():
    env = os.environ.copy()
    env["KOPI_WORKER_SLEEP"] = "30"
    return env


class _Worker:
    def __init__(self):
        descriptor, name = tempfile.mkstemp(prefix="kopi-worker-")
        os.close(descriptor)
        self.path = Path(name)
        self.err = self.path.open("w")
        self.proc = subprocess.Popen(
            MANAGE,
            cwd=ROOT,
            env=_env(),
            stdout=subprocess.DEVNULL,
            stderr=self.err,
        )

    def output(self) -> str:
        self.err.flush()
        return self.path.read_text()

    def stop(self) -> str:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        text = self.output()
        self.err.close()
        self.path.unlink(missing_ok=True)
        return text


def _wait(predicate, worker: _Worker, timeout: float = 8) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if worker.proc.poll() is not None:
            raise AssertionError(worker.output() or f"worker exited {worker.proc.returncode}")
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(worker.output() or "timed out")


def _lock_pid():
    path = worker_lock_path()
    if not path.is_file():
        return None
    text = path.read_text().strip()
    return int(text) if text.isdigit() else None


@test("the worker sleeps for the configured interval between ticks")
def _():
    _reset()
    slept = []

    def stop(seconds):
        slept.append(seconds)
        raise _Stop

    with patch("kopi.worker.time.sleep", stop):
        with raises(_Stop):
            serve(lambda run: None, sleep_seconds=settings.WORKER_SLEEP_SECONDS, now=timezone.now)
    assert slept == [settings.WORKER_SLEEP_SECONDS]
    assert worker_lock_path().parent == DATA_DIR


@test("one call of the tick does not sleep")
def _():
    _reset()
    link = _link()
    with tempfile.TemporaryDirectory() as tmp:
        with patch("kopi.worker.time.sleep") as sleep:
            tick(timezone.now(), child=lambda run: None, work_dir=Path(tmp))
    sleep.assert_not_called()
    assert Run.objects.filter(link=link, status=Run.Status.RUNNING).count() == 1


@test("one tick of the command claims a due link")
def _():
    _reset()
    link = _link()
    worker = _Worker()
    try:
        def claimed():
            connection.close()
            run = Run.objects.filter(link=link).first()
            return run is not None and run.started_at is not None

        _wait(claimed, worker)
        assert worker.proc.poll() is None
        connection.close()
        run = Run.objects.get(link=link)
        assert run.started_at is not None
        assert Run.objects.filter(link=link).count() == 1
    finally:
        worker.stop()


@test("a second process exits while the lock is held and acquires it after the holder exits")
def _():
    _reset()
    holder = _Worker()
    try:
        _wait(lambda: _lock_pid() == holder.proc.pid, holder)
        blocked = subprocess.run(
            MANAGE,
            cwd=ROOT,
            env=_env(),
            capture_output=True,
            text=True,
            timeout=5,
        )
        assert blocked.returncode != 0
        assert "worker lock is held" in blocked.stderr
    finally:
        holder.stop()

    acquired = _Worker()
    try:
        _wait(lambda: _lock_pid() == acquired.proc.pid, acquired)
        assert acquired.proc.poll() is None
    finally:
        acquired.stop()
