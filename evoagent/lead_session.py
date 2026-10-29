"""Compatibility wrapper for :mod:`evoagent.agent.lead_session`."""
import warnings

warnings.warn(
    "This top-level module is a compatibility wrapper; import from the new package path instead.",
    DeprecationWarning,
    stacklevel=2,
)

from .agent import lead_session as _impl

__all__ = getattr(
    _impl, "__all__",
    [name for name in dir(_impl) if not name.startswith("_")],
)


def __getattr__(name):
    return getattr(_impl, name)


def __dir__():
    return sorted(set(globals()) | set(__all__))
