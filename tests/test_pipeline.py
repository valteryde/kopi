import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path
from unittest.mock import patch

from ward import raises, test

from kopi.identity import decrypt, generate_identity
from kopi.pipeline import (
    BUFFER_SIZE,
    PipelineError,
    Producer,
    archive,
    archive_folder,
    marker_path,
    restore_folder,
)
from kopi.pipeline import _gnu_tar


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(BUFFER_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _secret_line(path: Path) -> bytes:
    for line in path.read_bytes().splitlines():
        if line.startswith(b"AGE-SECRET-KEY-"):
            return line
    raise AssertionError("identity file has no secret key")


def _archive_bytes(identity_path: Path, ciphertext: Path) -> bytes:
    source = ciphertext.open("rb")
    try:
        age = subprocess.Popen(
            ["age", "--decrypt", "--identity", os.fspath(identity_path)],
            stdin=source,
            stdout=subprocess.PIPE,
        )
        assert age.stdout is not None
        zstd = subprocess.Popen(
            ["zstd", "--decompress", "--stdout", "--quiet", "--single-thread"],
            stdin=age.stdout,
            stdout=subprocess.PIPE,
        )
        age.stdout.close()
        payload = zstd.stdout.read() if zstd.stdout is not None else b""
        zstd.wait()
        age.wait()
    finally:
        source.close()
    assert zstd.returncode == 0
    assert age.returncode == 0
    return payload


def _entry(listing: str, name: str) -> str:
    needle = "./" + name
    for line in listing.splitlines():
        head = line.split(" -> ", 1)[0]
        if head.split()[-1].rstrip("/") == needle:
            return line
    raise AssertionError(f"{name} missing from archive\n{listing}")


def _members(listing: str) -> set[str]:
    names = set()
    for line in listing.splitlines():
        head = line.split(" -> ", 1)[0]
        names.add(head.split()[-1].removeprefix("./").rstrip("/"))
    return names


@test("a folder tree round-trips contents, a symlink, and permissions")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        root_dir = Path(tmp)
        identity = generate_identity(root_dir / "secret.key")
        secret = _secret_line(identity.path)
        source = root_dir / "src"
        source.mkdir()
        readme = source / "readme.txt"
        readme.write_text("hello\n")
        os.chmod(readme, 0o640)
        script = source / "script.sh"
        script.write_text("#!/bin/sh\n")
        os.chmod(script, 0o750)
        nested_dir = source / "sub"
        nested_dir.mkdir()
        os.chmod(nested_dir, 0o750)
        (nested_dir / "nested.txt").write_text("nested\n")
        empty = source / "empty"
        empty.mkdir()
        os.chmod(empty, 0o750)
        (source / "link").symlink_to("readme.txt")
        outside = root_dir / "outside.txt"
        outside.write_bytes(b"outside-bytes")
        os.chmod(outside, 0o640)
        (source / "outlink").symlink_to(outside)
        (source / "secret.key").write_text("do-not-archive\n")
        (nested_dir / "secret.key").write_text("do-not-archive-nested\n")
        (source / "secret.key.bak").write_text("keep-me\n")

        ciphertext = root_dir / "work" / "run-1.age"
        previous = os.umask(0)
        try:
            with patch("kopi.pipeline.subprocess.Popen", wraps=subprocess.Popen) as popen:
                marker = archive_folder(source, identity, ciphertext, "run-1")
        finally:
            os.umask(previous)

        commands = [call.args[0] for call in popen.call_args_list]
        tar_create = next(argv for argv in commands if argv[0] == _gnu_tar() and "--create" in argv)
        assert "--format=posix" in tar_create
        assert "--dereference" not in tar_create
        assert "-h" not in tar_create
        zstd_compress = next(argv for argv in commands if argv[0] == "zstd" and "--decompress" not in argv)
        assert "--single-thread" in zstd_compress
        assert "-T0" not in zstd_compress
        age_encrypt = next(argv for argv in commands if argv[0] == "age" and "--encrypt" in argv)
        assert "--recipient" in age_encrypt
        assert "--identity" not in age_encrypt
        for argv in commands:
            blob = " ".join(argv).encode()
            assert secret not in blob
            assert b"AGE-SECRET-KEY-" not in blob

        assert stat.S_IMODE(ciphertext.stat().st_mode) == 0o600
        assert marker.run_id == "run-1"
        assert marker.size == ciphertext.stat().st_size
        assert marker.sha256 == _sha256(ciphertext)
        assert marker.path == marker_path(ciphertext)
        assert marker.path.parent == ciphertext.parent
        assert stat.S_IMODE(marker.path.stat().st_mode) == 0o600
        stored = json.loads(marker.path.read_text())
        assert stored == {"run_id": "run-1", "sha256": marker.sha256, "size": marker.size}

        payload = _archive_bytes(identity.path, ciphertext)
        assert b"do-not-archive" not in payload
        listing = subprocess.run(
            [_gnu_tar(), "--list", "--verbose", "--file=-"],
            input=payload,
            stdout=subprocess.PIPE,
            check=False,
        )
        assert listing.returncode == 0
        listing = listing.stdout.decode()
        assert _entry(listing, "readme.txt").startswith("-rw-r-----")
        assert _entry(listing, "script.sh").startswith("-rwxr-x---")
        assert _entry(listing, "empty").startswith("drwxr-x---")
        assert _entry(listing, "sub").startswith("drwxr-x---")
        assert _entry(listing, "link").startswith("l")
        assert " -> readme.txt" in _entry(listing, "link")
        assert " -> " in _entry(listing, "outlink")
        members = _members(listing)
        assert "secret.key" not in members
        assert "sub/secret.key" not in members
        assert "secret.key.bak" in members
        destination = root_dir / "restored"
        restored = restore_folder(ciphertext, identity, destination)
        assert restored == marker
        assert stat.S_IMODE(destination.stat().st_mode) == 0o700
        assert (destination / "readme.txt").read_text() == "hello\n"
        assert stat.S_IMODE((destination / "readme.txt").stat().st_mode) == 0o600
        assert (destination / "script.sh").read_text() == "#!/bin/sh\n"
        assert stat.S_IMODE((destination / "script.sh").stat().st_mode) == 0o600
        assert (destination / "sub" / "nested.txt").read_text() == "nested\n"
        assert stat.S_IMODE((destination / "sub" / "nested.txt").stat().st_mode) == 0o600
        assert stat.S_IMODE((destination / "sub").stat().st_mode) == 0o700
        assert stat.S_IMODE((destination / "empty").stat().st_mode) == 0o700
        assert (destination / "secret.key.bak").read_text() == "keep-me\n"
        assert not (destination / "secret.key").exists()
        assert not (destination / "sub" / "secret.key").exists()
        link = destination / "link"
        assert link.is_symlink()
        assert os.readlink(link) == "readme.txt"
        assert link.read_text() == "hello\n"
        outlink = destination / "outlink"
        assert outlink.is_symlink()
        assert os.readlink(outlink) == os.fspath(outside)
        assert stat.S_IMODE(outside.stat().st_mode) == 0o640
        assert outside.read_bytes() == b"outside-bytes"


def _stream_producer(total: int) -> Producer:
    script = (
        "import sys\n"
        f"chunk = b'x' * {BUFFER_SIZE}\n"
        f"left = {total}\n"
        "while left:\n"
        "    take = min(left, len(chunk))\n"
        "    sys.stdout.buffer.write(chunk[:take])\n"
        "    left -= take\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        bufsize=0,
    )
    return Producer([process])


@test("a stream larger than the buffer stays flat in memory")
def _():
    assert BUFFER_SIZE == 1024 * 1024
    total = BUFFER_SIZE * 8
    with tempfile.TemporaryDirectory() as tmp:
        root_dir = Path(tmp)
        identity = generate_identity(root_dir / "secret.key")
        ciphertext = root_dir / "stream.age"
        producer = _stream_producer(total)
        tracemalloc.start()
        try:
            marker = archive(producer, identity, ciphertext, "stream")
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert marker.size > BUFFER_SIZE
        assert marker.sha256 == _sha256(ciphertext)
        assert peak > 0
        assert peak < BUFFER_SIZE * 4, peak
        assert decrypt(identity, ciphertext.read_bytes()) == b"x" * total


@test("a killed producer leaves no marker")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        root_dir = Path(tmp)
        identity = generate_identity(root_dir / "secret.key")
        ciphertext = root_dir / "killed.age"
        process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import sys, time; sys.stdout.buffer.write(b'partial'); sys.stdout.buffer.flush(); time.sleep(30)",
            ],
            stdout=subprocess.PIPE,
            bufsize=0,
        )
        producer = Producer([process])
        box: dict = {}

        def run() -> None:
            try:
                box["marker"] = archive(producer, identity, ciphertext, "killed")
            except PipelineError as exc:
                box["error"] = exc

        worker = threading.Thread(target=run)
        worker.start()
        try:
            deadline = time.time() + 5
            while not ciphertext.exists() and time.time() < deadline:
                time.sleep(0.01)
            assert ciphertext.exists()
            producer.kill()
            worker.join(timeout=10)
        finally:
            producer.kill()
            producer.wait()
            worker.join(timeout=5)

        assert not worker.is_alive()
        assert isinstance(box.get("error"), PipelineError)
        assert "marker" not in box
        assert not marker_path(ciphertext).exists()
        assert not marker_path(ciphertext).with_name(marker_path(ciphertext).name + ".tmp").exists()


@test("include and exclude globs choose the archived tree and still skip secret.key")
def _():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        identity = generate_identity(root / "secret.key")
        source = root / "src"
        docs = source / "docs"
        nested = docs / "nested"
        nested.mkdir(parents=True)
        (docs / "keep.txt").write_text("keep\n")
        (docs / "skip.tmp").write_text("skip\n")
        (nested / "deep.txt").write_text("deep\n")
        (source / "other.txt").write_text("other\n")
        (source / "secret.key").write_text("do-not-archive\n")
        (docs / "secret.key").write_text("do-not-archive\n")
        (docs / "link").symlink_to("keep.txt")
        ciphertext = root / "work" / "run.age"
        archive_folder(
            source,
            identity,
            ciphertext,
            "filtered",
            includes="docs/**\n",
            excludes="*.tmp\n",
        )
        payload = _archive_bytes(identity.path, ciphertext)
        assert b"skip" not in payload
        assert b"other" not in payload
        assert b"do-not-archive" not in payload
        listing = subprocess.run(
            [_gnu_tar(), "--list", "--verbose", "--file=-"],
            input=payload,
            stdout=subprocess.PIPE,
            check=False,
        )
        assert listing.returncode == 0
        members = _members(listing.stdout.decode())
        assert {"docs", "docs/keep.txt", "docs/nested", "docs/nested/deep.txt", "docs/link"} <= members
        assert "docs/skip.tmp" not in members
        assert "other.txt" not in members
        assert "secret.key" not in members
        assert "docs/secret.key" not in members
        assert " -> keep.txt" in _entry(listing.stdout.decode(), "docs/link")
        missed = root / "missed.age"
        with raises(PipelineError):
            archive_folder(
                source,
                identity,
                missed,
                "missed",
                includes="missing/**\n",
            )
        assert not missed.exists()
