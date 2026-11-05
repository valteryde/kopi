"""The rule a hook path has to satisfy, shared by the run child and the check.

A link stores a path to a script that already sits on disk. Kopi runs it as
`bash <path>` as the Kopi user. The path has to name a regular file that is
not one of Kopi's own secrets and not on the work volume, where a script
could reach the ciphertext of a run in progress.
"""

from pathlib import Path
from shutil import which

from kopi.identity import IDENTITY_FILENAME
from kopi.sessionkey import FILENAME as SESSION_FILENAME
from kopi.settings import work_directory

BLOCKED_NAMES = (IDENTITY_FILENAME, SESSION_FILENAME, "kopi.sqlite3")
NOT_A_FILE = "is not a regular file"
NOT_ALLOWED = "is not allowed"


def resolve_hook(path_str: str) -> tuple[Path | None, str]:
    """`(path, "")` when Kopi may run the script, `(None, reason)` when it may not.

    A blank path is not a refusal. `(None, "")` means the hook is skipped and
    the run goes straight to the archive.
    """
    raw = str(path_str or "").strip()
    if not raw:
        return None, ""
    path = Path(raw)
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return None, f"{path} {NOT_A_FILE}"
    if resolved.is_symlink() or not resolved.is_file():
        return None, f"{path} {NOT_A_FILE}"
    if _blocked(resolved):
        return None, f"{path} {NOT_ALLOWED}"
    return resolved, ""


def has_bash() -> bool:
    """Whether the interpreter Kopi runs a hook with is installed."""
    return which("bash") is not None


def _blocked(path: Path) -> bool:
    """The key, the session key, the database, and anything on the work volume."""
    if path.name in BLOCKED_NAMES:
        return True
    try:
        work = Path(work_directory()).resolve()
    except OSError:
        return False
    return path == work or _is_inside(path, work)


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True
