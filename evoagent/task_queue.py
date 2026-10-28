"""Compatibility wrapper for :mod:`evoagent.adapters.task_queue`."""
from .adapters import task_queue as _impl

__all__ = getattr(
    _impl, "__all__",
    [name for name in dir(_impl) if not name.startswith("_")],
)


def __getattr__(name):
    return getattr(_impl, name)


def __dir__():
    return sorted(set(globals()) | set(__all__))
