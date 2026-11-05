"""Object storage.

`put`, `get`, `list`, and `delete` are the interface. `FakeStorage` is the
hermetic stand-in. `S3Storage` is the boto3 client, including Garage.

An object name carries the source, the UTC timestamp, and the ciphertext
checksum, so a listing is readable without the app database. Multipart part
size is at least 8 MiB and at least `ceil(file size / 10000)`. A file above
5 TiB is rejected. In-flight parts stay inside a 256 MiB budget, which means
a larger part size sends one part at a time.
"""

import hashlib
import os
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

MIB = 1024 * 1024
MIN_PART_SIZE = 8 * MIB
MAX_PARTS = 10_000
MAX_OBJECT_BYTES = 5 * 1024**4
IN_FLIGHT_BUDGET = 256 * MIB

_KEY_RE = re.compile(
    r"^(?P<source>[^/]+)/(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)/(?P<checksum>[0-9a-f]{64})$"
)
_CHECKSUM_RE = re.compile(r"^[0-9a-f]{64}$")

Progress = Callable[[int], None]


class StorageError(Exception):
    pass


@dataclass(frozen=True)
class ObjectName:
    source: str
    timestamp: datetime
    checksum: str


@dataclass(frozen=True)
class StoredObject:
    key: str
    size: int
    checksum: str


@dataclass(frozen=True)
class UploadPlan:
    size: int
    part_size: int
    part_count: int
    in_flight: int


class Storage(Protocol):
    def put(
        self,
        key: str,
        path: Path,
        *,
        checksum: str,
        on_progress: Progress | None = None,
    ) -> None:
        """Upload one file. `checksum` is the SHA-256 hex digest of its bytes."""

    def get(self, key: str, path: Path) -> None:
        """Download one object onto `path`."""

    def list(self, prefix: str = "") -> list[StoredObject]:
        """List objects whose names start with `prefix`."""

    def delete(self, key: str) -> None:
        """Delete one object. A missing object is not an error."""


def object_key(source: str, when: datetime, checksum: str) -> str:
    """`source/YYYY-MM-DDTHH:MM:SSZ/checksum`, with the timestamp in UTC."""
    _validate_source(source)
    if not _CHECKSUM_RE.fullmatch(checksum):
        raise StorageError("checksum must be a sha256 hex digest")
    if when.tzinfo is None or when.utcoffset() is None:
        raise StorageError("timestamp must be timezone-aware")
    stamp = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{source}/{stamp}/{checksum}"


def parse_object_key(key: str) -> ObjectName:
    match = _KEY_RE.fullmatch(key)
    if match is None:
        raise StorageError("object name is not source/timestamp/checksum")
    source = match.group("source")
    _validate_source(source)
    try:
        timestamp = datetime.strptime(match.group("stamp"), "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise StorageError("object name has a bad timestamp") from exc
    return ObjectName(source=source, timestamp=timestamp, checksum=match.group("checksum"))


def upload_plan(size: int) -> UploadPlan:
    """Part size and how many parts may be in flight for a file of `size` bytes."""
    if size < 0:
        raise StorageError("size is negative")
    if size > MAX_OBJECT_BYTES:
        raise StorageError("object is larger than 5 TiB")
    part_size = max(MIN_PART_SIZE, _ceil_div(size, MAX_PARTS) if size else 0)
    part_count = 1 if size == 0 else _ceil_div(size, part_size)
    in_flight = max(1, IN_FLIGHT_BUDGET // part_size)
    return UploadPlan(
        size=size,
        part_size=part_size,
        part_count=part_count,
        in_flight=in_flight,
    )


def part_lengths(plan: UploadPlan) -> list[int]:
    """Byte length of each part. The last part is shorter when the size is not aligned."""
    if plan.size == 0:
        return [0]
    lengths = []
    remaining = plan.size
    while remaining:
        part = min(plan.part_size, remaining)
        lengths.append(part)
        remaining -= part
    return lengths


class FakeStorage:
    """In-memory storage.

    `fail_next_put` makes the next `put` raise and store nothing, so a retry
    can be tested without Garage. Recorded plans and part lengths are what
    the hermetic tests assert against.
    """

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._fail_puts = 0
        self._fail_deletes = 0
        self.plans: list[UploadPlan] = []
        self.part_lengths: list[list[int]] = []
        self.max_in_flight: list[int] = []

    def fail_next_put(self) -> None:
        self._fail_puts += 1

    def fail_next_delete(self) -> None:
        self._fail_deletes += 1

    def put(
        self,
        key: str,
        path: Path,
        *,
        checksum: str,
        on_progress: Progress | None = None,
    ) -> None:
        parsed = parse_object_key(key)
        if parsed.checksum != checksum:
            raise StorageError("object name does not carry the checksum")
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise StorageError("put requires a regular file")
        plan = upload_plan(path.stat().st_size)
        self.plans.append(plan)
        if self._fail_puts:
            self._fail_puts -= 1
            raise StorageError("put failed")
        data, peak, lengths = _read_parts(path, plan, on_progress)
        digest = hashlib.sha256(data).hexdigest()
        if digest != checksum:
            raise StorageError("checksum does not match the file")
        self.part_lengths.append(lengths)
        self.max_in_flight.append(peak)
        self._objects[key] = data

    def get(self, key: str, path: Path) -> None:
        parsed = parse_object_key(key)
        try:
            data = self._objects[key]
        except KeyError as exc:
            raise StorageError("object not found") from exc
        digest = hashlib.sha256(data).hexdigest()
        if digest != parsed.checksum:
            raise StorageError("checksum does not match the stored object")
        path = Path(path)
        if path.is_symlink():
            raise StorageError("destination is a symlink")
        if not path.parent.is_dir():
            raise StorageError("destination directory is missing")
        partial = path.with_name(f".{path.name}.partial")
        try:
            with partial.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(partial, 0o600)
            os.replace(partial, path)
        except Exception:
            partial.unlink(missing_ok=True)
            raise

    def list(self, prefix: str = "") -> list[StoredObject]:
        found = []
        for key in sorted(self._objects):
            if not key.startswith(prefix):
                continue
            parsed = parse_object_key(key)
            found.append(
                StoredObject(key=key, size=len(self._objects[key]), checksum=parsed.checksum)
            )
        return found

    def delete(self, key: str) -> None:
        if self._fail_deletes:
            self._fail_deletes -= 1
            raise StorageError("delete failed")
        self._objects.pop(key, None)


def _validate_source(source: str) -> None:
    if (
        not source
        or source in {".", ".."}
        or "/" in source
        or any(ord(char) < 32 for char in source)
    ):
        raise StorageError("invalid source name")


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def _read_parts(
    path: Path,
    plan: UploadPlan,
    on_progress: Progress | None,
) -> tuple[bytes, int, list[int]]:
    """Read `path` in plan-sized parts, keeping at most `plan.in_flight` buffered."""
    expected_lengths = part_lengths(plan)
    window: deque[bytes] = deque()
    written = bytearray()
    read_lengths: list[int] = []
    peak = 0
    with path.open("rb") as handle:
        index = 0
        while index < len(expected_lengths) or window:
            while index < len(expected_lengths) and len(window) < plan.in_flight:
                expected = expected_lengths[index]
                part = handle.read(expected)
                if len(part) != expected:
                    raise StorageError("short read")
                window.append(part)
                read_lengths.append(len(part))
                index += 1
                peak = max(peak, len(window))
            part = window.popleft()
            written.extend(part)
            if on_progress is not None:
                on_progress(len(written))
        if handle.read(1):
            raise StorageError("file grew while it was read")
    return bytes(written), peak, read_lengths


class S3Storage:
    """boto3 multipart client. The checksum is the caller's SHA-256, not the ETag."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str,
        region: str,
        access_key_id: str,
        secret_access_key: str,
        path_style: bool = True,
    ) -> None:
        import boto3

        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=region,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            config=client_config(path_style),
        )

    def put(
        self,
        key: str,
        path: Path,
        *,
        checksum: str,
        on_progress: Progress | None = None,
    ) -> None:
        parsed = parse_object_key(key)
        if parsed.checksum != checksum:
            raise StorageError("object name does not carry the checksum")
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise StorageError("put requires a regular file")
        plan = upload_plan(path.stat().st_size)
        if plan.size == 0:
            if hashlib.sha256(b"").hexdigest() != checksum:
                raise StorageError("checksum does not match the file")
            self.client.put_object(Bucket=self.bucket, Key=key, Body=b"")
            if on_progress is not None:
                on_progress(0)
            return
        upload = self.client.create_multipart_upload(Bucket=self.bucket, Key=key)
        upload_id = upload["UploadId"]
        parts = []
        sent = 0
        hasher = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for number, length in enumerate(part_lengths(plan), start=1):
                    blob = handle.read(length)
                    if len(blob) != length:
                        raise StorageError("short read")
                    hasher.update(blob)
                    response = self.client.upload_part(
                        Bucket=self.bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=number,
                        Body=blob,
                    )
                    parts.append({"ETag": response["ETag"], "PartNumber": number})
                    sent += len(blob)
                    if on_progress is not None:
                        on_progress(sent)
            if hasher.hexdigest() != checksum:
                raise StorageError("checksum does not match the file")
            self.client.complete_multipart_upload(
                Bucket=self.bucket,
                Key=key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except Exception:
            self.client.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)
            raise

    def get(self, key: str, path: Path) -> None:
        parsed = parse_object_key(key)
        path = Path(path)
        if path.is_symlink():
            raise StorageError("destination is a symlink")
        if not path.parent.is_dir():
            raise StorageError("destination directory is missing")
        partial = path.with_name(f".{path.name}.partial")
        hasher = hashlib.sha256()
        try:
            body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
            with partial.open("wb") as handle:
                while True:
                    chunk = body.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    hasher.update(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if hasher.hexdigest() != parsed.checksum:
                raise StorageError("checksum does not match the stored object")
            os.chmod(partial, 0o600)
            os.replace(partial, path)
        except Exception:
            partial.unlink(missing_ok=True)
            raise

    def list(self, prefix: str = "") -> list[StoredObject]:
        found = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for item in page.get("Contents", []):
                key = item["Key"]
                try:
                    parsed = parse_object_key(key)
                except StorageError:
                    continue
                found.append(StoredObject(key=key, size=item["Size"], checksum=parsed.checksum))
        found.sort(key=lambda item: item.key)
        return found

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


# A check writes one small object here and removes it again. The name is not
# `source/timestamp/checksum`, so `list` skips it and retention cannot see it.
CHECK_PREFIX = ".kopi-check/"


def client_config(
    path_style: bool,
    *,
    connect_timeout: int | None = None,
    read_timeout: int | None = None,
    max_attempts: int = 3,
):
    """The boto3 config every S3 client in Kopi builds. A check shortens the clocks.

    Signing is always `s3v4`: a garage or MinIO bucket reached with the default
    signature reads as a credential that does not match.
    """
    from botocore.config import Config

    clocks = {}
    if connect_timeout is not None:
        clocks["connect_timeout"] = connect_timeout
    if read_timeout is not None:
        clocks["read_timeout"] = read_timeout
    return Config(
        signature_version="s3v4",
        retries={"max_attempts": max_attempts, "mode": "standard"},
        s3={"addressing_style": "path" if path_style else "virtual"},
        **clocks,
    )


def probe_client(
    *,
    endpoint_url: str,
    region: str,
    access_key_id: str,
    secret_access_key: str,
    path_style: bool = True,
    connect_timeout: int = 5,
    read_timeout: int = 10,
):
    """A boto3 S3 client for one connectivity check.

    Short timeouts and a single attempt, because an operator waits inside a
    request. `S3Storage` keeps its own retry policy for a real upload.
    """
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=endpoint_url or None,
        region_name=region or None,
        aws_access_key_id=access_key_id or None,
        aws_secret_access_key=secret_access_key or None,
        config=client_config(
            path_style,
            connect_timeout=connect_timeout,
            read_timeout=read_timeout,
            max_attempts=1,
        ),
    )
