import hashlib
import json
import os
import re
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path

from ward import fixture, skip, test

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env.example"


def load_env(path=ENV_FILE):
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def runtime_env():
    """Credentials from .env.example, then .env, then the process environment.

    Compose interpolates the published port from .env. The integration tests
    have to use that same port.
    """
    values = load_env()
    local = ROOT / ".env"
    if local.is_file():
        values.update(load_env(local))
    for key in list(values):
        if key in os.environ:
            values[key] = os.environ[key]
    return values


def write_hashed(path, chunks):
    """Write chunks to a file and return the SHA-256 of the bytes written."""
    hasher = hashlib.sha256()
    with path.open("wb") as handle:
        for chunk in chunks:
            handle.write(chunk)
            hasher.update(chunk)
        handle.flush()
        os.fsync(handle.fileno())
    return hasher.hexdigest()


def compose_ps():
    result = subprocess.run(
        ["docker", "compose", "ps", "-a", "--format", "json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "docker compose ps failed")
    text = result.stdout.strip()
    if not text:
        return []
    if text.startswith("["):
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else [parsed]
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def compose_is_down():
    try:
        return not compose_ps()
    except (OSError, subprocess.TimeoutExpired, RuntimeError, json.JSONDecodeError):
        return True


def service_row(name):
    matches = [row for row in compose_ps() if row.get("Service") == name]
    if not matches:
        raise AssertionError(f"{name} is not in this Compose project")
    return matches[0]


def postgres_port(env):
    return int(env["POSTGRES_PORT"])


def pg_dump_binary():
    candidates = [
        "pg_dump",
        "/opt/homebrew/opt/libpq/bin/pg_dump",
        "/opt/homebrew/opt/postgresql@18/bin/pg_dump",
        "/usr/lib/postgresql/18/bin/pg_dump",
    ]
    for candidate in candidates:
        if candidate != "pg_dump" and not Path(candidate).is_file():
            continue
        try:
            output = subprocess.check_output(
                [candidate, "--version"], text=True, stderr=subprocess.STDOUT
            )
        except (OSError, subprocess.CalledProcessError):
            continue
        match = re.search(r"(\d+)\.", output)
        if match and int(match.group(1)) >= 18:
            return candidate
    raise AssertionError("host pg_dump must be from Postgres 18 or newer")


def psql_query(env, sql):
    completed = subprocess.run(
        [
            "psql",
            "-h",
            "127.0.0.1",
            "-p",
            str(postgres_port(env)),
            "-U",
            env["POSTGRES_USER"],
            "-d",
            env["POSTGRES_DB"],
            "-v",
            "ON_ERROR_STOP=1",
            "-At",
            "-c",
            sql,
        ],
        env={
            **os.environ,
            "PGPASSWORD": env["POSTGRES_PASSWORD"],
            "PGCONNECT_TIMEOUT": "5",
        },
        capture_output=True,
        text=True,
        timeout=20,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.strip() or completed.stdout)
    return completed.stdout


def s3_client(env):
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=env["GARAGE_S3_ENDPOINT"],
        aws_access_key_id=env["GARAGE_ACCESS_KEY_ID"],
        aws_secret_access_key=env["GARAGE_SECRET_ACCESS_KEY"],
        region_name=env["GARAGE_S3_REGION"],
        config=Config(
            signature_version="s3v4",
            retries={"max_attempts": 3, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )


def listed_keys(client, bucket, prefix):
    response = client.list_objects_v2(Bucket=bucket, Prefix=prefix)
    return [item["Key"] for item in response.get("Contents", [])]


@fixture
def env():
    return runtime_env()


@test("devdata has data and work directories on the host")
def _():
    assert (ROOT / "devdata" / "data").is_dir()
    assert (ROOT / "devdata" / "work").is_dir()


@test("compose bind-mounts ./devdata and gives Postgres and Garage named volumes")
def _():
    text = (ROOT / "compose.yaml").read_text()
    assert "./devdata:/devdata" in text
    assert "postgres_data:/var/lib/postgresql" in text
    assert "garage_meta:/var/lib/garage/meta" in text
    assert "garage_data:/var/lib/garage/data" in text
    assert "127.0.0.1:${POSTGRES_PORT:-5432}:5432" in text
    assert "127.0.0.1:${GARAGE_S3_PORT:-3900}:3900" in text


@test("garage is one node with replication factor 1 and the dev credentials")
def _():
    env_file = load_env()
    toml = (ROOT / "harness" / "garage" / "garage.toml").read_text()
    assert "replication_factor = 1" in toml
    assert 's3_region = "garage"' in toml
    assert env_file["GARAGE_RPC_SECRET"] in toml
    assert env_file["GARAGE_ADMIN_TOKEN"] in toml
    key_id = env_file["GARAGE_ACCESS_KEY_ID"]
    secret = env_file["GARAGE_SECRET_ACCESS_KEY"]
    assert key_id.startswith("GK") and len(key_id) == 26
    assert len(secret) == 64
    assert len(env_file["GARAGE_RPC_SECRET"]) == 64
    assert env_file["POSTGRES_PORT"] == "5432"
    assert env_file["GARAGE_BUCKET"] == "kopi"


@test("the checksum of a written file is the sha256 of those bytes")
def _():
    chunks = [b"kopi", b"-", b"harness"]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "object.bin"
        digest = write_hashed(path, chunks)
        written = path.read_bytes()
    assert written == b"kopi-harness"
    assert digest == hashlib.sha256(written).hexdigest()
    assert "-" not in digest
    assert digest != "d41d8cd98f00b204e9800998ecf8427e-1"


@test("the harness database has the seed rows", tags=["integration"])
@skip("Compose is not up", when=compose_is_down)
def _(env=env):
    row = service_row("postgres")
    assert row.get("State") == "running"
    assert row.get("Health") == "healthy"
    bodies = psql_query(env, "SELECT body FROM kopi_notes ORDER BY id")
    assert bodies.splitlines() == ["alpha", "beta", "gamma"]


@test("pg_dump of the harness database succeeds and contains the seed rows", tags=["integration"])
@skip("Compose is not up", when=compose_is_down)
def _(env=env):
    completed = subprocess.run(
        [
            pg_dump_binary(),
            "-h",
            "127.0.0.1",
            "-p",
            str(postgres_port(env)),
            "-U",
            env["POSTGRES_USER"],
            "-d",
            env["POSTGRES_DB"],
            "--no-password",
        ],
        env={
            **os.environ,
            "PGPASSWORD": env["POSTGRES_PASSWORD"],
            "PGCONNECT_TIMEOUT": "5",
        },
        capture_output=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    dump = completed.stdout.decode()
    assert "kopi_notes" in dump
    assert "alpha" in dump
    assert "beta" in dump
    assert "gamma" in dump


@test("the garage bucket exists", tags=["integration"])
@skip("Compose is not up", when=compose_is_down)
def _(env=env):
    row = service_row("garage")
    assert row.get("State") == "running"
    assert row.get("Health") == "healthy"
    init = service_row("garage-init")
    assert init.get("State") == "exited"
    assert init.get("ExitCode") == 0
    with socket.create_connection(("127.0.0.1", 3900), timeout=5):
        pass
    s3_client(env).head_bucket(Bucket=env["GARAGE_BUCKET"])


@test(
    "a multipart upload reads back and the checksum is the sha256, not the etag",
    tags=["integration"],
)
@skip("Compose is not up", when=compose_is_down)
def _(env=env):
    client = s3_client(env)
    bucket = env["GARAGE_BUCKET"]
    key = f"harness/{uuid.uuid4().hex}"
    plain_key = f"harness/put-{uuid.uuid4().hex}"
    plain = b"put-get-list-delete"
    chunks = [b"kopi harness ", b"phase zero ", b"0123456789"]
    try:
        client.put_object(Bucket=bucket, Key=plain_key, Body=plain)
        assert client.get_object(Bucket=bucket, Key=plain_key)["Body"].read() == plain
        assert plain_key in listed_keys(client, bucket, plain_key)
        client.delete_object(Bucket=bucket, Key=plain_key)
        assert plain_key not in listed_keys(client, bucket, plain_key)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "object.bin"
            checksum = write_hashed(path, chunks)
            upload = client.create_multipart_upload(Bucket=bucket, Key=key)
            upload_id = upload["UploadId"]
            try:
                with path.open("rb") as handle:
                    part = client.upload_part(
                        Bucket=bucket,
                        Key=key,
                        UploadId=upload_id,
                        PartNumber=1,
                        Body=handle,
                    )
                completed = client.complete_multipart_upload(
                    Bucket=bucket,
                    Key=key,
                    UploadId=upload_id,
                    MultipartUpload={"Parts": [{"ETag": part["ETag"], "PartNumber": 1}]},
                )
            except Exception:
                client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=upload_id)
                raise
            etag = completed["ETag"].strip('"')
            downloaded = client.get_object(Bucket=bucket, Key=key)["Body"].read()
        assert downloaded == b"".join(chunks)
        assert checksum == hashlib.sha256(downloaded).hexdigest()
        assert checksum != etag
        assert key in listed_keys(client, bucket, key)
    finally:
        for object_key in (key, plain_key):
            try:
                client.delete_object(Bucket=bucket, Key=object_key)
            except Exception:
                pass
    assert key not in listed_keys(client, bucket, key)
