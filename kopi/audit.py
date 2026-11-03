"""Audit rows for sign-in, setup, and runs.

The sentence is stored when the event happens. Job stdout stays on the run.
"""

from kopi.models import AuditEvent

LOGIN = "login"
SETUP = "setup"
KEY = "key"
RUN_STARTED = "run_started"
RUN_FINISHED = "run_finished"
RUN_FAILED = "run_failed"

ICONS = {
    LOGIN: "sign-in",
    SETUP: "check-circle",
    KEY: "key",
    RUN_STARTED: "play",
    RUN_FINISHED: "check",
    RUN_FAILED: "x",
}

WORKER_NAME = "Worker"


def note_login(user) -> None:
    _user(user, LOGIN, "Login")


def note_setup(user) -> None:
    _user(user, SETUP, "Setup complete · first admin created")


def note_key(user) -> None:
    _user(user, KEY, "Key generated · offered for download once")


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
