"""Die with the worker, and take the tools this process started with it.

Linux can deliver SIGKILL when the parent dies. Every host also watches a
pipe the worker holds. When that pipe closes, this process kills its
process group. That covers a host without PDEATHSIG, and a parent that
exits before the death signal is armed.
"""

import os
import signal
import subprocess
import threading

PARENT_FD_ENV = "KOPI_PARENT_FD"
_PR_SET_PDEATHSIG = 1

_prctl = None
_resolved = False


def _load_prctl():
    global _prctl, _resolved
    if _resolved:
        return _prctl
    _resolved = True
    try:
        import ctypes

        lib = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None
    if not hasattr(lib, "prctl"):
        return None
    _prctl = lib.prctl
    return _prctl


def arm_pdeathsig() -> bool:
    """Ask the kernel to SIGKILL this process when its parent dies."""
    fn = _load_prctl()
    if fn is None:
        return False
    return fn(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) == 0


def preexec():
    """A `preexec_fn` for tools, or None when this host has no PDEATHSIG."""
    fn = _load_prctl()
    if fn is None:
        return None

    def _arm():
        fn(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)

    return _arm


def popen_kwargs() -> dict:
    """Keyword arguments that arm parent-death on a spawned tool."""
    fn = preexec()
    if fn is None:
        return {}
    return {"preexec_fn": fn}


def watch_parent() -> None:
    """Arm parent-death handling before the run does any work."""
    parent = os.getppid()
    arm_pdeathsig()
    if os.getppid() != parent:
        raise SystemExit(1)
    raw = os.environ.pop(PARENT_FD_ENV, "")
    if raw == "":
        return
    fd = int(raw)
    threading.Thread(target=_watch, args=(fd,), name="parent-pipe", daemon=True).start()


def _watch(fd: int) -> None:
    try:
        while os.read(fd, 64):
            pass
    except OSError:
        pass
    _kill_group()


def _kill_group() -> None:
    # Hooks live in their own process group. Signal those first, then the
    # group that holds tar, zstd, age, and pg_dump.
    me = os.getpid()
    try:
        listed = subprocess.run(
            ["pgrep", "-P", str(me)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        listed = None
    if listed is not None:
        for line in listed.stdout.split():
            try:
                pid = int(line)
            except ValueError:
                continue
            try:
                os.killpg(pid, signal.SIGKILL)
            except OSError:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
    try:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    except OSError:
        try:
            os.kill(me, signal.SIGKILL)
        except OSError:
            pass
