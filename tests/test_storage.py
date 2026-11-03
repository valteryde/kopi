import hashlib
import os
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ward import raises, test

from kopi.storage import (
    IN_FLIGHT_BUDGET,
    MAX_OBJECT_BYTES,
    MAX_PARTS,
    MIN_PART_SIZE,
    FakeStorage,
    StorageError,
    object_key,
    parse_object_key,
    part_lengths,
    upload_plan,
)

MIB = 1024 * 1024


def _checksum(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _key(payload: bytes, source: str = "notes", when: datetime | None = None) -> str:
    if when is None:
        when = datetime(2026, 11, 3, 17, 0, tzinfo=timezone.utc)
    return object_key(source, when, _checksum(payload))


@test("an object name carries the source, the timestamp, and the checksum")
def _():
    copenhagen = timezone(timedelta(hours=1))
    when = datetime(2026, 11, 3, 18, 0, tzinfo=copenhagen)
    checksum = "ab" * 32
    key = object_key("notes", when, checksum)
    assert key == "notes/2026-11-03T17:00:00Z/" + checksum

    parsed = parse_object_key(key)
    assert parsed.source == "notes"
    assert parsed.timestamp == datetime(2026, 11, 3, 17, 0, tzinfo=timezone.utc)
    assert parsed.checksum == checksum

    with raises(StorageError):
        object_key("a/b", when, checksum)
    with raises(StorageError):
        object_key("notes", when, "ABC")
    with raises(StorageError):
        object_key("notes", datetime(2026, 11, 3, 17, 0), checksum)
    with raises(StorageError):
        parse_object_key("notes/2026-13-03T17:00:00Z/" + checksum)
    with raises(StorageError):
        parse_object_key("notes/2026-11-03T17:00:00Z/" + "ab" * 31)


@test("part size is at least 8 MiB and at least ceil(file size / 10000)")
def _():
    assert MIN_PART_SIZE == 8 * MIB
    assert MAX_PARTS == 10_000
    assert IN_FLIGHT_BUDGET == 256 * MIB

    one = upload_plan(1)
    assert one.part_size == 8 * MIB
    assert one.part_count == 1
    assert one.in_flight == 32
    assert one.in_flight * one.part_size == IN_FLIGHT_BUDGET

    at_cap = upload_plan(8 * MIB * MAX_PARTS)
    assert at_cap.part_size == 8 * MIB
    assert at_cap.part_count == MAX_PARTS
    assert at_cap.in_flight == 32

    over = upload_plan(8 * MIB * MAX_PARTS + 1)
    assert over.part_size == 8 * MIB + 1
    assert over.part_size == (8 * MIB * MAX_PARTS + 1 + MAX_PARTS - 1) // MAX_PARTS
    assert over.part_count == MAX_PARTS
    assert over.part_count <= MAX_PARTS

    wider = upload_plan(16 * MIB * MAX_PARTS)
    assert wider.part_size == 16 * MIB
    assert wider.in_flight == 16
    assert wider.in_flight * wider.part_size <= IN_FLIGHT_BUDGET

    one_at_a_time = upload_plan((256 * MIB + 1) * MAX_PARTS)
    assert one_at_a_time.part_size == 256 * MIB + 1
    assert one_at_a_time.in_flight == 1
    assert one_at_a_time.part_count == MAX_PARTS

    full = upload_plan(MAX_OBJECT_BYTES)
    lengths = part_lengths(full)
    assert full.part_count == len(lengths) <= MAX_PARTS
    assert sum(lengths) == MAX_OBJECT_BYTES
    assert max(lengths) == full.part_size
    assert full.in_flight == 1

    with tempfile.TemporaryDirectory() as tmp:
        payload = b"a" * (8 * MIB) + b"b"
        path = Path(tmp) / "cipher"
        path.write_bytes(payload)
        storage = FakeStorage()
        seen = []
        storage.put(_key(payload), path, checksum=_checksum(payload), on_progress=seen.append)
        plan = storage.plans[-1]
        assert plan == upload_plan(len(payload))
        assert storage.part_lengths[-1] == [8 * MIB, 1]
        assert storage.max_in_flight[-1] == 2
        assert storage.max_in_flight[-1] <= plan.in_flight
        assert seen == [8 * MIB, len(payload)]
        destination = Path(tmp) / "restored"
        storage.get(_key(payload), destination)
        assert destination.read_bytes() == payload


@test("a file above 5 TiB is rejected")
def _():
    assert MAX_OBJECT_BYTES == 5 * 1024**4
    with raises(StorageError) as too_big:
        upload_plan(MAX_OBJECT_BYTES + 1)
    assert "5 TiB" in str(too_big.raised)

    storage = FakeStorage()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "huge"
        path.touch()
        os.truncate(path, MAX_OBJECT_BYTES + 1)
        key = object_key(
            "notes",
            datetime(2026, 11, 3, 17, 0, tzinfo=timezone.utc),
            "ab" * 32,
        )
        with raises(StorageError) as rejected:
            storage.put(key, path, checksum="ab" * 32)
        assert "5 TiB" in str(rejected.raised)
        assert storage.list() == []
        assert storage.part_lengths == []


@test("list, get, and delete round-trip one object")
def _():
    notes = b"ciphertext-notes"
    other = b"ciphertext-other"
    when = datetime(2026, 11, 3, 17, 0, tzinfo=timezone.utc)
    later = datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc)
    storage = FakeStorage()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        notes_path = root / "notes"
        other_path = root / "other"
        notes_path.write_bytes(notes)
        other_path.write_bytes(other)
        notes_key = _key(notes, source="notes", when=when)
        other_key = _key(other, source="db", when=later)
        seen = []
        storage.put(notes_key, notes_path, checksum=_checksum(notes), on_progress=seen.append)
        storage.put(other_key, other_path, checksum=_checksum(other))
        assert seen == [len(notes)]
        assert storage.plans[0].part_size == MIN_PART_SIZE
        assert storage.part_lengths[0] == [len(notes)]

        listed = storage.list()
        assert [(item.key, item.size, item.checksum) for item in listed] == [
            (other_key, len(other), _checksum(other)),
            (notes_key, len(notes), _checksum(notes)),
        ]
        assert [item.key for item in storage.list("notes/")] == [notes_key]
        assert storage.list("notes-other/") == []

        previous = os.umask(0)
        try:
            destination = root / "downloaded"
            storage.get(notes_key, destination)
        finally:
            os.umask(previous)
        assert destination.read_bytes() == notes
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600

        storage.delete(notes_key)
        storage.delete(notes_key)
        assert [item.key for item in storage.list()] == [other_key]
        with raises(StorageError):
            storage.get(notes_key, root / "missing")
        assert not (root / "missing").exists()

        notes_path.write_bytes(b"tampered")
        with raises(StorageError):
            storage.put(notes_key, notes_path, checksum=_checksum(notes))
        assert [item.key for item in storage.list()] == [other_key]


@test("a put can fail once and the retry stores the file")
def _():
    payload = b"retry-me"
    storage = FakeStorage()
    storage.fail_next_put()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cipher"
        path.write_bytes(payload)
        key = _key(payload)
        seen = []
        with raises(StorageError) as failed:
            storage.put(key, path, checksum=_checksum(payload), on_progress=seen.append)
        assert str(failed.raised) == "put failed"
        assert seen == []
        assert storage.list() == []
        assert storage.part_lengths == []

        storage.put(key, path, checksum=_checksum(payload), on_progress=seen.append)
        destination = Path(tmp) / "back"
        storage.get(key, destination)
        assert destination.read_bytes() == payload
        assert seen == [len(payload)]
