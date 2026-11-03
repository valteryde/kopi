"""Claim runs for the worker.

One tick inserts a pending row for each due link that has nothing in
progress, then hands runs to a stand-in child. Rows already running go
first. A new claim waits for a free slot and for free space on the work
directory. The claim transaction commits before the child is called.
This module does not start a process.

`serve` holds an exclusive flock on the data volume and loops the tick,
then sleeps. The lock lasts until the process exits.
"""

import errno
import fcntl
import os
import shutil
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from django.db import transaction

from kopi.models import Link, Run
from kopi.schedule import is_due
from kopi.settings import data_directory, work_directory

DEFAULT_SLOTS = 1
DEFAULT_DISK_FLOOR = 0

_BUSY = (Run.Status.PENDING, Run.Status.RUNNING)


def tick(
    now: datetime,
    *,
    child: Callable[[Run], None],
    slots: int = DEFAULT_SLOTS,
    disk_floor: int = DEFAULT_DISK_FLOOR,
    work_dir: Path | None = None,
) -> None:
    """Insert due rows and hand claimed runs to `child`.

    `now` is timezone-aware. `slots` is how many runs may be in progress.
    `disk_floor` is the free bytes the work directory must have before a
    pending row is promoted. A row that is already running is handed over
    either way.
    """
    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise ValueError("now must be timezone-aware")

    _insert_due(now)
    running = list(
        Run.objects.filter(status=Run.Status.RUNNING).order_by("started_at", "pk")
    )
    for run in running:
        child(run)

    free = slots - len(running)
    if free <= 0:
        return
    directory = Path(work_dir) if work_dir is not None else work_directory()
    if not _has_room(directory, disk_floor):
        return

    claimed = []
    for _ in range(free):
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
            Run.objects.filter(status=Run.Status.PENDING)
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
) -> bool:
    """Loop a tick, then sleep, until the process exits.

    Returns false when another worker holds the lock.
    """
    lock = acquire_worker_lock()
    if lock is None:
        return False
    # Closing `lock` releases the flock, so this reference stays until exit.
    while True:
        tick(now(), child=child, work_dir=work_directory())
        time.sleep(sleep_seconds)
