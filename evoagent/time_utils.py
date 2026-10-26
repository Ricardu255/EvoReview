"""UTC timestamps shared by runtime and persistence adapters."""
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
