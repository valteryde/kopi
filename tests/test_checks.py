"""The check lines an operator gets before a source, a target, or a link is saved.

`psql` is a stub script that records what it was given, so a probe can be
watched without a server. The S3 client is a fake object that answers the
calls a run makes. The endpoint that is not reachable is real: nothing
is listening on port 1.
"""

import io
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import tests.bootstrap  # noqa: F401
from tests.bootstrap import ensure_identity

from django.utils import timezone as clock
from ward import test

from kopi import checks
from kopi.checks import BAD, OK, WARN
from kopi.forms.links import LinkForm
from kopi.identity import encrypt_secret
from kopi.models import Link, Source, Target
from kopi.storage import CHECK_PREFIX, StorageError, parse_object_key


def _name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _lines(report) -> list[tuple[str, str]]:
    return [(line.tone, line.text) for line in report.lines]


def _texts(report) -> str:
    return "\n".join(line.text for line in report.lines)


def _tone_of(report, tone: str) -> list[str]:
    return [line.text for line in report.lines if line.tone == tone]


def fake_psql(path: Path, record: Path, *, code: int, out: str = "", err: str = "") -> None:
    path.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "pgpass = os.environ.get('PGPASSFILE', '')\n"
        "path = Path(pgpass)\n"
        f"Path({str(record)!r}).write_text(json.dumps({{\n"
        "    'argv': sys.argv,\n"
        "    'connect': os.environ.get('PGCONNECT_TIMEOUT', ''),\n"
        "    'pgpass': path.read_text() if pgpass else '',\n"
        "    'pgpass_path': pgpass,\n"
        "    'dir': os.path.dirname(pgpass),\n"
        "    'mode': oct(path.stat().st_mode & 0o777) if pgpass else '',\n"
        "}))\n"
        f"sys.stdout.write({out!r})\n"
        f"sys.stderr.write({err!r})\n"
        f"raise SystemExit({code})\n"
    )
    os.chmod(path, 0o755)


def tool_dir(tmp: Path, *, dump_version: str, psql_code: int = 0, out: str = "", err: str = "") -> Path:
    """A directory holding a `pg_dump` and the `psql` that sits beside it."""
    tools = tmp / "tools"
    tools.mkdir(exist_ok=True)
    dump = tools / "pg_dump"
    dump.write_text(f"#!/bin/sh\nprintf '%s\\n' 'pg_dump (PostgreSQL) {dump_version}'\n")
    os.chmod(dump, 0o755)
    fake_psql(tools / "psql", tmp / "record.json", code=psql_code, out=out, err=err)
    return tools


def _database(tmp: Path, **kwargs):
    tools = kwargs.pop("tools")
    values = {
        "host": "db.internal",
        "port": 5432,
        "pg_database": "billing",
        "user": "kopi",
        "password": "correct horse",
        "dump_binary": str(tools / "pg_dump"),
    }
    values.update(kwargs)
    return checks.database(**values)


class _Client:
    """The calls a run makes, with a code to fail each one on."""

    def __init__(self, *, fails=None, body=None, served=None):
        self.fails = dict(fails or {})
        self.calls: list[str] = []
        self.keys: list[str] = []
        self.body = body
        self.served = served

    def _refuse(self, action: str) -> None:
        code = self.fails.get(action)
        if not code:
            return
        from botocore.exceptions import ClientError

        raise ClientError({"Error": {"Code": code, "Message": f"{code} at the endpoint"}}, action)

    def list_objects_v2(self, **kwargs):
        self.calls.append("list")
        self._refuse("list")
        return {"KeyCount": 3, "Contents": []}

    def create_multipart_upload(self, **kwargs):
        self.calls.append("multipart")
        self._refuse("multipart")
        self.keys.append(kwargs["Key"])
        return {"UploadId": "upload-1"}

    def abort_multipart_upload(self, **kwargs):
        self.calls.append("abort")
        self._refuse("abort")
        return {}

    def put_object(self, **kwargs):
        self.calls.append("put")
        self._refuse("put")
        self.keys.append(kwargs["Key"])
        self.body = kwargs["Body"]
        return {}

    def get_object(self, **kwargs):
        self.calls.append("get")
        self._refuse("get")
        stored = self.body if isinstance(self.body, bytes) else b""
        return {"Body": io.BytesIO(self.served if self.served is not None else stored)}

    def delete_object(self, **kwargs):
        self.calls.append("delete")
        self._refuse("delete")
        return {}


@test("a folder source reports the tree it would archive and the rules that cut it")
def _():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        root = tmp / "uploads"
        (root / "images").mkdir(parents=True)
        (root / "images" / "one.png").write_bytes(b"x")
        (root / "notes.txt").write_text("hi")

        report = checks.folder(root_path=str(root))
        assert report.passed
        assert _tone_of(report, OK) == [f"{root} is a directory Kopi can read."]
        assert "3 entries" in _texts(report)

        only = checks.folder(root_path=str(root), include_rules="*.png")
        assert only.passed
        assert "1 entry" in _texts(only)

        nothing = checks.folder(root_path=str(root), include_rules="*.mov")
        assert not nothing.passed
        assert "would be empty" in _texts(nothing)

        missing = checks.folder(root_path=str(tmp / "gone"))
        assert not missing.passed
        assert "is not a directory" in _texts(missing)

        link_root = tmp / "link"
        link_root.symlink_to(root, target_is_directory=True)
        assert "symlink" in _texts(checks.folder(root_path=str(link_root)))
        assert "root path" in _texts(checks.folder(root_path="  "))


@test("a Postgres probe answers once, keeps the password out of the command line, and reads the server version")
def _():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        tools = tool_dir(tmp, dump_version="16.4", psql_code=0, out="16.4\n", err="")
        report = _database(tmp, tools=tools)
        assert report.passed, _texts(report)
        assert "db.internal:5432 answered. billing opened as kopi." in _tone_of(report, OK)
        assert "pg_dump 16.4 against server 16.4" in _texts(report)

        record = json.loads((tmp / "record.json").read_text())
        argv = record["argv"]
        assert "--no-password" in argv
        assert "correct horse" not in " ".join(argv)
        assert record["pgpass"].startswith("db.internal:5432:billing:kopi:")
        assert record["mode"] == "0o600"
        assert not Path(record["pgpass_path"]).exists(), (
            "the pgpass file has to be gone when the probe returns"
        )
        assert not Path(record["dir"]).exists(), "the scratch directory goes with it"
        assert int(record["connect"]) > 0, "psql needs a connect timeout of its own"


@test("a probe that is refused says why, and a dump binary older than the server is a bad line")
def _():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        tools = tool_dir(
            tmp,
            dump_version="16.4",
            psql_code=2,
            out="",
            err=(
                'psql: error: connection to server at "db.internal", port 5432 failed: '
                'FATAL:  password authentication failed for user "kopi"\n'
            ),
        )
        report = _database(tmp, tools=tools)
        assert not report.passed
        assert _tone_of(report, BAD) == ["The user or the password was refused."]
        assert "correct horse" not in _texts(report)

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        tools = tool_dir(tmp, dump_version="16.4", psql_code=0, out="18.2\n", err="")
        older = _database(tmp, tools=tools)
        assert not older.passed
        assert "older than the server" in _texts(older)

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        tools = tool_dir(tmp, dump_version="18.2", psql_code=0, out="16.4\n", err="")
        newer = _database(tmp, tools=tools)
        assert newer.passed, _texts(newer)
        assert "pg_dump 18.2 against server 16.4" in _texts(newer)


@test("a source that names a pooler is a warning, and a source with no password is not tried at all")
def _():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        tools = tool_dir(tmp, dump_version="16.4", psql_code=0, out="16.4\n", err="")

        pooler = _database(tmp, tools=tools, host="pgbouncer.internal")
        assert _tone_of(pooler, WARN), _texts(pooler)
        assert "connection pooler" in _texts(pooler)

        port = _database(tmp, tools=tools, port=6432)
        assert "PgBouncer" in _texts(port)

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        tools = tool_dir(tmp, dump_version="16.4", psql_code=0, out="16.4\n", err="")
        silent = _database(tmp, tools=tools, password="")
        assert not silent.passed
        assert not (tmp / "record.json").exists()
        assert "no password" in _texts(silent)

        empty = _database(tmp, tools=tools, dump_binary="")
        assert "no dump binary" in _texts(empty)


@test("a bucket is tried with the calls a run makes, in order")
def _():
    client = _Client()
    report = checks.s3(
        bucket="kopi-eu",
        endpoint="https://s3.example",
        region="eu-central-1",
        access_key="AKIA",
        secret="secret-value",
        path_style=True,
        client=client,
    )
    assert report.passed, _texts(report)
    assert client.calls == ["list", "multipart", "abort", "put", "get", "delete"]
    written = [key for key in client.keys if key][0]
    assert written.startswith(CHECK_PREFIX)
    assert len(report.lines) == 3


@test("a bucket that refuses one call stops there and says what it refused")
def _():
    for action, code, phrase in (
        ("list", "NoSuchBucket", "There is no bucket named kopi-eu"),
        ("list", "AccessDenied", "cannot use kopi-eu"),
        ("list", "InvalidAccessKeyId", "access key is not known"),
        ("list", "SignatureDoesNotMatch", "secret does not match"),
        ("multipart", "AccessDenied", "cannot use kopi-eu"),
        ("put", "AccessDenied", "cannot use kopi-eu"),
        ("delete", "AccessDenied", "cannot use kopi-eu"),
    ):
        client = _Client(fails={action: code})
        report = checks.s3(
            bucket="kopi-eu",
            endpoint="https://s3.example",
            region="eu-central-1",
            access_key="AKIA",
            secret="secret-value",
            path_style=False,
            client=client,
        )
        assert not report.passed, (action, _texts(report))
        assert phrase in _texts(report), (action, _texts(report))
        assert "secret-value" not in _texts(report)
        assert client.calls[-1] == action, (action, client.calls)
        if action != "delete":
            assert "delete" not in client.calls


@test("a probe object that cannot be read back or removed is reported, and always deleted when it can be")
def _():
    unreadable = _Client(fails={"get": "AccessDenied"})
    report = checks.s3(
        bucket="kopi-eu",
        endpoint="https://s3.example",
        region="garage",
        access_key="AKIA",
        secret="s",
        path_style=True,
        client=unreadable,
    )
    assert not report.passed
    assert "would not give it back" in _texts(report)
    assert "delete" in unreadable.calls

    leftover = _Client(fails={"delete": "AccessDenied"})
    report = checks.s3(
        bucket="kopi-eu",
        endpoint="https://s3.example",
        region="garage",
        access_key="AKIA",
        secret="s",
        path_style=True,
        client=leftover,
    )
    assert not report.passed
    assert CHECK_PREFIX in _texts(report)

    not_aborted = _Client(fails={"abort": "AccessDenied"})
    report = checks.s3(
        bucket="kopi-eu",
        endpoint="https://s3.example",
        region="garage",
        access_key="AKIA",
        secret="s",
        path_style=True,
        client=not_aborted,
    )
    assert report.passed, _texts(report)
    assert "abort" in _texts(report) and _tone_of(report, WARN)

    changed = _Client(served=b"different bytes")
    report = checks.s3(
        bucket="kopi-eu",
        endpoint="https://s3.example",
        region="garage",
        access_key="AKIA",
        secret="s",
        path_style=True,
        client=changed,
    )
    assert not report.passed
    assert "did not read back" in _texts(report)


@test("an endpoint that does not answer is one line, and empty credentials are not tried")
def _():
    report = checks.s3(
        bucket="kopi",
        endpoint="http://127.0.0.1:1",
        region="garage",
        access_key="AKIA",
        secret="hunter2",
        path_style=True,
    )
    assert not report.passed
    assert len(report.lines) == 1
    assert "The endpoint did not answer" in _texts(report), _texts(report)
    assert "hunter2" not in _texts(report)

    blank = checks.s3(
        bucket="kopi",
        endpoint="http://127.0.0.1:1",
        region="garage",
        access_key="",
        secret="",
        path_style=True,
    )
    assert "no access key and secret" in _texts(blank)
    assert "needs a bucket" in _texts(
        checks.s3(
            bucket="",
            endpoint="http://127.0.0.1:1",
            region="garage",
            access_key="a",
            secret="b",
            path_style=True,
        )
    )


@test("a saved source or target reads its own stored secret, and a key that cannot decrypt says so")
def _():
    identity = ensure_identity()
    folder_row = Source.objects.create(
        name=_name("src"), kind=Source.Kind.FOLDER, root_path="/definitely/not/here"
    )
    assert "is not a directory" in _texts(checks.source(folder_row))

    db_row = Source.objects.create(
        name=_name("src"),
        kind=Source.Kind.POSTGRES,
        pg_host="db.internal",
        pg_port=5432,
        pg_database="billing",
        pg_user="kopi",
        pg_password="not age ciphertext",
        pg_dump_binary="pg_dump",
    )
    assert "cannot decrypt" in _texts(checks.source(db_row))

    row = Target.objects.create(name=_name("tgt"), bucket="kopi", access_key_id="", secret_access_key="")
    assert "nothing stored" in _texts(checks.target(row))

    kept = Target.objects.create(
        name=_name("tgt"),
        bucket="kopi",
        endpoint_url="http://127.0.0.1:1",
        region="garage",
        path_style=True,
        access_key_id=encrypt_secret(identity, "AKIA"),
        secret_access_key=encrypt_secret(identity, "hunter2"),
    )
    report = checks.target(kept)
    assert "hunter2" not in _texts(report)
    assert len(report.lines) == 1


@test("a link walks every part of the job: both ends, the schedule, the hooks, and the work volume")
def _():
    identity = ensure_identity()
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        root = tmp / "tree"
        root.mkdir()
        (root / "a.txt").write_text("a")
        source_row = Source.objects.create(
            name=_name("src"), kind=Source.Kind.FOLDER, root_path=str(root)
        )
        target_row = Target.objects.create(
            name=_name("tgt"),
            bucket="kopi",
            endpoint_url="http://127.0.0.1:1",
            region="garage",
            path_style=True,
            access_key_id=encrypt_secret(identity, "AKIA"),
            secret_access_key=encrypt_secret(identity, "hunter2"),
        )
        script = tmp / "pre.sh"
        script.write_text("#!/bin/bash\nexit 0\n")
        os.chmod(script, 0o755)

        report = checks.link(
            source_row=source_row,
            target_row=target_row,
            pre_script_path=str(script),
            post_script_path=str(tmp / "gone.sh"),
            cadence="0 2 * * *",
            timezone_name="Europe/Copenhagen",
            now=datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc),
        )
        titles = [section["title"] for section in report.sections()]
        assert titles == ["Source", "Target", "Schedule", "Hooks", "Work volume"], titles
        assert not report.passed
        texts = _texts(report)
        assert "is a directory Kopi can read" in texts
        assert "endpoint" in texts
        assert "hunter2" not in texts
        assert "The next run is 6 Nov 02:00 Europe/Copenhagen." in texts
        assert "is a file Kopi may run" in texts
        assert "gone.sh is not a regular file" in texts

        retired = checks.link(
            source_row=source_row,
            target_row=target_row,
            cadence="0 2 * * *",
            timezone_name="Europe/Copenhagen",
            enabled=False,
            now=datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc),
        )
        assert "retired" in _texts(retired)

        never = checks.link(
            source_row=source_row,
            target_row=target_row,
            cadence="not a cron",
            timezone_name="Europe/Copenhagen",
            now=clock.now(),
        )
        assert "never fires" in _texts(never)


@test("the work volume reports its own floor")
def _():
    previous = os.environ.get("KOPI_DISK_FLOOR")
    os.environ["KOPI_DISK_FLOOR"] = str(5 * 1024**4)
    try:
        report = checks.volume()
        assert not report.passed
        assert "floor" in _texts(report)
    finally:
        if previous is None:
            os.environ.pop("KOPI_DISK_FLOOR", None)
        else:
            os.environ["KOPI_DISK_FLOOR"] = previous
    os.environ["KOPI_DISK_FLOOR"] = "0"
    try:
        assert checks.volume().passed
    finally:
        if previous is None:
            os.environ.pop("KOPI_DISK_FLOOR", None)
        else:
            os.environ["KOPI_DISK_FLOOR"] = previous


@test("a form that does not validate never reaches across the network")
def _():
    form = LinkForm(
        {
            "source": "",
            "target": "",
            "cadence": "0 2 * * *",
            "timezone": "Mars/Phobos",
            "retention": Link.Retention.COUNT,
            "retention_count": "0",
            "pre_script_timeout": "300",
            "post_script_timeout": "300",
        }
    )
    assert not form.is_valid()
    report = checks.form_report(form)
    assert not report.passed
    texts = _texts(report)
    assert "Source is required." in texts
    assert "Unknown timezone." in texts
    assert report.lines[0].group == checks.NONE


@test("a probe that never answers is cut off, and the pgpass file is cleaned anyway")
def _():
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        tools = tmp / "tools"
        tools.mkdir()
        dump = tools / "pg_dump"
        dump.write_text("#!/bin/sh\nprintf '%s\\n' 'pg_dump (PostgreSQL) 16.4'\n")
        os.chmod(dump, 0o755)
        psql = tools / "psql"
        psql.write_text(
            "#!" + sys.executable + "\n"
            "import json, os\n"
            "from pathlib import Path\n"
            "path = Path(os.environ['PGPASSFILE'])\n"
            "Path(" + repr(str(tmp / "late.json")) + ").write_text(json.dumps("
            "{'pgpass': path.read_text(), 'dir': os.path.dirname(os.fspath(path))}))\n"
            "import time\n"
            "time.sleep(30)\n"
        )
        os.chmod(psql, 0o755)
        report = checks.database(
            host="db.internal",
            port=5432,
            pg_database="billing",
            user="kopi",
            password="correct horse",
            dump_binary=str(dump),
            timeout=2,
        )
        assert not report.passed
        assert "did not answer within 2 seconds" in _texts(report)
        late = json.loads((tmp / "late.json").read_text())
        assert late["pgpass"] == "db.internal:5432:billing:kopi:correct horse\n"
        assert not Path(late["dir"]).exists(), "a killed probe still clears its scratch directory"


@test("a check key is not a backup name, so retention and the run list never see it")
def _():
    key = f"{CHECK_PREFIX}{uuid.uuid4().hex}"
    try:
        parse_object_key(key)
    except StorageError as exc:
        assert "source/timestamp/checksum" in str(exc)
    else:
        raise AssertionError("a probe object must not parse as a backup")
