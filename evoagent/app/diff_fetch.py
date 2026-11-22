"""Fetch the initial diff for a queued review task."""
from typing import Any, Dict

from ..errors import PermanentTaskError


def fetch_initial_diff(client: Any, payload: Dict[str, Any]) -> str:
    """Return the diff for a queued task without trusting a webhook URL.

    A pull-request task asks the client to derive the diff from the
    repository and number.  The older ``diff_url`` path remains for callers
    whose client predates ``fetch_pull_diff``.
    """
    client.ensure_repository_access(payload["repository"])
    fetch_pull_diff = getattr(client, "fetch_pull_diff", None)
    if payload.get("pull_request") is not None and callable(fetch_pull_diff):
        return fetch_pull_diff(
            payload["repository"], payload["pull_request"],
        )
    if payload.get("diff_url"):
        return client.fetch_diff(payload["diff_url"])
    raise PermanentTaskError("task has no pull request or diff URL to fetch")
