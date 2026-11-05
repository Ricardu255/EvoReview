"""Deterministic mutation-based synthetic review corpus.

This module creates labelled before/after source pairs by replacing a safe
implementation line with a known risky one.  It is intentionally separate from
``evaluation.benchmark`` so existing controlled experiments are untouched.
Generated data is for mechanism/regression testing, not production accuracy.
"""
import difflib
import json
import re
import textwrap
from typing import Dict, Iterable, List, Tuple

from ..domain.diff_parser import parse_unified_diff
from ..domain.finding_identity import canonical_cwe


SCHEMA_VERSION = 1
GENERATOR = "evo-mutation-v1"
REPOSITORIES = ["synthetic/service-%02d" % index for index in range(1, 11)]


def _dedent(value: str) -> str:
    return textwrap.dedent(value).strip("\n") + "\n"


def _operator(
    rule_id: str, severity: str, safe: str, unsafe: str,
    risk_pattern: str, required_after: Tuple[str, ...] = (),
    auto_fixable: bool = False,
) -> Dict[str, object]:
    return {
        "rule_id": rule_id,
        "severity": severity,
        "cwe": canonical_cwe(rule_id),
        "safe": _dedent(safe),
        "unsafe": _dedent(unsafe),
        "risk_pattern": risk_pattern,
        "required_after": list(required_after),
        "auto_fixable": auto_fixable,
    }


OPERATORS: Tuple[Dict[str, object], ...] = (
    _operator(
        "SEC-EVAL", "critical",
        """
        import json
        def process(value):
            result = json.loads(value)
            return result
        """,
        """
        import json
        def process(value):
            result = eval(value)
            return result
        """,
        r"\beval\s*\(", (r"json\.loads\(",), True,
    ),
    _operator(
        "SEC-SUBPROCESS-SHELL", "high",
        """
        import subprocess
        def process(command):
            result = subprocess.run(command, shell=False)
            return result
        """,
        """
        import subprocess
        def process(command):
            result = subprocess.run(command, shell=True)
            return result
        """,
        r"shell\s*=\s*True", (r"shell\s*=\s*False",), True,
    ),
    _operator(
        "SEC-HARDCODED-SECRET", "high",
        """
        import os
        def process():
            password = os.environ["PASSWORD"]
            return password
        """,
        """
        import os
        def process():
            password = "synthetic-secret-value"
            return password
        """,
        r"(?i)\bpassword\b\s*=\s*['\"]", (r"os\.environ",), True,
    ),
    _operator(
        "SEC-SQL-CONCAT", "high",
        """
        def process(cursor, value):
            cursor.execute("SELECT * FROM users WHERE id = ?", (value,))
            return value
        """,
        """
        def process(cursor, value):
            cursor.execute(f"SELECT * FROM users WHERE id = {value}")
            return value
        """,
        r"(?i)execute\s*\(\s*f['\"]", (), True,
    ),
    _operator(
        "REL-EMPTY-EXCEPT", "medium",
        """
        def process(value):
            try:
                return value
            except ValueError:
                return None
        """,
        """
        def process(value):
            try:
                return value
            except Exception:
                pass
        """,
        r"^\s*except\s+Exception\s*:\s*$", (r"except\s+ValueError",), True,
    ),
    _operator(
        "REL-DEBUG-PRINT", "low",
        """
        def process(value):
            result = value
            return result
        """,
        """
        def process(value):
            result = value
            print(result)
            return result
        """,
        r"\bprint\s*\(", (), True,
    ),
    _operator(
        "SEC-PATH-TRAVERSAL", "high",
        """
        def process(base, user_path):
            return read_under_base(base, user_path)
        """,
        """
        def process(base, user_path):
            return open(base / user_path).read()
        """,
        r"open\(base\s*/\s*user_path\)", (r"read_under_base\(",), True,
    ),
    _operator(
        "SEC-YAML-LOAD", "high",
        """
        import yaml
        def process(raw):
            return yaml.safe_load(raw)
        """,
        """
        import yaml
        def process(raw):
            return yaml.load(raw)
        """,
        r"\byaml\.load\s*\(", (r"yaml\.safe_load",), False,
    ),
    _operator(
        "SEC-WEAK-HASH", "medium",
        """
        import hashlib
        def process(value):
            return hashlib.sha256(value).hexdigest()
        """,
        """
        import hashlib
        def process(value):
            return hashlib.md5(value).hexdigest()
        """,
        r"\bhashlib\.md5\s*\(", (r"hashlib\.sha256",), False,
    ),
    _operator(
        "SEC-INSECURE-TEMPFILE", "medium",
        """
        import tempfile
        def process():
            return tempfile.NamedTemporaryFile()
        """,
        """
        import tempfile
        def process():
            return tempfile.mktemp()
        """,
        r"\btempfile\.mktemp\s*\(", (r"NamedTemporaryFile",), False,
    ),
    _operator(
        "SEC-WEAK-RANDOM", "medium",
        """
        import secrets
        def process():
            return secrets.token_hex(16)
        """,
        """
        import random
        def process():
            return random.random()
        """,
        r"\brandom\.random\s*\(", (r"secrets\.token",), False,
    ),
    _operator(
        "REL-UNBOUNDED-RETRY", "medium",
        """
        def process():
            for _ in range(3):
                pass
            return True
        """,
        """
        def process():
            while True:
                pass
            return True
        """,
        r"^\s*while\s+True\s*:\s*$", (r"for\s+_\s+in\s+range",), False,
    ),
    _operator(
        "SEC-ASSERT-AUTH", "medium",
        """
        def process(user):
            if not user.is_admin:
                raise PermissionError("admin required")
            return True
        """,
        """
        def process(user):
            assert user.is_admin
            return True
        """,
        r"^\s*assert\s+user\.is_admin", (r"raise\s+PermissionError",), False,
    ),
    _operator(
        "SEC-INSECURE-COOKIE", "medium",
        """
        def process(response, value):
            response.set_cookie("session", value, secure=True)
        """,
        """
        def process(response, value):
            response.set_cookie("session", value, secure=False)
        """,
        r"set_cookie\(.+secure\s*=\s*False", (r"secure\s*=\s*True",), False,
    ),
)


def _safe_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _diff(path: str, before: str, after: str) -> str:
    return "".join(difflib.unified_diff(
        before.splitlines(True), after.splitlines(True),
        fromfile="a/" + path, tofile="b/" + path, lineterm="\n",
    ))


def _case(
    identifier: str, repository: str, split: str, path: str,
    before: str, after: str, expected: List[dict], source: dict,
    repair_validation: dict,
) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "id": identifier,
        "repository": repository,
        "pull_request": 1,
        "split": split,
        "source": source,
        "diff": _diff(path, before, after),
        "before_files": {path: before},
        "after_files": {path: after},
        "expected_findings": expected,
        "repair_validation": repair_validation,
    }


def generate_mutation_cases(per_rule: int = 4, seed: int = 1) -> List[dict]:
    """Generate a deterministic, repository-split synthetic corpus.

    Each operator yields one risky case and one safe pair per repetition.  The
    labels are derived from the operator itself and verified by the tests.
    """
    per_rule = max(1, min(int(per_rule), 20))
    cases: List[dict] = []
    sequence = 0
    for operator in OPERATORS:
        rule_id = str(operator["rule_id"])
        slug = _safe_slug(rule_id)
        for variant in range(1, per_rule + 1):
            repository = REPOSITORIES[sequence % len(REPOSITORIES)]
            split = "validation" if REPOSITORIES.index(repository) < 8 else "holdout"
            path = "src/%s_%02d.py" % (slug, variant)
            risky = _case(
                "mut-%s-%02d" % (slug, variant), repository, split, path,
                str(operator["safe"]), str(operator["unsafe"]),
                [], {
                    "kind": "mutation-synthetic", "generator": GENERATOR,
                    "seed": int(seed), "operator": rule_id,
                }, {
                    "auto_fixable": bool(operator["auto_fixable"]),
                    "risk_pattern": str(operator["risk_pattern"]),
                    "required_after_patterns": list(operator["required_after"]),
                },
            )
            parsed = parse_unified_diff(risky["diff"])
            matches = [
                item for item in parsed.added_lines
                if re.search(str(operator["risk_pattern"]), item.content,
                             re.IGNORECASE | re.MULTILINE)
            ]
            if not matches:
                raise ValueError("operator %s produced no risk line" % rule_id)
            line = matches[0].line
            risky["expected_findings"] = [{
                "path": path, "start_line": line, "end_line": line,
                "cwe": str(operator["cwe"]), "rule_id": rule_id,
                "severity": str(operator["severity"]), "should_comment": True,
            }]
            cases.append(risky)

            safe = _case(
                "mut-%s-%02d-safe" % (slug, variant), repository, split, path,
                "", str(operator["safe"]), [], {
                    "kind": "mutation-synthetic", "generator": GENERATOR,
                    "seed": int(seed), "operator": rule_id,
                }, {},
            )
            cases.append(safe)
            sequence += 1

    hard_negatives = (
        """
        def process(cursor, value):
            cursor.execute("SELECT * FROM users WHERE id = ?", (value,))
            return value
        """,
        """
        import subprocess
        def process(command):
            return subprocess.run(command, shell=False)
        """,
        """
        import yaml
        def process(raw):
            return yaml.safe_load(raw)
        """,
        """
        import secrets
        def process():
            return secrets.token_hex(16)
        """,
        """
        def process(base, user_path):
            return read_under_base(base, user_path)
        """,
    )
    for index, source in enumerate(hard_negatives, 1):
        repository = REPOSITORIES[sequence % len(REPOSITORIES)]
        split = "validation" if REPOSITORIES.index(repository) < 8 else "holdout"
        path = "src/hard_negative_%02d.py" % index
        cases.append(_case(
            "mut-hard-negative-%02d" % index, repository, split, path,
            "", _dedent(source), [], {
                "kind": "mutation-synthetic", "generator": GENERATOR,
                "seed": int(seed), "operator": "HARD-NEGATIVE",
            }, {},
        ))
        sequence += 1
    return cases


def write_jsonl(cases: Iterable[dict], path: str) -> int:
    count = 0
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for case in cases:
            handle.write(json.dumps(
                case, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            ) + "\n")
            count += 1
    return count
