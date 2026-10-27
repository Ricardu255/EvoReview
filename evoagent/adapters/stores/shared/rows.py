"""Decode rows that carry JSON or timestamps from either SQL backend."""
import json
from typing import Any, Dict


def decode_json(value: Any) -> Any:
    """SQLite returns JSON text; psycopg already returns Python values."""
    return json.loads(value) if isinstance(value, str) else value


def memory_from_row(row) -> Dict[str, Any]:
    value = dict(row)
    value["keywords"] = decode_json(value.pop("keywords_json"))
    value["metadata"] = decode_json(value.pop("metadata_json"))
    for key in ("created_at", "expires_at"):
        if hasattr(value.get(key), "isoformat"):
            value[key] = value[key].isoformat()
    return value


def skill_artifact_from_row(row) -> Dict[str, Any]:
    value = dict(row)
    value["artifact"] = decode_json(value.pop("artifact_json"))
    value["active"] = bool(value["active"])
    if hasattr(value.get("created_at"), "isoformat"):
        value["created_at"] = value["created_at"].isoformat()
    return value
