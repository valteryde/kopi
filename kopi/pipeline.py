"""Stream a folder into age ciphertext and restore it.

tar writes a posix archive and zstd compresses it on one thread. The copy
loop feeds that stream to age through a fixed 1 MiB buffer and does not keep
the chunks. A second 1 MiB buffer reads age's stdout, because the ciphertext
has to be drained while the producer is still writing. That buffer updates
the SHA-256. The sibling marker is written only after both commands exit 0
and the ciphertext has been fsynced. No marker means the file is unfinished.
"""

import hashlib
import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from kopi.identity import IDENTITY_FILENAME, Identity, _cli_env
from kopi.pdeath import popen_kwargs

BUFFER_SIZE = 1024 * 1024

_GNU_TAR: str | None = None


class PipelineError(Exception):
    pass


@dataclass(frozen=True)
class Marker:
    run_id: str
    size: int
    sha256: str
    path: Path


class Producer:
    """Processes whose last stdout is the stream copied into age."""

    def __init__(self, processes: list[subprocess.Popen[bytes]]):
        if not processes:
            raise PipelineError("missing producer")
        stdout = processes[-1].stdout
        if stdout is None:
            raise PipelineError("producer has no stdout")
        self.processes = processes
        self.stdout = stdout

    def kill(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.kill()

    def wait(self) -> int:
        code = 0
        for process in self.processes:
            returned = process.wait()
            if code == 0 and returned != 0:
                code = returned
        return code


class _Written:
    def __init__(self) -> None:
        self.hasher = hashlib.sha256()
        self.size = 0


def marker_path(ciphertext: Path) -> Path:
    return ciphertext.with_name(ciphertext.name + ".marker")


def start_folder_producer(root: Path, *, tmpdir: Path | None = None) -> Producer:
    """GNU tar, posix format, into single-threaded zstd. Symlinks stay symlinks."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise PipelineError("folder source is not a directory")
    tar = _popen(_tar_create(root), stdout=subprocess.PIPE, tmpdir=tmpdir)
    try:
        assert tar.stdout is not None
        zstd = _popen(
            ["zstd", "--stdout", "--quiet", "--single-thread"],
            stdin=tar.stdout,
            stdout=subprocess.PIPE,
            tmpdir=tmpdir,
        )
    except Exception:
        tar.kill()
        tar.wait()
        raise
    tar.stdout.close()
    return Producer([tar, zstd])


def archive_folder(
    root: Path,
    identity: Identity,
    ciphertext: Path,
    run_id: str,
    *,
    on_bytes=None,
    tmpdir: Path | None = None,
) -> Marker:
    return archive(
        start_folder_producer(root, tmpdir=tmpdir),
        identity,
        ciphertext,
        run_id,
        on_bytes=on_bytes,
        tmpdir=tmpdir,
    )


def archive(
    producer: Producer,
    identity: Identity,
    ciphertext: Path,
    run_id: str,
    *,
    on_bytes=None,
    tmpdir: Path | None = None,
) -> Marker:
    """Copy `producer` into age. Write the marker only when that finishes cleanly."""
    try:
        if not isinstance(run_id, str) or run_id == "":
            raise PipelineError("run id is missing")
        ciphertext = Path(ciphertext)
        if ciphertext.is_symlink() or ciphertext.exists():
            raise PipelineError("ciphertext already exists")
        ciphertext.parent.mkdir(parents=True, exist_ok=True)
        age = _popen(
            ["age", "--encrypt", "--recipient", identity.recipient],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            tmpdir=tmpdir,
        )
    except Exception:
        _discard(producer)
        raise

    fd = -1
    thread: threading.Thread | None = None
    succeeded = False
    try:
        fd = os.open(ciphertext, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        written = _Written()
        errors: list[BaseException] = []
        thread = threading.Thread(
            target=_pump_ciphertext,
            args=(age.stdout, fd, written, errors, producer),
            name="kopi-ciphertext",
        )
        thread.start()
        try:
            assert age.stdin is not None
            _copy_into(producer.stdout, age.stdin, on_bytes)
        except Exception as exc:
            errors.append(exc)
        finally:
            _close(age.stdin)
        thread.join()
        age_code = age.wait()
        producer_code = producer.wait()
        if errors or age_code != 0 or producer_code != 0:
            cause = errors[0] if errors else None
            raise PipelineError(
                f"archive failed: producer {producer_code}, age {age_code}"
            ) from cause
        os.fsync(fd)
        os.fchmod(fd, 0o600)
        if os.fstat(fd).st_size != written.size:
            raise PipelineError("archive failed")
        os.close(fd)
        fd = -1
        digest = written.hasher.hexdigest()
        path = _write_marker(ciphertext, run_id, written.size, digest)
        succeeded = True
        return Marker(run_id=run_id, size=written.size, sha256=digest, path=path)
    finally:
        if not succeeded:
            if age.poll() is None:
                age.kill()
            producer.kill()
            if thread is not None and thread.is_alive():
                thread.join()
        age.wait()
        producer.wait()
        _close(producer.stdout)
        _close(age.stdin)
        _close(age.stdout)
        if fd >= 0:
            os.close(fd)


def restore_folder(ciphertext: Path, identity: Identity, destination: Path) -> Marker:
    """Decrypt one marked archive into `destination`.

    Files are mode 0600 and directories are mode 0700. Symlinks are extracted
    as symlinks and are not followed when the modes are tightened.
    """
    ciphertext = Path(ciphertext)
    destination = Path(destination)
    marker = _load_marker(ciphertext)
    _verify_ciphertext(ciphertext, marker)
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise PipelineError("restore destination is not a directory")
    destination.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(destination, 0o700)

    processes: list[subprocess.Popen[bytes]] = []
    source = ciphertext.open("rb")
    try:
        age = _popen(
            ["age", "--decrypt", "--identity", os.fspath(identity.path)],
            stdin=source,
            stdout=subprocess.PIPE,
        )
        processes.append(age)
        assert age.stdout is not None
        zstd = _popen(
            ["zstd", "--decompress", "--stdout", "--quiet", "--single-thread"],
            stdin=age.stdout,
            stdout=subprocess.PIPE,
        )
        processes.append(zstd)
        age.stdout.close()
        assert zstd.stdout is not None
        tar = _popen(_tar_extract(destination), stdin=zstd.stdout)
        processes.append(tar)
        zstd.stdout.close()
        codes = [process.wait() for process in reversed(processes)]
        if any(code != 0 for code in codes):
            raise PipelineError("restore failed")
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
        source.close()
    _restrict_permissions(destination)
    return marker


def _tar_create(root: Path) -> list[str]:
    # Permissions are stored by default. Symlinks are archived as symlinks.
    # A file named secret.key is skipped wherever it sits in the tree.
    name = IDENTITY_FILENAME
    return [
        _gnu_tar(),
        "--format=posix",
        "--create",
        "--file=-",
        "--directory",
        os.fspath(root),
        "--exclude",
        name,
        "--exclude",
        f"*/{name}",
        ".",
    ]


def _tar_extract(destination: Path) -> list[str]:
    return [
        _gnu_tar(),
        "--extract",
        "--file=-",
        "--format=posix",
        "--directory",
        os.fspath(destination),
    ]


def _gnu_tar() -> str:
    global _GNU_TAR
    if _GNU_TAR is not None:
        return _GNU_TAR
    for name in ("gtar", "tar"):
        path = shutil.which(name)
        if path is None:
            continue
        result = subprocess.run(
            [path, "--version"],
            capture_output=True,
            env=_cli_env(),
            check=False,
            **popen_kwargs(),
        )
        if result.returncode == 0 and result.stdout.startswith(b"tar (GNU tar)"):
            _GNU_TAR = path
            return path
    raise PipelineError("GNU tar is not installed")


def _copy_into(source, sink, on_bytes=None) -> None:
    """Copy `source` into age's stdin. The buffer is reused and not retained."""
    buf = bytearray(BUFFER_SIZE)
    view = memoryview(buf)
    total = 0
    while True:
        count = source.readinto(buf)
        if count is None:
            raise PipelineError("archive failed")
        if count == 0:
            return
        _write_stream(sink, view[:count])
        total += count
        if on_bytes is not None:
            on_bytes(total)


def _pump_ciphertext(
    stdout,
    fd: int,
    written: _Written,
    errors: list[BaseException],
    producer: Producer,
) -> None:
    """Write age's stdout to disk and hash those bytes with the same buffer."""
    buf = bytearray(BUFFER_SIZE)
    view = memoryview(buf)
    try:
        while True:
            count = stdout.readinto(buf)
            if count is None:
                raise PipelineError("archive failed")
            if count == 0:
                break
            piece = view[:count]
            _write_fd(fd, piece)
            written.hasher.update(piece)
            written.size += count
    except Exception as exc:
        errors.append(exc)
        # Unblock the producer copy. A failed ciphertext write must not leave
        # both sides waiting on each other.
        producer.kill()
    finally:
        _close(stdout)


def _write_stream(sink, data: memoryview) -> None:
    while len(data):
        written = sink.write(data)
        if not written:
            raise PipelineError("archive failed")
        data = data[written:]


def _write_fd(fd: int, data: memoryview) -> None:
    while len(data):
        written = os.write(fd, data)
        if written == 0:
            raise PipelineError("archive failed")
        data = data[written:]


def _write_marker(ciphertext: Path, run_id: str, size: int, digest: str) -> Path:
    path = marker_path(ciphertext)
    temporary = path.with_name(path.name + ".tmp")
    payload = json.dumps(
        {"run_id": run_id, "size": size, "sha256": digest},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8") + b"\n"
    temporary.unlink(missing_ok=True)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        _write_fd(fd, memoryview(payload))
        os.fsync(fd)
    except Exception:
        os.close(fd)
        temporary.unlink(missing_ok=True)
        raise
    os.close(fd)
    try:
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    _fsync_directory(path.parent)
    return path


def _load_marker(ciphertext: Path) -> Marker:
    path = marker_path(ciphertext)
    if path.is_symlink() or not path.is_file():
        raise PipelineError("missing marker")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PipelineError("bad marker") from exc
    if not isinstance(data, dict):
        raise PipelineError("bad marker")
    run_id = data.get("run_id")
    size = data.get("size")
    digest = data.get("sha256")
    if not isinstance(run_id, str) or run_id == "":
        raise PipelineError("bad marker")
    if type(size) is not int or size < 0:
        raise PipelineError("bad marker")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        raise PipelineError("bad marker")
    return Marker(run_id=run_id, size=size, sha256=digest, path=path)


def _verify_ciphertext(ciphertext: Path, marker: Marker) -> None:
    if ciphertext.is_symlink() or not ciphertext.is_file():
        raise PipelineError("missing ciphertext")
    if ciphertext.stat().st_size != marker.size:
        raise PipelineError("size mismatch")
    digest = hashlib.sha256()
    with ciphertext.open("rb") as handle:
        while True:
            chunk = handle.read(BUFFER_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    if digest.hexdigest() != marker.sha256:
        raise PipelineError("checksum mismatch")


def _restrict_permissions(root: Path) -> None:
    os.chmod(root, 0o700)
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        kept: list[str] = []
        for name in dirnames:
            path = Path(directory, name)
            if path.is_symlink():
                continue
            os.chmod(path, 0o700)
            kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            path = Path(directory, name)
            if path.is_symlink() or not path.is_file():
                continue
            os.chmod(path, 0o600)


def _fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _discard(producer: Producer) -> None:
    producer.kill()
    _close(producer.stdout)
    producer.wait()


def read_marker(ciphertext: Path) -> Marker | None:
    """Return the marker when its size and hash match the ciphertext."""
    try:
        marker = _load_marker(ciphertext)
        _verify_ciphertext(ciphertext, marker)
    except PipelineError:
        return None
    return marker


def _popen(argv: list[str], *, tmpdir: Path | None = None, **kwargs) -> subprocess.Popen[bytes]:
    env = _cli_env()
    if tmpdir is not None:
        env["TMPDIR"] = os.fspath(tmpdir)
    try:
        return subprocess.Popen(argv, env=env, bufsize=0, **popen_kwargs(), **kwargs)
    except FileNotFoundError as exc:
        raise PipelineError(f"{argv[0]} is not installed") from exc


def _close(stream) -> None:
    if stream is None or stream.closed:
        return
    try:
        stream.close()
    except BrokenPipeError:
        pass
    except OSError:
        pass
