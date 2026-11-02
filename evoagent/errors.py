"""Domain-neutral routing errors shared by application and queue adapters."""


class PermanentTaskError(RuntimeError):
    """An error that must not be retried."""


class TaskCancelled(RuntimeError):
    """A task was cancelled on purpose; delivery must not retry or alert."""


class ModelNotConfigured(RuntimeError):
    """The requested review path needs a model, but none is configured."""


class AuthenticationRequired(PermissionError):
    """The caller did not present a usable credential (HTTP 401).

    Kept separate from a real authorization failure so the API can answer 401
    for missing/expired tokens and 403 only for an authenticated caller that
    lacks the required permission.
    """
