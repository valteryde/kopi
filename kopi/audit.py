"""Audit rows for sign-in, setup, sources, targets, links, and runs.

The sentence is stored when the event happens. Job stdout stays on the run.
"""

from kopi.models import AuditEvent

LOGIN = "login"
SETUP = "setup"
KEY = "key"
RUN_STARTED = "run_started"
RUN_FINISHED = "run_finished"
RUN_FAILED = "run_failed"
SOURCE_CREATED = "source_created"
SOURCE_CHANGED = "source_changed"
TARGET_CREATED = "target_created"
TARGET_CHANGED = "target_changed"
LINK_CREATED = "link_created"
LINK_CHANGED = "link_changed"
LINK_QUEUED = "link_queued"

ICONS = {
    LOGIN: "sign-in",
    SETUP: "check-circle",
    KEY: "key",
    RUN_STARTED: "play",
    RUN_FINISHED: "check",
    RUN_FAILED: "x",
    SOURCE_CREATED: "folder-plus",
    SOURCE_CHANGED: "pencil-simple",
    TARGET_CREATED: "cloud",
    TARGET_CHANGED: "pencil-simple",
    LINK_CREATED: "link",
    LINK_CHANGED: "pencil-simple",
    LINK_QUEUED: "play",
}

WORKER_NAME = "Worker"


def note_login(user) -> None:
    _user(user, LOGIN, "Login")


def note_setup(user) -> None:
    _user(user, SETUP, "Setup complete · first admin created")


def note_key(user) -> None:
    _user(user, KEY, "Key generated · offered for download once")


def note_source_created(user, name: str) -> None:
    _user(user, SOURCE_CREATED, f"Source created · {name}")


def note_source_changed(user, name: str) -> None:
    _user(user, SOURCE_CHANGED, f"Source changed · {name}")


def note_target_created(user, name: str) -> None:
    _user(user, TARGET_CREATED, f"Target created · {name}")


def note_target_changed(user, name: str) -> None:
    _user(user, TARGET_CHANGED, f"Target changed · {name}")


def note_link_created(user, source: str, target: str) -> None:
    _user(user, LINK_CREATED, f"Link created · {source} → {target}")


def note_link_changed(user, source: str, target: str) -> None:
    _user(user, LINK_CHANGED, f"Link changed · {source} → {target}")


def note_link_queued(user, source: str, target: str) -> None:
    _user(user, LINK_QUEUED, f"Run queued · {source} → {target}")


def note_run_started(source: str, target: str) -> None:
    _worker(RUN_STARTED, f"Run started · {source} → {target}")


def note_run_finished(source: str, size: int | None) -> None:
    text = f"Run finished · {source} succeeded"
    if size is not None:
        text = f"{text} · {format_size(size)}"
    _worker(RUN_FINISHED, text)


def note_run_failed(source: str, reason: str) -> None:
    text = f"Run failed · {source}"
    if reason:
        text = f"{text} · {reason}"
    _worker(RUN_FAILED, text)


def format_size(size: int) -> str:
    value = float(size)
    for label in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or label == "TB":
            if value == int(value):
                shown = str(int(value))
            else:
                shown = f"{value:.1f}"
            return f"{shown} {label}"
        value /= 1024
    return f"{size} B"


def _user(user, kind: str, event: str) -> None:
    AuditEvent.objects.create(
        actor=AuditEvent.Actor.USER,
        user=user,
        name=user.get_username(),
        kind=kind,
        event=event,
    )


def _worker(kind: str, event: str) -> None:
    AuditEvent.objects.create(
        actor=AuditEvent.Actor.WORKER,
        name=WORKER_NAME,
        kind=kind,
        event=event,
    )
