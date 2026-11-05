"""The same checks against the Compose harness.

`psql` is the sibling of the host `pg_dump`, so the probe opens a real
connection to the real server. The bucket is Garage, and the probe object is
written, read back, and removed there.
"""

import tests.bootstrap  # noqa: F401
from ward import skip, test

from kopi import checks
from kopi.postgres import psql_binary_for
from kopi.storage import CHECK_PREFIX
from tests.test_harness import (
    compose_is_down,
    listed_keys,
    pg_dump_binary,
    postgres_port,
    runtime_env,
    s3_client,
)


def _texts(report) -> str:
    return "\n".join(line.text for line in report.lines)


def _database(env, *, password: str, binary: str) -> checks.Report:
    return checks.database(
        host="127.0.0.1",
        port=postgres_port(env),
        pg_database=env["POSTGRES_DB"],
        user=env["POSTGRES_USER"],
        password=password,
        dump_binary=binary,
    )


@test(
    "the Postgres check answers the harness server, and a wrong password is named",
    tags=["integration"],
)
@skip("Compose is not up", when=compose_is_down)
def _():
    env = runtime_env()
    binary = pg_dump_binary()
    assert psql_binary_for(binary) != "psql", "the probe uses the psql beside that pg_dump"

    answered = _database(env, password=env["POSTGRES_PASSWORD"], binary=binary)
    assert answered.passed, _texts(answered)
    texts = _texts(answered)
    assert f"127.0.0.1:{postgres_port(env)} answered. {env['POSTGRES_DB']} opened" in texts
    assert "pg_dump 18" in texts

    refused = _database(env, password="not-the-password", binary=binary)
    assert not refused.passed
    assert _texts(refused) == "The user or the password was refused."

    # A pooler port is refused before a connection is opened for it.
    pooler = checks.database(
        host="127.0.0.1",
        port=6432,
        pg_database=env["POSTGRES_DB"],
        user=env["POSTGRES_USER"],
        password=env["POSTGRES_PASSWORD"],
        dump_binary=binary,
    )
    assert "PgBouncer" in _texts(pooler)


@test(
    "the target check writes to Garage, reads the probe back, and leaves nothing behind",
    tags=["integration"],
)
@skip("Compose is not up", when=compose_is_down)
def _():
    env = runtime_env()
    report = checks.s3(
        bucket=env["GARAGE_BUCKET"],
        endpoint=env["GARAGE_S3_ENDPOINT"],
        region=env["GARAGE_S3_REGION"],
        access_key=env["GARAGE_ACCESS_KEY_ID"],
        secret=env["GARAGE_SECRET_ACCESS_KEY"],
        path_style=True,
    )
    assert report.passed, _texts(report)
    assert "wrote, read back, and removed a probe object" in _texts(report)

    assert listed_keys(s3_client(env), env["GARAGE_BUCKET"], CHECK_PREFIX) == []

    # Garage answers AccessDenied where AWS answers SignatureDoesNotMatch. Both
    # are a refusal to use these credentials, and that is what the line says.
    wrong = checks.s3(
        bucket=env["GARAGE_BUCKET"],
        endpoint=env["GARAGE_S3_ENDPOINT"],
        region=env["GARAGE_S3_REGION"],
        access_key=env["GARAGE_ACCESS_KEY_ID"],
        secret="0" * 40,
        path_style=True,
    )
    assert not wrong.passed
    texts = _texts(wrong)
    assert "secret does not match" in texts or "cannot use" in texts, texts

    gone = checks.s3(
        bucket="no-such-bucket-kopi-check",
        endpoint=env["GARAGE_S3_ENDPOINT"],
        region=env["GARAGE_S3_REGION"],
        access_key=env["GARAGE_ACCESS_KEY_ID"],
        secret=env["GARAGE_SECRET_ACCESS_KEY"],
        path_style=True,
    )
    assert not gone.passed
    assert "no bucket named" in _texts(gone)
