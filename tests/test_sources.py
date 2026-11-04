import os
import re
import uuid
from pathlib import Path

import tests.bootstrap  # noqa: F401

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.test import Client
from django.urls import reverse
from ward import test

from kopi.identity import (
    IDENTITY_FILENAME,
    decrypt_secret,
    generate_identity,
    load_identity,
)
from kopi.models import AuditEvent, Source


def _client():
    return Client(enforce_csrf_checks=True, HTTP_HOST="localhost")


def _csrf(body: str) -> str:
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', body)
    assert match, body
    return match.group(1)


def _user(name: str, role: str) -> User:
    user = User.objects.create_user(name, password="correct-password")
    user.groups.add(Group.objects.get(name=role))
    return user


def _post(client, url, token, data):
    return client.post(url, {"csrfmiddlewaretoken": token, **data}, follow=True)


@test("a folder and a Postgres source round-trip, the password is ciphertext, and a viewer gets the key-value list")
def _():
    token = uuid.uuid4().hex[:8]
    folder_name = f"aaa-{token}"
    pg_name = f"zzz-{token}"
    root = f"/srv/src-{token}"
    changed_root = f"/var/lib/src-{token}"
    password = f"pw-{token}"
    admin = _user(f"src-admin-{token}", "admin")
    viewer = _user(f"src-viewer-{token}", "viewer")
    key = Path(settings.DATA_DIR) / IDENTITY_FILENAME
    created_key = not key.exists()
    identity = generate_identity(key) if created_key else load_identity(key)
    dump_dir = Path(settings.DATA_DIR) / f"dump-{token}"
    dump_dir.mkdir()
    dump = dump_dir / "pg_dump"
    dump.write_text("#!/bin/sh\nprintf '%s\\n' 'pg_dump (PostgreSQL) 16.4'\n")
    dump.chmod(0o755)
    previous_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{dump_dir}{os.pathsep}{previous_path}"
    try:
        anonymous = Client(HTTP_HOST="localhost")
        assert anonymous.get(reverse("sources")).status_code == 302

        client = _client()
        client.force_login(admin)
        new = client.get(reverse("source-new"))
        assert new.status_code == 200
        body = new.content.decode()
        token_csrf = _csrf(body)
        assert "New source" in body
        assert "kind=postgres" in body
        assert "kind=folder" in body
        assert f'value="{dump}"' in body
        assert "pg_dump 16" in body
        assert 'id="find-sources"' in body
        assert "filter.js" in body

        missing = _post(client, reverse("source-new"), token_csrf, {"kind": "folder", "name": folder_name})
        assert missing.status_code == 200
        assert "Root path is required." in missing.content.decode()
        assert not Source.objects.filter(name=folder_name).exists()
        assert not AuditEvent.objects.filter(user=admin, event=f"Source created · {folder_name}").exists()

        created = _post(
            client,
            reverse("source-new"),
            token_csrf,
            {
                "kind": "folder",
                "name": folder_name,
                "root_path": root,
                "include_rules": "docs/**",
                "exclude_rules": "*.tmp",
            },
        )
        assert created.status_code == 200
        folder = Source.objects.get(name=folder_name)
        assert folder.kind == Source.Kind.FOLDER
        assert folder.root_path == root
        assert folder.include_rules == "docs/**"
        assert folder.exclude_rules == "*.tmp"
        assert created.request["PATH_INFO"] == reverse("source", args=[folder.pk])
        created_body = created.content.decode()
        assert root in created_body
        assert "docs/**" in created_body
        assert "*.tmp" in created_body
        assert f'class="pick is-on" href="{reverse("source", args=[folder.pk])}"' in created_body
        assert AuditEvent.objects.filter(user=admin, event=f"Source created · {folder_name}").exists()

        edited = _post(
            client,
            reverse("source", args=[folder.pk]),
            _csrf(created_body),
            {
                "name": folder_name,
                "root_path": changed_root,
                "include_rules": "docs/**",
                "exclude_rules": "*.tmp",
            },
        )
        assert edited.status_code == 200
        folder.refresh_from_db()
        assert folder.root_path == changed_root
        assert changed_root in edited.content.decode()
        assert AuditEvent.objects.filter(user=admin, event=f"Source changed · {folder_name}").exists()

        duplicate = _post(
            client,
            reverse("source-new") + "?kind=folder",
            _csrf(edited.content.decode()),
            {"kind": "folder", "name": folder_name, "root_path": "/tmp/other"},
        )
        assert "already has this name" in duplicate.content.decode()
        assert Source.objects.filter(name=folder_name).count() == 1

        pg_page = client.get(reverse("source-new") + "?kind=postgres")
        stored = _post(
            client,
            reverse("source-new"),
            _csrf(pg_page.content.decode()),
            {
                "kind": "postgres",
                "name": pg_name,
                "pg_host": "db.internal",
                "pg_port": "5432",
                "pg_database": "billing",
                "pg_user": "kopi",
                "pg_password": password,
                "pg_dump_binary": os.fspath(dump),
            },
        )
        assert stored.status_code == 200
        pg = Source.objects.get(name=pg_name)
        assert pg.kind == Source.Kind.POSTGRES
        assert pg.pg_host == "db.internal"
        assert pg.pg_port == 5432
        assert pg.pg_database == "billing"
        assert pg.pg_user == "kopi"
        assert pg.pg_dump_binary == os.fspath(dump)
        assert password not in pg.pg_password
        assert "BEGIN AGE" in pg.pg_password
        assert decrypt_secret(identity, pg.pg_password) == password
        admin_body = stored.content.decode()
        assert password not in admin_body
        assert pg.pg_password not in admin_body
        assert "BEGIN AGE" not in admin_body
        assert 'name="pg_password"' in admin_body
        assert "Postgres 16 · db.internal" in admin_body
        assert AuditEvent.objects.filter(user=admin, event=f"Source created · {pg_name}").exists()

        kept = pg.pg_password
        renamed = _post(
            client,
            reverse("source", args=[pg.pk]),
            _csrf(admin_body),
            {
                "name": pg_name,
                "pg_host": "db.other",
                "pg_port": "5432",
                "pg_database": "billing",
                "pg_user": "kopi",
                "pg_password": "",
                "pg_dump_binary": os.fspath(dump),
            },
        )
        assert renamed.status_code == 200
        pg.refresh_from_db()
        assert pg.pg_host == "db.other"
        assert pg.pg_password == kept
        assert decrypt_secret(identity, pg.pg_password) == password
        assert password not in renamed.content.decode()
        assert AuditEvent.objects.filter(user=admin, event=f"Source changed · {pg_name}").exists()

        home = client.get(reverse("sources"))
        assert home.status_code == 302
        assert home.url == reverse("source", args=[folder.pk])

        viewer_client = _client()
        viewer_client.force_login(viewer)
        page = viewer_client.get(reverse("source", args=[pg.pk]))
        refused = viewer_client.post(
            reverse("source-new"),
            {"csrfmiddlewaretoken": _csrf(page.content.decode()), "name": pg_name},
        )
        assert refused.status_code == 403
        assert "CSRF" not in refused.content.decode()
        assert viewer_client.get(reverse("source-new")).status_code == 403
        assert Source.objects.get(pk=pg.pk).pg_host == "db.other"
        page = viewer_client.get(reverse("source", args=[pg.pk]))
        assert page.status_code == 200
        seen = page.content.decode()
        assert "Set. Not shown." in seen
        assert "db.other" in seen
        assert "billing" in seen
        assert "pg_dump 16" in seen
        assert password not in seen
        assert pg.pg_password not in seen
        assert 'name="pg_password"' not in seen
        assert 'name="pg_host"' not in seen
        assert "New source" not in seen
        assert 'href="/sources/"' in seen
        assert 'aria-current="page"' in seen
    finally:
        os.environ["PATH"] = previous_path
        if created_key and key.exists():
            key.unlink()
