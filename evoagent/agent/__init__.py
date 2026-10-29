"""Agent runtime modules split out of the original agentic core."""
__all__ = ["AgenticReviewer"]


def __getattr__(name):
    if name == "AgenticReviewer":
        from .reviewer import AgenticReviewer
        return AgenticReviewer
    raise AttributeError(name)
