"""Wait until Garage is up, then assign the layout, key, and bucket.

Safe to run again. A node that already has a role is left alone, and an
existing key or bucket is reused.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ADMIN = os.environ.get("GARAGE_ADMIN_URL", "http://garage:3903")
TOKEN = os.environ["GARAGE_ADMIN_TOKEN"]
KEY_ID = os.environ["GARAGE_ACCESS_KEY_ID"]
SECRET = os.environ["GARAGE_SECRET_ACCESS_KEY"]
BUCKET = os.environ["GARAGE_BUCKET"]
KEY_NAME = "kopi-dev"
# Garage counts capacity in SI bytes. 1 GB keeps the single node in the layout.
CAPACITY_BYTES = 1_000_000_000
ZONE = "dc1"


class AdminError(Exception):
    def __init__(self, method, path, status, body):
        self.status = status
        self.body = body
        super().__init__(f"{method} {path} -> {status} {body}")


def call(method, path, body=None, query=None):
    url = ADMIN + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {TOKEN}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise AdminError(method, path, exc.code, detail) from exc
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{method} {path} returned non-json: {raw!r}") from exc


def wait_until_up():
    deadline = time.monotonic() + 90
    last_error = "no response"
    while time.monotonic() < deadline:
        try:
            status = call("GET", "/v2/GetClusterStatus")
        except AdminError as exc:
            if exc.status == 401:
                raise SystemExit(f"Garage rejected the admin token: {exc}") from exc
            last_error = str(exc)
            time.sleep(1)
            continue
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = str(exc)
            time.sleep(1)
            continue
        nodes = status.get("nodes") or []
        if any(_is_up(node) for node in nodes):
            return status
        last_error = f"no up node in {status}"
        time.sleep(1)
    raise SystemExit(f"Garage did not become reachable: {last_error}")


def ensure_layout(status):
    nodes = [node for node in status.get("nodes") or [] if _is_up(node)]
    if not nodes:
        raise SystemExit(f"Garage status has no up node: {status}")
    unassigned = [node for node in nodes if not node.get("role")]
    if not unassigned:
        print("layout already assigned", flush=True)
        return
    call(
        "POST",
        "/v2/UpdateClusterLayout",
        {
            "roles": [
                {
                    "id": node["id"],
                    "zone": ZONE,
                    "capacity": CAPACITY_BYTES,
                    "tags": [],
                }
                for node in unassigned
            ]
        },
    )
    version = _layout_version(status) + 1
    try:
        call("POST", "/v2/ApplyClusterLayout", {"version": version})
    except AdminError as exc:
        refreshed = call("GET", "/v2/GetClusterStatus")
        still_missing = [
            node for node in refreshed.get("nodes") or [] if _is_up(node) and not node.get("role")
        ]
        if still_missing:
            raise SystemExit(f"layout apply failed: {exc}") from exc
        print(f"layout already applied ({exc})", flush=True)
        return
    print(f"applied layout version {version}", flush=True)


def wait_until_healthy():
    deadline = time.monotonic() + 60
    last = None
    while time.monotonic() < deadline:
        try:
            last = call("GET", "/v2/GetClusterHealth")
        except (AdminError, urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last = str(exc)
            time.sleep(1)
            continue
        if last.get("status") == "healthy":
            print("cluster healthy", flush=True)
            return
        time.sleep(1)
    raise SystemExit(f"Garage layout is not healthy: {last}")


def ensure_key():
    try:
        call(
            "POST",
            "/v2/ImportKey",
            {
                "name": KEY_NAME,
                "accessKeyId": KEY_ID,
                "secretAccessKey": SECRET,
            },
        )
        print(f"imported access key {KEY_ID}", flush=True)
        return
    except AdminError as exc:
        try:
            call("GET", "/v2/GetKeyInfo", query={"id": KEY_ID})
        except AdminError:
            raise SystemExit(f"access key import failed: {exc}") from exc
        print(f"access key {KEY_ID} already exists", flush=True)


def ensure_bucket():
    try:
        info = call("POST", "/v2/CreateBucket", {"globalAlias": BUCKET})
        print(f"created bucket {BUCKET}", flush=True)
    except AdminError as create_error:
        try:
            info = call("GET", "/v2/GetBucketInfo", query={"globalAlias": BUCKET})
        except AdminError:
            raise SystemExit(f"bucket create failed: {create_error}") from create_error
        print(f"bucket {BUCKET} already exists", flush=True)
    bucket_id = info.get("id") if isinstance(info, dict) else None
    if not bucket_id:
        raise SystemExit(f"bucket response has no id: {info}")
    call(
        "POST",
        "/v2/AllowBucketKey",
        {
            "bucketId": bucket_id,
            "accessKeyId": KEY_ID,
            "permissions": {"read": True, "write": True, "owner": True},
        },
    )
    print(f"granted {KEY_ID} read, write, and owner on {BUCKET}", flush=True)


def _is_up(node):
    if "isUp" in node:
        return bool(node["isUp"])
    return bool(node.get("is_up"))


def _layout_version(status):
    for key in ("layoutVersion", "layout_version"):
        if key in status:
            return int(status[key])
    raise SystemExit(f"Garage status has no layout version: {status}")


def main():
    for directory in ("/devdata/data", "/devdata/work"):
        os.makedirs(directory, exist_ok=True)
    status = wait_until_up()
    ensure_layout(status)
    wait_until_healthy()
    ensure_key()
    ensure_bucket()
    print("garage init complete", flush=True)


if __name__ == "__main__":
    try:
        main()
    except AdminError as exc:
        raise SystemExit(str(exc)) from exc
    except KeyError as exc:
        raise SystemExit(f"missing {exc} in the Garage response") from exc
