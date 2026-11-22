"""Restricted process environments for subprocesses that run repository code.

``RepositoryToolSuite.run_scanners``/``run_repository_checks`` and
``RepairVerifier.verify_worktree`` all execute code that originates from a
reviewed (possibly attacker-controlled) repository.  They must therefore share
one allowlist: enough to let toolchains resolve binaries, temp directories and
per-user caches, but never credentials.

Nothing outside ``ALLOWED_ENV_KEYS`` is forwarded, so secrets such as
``EVOAGENT_*``, ``GITHUB_TOKEN``, ``AWS_*``, ``OPENROUTER_API_KEY`` or
``DATABASE_URL`` cannot leak into a child process.
"""
import os
import shlex
import shutil
import sys
from typing import Dict, Mapping, Optional

# Windows entries are harmless on POSIX; keeping a single list avoids the two
# call sites drifting apart again (a narrower copy in the repair verifier used
# to make Windows test gates fail for missing APPDATA/LOCALAPPDATA).
ALLOWED_ENV_KEYS = frozenset({
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC",
    "LANG", "LC_ALL", "LC_CTYPE", "TMP", "TEMP", "TMPDIR",
    "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "HOME", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "NUMBER_OF_PROCESSORS",
})


def restricted_env(
    extra: Optional[Mapping[str, str]] = None,
) -> Dict[str, str]:
    """Return only the allowlisted variables from ``os.environ``.

    ``extra`` is merged afterwards for values the caller sets deliberately
    (for example a private ``TMPDIR``); it is never read from the environment
    of the parent process.
    """
    env = {
        key: value for key, value in os.environ.items()
        if key in ALLOWED_ENV_KEYS
    }
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath(
            [os.path.realpath(root), os.path.realpath(path)]
        ) == os.path.realpath(root)
    except ValueError:
        return False


def resolve_command(command: str, root: str = "") -> list:
    """Resolve the executable of a configured test command to an absolute path.

    The command itself is administrator configuration, but it runs in a
    directory controlled by the reviewed repository.  On Windows, resolving a
    bare name through ``CreateProcess`` can execute an attacker-planted
    ``pytest.exe`` from the current directory.  Resolve bare names through a
    sanitized PATH that excludes entries inside the checkout, and return an
    argument list with an absolute executable so the child process never
    searches the checkout for an implicit command.  Explicit paths such as
    ``./scripts/test.sh`` remain allowed because the operator named them.
    """
    parts = shlex.split(str(command or ""), posix=os.name != "nt")
    if not parts:
        raise ValueError("test command is empty")
    # ``shlex.split(posix=False)`` keeps surrounding quotes on Windows, so a
    # quoted absolute executable such as "C:\Program Files\...\python.exe"
    # must be unquoted before it is stat'ed.
    executable = parts[0].strip("\"'")
    if not executable:
        raise ValueError("test executable is empty")
    root_path = os.path.realpath(root) if root else ""
    path_like = (
        os.path.isabs(executable) or os.sep in executable
        or "\\" in executable or (os.altsep is not None and os.altsep in executable)
    )
    if path_like:
        # An explicit path is administrator configuration, including a
        # repository script such as ./scripts/test.sh.  Resolve it relative
        # to the checkout so the child process sees the same file.
        if os.path.isabs(executable):
            candidate = os.path.realpath(executable)
        else:
            candidate = os.path.realpath(
                os.path.join(root_path or os.getcwd(), executable)
            )
        if not os.path.isfile(candidate):
            raise ValueError("test executable does not exist: %s" % executable)
    else:
        entries = []
        for entry in os.environ.get("PATH", "").split(os.pathsep):
            if not entry or entry in {".", ".."}:
                continue
            if root_path and _within(entry, root_path):
                continue
            entries.append(entry)
        candidate = (
            shutil.which(executable, path=os.pathsep.join(entries))
            if entries else None
        )
        from_path = candidate is not None
        if candidate is None and root_path:
            # Preserve repository wrapper scripts such as ``gradlew.bat`` or
            # ``mvnw`` that are not installed on PATH.  Only fall back to the
            # checkout when the name was not resolved from PATH, so a planted
            # ``pytest.exe`` cannot shadow an installed tool.
            candidate = shutil.which(executable, path=root_path)
        if candidate is None and executable.lower() in {
            "python", "python3", "python.exe", "python3.exe",
        }:
            candidate = sys.executable
        if candidate is None:
            raise ValueError("test executable was not found: %s" % executable)
        candidate = os.path.realpath(candidate)
        if from_path and root_path and _within(candidate, root_path):
            raise ValueError("test executable must not come from the repository")
        if not os.path.isfile(candidate):
            raise ValueError("test executable does not exist: %s" % executable)
    return [candidate] + parts[1:]
