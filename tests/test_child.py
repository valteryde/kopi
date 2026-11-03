import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import tests.bootstrap  # noqa: F401
from tests.bootstrap import DATA_DIR
from django.db import connection
from django.utils import timezone
from ward import test

from kopi.child import Progress, execute, reconcile
from kopi.identity import generate_identity
from kopi.models import Link, Run, Source, Target
from kopi.pipeline import marker_path
from kopi.storage import FakeStorage, StorageError

ROOT = Path(__file__).resolve().parents[1]


def _name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _script(path: Path, body: str) -> str:
    path.write_text(body)
    os.chmod(path, 0o700)
    return str(path)


def _folder(tmp: Path) -> Path:
    source = tmp / "source"
    source.mkdir()
    (source / "hello.txt").write_text("hello")
    return source


def _run(
    tmp: Path,
    *,
    pre: str = "",
    post: str = "",
    pre_timeout: int = 30,
    post_timeout: int = 30,
    kind: str = Source.Kind.FOLDER,
) -> Run:
    source_dir = _folder(tmp) if kind == Source.Kind.FOLDER else None
    source = Source.objects.create(
        name=_name("src"),
        kind=kind,
        root_path=str(source_dir or ""),
        pg_host="127.0.0.1" if kind == Source.Kind.POSTGRES else "",
        pg_database="kopi" if kind == Source.Kind.POSTGRES else "",
        pg_user="kopi" if kind == Source.Kind.POSTGRES else "",
        pg_password="ciphertext" if kind == Source.Kind.POSTGRES else "",
    )
    target = Target.objects.create(name=_name("tgt"), bucket="kopi")
    link = Link.objects.create(
        source=source,
        target=target,
        cadence="0 3 * * *",
        timezone="Europe/Copenhagen",
        retention_kind=Link.Retention.COUNT,
        retention_count=3,
        pre_script_path=pre,
        post_script_path=post,
        pre_script_timeout=pre_timeout,
        post_script_timeout=post_timeout,
    )
    return Run.objects.create(link=link)


def _refresh(run: Run) -> Run:
    connection.close()
    run.refresh_from_db()
    return run


def _wait(predicate, timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("timed out")


class _HungStorage(FakeStorage):
    def __init__(self, stalled: threading.Event) -> None:
        super().__init__()
        self._stalled = stalled

    def put(self, key, path, *, checksum, on_progress=None):
        while not self._stalled.is_set():
            time.sleep(0.02)
        raise StorageError("cancelled")


def _kill_tree(pid: int) -> None:
    listed = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True)
    for line in listed.stdout.split():
        _kill_tree(int(line))
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


@test("a run archives, uploads, and deletes the local ciphertext")
def _():
    with tempfile_dir() as tmp:
        pre_flag = tmp / "pre"
        post_flag = tmp / "post"
        pre = _script(tmp / "pre.sh", f"#!/bin/bash\ntouch {pre_flag}\necho pre-out\n")
        post = _script(tmp / "post.sh", f"#!/bin/bash\ntouch {post_flag}\necho post-out\n")
        run = _run(tmp, pre=pre, post=post)
        identity = generate_identity(tmp / "secret.key")
        storage = FakeStorage()
        execute(run.pk, identity=identity, storage=storage, work_dir=tmp / "work")
        run = _refresh(run)
        assert pre_flag.is_file()
        assert post_flag.is_file()
        assert run.status == Run.Status.SUCCEEDED
        assert run.phase == Run.Phase.POST
        assert run.checksum
        assert run.object_name.startswith(f"{run.link.source.name}/")
        assert run.object_name.endswith(run.checksum)
        assert run.object_name.endswith("Z/" + run.checksum)
        listed = storage.list()
        assert [item.key for item in listed] == [run.object_name]
        assert listed[0].checksum == run.checksum
        assert listed[0].size == run.size
        assert not (tmp / "work" / str(run.pk) / "payload.age").exists()
        assert not marker_path(tmp / "work" / str(run.pk) / "payload.age").exists()
        assert "pre-out" in run.log
        assert "post-out" in run.log


@test("progress writes phase immediately and bytes at most once a second")
def _():
    with tempfile_dir() as tmp:
        run = _run(tmp)
        progress = Progress(run.pk, interval=1)
        progress.set_bytes(10, now=0)
        progress.set_bytes(20, now=0.2)
        stored = _refresh(run)
        assert stored.bytes_moved == 10
        assert stored.updated_at is not None
        progress.set_bytes(30, now=1.0)
        progress.set_phase(Run.Phase.UPLOAD, 0, now=1.0)
        stored = _refresh(run)
        assert stored.bytes_moved == 0
        assert stored.phase == Run.Phase.UPLOAD


@test("the pre script publishes the phase before it returns")
def _():
    with tempfile_dir() as tmp:
        ready = tmp / "ready"
        gate = tmp / "gate"
        pre = _script(
            tmp / "pre.sh",
            f"#!/bin/bash\ntouch {ready}\nwhile [ ! -f {gate} ]; do sleep 0.05; done\n",
        )
        run = _run(tmp, pre=pre)
        identity = generate_identity(tmp / "secret.key")
        box: dict = {}

        def run_child():
            try:
                execute(run.pk, identity=identity, storage=FakeStorage(), work_dir=tmp / "work")
            except Exception as exc:
                box["error"] = exc

        thread = threading.Thread(target=run_child)
        thread.start()
        _wait(lambda: ready.is_file())
        _wait(lambda: _refresh(run).phase == Run.Phase.PRE)
        assert _refresh(run).status == Run.Status.RUNNING
        gate.write_text("go")
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert "error" not in box
        assert _refresh(run).status == Run.Status.SUCCEEDED


@test("hook commands get only PATH, HOME, LANG, and the run id")
def _():
    with tempfile_dir() as tmp:
        script = _script(tmp / "pre.sh", "#!/bin/bash\nexit 0\n")
        run = _run(tmp, pre=script)
        identity = generate_identity(tmp / "secret.key")
        os.environ["PGPASSWORD"] = "db-password"
        os.environ["AGE_SECRET_KEY"] = "AGE-SECRET-KEY-TEST"
        try:
            with patch("subprocess.Popen", wraps=subprocess.Popen) as spy:
                execute(run.pk, identity=identity, storage=FakeStorage(), work_dir=tmp / "work")
        finally:
            del os.environ["PGPASSWORD"]
            del os.environ["AGE_SECRET_KEY"]
        bash_calls = [call for call in spy.call_args_list if call.args and call.args[0][:1] == ["bash"]]
        assert bash_calls
        for call in spy.call_args_list:
            argv = call.args[0]
            env = call.kwargs.get("env")
            assert env is not None
            blob = " ".join(argv) + "\n" + "\n".join(f"{key}={value}" for key, value in env.items())
            assert "db-password" not in blob
            assert "AGE-SECRET-KEY-TEST" not in blob
        env = bash_calls[0].kwargs["env"]
        assert set(env) <= {"PATH", "HOME", "LANG", "KOPI_RUN_ID"}
        assert env["KOPI_RUN_ID"] == str(run.pk)
        assert "TMPDIR" not in env


@test("a pre-script failure skips the archive, the upload, and the post script")
def _():
    with tempfile_dir() as tmp:
        post_flag = tmp / "post"
        pre = _script(tmp / "pre.sh", "#!/bin/bash\necho no >&2\nexit 1\n")
        post = _script(tmp / "post.sh", f"#!/bin/bash\ntouch {post_flag}\n")
        run = _run(tmp, pre=pre, post=post)
        identity = generate_identity(tmp / "secret.key")
        storage = FakeStorage()
        execute(run.pk, identity=identity, storage=storage, work_dir=tmp / "work")
        run = _refresh(run)
        assert run.status == Run.Status.FAILED
        assert not post_flag.exists()
        assert storage.list() == []
        assert not (tmp / "work" / str(run.pk) / "payload.age").exists()
        assert "script exited 1" in run.log


@test("a pre-script timeout skips the archive and the post script")
def _():
    with tempfile_dir() as tmp:
        post_flag = tmp / "post"
        pre = _script(tmp / "pre.sh", "#!/bin/bash\nsleep 30\n")
        post = _script(tmp / "post.sh", f"#!/bin/bash\ntouch {post_flag}\n")
        run = _run(tmp, pre=pre, post=post, pre_timeout=1)
        identity = generate_identity(tmp / "secret.key")
        execute(run.pk, identity=identity, storage=FakeStorage(), work_dir=tmp / "work")
        run = _refresh(run)
        assert run.status == Run.Status.FAILED
        assert not post_flag.exists()
        assert "timed out" in run.log


@test("a path that is not a regular file fails the pre script")
def _():
    with tempfile_dir() as tmp:
        missing = tmp / "missing.sh"
        run = _run(tmp, pre=str(missing), post=str(tmp / "post.sh"))
        identity = generate_identity(tmp / "secret.key")
        execute(run.pk, identity=identity, storage=FakeStorage(), work_dir=tmp / "work")
        run = _refresh(run)
        assert run.status == Run.Status.FAILED
        assert "not a regular file" in run.log


@test("stdout and stderr are capped at 1 MiB")
def _():
    with tempfile_dir() as tmp:
        script = _script(
            tmp / "pre.sh",
            "#!/bin/bash\ndd if=/dev/zero bs=1024 count=2048 2>/dev/null | tr '\\0' A\n",
        )
        run = _run(tmp, pre=script)
        identity = generate_identity(tmp / "secret.key")
        execute(run.pk, identity=identity, storage=FakeStorage(), work_dir=tmp / "work")
        run = _refresh(run)
        assert run.status == Run.Status.SUCCEEDED
        assert len(run.log.encode()) <= 1024 * 1024
        assert len(run.log) < 2048 * 1024


@test("the post script runs when the upload fails and a later retry uploads the marker")
def _():
    with tempfile_dir() as tmp:
        post_flag = tmp / "post"
        post = _script(tmp / "post.sh", f"#!/bin/bash\ntouch {post_flag}\n")
        run = _run(tmp, post=post)
        identity = generate_identity(tmp / "secret.key")
        storage = FakeStorage()
        storage.fail_next_put()
        storage.fail_next_put()
        work = tmp / "work"
        execute(run.pk, identity=identity, storage=storage, work_dir=work)
        run = _refresh(run)
        assert run.status == Run.Status.FAILED
        assert post_flag.is_file()
        assert storage.list() == []
        ciphertext = work / str(run.pk) / "payload.age"
        assert ciphertext.is_file()
        assert marker_path(ciphertext).is_file()
        # The row is failed, so this is a new attempt that finds the marker by
        # being left running the way a killed upload would be.
        Run.objects.filter(pk=run.pk).update(
            status=Run.Status.RUNNING,
            phase=Run.Phase.UPLOAD,
            finished_at=None,
        )
        with patch("kopi.child.start_folder_producer") as started:
            reconcile(run.pk, identity=identity, storage=storage, work_dir=work)
            assert started.call_count == 0
        run = _refresh(run)
        assert run.status == Run.Status.SUCCEEDED
        assert [item.key for item in storage.list()] == [run.object_name]
        assert not ciphertext.exists()


@test("one failed put is retried and the run succeeds")
def _():
    with tempfile_dir() as tmp:
        run = _run(tmp)
        identity = generate_identity(tmp / "secret.key")
        storage = FakeStorage()
        storage.fail_next_put()
        execute(run.pk, identity=identity, storage=storage, work_dir=tmp / "work")
        assert len(storage.plans) == 2
        assert _refresh(run).status == Run.Status.SUCCEEDED


@test("a post-script failure keeps a successful upload and fails the run")
def _():
    with tempfile_dir() as tmp:
        post = _script(tmp / "post.sh", "#!/bin/bash\nexit 1\n")
        run = _run(tmp, post=post)
        identity = generate_identity(tmp / "secret.key")
        storage = FakeStorage()
        execute(run.pk, identity=identity, storage=storage, work_dir=tmp / "work")
        run = _refresh(run)
        assert run.status == Run.Status.FAILED
        assert [item.key for item in storage.list()] == [run.object_name]
        assert not (tmp / "work" / str(run.pk) / "payload.age").exists()
        assert "post script failed" in run.log


@test("a post-script timeout keeps a successful upload")
def _():
    with tempfile_dir() as tmp:
        post = _script(tmp / "post.sh", "#!/bin/bash\nsleep 30\n")
        run = _run(tmp, post=post, post_timeout=1)
        identity = generate_identity(tmp / "secret.key")
        storage = FakeStorage()
        execute(run.pk, identity=identity, storage=storage, work_dir=tmp / "work")
        run = _refresh(run)
        assert run.status == Run.Status.FAILED
        assert [item.key for item in storage.list()] == [run.object_name]
        assert "timed out" in run.log


@test("a stall fails the run and still runs the post script")
def _():
    with tempfile_dir() as tmp:
        post_flag = tmp / "post"
        post = _script(tmp / "post.sh", f"#!/bin/bash\ntouch {post_flag}\n")
        run = _run(tmp, post=post)
        identity = generate_identity(tmp / "secret.key")
        stalled = threading.Event()
        storage = _HungStorage(stalled)
        started = time.monotonic()
        execute(
            run.pk,
            identity=identity,
            storage=storage,
            work_dir=tmp / "work",
            stall_seconds=0.4,
            stalled=stalled,
        )
        assert time.monotonic() - started < 3
        run = _refresh(run)
        assert run.status == Run.Status.FAILED
        assert "stalled" in run.log
        assert post_flag.is_file()
        assert storage.list() == []


@test("no marker deletes the partial and runs the post script when pre succeeded")
def _():
    with tempfile_dir() as tmp:
        post_flag = tmp / "post"
        post = _script(tmp / "post.sh", f"#!/bin/bash\ntouch {post_flag}\n")
        run = _run(tmp, post=post)
        work = tmp / "work"
        partial = work / str(run.pk) / "payload.age"
        partial.parent.mkdir(parents=True)
        partial.write_bytes(b"partial")
        Run.objects.filter(pk=run.pk).update(
            status=Run.Status.RUNNING,
            phase=Run.Phase.ARCHIVE,
            started_at=timezone.now(),
        )
        identity = generate_identity(tmp / "secret.key")
        with patch("kopi.child.start_folder_producer") as started:
            reconcile(run.pk, identity=identity, storage=FakeStorage(), work_dir=work)
            assert started.call_count == 0
        run = _refresh(run)
        assert run.status == Run.Status.FAILED
        assert not partial.exists()
        assert post_flag.is_file()


@test("a pre script that did not finish is not followed by the post script")
def _():
    with tempfile_dir() as tmp:
        post_flag = tmp / "post"
        post = _script(tmp / "post.sh", f"#!/bin/bash\ntouch {post_flag}\n")
        run = _run(tmp, pre=str(tmp / "pre.sh"), post=post)
        _script(tmp / "pre.sh", "#!/bin/bash\nexit 0\n")
        work = tmp / "work"
        partial = work / str(run.pk) / "payload.age"
        partial.parent.mkdir(parents=True)
        partial.write_bytes(b"partial")
        Run.objects.filter(pk=run.pk).update(
            status=Run.Status.RUNNING,
            phase=Run.Phase.PRE,
            started_at=timezone.now(),
        )
        reconcile(
            run.pk,
            identity=generate_identity(tmp / "secret.key"),
            storage=FakeStorage(),
            work_dir=work,
        )
        assert _refresh(run).status == Run.Status.FAILED
        assert not partial.exists()
        assert not post_flag.exists()


@test("a finished upload runs the post script again without uploading")
def _():
    with tempfile_dir() as tmp:
        post_flag = tmp / "post"
        post = _script(tmp / "post.sh", f"#!/bin/bash\ntouch {post_flag}\n")
        run = _run(tmp, post=post)
        storage = FakeStorage()
        Run.objects.filter(pk=run.pk).update(
            status=Run.Status.RUNNING,
            phase=Run.Phase.POST,
            object_name="notes/2026-11-03T17:00:00Z/" + "ab" * 32,
            checksum="ab" * 32,
            started_at=timezone.now(),
        )
        reconcile(
            run.pk,
            identity=generate_identity(tmp / "secret.key"),
            storage=storage,
            work_dir=tmp / "work",
        )
        run = _refresh(run)
        assert run.status == Run.Status.SUCCEEDED
        assert post_flag.is_file()
        assert storage.plans == []


@test("a killed child leaves no marker and reconciliation runs the post script")
def _():
    with tempfile_dir() as tmp:
        bindir = tmp / "bin"
        bindir.mkdir()
        fake = bindir / "gtar"
        fake.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then\n'
            '  echo "tar (GNU tar) 1.35"\n'
            "  exit 0\n"
            "fi\n"
            "sleep 30\n"
        )
        os.chmod(fake, 0o755)
        pre_flag = tmp / "pre"
        post_flag = tmp / "post"
        pre = _script(tmp / "pre.sh", f"#!/bin/bash\ntouch {pre_flag}\n")
        post = _script(tmp / "post.sh", f"#!/bin/bash\ntouch {post_flag}\n")
        run = _run(tmp, pre=pre, post=post)
        identity = generate_identity(tmp / "secret.key")
        work = tmp / "work"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import sys\n"
                    "from pathlib import Path\n"
                    "import django\n"
                    "django.setup()\n"
                    "from kopi.child import execute\n"
                    "from kopi.identity import load_identity\n"
                    "from kopi.storage import FakeStorage\n"
                    "execute(int(sys.argv[1]), identity=load_identity(Path(sys.argv[2])), "
                    "storage=FakeStorage(), work_dir=Path(sys.argv[3]), stall_seconds=30)\n"
                ),
                str(run.pk),
                str(identity.path),
                str(work),
            ],
            cwd=ROOT,
            env={
                **os.environ,
                "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
                "DJANGO_SETTINGS_MODULE": "kopi.settings",
            },
            start_new_session=True,
        )
        try:
            _wait(lambda: _refresh(run).phase == Run.Phase.ARCHIVE)
            assert pre_flag.is_file()
            assert not post_flag.exists()
            _kill_tree(proc.pid)
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                _kill_tree(proc.pid)
                proc.wait(timeout=5)
        leftover = subprocess.run(["pgrep", "-f", str(fake)], capture_output=True, text=True)
        assert leftover.stdout.strip() == ""
        ciphertext = work / str(run.pk) / "payload.age"
        assert not marker_path(ciphertext).exists()
        with patch("kopi.child.start_folder_producer") as started:
            reconcile(run.pk, identity=identity, storage=FakeStorage(), work_dir=work)
            assert started.call_count == 0
        run = _refresh(run)
        assert run.status == Run.Status.FAILED
        assert post_flag.is_file()
        assert not ciphertext.exists()


@contextmanager
def tempfile_dir():
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)
