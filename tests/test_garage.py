import os
from datetime import datetime, timezone
from pathlib import Path

from ward import skip, test

from kopi.identity import generate_identity
from kopi.pipeline import archive_folder, marker_path, restore_folder
from kopi.storage import S3Storage, object_key
from tests.test_harness import compose_is_down, runtime_env

ROOT = Path(__file__).resolve().parents[1]


def _signature(root: Path) -> dict[str, tuple[str, bytes | str]]:
    found = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        for name in dirnames:
            path = current / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                found[relative] = ("link", os.readlink(path))
        for name in filenames:
            path = current / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                found[relative] = ("link", os.readlink(path))
            else:
                found[relative] = ("file", path.read_bytes())
    return found


@test(
    "a folder archive uploaded to Garage downloads, decrypts, and matches the tree",
    tags=["integration"],
)
@skip("Compose is not up", when=compose_is_down)
def _():
    env = runtime_env()
    storage = S3Storage(
        bucket=env["GARAGE_BUCKET"],
        endpoint_url=env["GARAGE_S3_ENDPOINT"],
        region=env["GARAGE_S3_REGION"],
        access_key_id=env["GARAGE_ACCESS_KEY_ID"],
        secret_access_key=env["GARAGE_SECRET_ACCESS_KEY"],
        path_style=True,
    )
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        identity = generate_identity(root / "identity.key")
        source = root / "source"
        nested = source / "nested"
        nested.mkdir(parents=True)
        (source / "hello.txt").write_text("hello")
        (nested / "note.txt").write_text("note")
        (nested / "link").symlink_to("note.txt")
        ciphertext = root / "payload.age"
        marker = archive_folder(source, identity, ciphertext, "garage-round-trip")
        when = datetime(2026, 11, 3, 18, 15, tzinfo=timezone.utc)
        key = object_key("garage-folder", when, marker.sha256)
        try:
            seen = []
            storage.put(key, ciphertext, checksum=marker.sha256, on_progress=seen.append)
            assert seen[-1] == ciphertext.stat().st_size
            head = storage.client.head_object(Bucket=storage.bucket, Key=key)
            etag = head["ETag"].strip('"')
            assert etag != marker.sha256
            listed = storage.list("garage-folder/")
            assert [item.key for item in listed] == [key]
            assert listed[0].checksum == marker.sha256
            downloaded = root / "downloaded.age"
            storage.get(key, downloaded)
            assert downloaded.read_bytes() == ciphertext.read_bytes()
            restored_marker = marker_path(downloaded)
            restored_marker.write_bytes(marker.path.read_bytes())
            os.chmod(restored_marker, 0o600)
            restored = root / "restored"
            restore_folder(downloaded, identity, restored)
            assert _signature(restored) == _signature(source)
        finally:
            storage.delete(key)
        assert storage.list("garage-folder/") == []
