"""Compatibility re-exports for the split agent runtime."""
import warnings

warnings.warn(
    "This top-level module is a compatibility wrapper; import from the new package path instead.",
    DeprecationWarning,
    stacklevel=2,
)

from .agent.reviewer import (
    AgenticReviewer, MAX_TRACKED_SUMMARIES, SUMMARY_CHECKPOINT,
)
from .agent.bounded_role import BoundedRole

__all__ = [
    "AgenticReviewer", "BoundedRole",
    "MAX_TRACKED_SUMMARIES", "SUMMARY_CHECKPOINT",
]
