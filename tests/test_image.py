"""The image round-trip. Skips unless https://127.0.0.1:8443/ answers."""

import http.cookiejar
import json
import os
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from ward import skip, test

from kopi.storage import CHECK_PREFIX, S3Storage
from tests.test_harness import listed_keys, runtime_env

ROOT = Path(__file__).resolve().parents[1]
BASE = "https://127.0.0.1:8443"
ADMIN_NAME = "admin"
ADMIN_PASSWORD = "kopi-image-pass"
FIXTURE = ROOT / "harness" / "folder"

_WALK = """
import base64, json, os, sys
from pathlib import Path
root = Path(sys.argv[1])
found = {}
for directory, dirnames, filenames in os.walk(root, followlinks=False):
    current = Path(directory)
    for name in list(dirnames) + list(filenames):
        path = current / name
        if name in dirnames and not path.is_symlink():
            continue
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            found[rel] = ["link", os.readlink(path)]
        elif path.is_file():
            found[rel] = ["file", base64.b64encode(path.read_bytes()).decode("ascii")]
json.dump(found, sys.stdout, sort_keys=True)
"""


def image_is_down():
    context = ssl._create_unverified_context()
    try:
        with urllib.request.urlopen(BASE + "/", context=context, timeout=3) as response:
            return response.status != 200
    except (OSError, urllib.error.URLError):
        return True


def _ssl_context():
    cert = ROOT / "devdata" / "cert" / "tls.crt"
    if cert.is_file():
        return ssl.create_default_context(cafile=os.fspath(cert))
    return ssl._create_unverified_context()


def _compose(*args, timeout=60):
    return subprocess.run(
        ["docker", "compose", "--profile", "app", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class _Site:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=_ssl_context()),
            urllib.request.HTTPCookieProcessor(self.jar),
        )

    def open(self, path, data=None, timeout=60):
        url = BASE + path
        body = None
        headers = {}
        if data is not None:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            headers["Referer"] = url
        request = urllib.request.Request(url, data=body, headers=headers)
        try:
            response = self.opener.open(request, timeout=timeout)
            status = response.status
            final = response.geturl()
            page = response.read().decode()
        except urllib.error.HTTPError as exc:
            status = exc.code
            final = exc.geturl()
            page = exc.read().decode(errors="replace")
        return status, final, page


def _csrf(page):
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page)
    if match is None:
        raise AssertionError(_hints(page) or "no csrf token")
    return match.group(1)


def _hints(page):
    found = re.findall(r'class="hint error"[^>]*>(.*?)</p>', page, re.S)
    text = " ".join(re.sub(r"<[^>]+>", "", item) for item in found)
    return re.sub(r"\s+", " ", text).strip()


def _pk(url, kind):
    match = re.search(rf"/{kind}/(\d+)/", url)
    if match is None:
        raise AssertionError(url)
    return match.group(1)


def _sign_in(site):
    status, _url, page = site.open("/login/")
    assert status == 200, status
    payload = {
        "csrfmiddlewaretoken": _csrf(page),
        "name": ADMIN_NAME,
        "password": ADMIN_PASSWORD,
    }
    if "Set up this install" not in page and "Sign in to the operator" not in page:
        raise AssertionError(page[:400])
    status, _url, page = site.open("/login/", payload)
    assert status == 200, _hints(page) or page[:400]
    assert _hints(page) == "", _hints(page)
    matches = [cookie for cookie in site.jar if cookie.name == "sessionid"]
    assert len(matches) == 1, [cookie.name for cookie in site.jar]
    assert matches[0].secure
    status, _url, home = site.open("/sources/")
    assert status == 200, home[:400]
    assert "Sources" in home
    assert "Sign in to the operator" not in home


def _save(site, path, fields):
    status, _url, page = site.open(path)
    assert status == 200, page[:400]
    status, url, page = site.open(path, {"csrfmiddlewaretoken": _csrf(page), **fields})
    assert status == 200, _hints(page) or page[:600]
    assert _hints(page) == "", _hints(page)
    return url


def _dump_18(page):
    match = re.search(r'<option value="([^"]*\/18\/[^"]*)"[^>]*>pg_dump 18</option>', page)
    if match is None:
        raise AssertionError("pg_dump 18 is not on the form")
    return match.group(1)


def _run_ids(site):
    status, url, page = site.open("/runs/")
    assert status == 200, page[:400]
    return set(re.findall(r"/runs/(\d+)/", url + page))


def _status(site, run_id):
    status, _url, body = site.open(f"/runs/{run_id}/status/")
    assert status == 200, body[:400]
    payload = json.loads(body)
    assert {"phase", "status", "bytes_moved"} <= set(payload)
    return payload


def _await_new(site, seen):
    """Run now, then wait until every new run has succeeded."""
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        new = _run_ids(site) - seen
        if not new:
            time.sleep(1)
            continue
        payloads = {run_id: _status(site, run_id) for run_id in new}
        failed = {run_id: payload for run_id, payload in payloads.items() if payload["status"] == "failed"}
        if failed:
            run_id = next(iter(failed))
            _code, _url, detail = site.open(f"/runs/{run_id}/")
            raise AssertionError(f"{failed[run_id]} {detail[:1200]}")
        if all(payload["status"] == "succeeded" for payload in payloads.values()):
            return new
        time.sleep(1)
    raise AssertionError("run did not finish")


def _run_now(site, link_id, seen):
    _status_code, _url, page = site.open(f"/links/{link_id}/")
    site.open(f"/links/{link_id}/run/", {"csrfmiddlewaretoken": _csrf(page)})
    finished = _await_new(site, seen)
    seen.update(finished)
    return finished


def _run_for(site, run_ids, source_name):
    for run_id in sorted(run_ids, key=int, reverse=True):
        _code, _url, page = site.open(f"/runs/{run_id}/")
        row = re.search(r'<tr class="is-on">.*?</tr>', page, re.S)
        if row is not None and source_name in row.group(0):
            return run_id
    raise AssertionError(f"no run for {source_name}")


def _restore(site, run_id, destination):
    _status, _url, page = site.open(f"/runs/{run_id}/")
    status, _url, page = site.open(
        f"/runs/{run_id}/restore/",
        {"csrfmiddlewaretoken": _csrf(page), "destination": destination},
        timeout=120,
    )
    assert status == 200, _hints(page) or page[:600]
    assert "Restored" in page, _hints(page) or page[:600]


def _tree(root):
    completed = subprocess.run(
        ["python3", "-c", _WALK, os.fspath(root)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


def _container_tree(path):
    completed = _compose("exec", "-T", "-u", "root", "app", "python3", "-c", _WALK, path)
    if completed.returncode != 0:
        raise AssertionError(completed.stderr or completed.stdout)
    return json.loads(completed.stdout)


def _psql(env, database, sql):
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
        env={**os.environ, "PGPASSWORD": env["POSTGRES_PASSWORD"], "PGCONNECT_TIMEOUT": "5"},
        capture_output=True,
        text=True,
        timeout=20,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr.strip() or completed.stdout)
    return completed.stdout


def _drop_database(env, name):
    _psql(
        env,
        env["POSTGRES_DB"],
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{name}' AND pid <> pg_backend_pid()",
    )
    _psql(env, env["POSTGRES_DB"], f'DROP DATABASE IF EXISTS "{name}"')


def _delete_prefix(storage, name):
    for item in storage.list(f"{name}/"):
        storage.delete(item.key)


def _check(site, path, fields):
    """The Test button's request. It answers with lines and writes nothing."""
    status, _url, body = site.open(path, fields)
    assert status == 200, body[:400]
    assert "data-check-line" in body, body[:400]
    return body


def _link(site, source_id, target_id):
    return _pk(
        _save(
            site,
            "/links/new/",
            {
                "source": source_id,
                "target": target_id,
                "cadence": "0 0 1 1 *",
                "timezone": "UTC",
                "retention": "count",
                "retention_count": "14",
                "pre_script_path": "",
                "post_script_path": "",
                "pre_script_timeout": "300",
                "post_script_timeout": "300",
            },
        ),
        "links",
    )


@test("the image build excludes env, devdata, and secret.key")
def _():
    ignored = (ROOT / ".dockerignore").read_text().splitlines()
    assert ".env" in ignored
    assert "devdata" in ignored
    assert "secret.key" in ignored


@test("the Dockerfile pins s6-overlay and runs as the kopi user")
def _():
    text = (ROOT / "Dockerfile").read_text()
    assert "S6_OVERLAY_VERSION=3.2.3.2" in text
    assert "5379750ed30a84bbd2e2dd74847ba6b5bd29cd0b2e3ea2ec58049b57eb2eda12" in text
    assert "--uid 10001" in text
    assert "--gid 10001" in text
    assert "postgresql-client-17" in text
    assert "postgresql-client-18" in text
    assert 'ENTRYPOINT ["/init"]' in text
    assert "uv sync --frozen --no-dev" in text


@test("compose starts Kopi only with the app profile")
def _():
    text = (ROOT / "compose.yaml").read_text()
    assert 'profiles: ["app"]' in text
    assert "127.0.0.1:8443:8443" in text
    assert "KOPI_ALLOWED_HOSTS:" in text
    assert "KOPI_ALLOW_HTTP" not in text
    app = text.split("\n  app:", 1)[1]
    assert "env_file" not in app
    assert "kopi_data:/data" in app
    assert "kopi_work:/work" in app
    assert "./devdata/cert:/cert:ro" in app
    assert "./harness/folder:/fixtures/folder:ro" in app


@test("the image has age, psql, both pg_dump majors, and no dev secrets", tags=["image"])
@skip("the image is not answering", when=image_is_down)
def _():
    listed = _compose("images", "-q", "app")
    assert listed.returncode == 0, listed.stderr
    image = listed.stdout.strip().splitlines()[0]
    script = r"""
set -eu
test ! -e /opt/kopi/.env
test ! -e /opt/kopi/secret.key
test ! -d /devdata
test ! -e /secret.key
command -v age >/dev/null
command -v age-keygen >/dev/null
command -v bash >/dev/null
command -v zstd >/dev/null
command -v pgrep >/dev/null
tar --version | head -n 1 | grep -q 'GNU tar'
test "$(id -u kopi)" = 10001
test "$(id -g kopi)" = 10001
/usr/lib/postgresql/17/bin/pg_dump --version | grep -q ' 17\.'
/usr/lib/postgresql/18/bin/pg_dump --version | grep -q ' 18\.'
test -x /usr/lib/postgresql/17/bin/pg_restore
test -x /usr/lib/postgresql/18/bin/pg_restore
test -x /usr/lib/postgresql/17/bin/psql
test -x /usr/lib/postgresql/18/bin/psql
/opt/kopi/.venv/bin/python -c 'import boto3, gunicorn, importlib.util, sys; sys.exit(0 if importlib.util.find_spec("ward") is None else 1)'
unset KOPI_ALLOWED_HOSTS
set +e
message=$(/etc/s6-overlay/s6-rc.d/web/run 2>&1)
code=$?
set -e
test "$code" -ne 0
printf '%s\n' "$message" | grep -q 'KOPI_ALLOWED_HOSTS is unset'
"""
    completed = subprocess.run(
        ["docker", "run", "--rm", "--network", "none", "--entrypoint", "/bin/sh", image, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    cmdline = _compose("exec", "-T", "app", "cat", "/proc/1/cmdline")
    assert cmdline.returncode == 0, cmdline.stderr
    pid1 = cmdline.stdout.replace("\x00", " ")
    assert "s6-svscan" in pid1, pid1
    processes = _compose("exec", "-T", "app", "ps", "-eo", "user,args")
    assert processes.returncode == 0, processes.stderr
    assert "gunicorn" in processes.stdout
    assert "manage.py work" in processes.stdout


@test("one image performs the folder and postgres round-trip", tags=["image"])
@skip("the image is not answering", when=image_is_down)
def _():
    env = runtime_env()
    token = uuid.uuid4().hex[:8]
    folder_name = f"imgf{token}"
    database_name = f"imgp{token}"
    target_name = f"imgt{token}"
    restore_db = f"imgd{token}"
    storage = S3Storage(
        bucket=env["GARAGE_BUCKET"],
        endpoint_url=env["GARAGE_S3_ENDPOINT"],
        region=env["GARAGE_S3_REGION"],
        access_key_id=env["GARAGE_ACCESS_KEY_ID"],
        secret_access_key=env["GARAGE_SECRET_ACCESS_KEY"],
        path_style=True,
    )
    site = _Site()
    created = False
    try:
        _sign_in(site)
        seen = _run_ids(site)
        folder_id = _pk(
            _save(
                site,
                "/sources/new/?kind=folder",
                {
                    "kind": "folder",
                    "name": folder_name,
                    "root_path": "/fixtures/folder",
                    "include_rules": "",
                    "exclude_rules": "",
                },
            ),
            "sources",
        )
        _code, _url, form = site.open("/sources/new/?kind=postgres")
        binary = _dump_18(form)
        assert "/18/" in binary
        database_id = _pk(
            _save(
                site,
                "/sources/new/?kind=postgres",
                {
                    "kind": "postgres",
                    "name": database_name,
                    "pg_host": "postgres",
                    "pg_port": "5432",
                    "pg_database": env["POSTGRES_DB"],
                    "pg_user": env["POSTGRES_USER"],
                    "pg_password": env["POSTGRES_PASSWORD"],
                    "pg_dump_binary": binary,
                },
            ),
            "sources",
        )
        target_id = _pk(
            _save(
                site,
                "/targets/new/",
                {
                    "name": target_name,
                    "endpoint": "http://garage:3900",
                    "region": env["GARAGE_S3_REGION"],
                    "bucket": env["GARAGE_BUCKET"],
                    "addressing": "path",
                    "access_key": env["GARAGE_ACCESS_KEY_ID"],
                    "secret": env["GARAGE_SECRET_ACCESS_KEY"],
                },
            ),
            "targets",
        )
        folder_link = _link(site, folder_id, target_id)

        # The Test button reaches the server, the bucket, and the job the run
        # will reach. The password and secret boxes stay empty, so the check
        # uses what the install stored.
        _code, _url, page = site.open(f"/sources/{database_id}/")
        database_check = _check(
            site,
            "/sources/test/",
            {
                "csrfmiddlewaretoken": _csrf(page),
                "pk": database_id,
                "name": database_name,
                "pg_host": "postgres",
                "pg_port": "5432",
                "pg_database": env["POSTGRES_DB"],
                "pg_user": env["POSTGRES_USER"],
                "pg_password": "",
                "pg_dump_binary": binary,
            },
        )
        assert "postgres:5432 answered" in database_check, database_check[:600]
        assert "pg_dump 18 against server 18" in database_check, database_check[:600]

        _code, _url, page = site.open(f"/targets/{target_id}/")
        target_check = _check(
            site,
            "/targets/test/",
            {
                "csrfmiddlewaretoken": _csrf(page),
                "pk": target_id,
                "name": target_name,
                "endpoint": "http://garage:3900",
                "region": env["GARAGE_S3_REGION"],
                "bucket": env["GARAGE_BUCKET"],
                "addressing": "path",
                "access_key": "",
                "secret": "",
            },
        )
        assert "wrote, read back, and removed a probe object" in target_check, target_check[:600]
        assert "Ready to save." in target_check, target_check[:600]
        assert listed_keys(storage.client, env["GARAGE_BUCKET"], CHECK_PREFIX) == []

        _code, _url, page = site.open(f"/links/{folder_link}/")
        link_check = _check(
            site,
            "/links/test/",
            {
                "csrfmiddlewaretoken": _csrf(page),
                "pk": folder_link,
                "source": folder_id,
                "target": target_id,
                "cadence": "0 0 1 1 *",
                "timezone": "UTC",
                "retention": "count",
                "retention_count": "14",
                "pre_script_path": "",
                "post_script_path": "",
                "pre_script_timeout": "300",
                "post_script_timeout": "300",
            },
        )
        assert "Ready to save." in link_check, link_check[:900]
        assert ">Source</p>" in link_check and ">Work volume</p>" in link_check

        folder_runs = _run_now(site, folder_link, seen)
        made = _compose("exec", "-T", "-u", "root", "app", "chmod", "777", "/restore")
        assert made.returncode == 0, made.stderr
        folder_run = _run_for(site, folder_runs, folder_name)
        _restore(site, folder_run, f"/restore/{folder_name}")
        assert _container_tree(f"/restore/{folder_name}") == _tree(FIXTURE)
        database_link = _link(site, database_id, target_id)
        database_runs = _run_now(site, database_link, seen)
        _psql(env, env["POSTGRES_DB"], f'CREATE DATABASE "{restore_db}"')
        created = True
        database_run = _run_for(site, database_runs, database_name)
        _restore(site, database_run, restore_db)
        restored = _psql(env, restore_db, "SELECT body FROM kopi_notes ORDER BY id").splitlines()
        original = _psql(env, env["POSTGRES_DB"], "SELECT body FROM kopi_notes ORDER BY id").splitlines()
        assert restored == original == ["alpha", "beta", "gamma"]
        _delete_prefix(storage, folder_name)
        _delete_prefix(storage, database_name)
        assert storage.list(f"{folder_name}/") == []
        assert storage.list(f"{database_name}/") == []
    finally:
        try:
            _delete_prefix(storage, folder_name)
            _delete_prefix(storage, database_name)
        except Exception:
            pass
        if created:
            _drop_database(env, restore_db)
