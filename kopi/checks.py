"""Ask whether a source, a target, or a link would work before it is saved.

Nothing here writes a backup and nothing here is stored. A report is the
lines an operator needs: what answered, what refused, and what to change. A
line tone is `ok`, `bad`, `warn`, or `info`, and a report fails when it holds
a bad line. A rule that matches no file, and a bucket that cannot be written
to, are bad rather than notes: both would end a run with an empty or a
missing backup.

Every probe is short. `psql` gets a connect timeout, the S3 client gets a
connect and a read timeout, and a folder walk stops counting at
`MEMBER_SAMPLE`, because a person is waiting inside a web request.
"""

import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from django.utils import timezone

from kopi.audit import format_size
from kopi.hooks import has_bash, resolve_hook
from kopi.identity import IdentityError, decrypt_secret
from kopi.models import Source, Target
from kopi.pipeline import folder_member_paths
from kopi.postgres import (
    POOLER_EXPLAINED,
    PROBE_TIMEOUT,
    Probe,
    binary_version,
    pooler_reason,
    probe_database,
    version_major,
)
from kopi.rekey import open_identity
from kopi.schedule import next_run_at, stamp, zone_of
from kopi.settings import work_directory
from kopi.storage import CHECK_PREFIX, probe_client
from kopi.worker import configured_disk_floor

OK = "ok"
BAD = "bad"
WARN = "warn"
INFO = "info"

ICONS = {OK: "check", BAD: "x", WARN: "warning", INFO: "info"}

# A tree larger than this is not worth counting. The figure is a sanity
# check, not an inventory.
MEMBER_SAMPLE = 2000

NONE = ""
SOURCE_GROUP = "Source"
TARGET_GROUP = "Target"
SCHEDULE_GROUP = "Schedule"
HOOK_GROUP = "Hooks"
VOLUME_GROUP = "Work volume"

@dataclass(frozen=True)
class Line:
    tone: str
    text: str
    group: str = NONE


class Report:
    """The lines of one check, in the order the operator should read them."""

    def __init__(self) -> None:
        self.lines: list[Line] = []

    def note(self, tone: str, text: str, *, group: str = NONE) -> "Report":
        self.lines.append(Line(tone, text, group))
        return self

    def ok(self, text: str, *, group: str = NONE) -> "Report":
        return self.note(OK, text, group=group)

    def bad(self, text: str, *, group: str = NONE) -> "Report":
        return self.note(BAD, text, group=group)

    def warn(self, text: str, *, group: str = NONE) -> "Report":
        return self.note(WARN, text, group=group)

    def info(self, text: str, *, group: str = NONE) -> "Report":
        return self.note(INFO, text, group=group)

    def adopt(self, other: "Report", *, group: str = NONE) -> "Report":
        """Add another report under one heading."""
        for line in other.lines:
            self.lines.append(Line(line.tone, line.text, group or line.group))
        return self

    @property
    def passed(self) -> bool:
        return not any(line.tone == BAD for line in self.lines)

    def sections(self) -> list[dict]:
        """The grouped lines, for the fragment template."""
        found: list[dict] = []
        for line in self.lines:
            if not found or found[-1]["title"] != line.group:
                found.append({"title": line.group, "lines": []})
            found[-1]["lines"].append(
                {"icon": ICONS[line.tone], "text": line.text, "tone": line.tone}
            )
        return found


def source(row: Source, *, password: str | None = None, group: str = NONE) -> Report:
    """Check one source. A `None` password is read from stored ciphertext."""
    if row.kind == Source.Kind.FOLDER:
        return folder(
            root_path=row.root_path,
            include_rules=row.include_rules,
            exclude_rules=row.exclude_rules,
            group=group,
        )
    if password is None:
        secret, failed = secret_of(row.pg_password)
        if failed is not None:
            return failed
        password = secret
    return database(
        host=row.pg_host,
        port=row.pg_port,
        pg_database=row.pg_database,
        user=row.pg_user,
        password=password,
        dump_binary=row.pg_dump_binary,
        group=group,
    )


def target(
    row: Target,
    *,
    access_key: str | None = None,
    secret: str | None = None,
    group: str = NONE,
) -> Report:
    """Check one target. A `None` credential is read from stored ciphertext."""
    if access_key is None:
        access_key, failed = secret_of(row.access_key_id)
        if failed is not None:
            return failed
    if secret is None:
        secret, failed = secret_of(row.secret_access_key)
        if failed is not None:
            return failed
    return s3(
        bucket=row.bucket,
        endpoint=row.endpoint_url,
        region=row.region,
        access_key=access_key,
        secret=secret,
        path_style=row.path_style,
        group=group,
    )


def s3(
    *,
    bucket: str,
    endpoint: str,
    region: str,
    access_key: str,
    secret: str,
    path_style: bool,
    group: str = NONE,
    client=None,
) -> Report:
    """Try the calls a run makes: list, a multipart upload started and
    aborted, write, read back, delete.

    The probe object is a few bytes under `.kopi-check/`, a name that is not
    `source/timestamp/checksum`. `S3Storage.list` skips such names, so the
    probe is invisible to retention and to the run list, and it is deleted
    before this returns.
    """
    report = Report()
    if not bucket:
        return report.bad("A target needs a bucket.", group=group)
    if not access_key or not secret:
        return report.bad("This target has no access key and secret to try.", group=group)
    if not endpoint and not region:
        report.warn(
            "No endpoint and no region. Kopi will look for the garage default.", group=group
        )
    built = client is None
    if built:
        try:
            client = probe_client(
                endpoint_url=endpoint,
                region=region or "garage",
                access_key_id=access_key,
                secret_access_key=secret,
                path_style=path_style,
            )
        except ImportError:
            return report.bad("boto3 is not installed in this image.", group=group)
        except Exception as exc:
            return report.bad(_s3_detail(exc, bucket), group=group)
    try:
        _walk_storage(report, client, bucket, group=group)
    finally:
        close = getattr(client, "close", None)
        if built and callable(close):
            close()
    return report


def folder(
    *,
    root_path: str,
    include_rules: str = "",
    exclude_rules: str = "",
    group: str = NONE,
) -> Report:
    """Whether the tree can be archived, and how much of it the rules match."""
    report = Report()
    text = str(root_path or "").strip()
    if not text:
        return report.bad("A folder source needs a root path.", group=group)
    root = Path(text)
    if root.is_symlink():
        return report.bad(
            f"{root} is a symlink. Kopi archives a directory, not a link.", group=group
        )
    if not root.is_dir():
        return report.bad(f"{root} is not a directory.", group=group)
    if not os.access(root, os.R_OK | os.X_OK):
        return report.bad(f"Kopi cannot read {root}. Check the owner and the mode.", group=group)

    total, unreadable = _count_members(root, include_rules, exclude_rules)
    if unreadable:
        report.bad(
            f"Kopi cannot read {unreadable[0]}."
            + ("" if len(unreadable) == 1 else f" {len(unreadable) - 1} more are unreadable."),
            group=group,
        )
    if total == 0:
        return report.bad(
            f"{root} has nothing these rules match. The archive would be empty.", group=group
        )
    report.ok(f"{root} is a directory Kopi can read.", group=group)
    word = "entry" if total == 1 else "entries"
    shown = f"{total} or more" if total >= MEMBER_SAMPLE else str(total)
    return report.info(f"The archive would hold {shown} {word}.", group=group)


def database(
    *,
    host: str,
    port: int,
    pg_database: str,
    user: str,
    password: str,
    dump_binary: str,
    timeout: int = PROBE_TIMEOUT,
    group: str = NONE,
) -> Report:
    """One connection attempt, then a look at the dump binary that would follow."""
    report = Report()
    reason = pooler_reason(host, port)
    if reason:
        report.warn(f"{reason}. {POOLER_EXPLAINED}", group=group)
    if not password:
        return report.bad("There is no password to try.", group=group)

    directory = _scratch()
    try:
        probe = probe_database(
            binary=dump_binary,
            host=str(host or ""),
            port=int(port or 0),
            user=user,
            database=pg_database,
            password=password,
            tmpdir=directory,
            timeout=timeout,
        )
    finally:
        shutil.rmtree(directory, ignore_errors=True)

    if not probe.ok:
        report.bad(probe.detail, group=group)
    else:
        report.ok(f"{host}:{port} answered. {pg_database} opened as {user}.", group=group)
    return _dump_binary(report, dump_binary, probe, group=group)


def link(
    *,
    source_row: Source,
    target_row: Target,
    pre_script_path: str = "",
    post_script_path: str = "",
    cadence: str = "",
    timezone_name: str = "",
    enabled: bool = True,
    now: datetime | None = None,
) -> Report:
    """Would this job run? Source, target, schedule, hooks, and free space."""
    report = Report()
    report.adopt(source(source_row, group=SOURCE_GROUP), group=SOURCE_GROUP)
    report.adopt(target(target_row, group=TARGET_GROUP), group=TARGET_GROUP)
    report.adopt(
        _schedule(cadence, timezone_name, enabled=enabled, now=now or timezone.now()),
        group=SCHEDULE_GROUP,
    )
    report.adopt(_hooks(pre_script_path, post_script_path), group=HOOK_GROUP)
    report.adopt(volume(), group=VOLUME_GROUP)
    return report


def volume(*, group: str = VOLUME_GROUP) -> Report:
    """Free bytes on the work volume, against the floor the worker demands."""
    report = Report()
    directory = Path(work_directory())
    try:
        directory.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(directory).free
    except OSError:
        return report.bad(f"{directory} is not a usable directory.", group=group)
    if not os.access(directory, os.W_OK | os.X_OK):
        return report.bad(f"{directory} is not writable by Kopi.", group=group)
    floor = configured_disk_floor()
    if floor and free < floor:
        return report.bad(
            f"{directory} has {format_size(free)} free, under the {format_size(floor)} floor."
            " The worker will not start a run there.",
            group=group,
        )
    text = f"{directory} has {format_size(free)} free."
    if floor:
        text = f"{text} The floor is {format_size(floor)}."
    return report.ok(text, group=group)


def secret_of(stored: str) -> tuple[str, Report | None]:
    """The plaintext for a stored secret, or a report that says why there is none."""
    if not stored:
        return "", Report().bad("There is nothing stored to try.")
    try:
        identity = open_identity()
    except IdentityError:
        return "", Report().bad("secret.key cannot be read.")
    try:
        return decrypt_secret(identity, stored), None
    except IdentityError:
        return "", Report().bad("secret.key cannot decrypt this credential. Set it again.")


def form_report(form) -> Report:
    """Why the form itself refuses these values, as lines.

    A field has to be filled before there is anything to reach across the
    network, so a form that does not validate never starts a probe.
    """
    report = Report()
    seen: set[str] = set()
    for messages in form.errors.values():
        for error in messages:
            text = str(error)
            if text not in seen:
                seen.add(text)
                report.bad(text)
    return report


def _walk_storage(report: Report, client, bucket: str, group: str) -> None:
    """List, start a multipart upload, write, read back, delete. Stop at the first refusal.

    Every S3 client error is caught by name in `_s3_detail`. A check that
    raised would end the request with a server error instead of a line the
    operator can read.
    """
    try:
        client.list_objects_v2(Bucket=bucket, MaxKeys=1)
    except Exception as exc:
        report.bad(_s3_detail(exc, bucket), group=group)
        return
    report.ok(f"The bucket {bucket} answered. Kopi can list it.", group=group)

    key = f"{CHECK_PREFIX}{uuid.uuid4().hex}"
    try:
        started = client.create_multipart_upload(Bucket=bucket, Key=key)
    except Exception as exc:
        report.bad(_s3_detail(exc, bucket), group=group)
        return
    try:
        client.abort_multipart_upload(Bucket=bucket, Key=key, UploadId=started["UploadId"])
    except Exception:
        report.warn(
            "Kopi could not abort that upload. A run aborts an upload it cannot"
            " finish, so this bucket needs the abort right.",
            group=group,
        )
    report.ok("Kopi can start a multipart upload in it.", group=group)

    payload = b"kopi connection check\n"
    try:
        client.put_object(Bucket=bucket, Key=key, Body=payload)
    except Exception as exc:
        report.bad(_s3_detail(exc, bucket), group=group)
        return

    wrote = True
    try:
        body = client.get_object(Bucket=bucket, Key=key)["Body"]
        if body.read(len(payload) + 1) != payload:
            wrote = False
            report.bad(
                f"The probe object did not read back in {bucket}."
                " A backup would still land, but nothing in this bucket could be restored.",
                group=group,
            )
    except Exception as exc:
        wrote = False
        report.bad(
            f"The probe object was written, but this endpoint would not give it back:"
            f" {_s3_detail(exc, bucket)} A backup would land here; nothing could be restored.",
            group=group,
        )

    try:
        client.delete_object(Bucket=bucket, Key=key)
    except Exception as exc:
        report.bad(
            f"{_s3_detail(exc, bucket)} Retention deletes objects, and the probe object {key}"
            " is still in the bucket. Remove it by hand.",
            group=group,
        )
        return
    if wrote:
        report.ok("Kopi wrote, read back, and removed a probe object.", group=group)


def _s3_detail(exc: Exception, bucket: str) -> str:
    """One sentence about a storage failure, from the code the endpoint gave."""
    from botocore.exceptions import BotoCoreError, ClientError

    if isinstance(exc, ClientError):
        error = (exc.response.get("Error") or {}) if isinstance(exc.response, dict) else {}
        code = str(error.get("Code", ""))
        if code in {"NoSuchBucket", "404", "NotFound"}:
            return f"There is no bucket named {bucket} at this endpoint."
        if code in {"403", "AccessDenied", "Forbidden"}:
            return f"The credentials cannot use {bucket}."
        if code == "InvalidAccessKeyId":
            return "That access key is not known at this endpoint."
        if code == "SignatureDoesNotMatch":
            return "That secret does not match this access key."
        if code in {"PermanentRedirect", "BadRequest"}:
            return f"{bucket} is not served at this endpoint. Check the endpoint and the region."
        sentence = str(error.get("Message") or code or "").strip()
        if sentence:
            return sentence[:200]
    if isinstance(exc, BotoCoreError):
        name = exc.__class__.__name__
        if name in {
            "EndpointConnectionError",
            "ConnectTimeoutError",
            "NewConnectionError",
            "ProxyConnectionError",
        }:
            return "The endpoint did not answer. Check the endpoint, the port, and the firewall."
        if name == "InvalidEndpointURL":
            return "That endpoint is not a URL. Write http://host:port or https://host."
        if name == "NoRegionError":
            return "Set a region, an endpoint, or both."
        first = str(exc).splitlines()[0] if str(exc) else name
        return first[:200]
    return str(exc).splitlines()[0][:200] if str(exc) else "the request failed"


def _count_members(root: Path, includes: str, excludes: str) -> tuple[int, list[str]]:
    """Members the rules match, up to the sample, and the directories that refused the walk."""
    unreadable: list[str] = []

    def note(error: OSError) -> None:
        unreadable.append(os.fspath(getattr(error, "filename", root)))

    total = 0
    for _ in folder_member_paths(root, includes, excludes, onerror=note):
        total += 1
        if total >= MEMBER_SAMPLE:
            break
    return total, unreadable


def _dump_binary(report: Report, dump_binary: str, probe: Probe, group: str) -> Report:
    """A dump binary this host cannot run, or one older than the server, fails later."""
    if not dump_binary:
        return report.bad("This source names no dump binary.", group=group)
    found = binary_version(dump_binary)
    if not found:
        return report.bad(f"{dump_binary} cannot be run on this host.", group=group)
    if not probe.ok or not probe.version:
        return report
    if version_major(found) < version_major(probe.version):
        return report.bad(
            f"That pg_dump is {found} and the server is {probe.version}."
            " A dump binary older than the server refuses to dump it.",
            group=group,
        )
    return report.info(f"pg_dump {found} against server {probe.version}.", group=group)


def _schedule(cadence: str, zone_name: str, *, enabled: bool, now: datetime) -> Report:
    report = Report()
    if not enabled:
        report.warn("This link is retired. The schedule will not start it.", group=SCHEDULE_GROUP)
    moment = next_run_at(cadence, zone_name, now)
    if moment is None:
        return report.bad("That cron never fires in that timezone.", group=SCHEDULE_GROUP)
    local = moment.astimezone(zone_of(zone_name))
    return report.ok(
        f"The next run is {stamp(local)} {zone_name}.",
        group=SCHEDULE_GROUP,
    )


def _hooks(pre_script_path: str, post_script_path: str) -> Report:
    report = Report()
    if not has_bash():
        report.bad("bash is not installed, so Kopi could not run a hook script.", group=HOOK_GROUP)
    for label, value in (("Pre", pre_script_path), ("Post", post_script_path)):
        resolved, reason = resolve_hook(value)
        if resolved is None:
            if reason:
                report.bad(f"{label} script {reason}.", group=HOOK_GROUP)
            else:
                report.info(f"{label} script is not set. That step is skipped.", group=HOOK_GROUP)
            continue
        report.ok(f"{label} script {resolved} is a file Kopi may run.", group=HOOK_GROUP)
    return report


def _scratch() -> Path:
    root = Path(tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="kopi-check-"))

