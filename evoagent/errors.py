"""Domain-neutral routing errors shared by application and queue adapters."""


class PermanentTaskError(RuntimeError):
    """An error that must not be retried."""


class ModelNotConfigured(RuntimeError):
    """The requested review path needs a model, but none is configured."""
