import re
from dataclasses import dataclass
from typing import List

from .models import ChangedLine


@dataclass
class ParsedDiff:
    files: List[str]
    added_lines: List[ChangedLine]


HUNK = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@"
)


def parse_unified_diff(diff: str) -> ParsedDiff:
    """Parse a unified diff without guessing from prefix characters.

    Hunk headers declare how many old/new lines follow.  Consuming exactly that
    many body lines is the only reliable way to tell a file header (``+++ b/x``)
    from an added line whose content itself starts with ``++ `` (rendered as
    ``+++ ``).  Prefix-only detection silently drops such lines.
    """
    files: List[str] = []
    added: List[ChangedLine] = []
    current_path = ""
    new_line = 0
    old_remaining = new_remaining = 0
    in_hunk = False

    def finish_hunk_if_complete() -> None:
        nonlocal in_hunk
        if old_remaining <= 0 and new_remaining <= 0:
            in_hunk = False

    for raw in diff.splitlines():
        if in_hunk:
            if raw.startswith("\\ No newline"):
                continue
            if raw.startswith("+"):
                added.append(ChangedLine(current_path or "unknown", new_line, raw[1:]))
                new_line += 1
                new_remaining -= 1
            elif raw.startswith("-"):
                old_remaining -= 1
            else:
                # Context line.  A blank line in a diff may have lost its
                # leading space in transport; treat it as context.
                new_line += 1
                old_remaining -= 1
                new_remaining -= 1
            finish_hunk_if_complete()
            continue

        if raw.startswith("+++ "):
            current_path = raw[4:].strip().split("\t", 1)[0]
            if current_path.startswith("b/"):
                current_path = current_path[2:]
            if current_path != "/dev/null" and current_path not in files:
                files.append(current_path)
            continue

        match = HUNK.match(raw)
        if match:
            old_remaining = int(match.group("old_count") or 1)
            new_remaining = int(match.group("new_count") or 1)
            new_line = int(match.group("new_start"))
            in_hunk = old_remaining > 0 or new_remaining > 0
            continue

    return ParsedDiff(files=files, added_lines=added)

