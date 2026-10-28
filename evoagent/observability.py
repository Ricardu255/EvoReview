"""Compatibility wrapper for :mod:`evoagent.adapters.observability`."""
from .adapters import observability as _impl

__all__ = getattr(
    _impl, "__all__",
    [name for name in dir(_impl) if not name.startswith("_")],
)


def __getattr__(name):
    return getattr(_impl, name)


def __dir__():
    return sorted(set(globals()) | set(__all__))
