"""Claim runs and supervise their processes.

One tick inserts a pending row for each due link that has nothing in
progress, then hands runs to the child. Rows already running go first.
A new claim waits for a free slot and for free space on the work
directory. The claim transaction commits before the child is called.
Each tick sends failure mail and the digest before it claims. A mail
error does not stop the claims.

`serve` holds an exclusive flock on the data volume. On startup, and
after a child exits, running rows are reconciled before any new claim.
SIGTERM stops new claims, signals each child session, waits for those
processes to exit, and returns. Rows left running stay running.
"""

import errno
import fcntl
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from django.db import transaction

from kopi.models import Link, Run
from kopi.notify import notify
from kopi.pdeath import PARENT_FD_ENV
from kopi.schedule import is_due
from kopi.settings import BASE_DIR, data_directory, work_directory

DEFAULT_SLOTS = 1
# `tick` stays at 0 unless a caller passes a floor. The worker process uses
# `configured_disk_floor`, which defaults to 64 MiB.
DEFAULT_DISK_FLOOR = 0
CONFIGURED_DISK_FLOOR = 64 * 1024 * 1024

_BUSY = (Run.Status.PENDING, Run.Status.RUNNING)


class _Stopping(Exception):
    """Raised from the SIGTERM handler so the sleep ends."""


def tick(
    now: datetime,
    *,
    child: Callable[[Run], None],
    slots: int = DEFAULT_SLOTS,
    disk_floor: int = DEFAULT_DISK_FLOOR,
    work_dir: Path | None = None,
    claimable: Callable[[], bool] | None = None,
    settled: Callable[[], None] | None = None,
    send: Callable | None = None,
) -> None:
    """Insert due rows and hand claimed runs to `child`.

    `now` is timezone-aware. `slots` is how many runs may be in progress.
    `disk_floor` is the free bytes the work directory must have before a
    pending row is promoted. A row that is already running is handed over
    either way. When `claimable` returns false, no further row is promoted.
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be timezone-aware")

    notify(now, send=send)
    _insert_due(now)
    running = list(
        Run.objects.filter(status=Run.Status.RUNNING).order_by("started_at", "pk")
    )
    for run in running:
        child(run)
    if settled is not None:
        settled()

    free = slots - len(running)
    if free <= 0:
        return
    directory = Path(work_dir) if work_dir is not None else work_directory()
    if not _has_room(directory, disk_floor):
        return

    claimed = []
    for _ in range(free):
        if claimable is not None and not claimable():
            break
        run = _claim_next(now)
        if run is None:
            break
        claimed.append(run)
    for run in claimed:
        child(run)


def _insert_due(now: datetime) -> None:
    for link in Link.objects.order_by("pk"):
        if not is_due(link, now):
            continue
        with transaction.atomic():
            busy = Run.objects.filter(link=link, status__in=_BUSY).exists()
            if busy:
                continue
            Run.objects.create(link=link, status=Run.Status.PENDING)


def _claim_next(now: datetime) -> Run | None:
    """Promote the oldest pending row whose link is not already running.

    The transaction ends before this returns, so the caller can start the
    child without holding it.
    """
    with transaction.atomic():
        running_links = Run.objects.filter(status=Run.Status.RUNNING).values("link_id")
        candidate = (
            Run.objects.filter(status=Run.Status.PENDING, link__enabled=True)
            .exclude(link_id__in=running_links)
            .order_by("created_at", "pk")
            .first()
        )
        if candidate is None:
            return None
        updated = Run.objects.filter(pk=candidate.pk, status=Run.Status.PENDING).update(
            status=Run.Status.RUNNING,
            started_at=now,
            updated_at=now,
        )
        if not updated:
            return None
        run_id = candidate.pk
    return Run.objects.get(pk=run_id)


def configured_disk_floor(environ=None) -> int:
    """Free bytes required before a new claim. `KOPI_DISK_FLOOR=0` turns it off."""
    source = os.environ if environ is None else environ
    raw = source.get("KOPI_DISK_FLOOR")
    if raw is None or not raw.strip():
        return CONFIGURED_DISK_FLOOR
    try:
        value = int(raw.strip(), 10)
    except ValueError as exc:
        raise ValueError("KOPI_DISK_FLOOR must be a number of bytes") from exc
    if value < 0:
        raise ValueError("KOPI_DISK_FLOOR must be a number of bytes")
    return value


def _has_room(work_dir: Path, disk_floor: int) -> bool:
    work_dir.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(work_dir).free >= disk_floor


def worker_lock_path() -> Path:
    return data_directory() / "worker.lock"


def acquire_worker_lock(path: Path | None = None):
    """Take the exclusive worker lock, or return None when another process holds it.

    The returned file stays open for the process lifetime. Closing it releases
    the flock. This is not a database transaction.
    """
    target = Path(path) if path is not None else worker_lock_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    handle = target.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if isinstance(exc, BlockingIOError) or exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
            return None
        raise
    handle.seek(0)
    handle.truncate()
    handle.write(f"{os.getpid()}\n")
    handle.flush()
    os.fsync(handle.fileno())
    return handle


def serve(
    child: Callable[[Run], None],
    *,
    sleep_seconds: float,
    now: Callable[[], datetime],
    before_tick: Callable[[], list[int] | None] | None = None,
    reconcile: Callable[[list[int]], None] | None = None,
    on_stop: Callable[[], None] | None = None,
) -> bool:
    """Loop a tick, then sleep, until the process exits or SIGTERM arrives.

    Returns false when another worker holds the lock. Running rows are
    reconciled before the first claim, and again when `before_tick` reports
    that a child has exited. SIGTERM runs `on_stop` and returns true. The
    rows it leaves running stay running.
    """
    lock = acquire_worker_lock()
    if lock is None:
        return False
    # Closing `lock` releases the flock, so this reference stays until exit.
    stopping = False
    previous = signal.getsignal(signal.SIGTERM)

    def _term(signum, frame):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        raise _Stopping

    signal.signal(signal.SIGTERM, _term)
    try:
        try:
            if reconcile is not None:
                reconcile(_running_ids())
            while True:
                if stopping:
                    break
                exited: list[int] = []
                if before_tick is not None:
                    found = before_tick()
                    if found:
                        exited = list(found)
                if reconcile is not None and exited:
                    reconcile(exited)
                if stopping:
                    break

                def settled() -> None:
                    if before_tick is None or reconcile is None:
                        return
                    found = before_tick()
                    if found:
                        reconcile(list(found))

                tick(
                    now(),
                    child=child,
                    work_dir=work_directory(),
                    disk_floor=configured_disk_floor(),
                    claimable=lambda: not stopping,
                    settled=settled,
                )
                if stopping:
                    break
                time.sleep(sleep_seconds)
        except _Stopping:
            stopping = True
        if stopping and on_stop is not None:
            on_stop()
    finally:
        signal.signal(signal.SIGTERM, previous)
    return True


def _running_ids() -> list[int]:
    return list(
        Run.objects.filter(status=Run.Status.RUNNING).order_by("pk").values_list("pk", flat=True)
    )


_CHILD_ENV = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "DJANGO_SETTINGS_MODULE",
    "KOPI_DATA_DIR",
    "KOPI_WORK_DIR",
    "TMPDIR",
)


class Children:
    """Live `runchild` processes. The claim transaction has already ended."""

    def __init__(self) -> None:
        self._procs: dict[int, subprocess.Popen] = {}
        self._writes: dict[int, int] = {}
        self._exited: list[int] = []

    def reap(self) -> list[int]:
        """Collect exited children and return their run ids."""
        self._poll()
        found = self._exited
        self._exited = []
        return found

    def hand(self, run: Run) -> None:
        self._poll()
        if run.pk in self._exited:
            return
        current = self._procs.get(run.pk)
        if current is not None:
            if current.poll() is None:
                return
            self._collect(run.pk)
            if run.pk not in self._exited:
                self._exited.append(run.pk)
            return
        self._spawn(run.pk)

    def stop(self) -> None:
        """Signal each child session and wait until it has exited."""
        for proc in list(self._procs.values()):
            if proc.poll() is None:
                self._signal_tree(proc.pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        for run_id in list(self._procs):
            proc = self._procs.get(run_id)
            if proc is None:
                continue
            remaining = deadline - time.monotonic()
            try:
                proc.wait(timeout=max(0.1, remaining))
            except subprocess.TimeoutExpired:
                self._signal_tree(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
            self._collect(run_id)

    def _signal_tree(self, pid: int, sig: int) -> None:
        # Hooks run in their own process group, inside this session. Signal
        # those groups first, then the group that holds tar, zstd, age, and
        # pg_dump.
        for child in self._direct_children(pid):
            self._signal(child, sig)
        self._signal(pid, sig)

    def _direct_children(self, pid: int) -> list[int]:
        try:
            listed = subprocess.run(
                ["pgrep", "-P", str(pid)],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return []
        found = []
        for line in listed.stdout.split():
            try:
                found.append(int(line))
            except ValueError:
                continue
        return found

    def _signal(self, pid: int, sig: int) -> None:
        try:
            os.killpg(pid, sig)
        except OSError:
            try:
                os.kill(pid, sig)
            except OSError:
                pass

    def _poll(self) -> None:
        for run_id, proc in list(self._procs.items()):
            if proc.poll() is not None:
                self._collect(run_id)
                if run_id not in self._exited:
                    self._exited.append(run_id)

    def _spawn(self, run_id: int) -> None:
        read_fd, write_fd = os.pipe()
        os.set_inheritable(read_fd, True)
        env = {name: os.environ[name] for name in _CHILD_ENV if name in os.environ}
        env[PARENT_FD_ENV] = str(read_fd)
        manage = BASE_DIR / "manage.py"
        try:
            proc = subprocess.Popen(
                [sys.executable, os.fspath(manage), "runchild", str(run_id)],
                start_new_session=True,
                pass_fds=(read_fd,),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
            )
        except Exception:
            os.close(read_fd)
            os.close(write_fd)
            raise
        os.close(read_fd)
        self._procs[run_id] = proc
        self._writes[run_id] = write_fd

    def _collect(self, run_id: int) -> None:
        proc = self._procs.pop(run_id, None)
        write_fd = self._writes.pop(run_id, None)
        if write_fd is not None:
            os.close(write_fd)
        if proc is not None:
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)
