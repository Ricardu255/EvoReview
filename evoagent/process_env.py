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
