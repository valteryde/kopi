import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import tests.bootstrap  # noqa: F401
from ward import skip, test

from kopi.child import execute
from kopi.identity import encrypt_secret, generate_identity
from kopi.models import Link, Run, Source, Target
from kopi.pipeline import marker_path
from kopi.postgres import pooler_reason, restore_database
from kopi.storage import FakeStorage, S3Storage
from tests.test_harness import compose_is_down, pg_dump_binary, runtime_env


def _run(tmp: Path, *, binary: str, password: str, identity) -> Run:
    source = Source.objects.create(
        name=f"pg-{uuid.uuid4().hex[:8]}",
        kind=Source.Kind.POSTGRES,
        pg_host="127.0.0.1",
        pg_port=5432,
        pg_database="kopi",
        pg_user="kopi",
        pg_password=encrypt_secret(identity, password),
        pg_dump_binary=binary,
    )
    target = Target.objects.create(name=f"tgt-{uuid.uuid4().hex[:8]}", bucket="kopi")
    link = Link.objects.create(
        source=source,
        target=target,
        cadence="0 3 * * *",
        timezone="Europe/Copenhagen",
        retention_kind=Link.Retention.COUNT,
        retention_count=3,
    )
    return Run.objects.create(link=link)


def _fake_dump(path: Path, record: Path, *, exit_code: int) -> None:
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "pgpass = os.environ.get('PGPASSFILE', '')\n"
        f"Path({str(record)!r}).write_text(json.dumps({{\n"
        "    'argv': sys.argv,\n"
        "    'env': dict(os.environ),\n"
        "    'pgpass': Path(pgpass).read_text() if pgpass else '',\n"
        "    'pgpass_path': pgpass,\n"
        "    'mode': oct(Path(pgpass).stat().st_mode & 0o777) if pgpass else '',\n"
        "}))\n"
        "sys.stdout.buffer.write(b'PGDMP')\n"
        f"raise SystemExit({exit_code})\n"
    )
    os.chmod(path, 0o755)


def _assert_secret_stays_off_argv(record: dict, password: str) -> None:
    assert password not in record["argv"]
    assert "--no-password" in record["argv"]
    assert "-Fc" in record["argv"]
    assert "PGPASSWORD" not in record["env"]
    assert all(password not in value for value in record["env"].values())
    assert password in record["pgpass"]
    assert record["mode"] == "0o600"
    assert not Path(record["pgpass_path"]).exists()


@test("a pooler host or port is named as a pooler, and a plain server is not")
def _():
    assert pooler_reason("pgbouncer", 5432) == "host 'pgbouncer' looks like a connection pooler"
    assert (
        pooler_reason("Postgres-Pooler-01", 5432)
        == "host 'Postgres-Pooler-01' looks like a connection pooler"
    )
    assert pooler_reason("db.internal", 6432) == "port 6432 is the PgBouncer default"
    assert pooler_reason("db.internal", 9999) == "port 9999 is the PgPool-II default"
    assert pooler_reason("db.internal", 5000) == "port 5000 is the Odyssey default"
    assert pooler_reason("db.internal", 5432) is None
    assert pooler_reason("postgres", 5432) is None
    assert pooler_reason("poolhouse", 5432) is None
    assert pooler_reason("", 5432) is None
    assert pooler_reason("db.internal", None) is None
    assert pooler_reason("db.internal", "5432") is None
    assert pooler_reason("db.internal", "bad") is None


@test("pg_dump is invoked without the password and the pgpass file does not survive")
def _():
    import tempfile

    password = "super-secret-db"
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        identity = generate_identity(tmp / "secret.key")
        record_path = tmp / "record.json"
        binary = tmp / "pg_dump"
        _fake_dump(binary, record_path, exit_code=0)
        run = _run(tmp, binary=str(binary), password=password, identity=identity)
        work = tmp / "work"
        execute(run.pk, identity=identity, storage=FakeStorage(), work_dir=work)
        run.refresh_from_db()
        assert run.status == Run.Status.SUCCEEDED
        recorded = json.loads(record_path.read_text())
        _assert_secret_stays_off_argv(recorded, password)
        assert not any(path.name == "pgpass" for path in work.rglob("*"))
        assert password not in run.log


@test("a failed pg_dump still deletes the pgpass file")
def _():
    import tempfile

    password = "super-secret-db"
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        identity = generate_identity(tmp / "secret.key")
        record_path = tmp / "record.json"
        binary = tmp / "pg_dump"
        _fake_dump(binary, record_path, exit_code=1)
        run = _run(tmp, binary=str(binary), password=password, identity=identity)
        work = tmp / "work"
        execute(run.pk, identity=identity, storage=FakeStorage(), work_dir=work)
        run.refresh_from_db()
        assert run.status == Run.Status.FAILED
        recorded = json.loads(record_path.read_text())
        _assert_secret_stays_off_argv(recorded, password)
        assert not any(path.name == "pgpass" for path in work.rglob("*"))


def _psql(env, database: str, sql: str) -> str:
    completed = subprocess.run(
        [
            "psql",
            "-h",
            "127.0.0.1",
            "-p",
            str(env["POSTGRES_PORT"]),
            "-U",
            env["POSTGRES_USER"],
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
            "-At",
            "-c",
            sql,
        ],
        env={**os.environ, "PGPASSWORD": env["POSTGRES_PASSWORD"]},
        capture_output=True,
        text=True,
        timeout=20,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.strip() or completed.stdout)
    return completed.stdout


def _rows(env, database: str) -> list[str]:
    return _psql(env, database, "SELECT body FROM kopi_notes ORDER BY id").splitlines()


@test(
    "a Postgres dump uploaded to Garage restores into a second database",
    tags=["integration"],
)
@skip("Compose is not up", when=compose_is_down)
def _():
    import tempfile

    env = runtime_env()
    password = env["POSTGRES_PASSWORD"]
    restore_name = "kopi_restore_" + uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        identity = generate_identity(tmp / "secret.key")
        binary = pg_dump_binary()
        run = _run(tmp, binary=binary, password=password, identity=identity)
        run.link.source.pg_port = int(env["POSTGRES_PORT"])
        run.link.source.pg_database = env["POSTGRES_DB"]
        run.link.source.pg_user = env["POSTGRES_USER"]
        run.link.source.save()
        storage = S3Storage(
            bucket=env["GARAGE_BUCKET"],
            endpoint_url=env["GARAGE_S3_ENDPOINT"],
            region=env["GARAGE_S3_REGION"],
            access_key_id=env["GARAGE_ACCESS_KEY_ID"],
            secret_access_key=env["GARAGE_SECRET_ACCESS_KEY"],
            path_style=True,
        )
        work = tmp / "work"
        try:
            execute(run.pk, identity=identity, storage=storage, work_dir=work)
            run.refresh_from_db()
            assert run.status == Run.Status.SUCCEEDED, run.log
            assert not any(path.name == "pgpass" for path in work.rglob("*"))
            downloaded = tmp / "dump.age"
            storage.get(run.object_name, downloaded)
            marker = marker_path(downloaded)
            marker.write_text(
                json.dumps(
                    {"run_id": str(run.pk), "sha256": run.checksum, "size": run.size},
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
            os.chmod(marker, 0o600)
            _psql(env, env["POSTGRES_DB"], f'CREATE DATABASE "{restore_name}"')
            restore_database(
                downloaded,
                identity,
                binary=binary,
                host="127.0.0.1",
                port=int(env["POSTGRES_PORT"]),
                user=env["POSTGRES_USER"],
                database=restore_name,
                password=password,
                tmpdir=tmp / "restore-tmp",
            )
            assert _rows(env, restore_name) == _rows(env, env["POSTGRES_DB"])
            assert _rows(env, restore_name) == ["alpha", "beta", "gamma"]
            assert not any(path.name == "pgpass" for path in (tmp / "restore-tmp").rglob("*"))
        finally:
            try:
                storage.delete(run.object_name)
            except Exception:
                pass
            _psql(
                env,
                env["POSTGRES_DB"],
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{restore_name}' AND pid <> pg_backend_pid()",
            )
            _psql(env, env["POSTGRES_DB"], f'DROP DATABASE IF EXISTS "{restore_name}"')
