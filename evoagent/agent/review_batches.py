"""Lossless text coverage and deterministic packing for a unified diff.

Ported from the parallel EvoAgnet branch.  ``plan_batches`` splits a diff into
token-bounded batches that together cover every changed line (including
fragmented oversized lines), so the batched review engine reviews a large PR
without lossy compression.  Only ``estimate_tokens`` and the repository tool
suite are shared with the rest of the agent package; everything else here is a
pure function over the diff text.
"""

import ast
import os
import re
import shlex
from dataclasses import dataclass

from .context_manager import estimate_tokens


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")


def token_bound(value: str) -> int:
    """Conservative upper bound for the token cost of a rendered diff part."""
    return (estimate_tokens(value) * 5 + 3) // 4 + 32


@dataclass(frozen=True)
class Change:
    old_path: str
    new_path: str
    hunk: int
    side: str
    line: int
    old_cursor: int
    new_cursor: int
    text: str
    offset: int = 0

    @property
    def path(self):
        return self.old_path if self.new_path == "/dev/null" else self.new_path


def _path(value, prefix):
    value = value.split("\t", 1)[0].strip()
    if value.startswith('"'):
        # Git quotes non-ASCII bytes and control characters using C escapes.
        try:
            value = ast.literal_eval("b" + value).decode("utf-8", errors="replace")
        except (SyntaxError, ValueError):
            value = value.strip('"')
    return value[len(prefix):] if value.startswith(prefix) else value


def parse_changes(diff):
    changes, metadata = [], []
    old_path = new_path = ""
    old = new = old_left = new_left = hunk = 0
    binary = False
    for raw in (diff[:-1] if diff.endswith("\n") else diff).split("\n"):
        if raw.startswith("diff --git "):
            try:
                paths = shlex.split(raw[len("diff --git "):], posix=False)
            except ValueError:
                paths = []
            old_path = _path(paths[0], "a/") if paths else ""
            new_path = _path(paths[1], "b/") if len(paths) > 1 else old_path
            if old_left or new_left:
                raise ValueError("incomplete unified diff hunk")
            binary = False
            continue
        if binary:
            continue
        if raw.startswith(("GIT binary patch", "Binary files ")):
            metadata.append({"path": new_path or old_path, "kind": "binary"})
            binary = True
            continue
        if raw.startswith(("rename from ", "rename to ", "old mode ", "new mode ")):
            metadata.append({"path": new_path or old_path, "kind": raw.split(" ", 2)[0], "detail": raw})
            continue
        match = HUNK.match(raw)
        if match:
            if old_left or new_left or not old_path or not new_path:
                raise ValueError("invalid unified diff hunk")
            old, new = int(match[1]), int(match[3])
            old_left, new_left = int(match[2] or 1), int(match[4] or 1)
            hunk += 1
            continue
        if not old_left and not new_left:
            if raw.startswith("--- "):
                old_path = _path(raw[4:], "a/")
            elif raw.startswith("+++ "):
                new_path = _path(raw[4:], "b/")
            continue
        if raw.startswith("\\ No newline"):
            continue
        # A blank line inside a hunk may have lost its leading space in
        # transport; treat it as an empty context line like the domain parser.
        side = raw[:1] if raw else " "
        if side not in {"+", "-", " "}:
            raise ValueError("invalid unified diff line inside hunk")
        text = raw[1:] if raw else ""
        changes.append(Change(old_path, new_path, hunk, side, old if side == "-" else new,
                              old, new, text))
        old += side != "+"
        new += side != "-"
        old_left -= side != "+"
        new_left -= side != "-"
        if old_left < 0 or new_left < 0:
            raise ValueError("invalid unified diff hunk lengths")
    if old_left or new_left:
        raise ValueError("incomplete unified diff hunk")
    if not any(row.side != " " for row in changes) and not metadata:
        raise ValueError("diff contains no reviewable changes")
    return changes, metadata


def _render(rows):
    if not rows:
        return ""
    first = rows[0]
    old_count = sum(row.side != "+" for row in rows)
    new_count = sum(row.side != "-" for row in rows)
    old_path = first.old_path if first.old_path == "/dev/null" else "a/" + first.old_path
    new_path = first.new_path if first.new_path == "/dev/null" else "b/" + first.new_path
    return "\n".join([
        f"--- {old_path}", f"+++ {new_path}",
        f"@@ -{first.old_cursor},{old_count} +{first.new_cursor},{new_count} @@",
        *(row.side + row.text for row in rows),
    ]) + "\n"


def plan_batches(diff, limit, repository_root=""):
    from dataclasses import replace
    from itertools import groupby

    rows, metadata = parse_changes(diff)
    pending, batches = [], []
    pending_tokens = 0

    def flush():
        nonlocal pending_tokens
        if not pending:
            return
        changed = [row for part in pending for row in part if row.side != " "]
        if changed:
            ranges = []
            for row in changed:
                side = "old" if row.side == "-" else "new"
                path = row.old_path if side == "old" else row.new_path
                if (ranges and not row.offset and not ranges[-1].get("offset")
                        and (ranges[-1]["path"], ranges[-1]["hunk"], ranges[-1]["side"], ranges[-1]["end"] + 1)
                        == (path, row.hunk, side, row.line)):
                    ranges[-1]["end"] = row.line
                else:
                    ranges.append({"path": path, "hunk": row.hunk, "side": side,
                                   "start": row.line, "end": row.line, "offset": row.offset,
                                   **({"fragment_length": len(row.text)} if row.offset or len(changed) == 1 else {})})
            batches.append({"id": len(batches) + 1, "diff": "".join(map(_render, pending)),
                            "ranges": ranges, "files": list(dict.fromkeys(r.path for r in changed))})
        pending.clear()
        pending_tokens = 0

    def pack(part):
        nonlocal pending_tokens
        cost = token_bound(_render(part))
        if pending_tokens + cost > limit:
            flush()
        pending.append(part)
        pending_tokens += cost

    for _, group in groupby(rows, key=lambda row: row.hunk):
        part = []
        for row in group:
            if token_bound(_render([*part, row])) <= limit:
                part.append(row)
                continue
            if part:
                pack(part)
            part = []
            if token_bound(_render([row])) <= limit:
                part = [row]
                continue
            if row.side == " ":
                continue
            offset = 0
            while offset < len(row.text):
                size = min(len(row.text) - offset, limit)
                while size and token_bound(_render([replace(row, text=row.text[offset:offset + size])])) > limit:
                    size //= 2
                if not size:
                    raise ValueError("review batch budget cannot fit a diff header")
                fragment = replace(row, text=row.text[offset:offset + size], offset=offset)
                pack([fragment])
                flush()
                offset += size
        if part:
            pack(part)
    flush()
    if repository_root:
        _prioritize(batches, repository_root)
    return batches, metadata


def parse_batch(diff):
    """Build the reviewer-facing ParsedDiff for one rendered batch."""
    from ..domain.diff_parser import ParsedDiff
    from ..domain.models import ChangedLine

    rows, _ = parse_changes(diff)
    return ParsedDiff(
        list(dict.fromkeys(row.path for row in rows)),
        [ChangedLine(row.path, row.line, row.text) for row in rows if row.side == "+"],
        [ChangedLine(row.old_path, row.line, row.text) for row in rows if row.side == "-"],
    )


def _prioritize(batches, root):
    """Use resolved Python syntax and imports as bounded repository context."""
    from collections import defaultdict
    from pathlib import Path
    from .repository_tools import RepositoryToolSuite

    suite = RepositoryToolSuite(root, "", None)
    if not suite.repository_available:
        return
    trees, importers = {}, defaultdict(set)
    changed = list(dict.fromkeys(path for batch in batches for path in batch["files"]))
    for path in dict.fromkeys([*changed, *list(suite._files())[:150]]):
        if not path.endswith(".py"):
            continue
        try:
            file = Path(suite._safe_path(path))
            if file.stat().st_size > 262144:
                continue
            tree = ast.parse(file.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeError, ValueError, PermissionError):
            continue
        trees[path] = tree
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and not node.level:
                importers[node.module].add(path)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    importers[alias.name].add(path)
    for batch in batches:
        ranges = defaultdict(list)
        for item in batch["ranges"]:
            if item["side"] == "new":
                ranges[item["path"]].append((item["start"], item["end"]))
        signals = []
        for path, spans in ranges.items():
            if path not in trees:
                continue
            for node in ast.walk(trees[path]):
                if not isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                if not any(start <= node.lineno <= end for start, end in spans):
                    continue
                module = path[:-3].replace(os.sep, "/").replace("/", ".").removesuffix(".__init__")
                signals.append({"path": path, "line": node.lineno, "kind": type(node).__name__,
                                "importers": sorted(importers[module] - {path})[:8]})
                if len(signals) >= 12:
                    break
            if len(signals) >= 12:
                break
        batch["structural_signals"] = signals
    batches.sort(key=lambda batch: (-sum(1 + len(item["importers"]) for item in batch["structural_signals"]), batch["id"]))
