"""One run of one link: pre script, archive, upload, post script.

Tests call `execute` directly. `reconcile` finishes a run left in `running`.
A valid marker uploads again and does not archive again. No marker deletes
the partial and runs the post script when the pre script had succeeded.
"""

import os
import signal
import subprocess
import threading
import time
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from kopi.identity import Identity
from kopi.models import Run, Source
from kopi.pipeline import (
    Marker,
    PipelineError,
    archive,
    marker_path,
    read_marker,
    start_folder_producer,
)
from kopi.storage import StorageError, object_key

LOG_CAP = 1024 * 1024
STALL_INTERVAL = 15 * 60
_HOOK_ENV = ("PATH", "HOME", "LANG")


class HookError(Exception):
    pass


class _Stalled(Exception):
    pass


class _Context:
    def __init__(self, stalled: threading.Event | None = None) -> None:
        self.stalled = stalled if stalled is not None else threading.Event()
        self.producer = None
        self._lock = threading.Lock()
        self._bytes = 0
        self._token = 0
        self.watch = False

    def begin_phase(self) -> None:
        with self._lock:
            self._bytes = 0
            self._token += 1

    def note(self, total: int) -> None:
        with self._lock:
            self._bytes = total

    def sample(self) -> tuple[int, int, bool]:
        with self._lock:
            return self._bytes, self._token, self.watch


class Progress:
    """Write phase and bytes at most once a second, except on a phase change."""

    def __init__(self, run_id: int, interval: float = 1.0) -> None:
        self.run_id = run_id
        self.interval = interval
        self.phase = ""
        self.last_write = float("-inf")
        self._lock = threading.Lock()

    def set_phase(self, phase: str, total: int = 0, *, now: float | None = None) -> None:
        with self._lock:
            self.phase = phase
            self._write(total, now=now, force=True)

    def set_bytes(self, total: int, *, now: float | None = None, force: bool = False) -> None:
        with self._lock:
            self._write(total, now=now, force=force)

    def _write(self, total: int, *, now: float | None, force: bool) -> None:
        moment = time.monotonic() if now is None else now
        if not force and moment - self.last_write < self.interval:
            return
        self.last_write = moment
        with transaction.atomic():
            Run.objects.filter(pk=self.run_id).update(
                phase=self.phase,
                bytes_moved=total,
                updated_at=timezone.now(),
            )


class _Watcher:
    def __init__(self, ctx: _Context, progress: Progress, stall_seconds: float) -> None:
        self.ctx = ctx
        self.progress = progress
        self.stall_seconds = stall_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        from django.db import connection

        last_token = None
        last_bytes = None
        changed = time.monotonic()
        try:
            while not self._stop.wait(0.05):
                total, token, watch = self.ctx.sample()
                now = time.monotonic()
                if not watch:
                    last_token = None
                    last_bytes = None
                    changed = now
                    continue
                if token != last_token or total != last_bytes:
                    last_token = token
                    last_bytes = total
                    changed = now
                elif now - changed >= self.stall_seconds:
                    self.ctx.stalled.set()
                    producer = self.ctx.producer
                    if producer is not None:
                        producer.kill()
                    return
                self.progress.set_bytes(total, now=now)
        finally:
            connection.close()


class _Log:
    def __init__(self, existing: str = "", limit: int = LOG_CAP) -> None:
        self.limit = limit
        raw = existing.encode()
        self._buf = bytearray(raw[:limit])
        self._lock = threading.Lock()

    def add(self, data) -> None:
        if not data:
            return
        if isinstance(data, str):
            data = data.encode()
        with self._lock:
            room = self.limit - len(self._buf)
            if room <= 0:
                return
            self._buf.extend(data[:room])

    def text(self) -> str:
        with self._lock:
            return bytes(self._buf).decode("utf-8", "replace")


def execute(
    run_id: int,
    *,
    identity: Identity,
    storage,
    work_dir: Path,
    stall_seconds: float = STALL_INTERVAL,
    stalled: threading.Event | None = None,
) -> None:
    run = _load(run_id)
    link = run.link
    directory = _prepare(work_dir, run_id)
    ciphertext = directory / "payload.age"
    log = _Log()
    ctx = _Context(stalled)
    progress = Progress(run_id)
    watcher = _Watcher(ctx, progress, stall_seconds)
    watcher.start()
    failure = None
    pre_ok = False
    try:
        _mark_running(run_id)
        if link.pre_script_path.strip():
            _begin(ctx, progress, Run.Phase.PRE)
            run_hook(link.pre_script_path, link.pre_script_timeout, run_id, log)
        pre_ok = True
        _begin(ctx, progress, Run.Phase.ARCHIVE)
        ctx.watch = True
        try:
            marker = _archive(run, ciphertext, identity, directory / "tmp", ctx)
        finally:
            ctx.watch = False
        _begin(ctx, progress, Run.Phase.UPLOAD)
        ctx.watch = True
        try:
            _upload_marker(run_id, run.link.source.name, marker, storage, ctx)
        finally:
            ctx.watch = False
    except HookError as exc:
        failure = str(exc)
    except _Stalled:
        failure = "stalled"
    except Exception as exc:
        failure = "stalled" if ctx.stalled.is_set() else (str(exc) or "failed")
    finally:
        watcher.stop()
    _finish_with_post(run_id, link, log, pre_ok=pre_ok, failure=failure, progress=progress)


def reconcile(
    run_id: int,
    *,
    identity: Identity,
    storage,
    work_dir: Path,
    stall_seconds: float = STALL_INTERVAL,
) -> None:
    """Finish a run the child did not. A marker uploads; a partial is deleted."""
    run = _load(run_id)
    if run.status != Run.Status.RUNNING:
        return
    directory = _prepare(work_dir, run_id)
    ciphertext = directory / "payload.age"
    marker = read_marker(ciphertext)
    if marker is not None and marker.run_id != str(run_id):
        marker = None
    pre_ok = _pre_succeeded(run)
    log = _Log(run.log)
    ctx = _Context()
    progress = Progress(run_id)
    watcher = _Watcher(ctx, progress, stall_seconds)
    watcher.start()
    failure = None
    upload_done = bool(run.object_name)
    try:
        if marker is not None and not upload_done:
            _begin(ctx, progress, Run.Phase.UPLOAD)
            ctx.watch = True
            try:
                _upload_marker(run_id, run.link.source.name, marker, storage, ctx, when=run.started_at)
            finally:
                ctx.watch = False
            upload_done = True
        else:
            _delete_partial(ciphertext)
            if not upload_done:
                failure = "incomplete archive"
    except _Stalled:
        failure = "stalled"
    except Exception as exc:
        failure = "stalled" if ctx.stalled.is_set() else (str(exc) or "failed")
    finally:
        watcher.stop()
    _finish_with_post(
        run_id,
        run.link,
        log,
        pre_ok=pre_ok,
        failure=None if upload_done and failure == "incomplete archive" else failure,
        progress=progress,
        failed_override=not upload_done or (failure not in (None, "incomplete archive")),
    )


def run_hook(path_str: str, timeout: int, run_id: int, log: _Log) -> None:
    if not path_str or not str(path_str).strip():
        return
    path = Path(path_str)
    if not path.is_file():
        raise HookError(f"{path} is not a regular file")
    proc = subprocess.Popen(
        ["bash", os.fspath(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=hook_env(run_id),
        process_group=0,
    )
    captured = [bytearray(), bytearray()]

    def read(stream, sink: bytearray) -> None:
        try:
            while True:
                block = stream.read(65536)
                if not block:
                    break
                room = LOG_CAP - len(sink)
                if room > 0:
                    sink.extend(block[:room])
        finally:
            stream.close()

    threads = [
        threading.Thread(target=read, args=(proc.stdout, captured[0]), daemon=True),
        threading.Thread(target=read, args=(proc.stderr, captured[1]), daemon=True),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
    for thread in threads:
        thread.join()
    log.add(captured[0])
    log.add(captured[1])
    if timed_out:
        raise HookError("script timed out")
    if proc.returncode != 0:
        raise HookError(f"script exited {proc.returncode}")


def hook_env(run_id: int) -> dict[str, str]:
    env = {}
    for name in _HOOK_ENV:
        value = os.environ.get(name)
        if value:
            env[name] = value
    env["KOPI_RUN_ID"] = str(run_id)
    return env


def _archive(run, ciphertext: Path, identity: Identity, tmpdir: Path, ctx: _Context) -> Marker:
    source = run.link.source
    if source.kind != Source.Kind.FOLDER:
        raise PipelineError("unsupported source")
    ctx.producer = start_folder_producer(Path(source.root_path), tmpdir=tmpdir)
    try:
        if ctx.stalled.is_set():
            ctx.producer.kill()
            raise _Stalled("stalled")
        return archive(
            ctx.producer,
            identity,
            ciphertext,
            str(run.pk),
            on_bytes=ctx.note,
            tmpdir=tmpdir,
        )
    finally:
        ctx.producer = None


def _upload_marker(run_id, source_name, marker: Marker, storage, ctx: _Context, when=None) -> None:
    if when is None:
        when = Run.objects.get(pk=run_id).started_at or timezone.now()
    key = object_key(source_name, when, marker.sha256)
    last: Exception | None = None
    for _attempt in range(2):
        if ctx.stalled.is_set():
            raise _Stalled("stalled")
        try:
            storage.put(
                key,
                _ciphertext(marker),
                checksum=marker.sha256,
                on_progress=ctx.note,
            )
            _record_upload(run_id, key, marker)
            _delete_archive(marker)
            return
        except StorageError as exc:
            if ctx.stalled.is_set():
                raise _Stalled("stalled") from exc
            last = exc
    assert last is not None
    raise last


def _ciphertext(marker: Marker) -> Path:
    # The marker is the sibling `payload.age.marker`.
    name = marker.path.name
    if name.endswith(".marker"):
        return marker.path.with_name(name[: -len(".marker")])
    raise PipelineError("marker is not beside a ciphertext")


def _finish_with_post(run_id, link, log, *, pre_ok, failure, progress, failed_override=None) -> None:
    post_failed = None
    if pre_ok and link.post_script_path.strip():
        _begin(None, progress, Run.Phase.POST)
        try:
            run_hook(link.post_script_path, link.post_script_timeout, run_id, log)
        except HookError as exc:
            post_failed = str(exc)
            log.add(post_failed)
    if failure:
        log.add(failure)
    if post_failed:
        log.add("post script failed")
    failed = post_failed is not None or failure is not None
    if failed_override is not None:
        failed = failed_override or post_failed is not None
    _finish(run_id, Run.Status.FAILED if failed else Run.Status.SUCCEEDED, log)


def _begin(ctx: _Context | None, progress: Progress, phase: str) -> None:
    if ctx is not None:
        ctx.begin_phase()
    progress.set_phase(phase, 0)


def _pre_succeeded(run) -> bool:
    if run.phase in {Run.Phase.ARCHIVE, Run.Phase.UPLOAD, Run.Phase.POST}:
        return True
    if not run.link.pre_script_path.strip() and run.phase != Run.Phase.PRE:
        return True
    return False


def _load(run_id: int):
    return Run.objects.select_related("link__source", "link__target").get(pk=run_id)


def _prepare(work_dir: Path, run_id: int) -> Path:
    directory = Path(work_dir) / str(run_id)
    tmp = directory / "tmp"
    directory.mkdir(parents=True, exist_ok=True)
    tmp.mkdir(exist_ok=True)
    os.chmod(directory, 0o700)
    os.chmod(tmp, 0o700)
    return directory


def _mark_running(run_id: int) -> None:
    now = timezone.now()
    with transaction.atomic():
        current = Run.objects.get(pk=run_id)
        Run.objects.filter(pk=run_id).update(
            status=Run.Status.RUNNING,
            started_at=current.started_at or now,
            updated_at=now,
        )


def _record_upload(run_id: int, key: str, marker: Marker) -> None:
    with transaction.atomic():
        Run.objects.filter(pk=run_id).update(
            size=marker.size,
            checksum=marker.sha256,
            object_name=key,
            phase=Run.Phase.POST,
            updated_at=timezone.now(),
        )


def _delete_archive(marker: Marker) -> None:
    _ciphertext(marker).unlink(missing_ok=True)
    marker.path.unlink(missing_ok=True)


def _delete_partial(ciphertext: Path) -> None:
    ciphertext.unlink(missing_ok=True)
    marker_path(ciphertext).unlink(missing_ok=True)


def _finish(run_id: int, status: str, log: _Log) -> None:
    now = timezone.now()
    with transaction.atomic():
        Run.objects.filter(pk=run_id).update(
            status=status,
            finished_at=now,
            updated_at=now,
            log=log.text(),
        )
