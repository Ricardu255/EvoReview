"""Canonical finding identities shared by review and evaluation code."""

import re
from typing import Any, Dict, Iterable, List, Set, Tuple


# The evaluator compares canonical CWE identities, not reviewer-specific rule names.
RULE_TO_CWE = {
    "SEC-EVAL": "CWE-95",
    "SEC-SUBPROCESS-SHELL": "CWE-78",
    "SEC-HARDCODED-SECRET": "CWE-798",
    "SEC-SQL-CONCAT": "CWE-89",
    "REL-EMPTY-EXCEPT": "CWE-703",
    "REL-DEBUG-PRINT": "CWE-532",
    "SEC-PATH-TRAVERSAL": "CWE-22",
    "SEC-YAML-LOAD": "CWE-502",
    "SEC-WEAK-HASH": "CWE-328",
    "SEC-INSECURE-TEMPFILE": "CWE-377",
    "SEC-WEAK-RANDOM": "CWE-330",
    "REL-UNBOUNDED-RETRY": "CWE-835",
    "SEC-ASSERT-AUTH": "CWE-617",
    "SEC-INSECURE-COOKIE": "CWE-614",
    "SEC-PICKLE-LOAD": "CWE-502",
    "REL-FLOAT-MONEY": "CWE-682",
    "REL-NAIVE-DATETIME": "CWE-367",
    "REL-BLOCKING-ASYNC": "CWE-400",
    "REL-NONATOMIC-WRITE": "CWE-362",
    "SEC-OPEN-REDIRECT": "CWE-601",
    "SEC-LOG-FORGING": "CWE-117",
}

# Worker-selectable stable finding categories.  CWE only applies to rules with
# a standard weakness mapping; correctness, compatibility and test categories
# are not forced into CWE.
_RULE_DESCRIPTIONS = {
    "SEC-EVAL": "Dynamic code execution through eval or exec.",
    "SEC-SUBPROCESS-SHELL": "Command execution through a shell.",
    "SEC-HARDCODED-SECRET": "Credential or secret committed in source.",
    "SEC-SQL-CONCAT": "SQL built by string interpolation or concatenation.",
    "SEC-PATH-TRAVERSAL": "Untrusted path can escape the intended root.",
    "SEC-YAML-LOAD": "Unsafe deserialization through a permissive YAML loader.",
    "SEC-WEAK-HASH": "Security-sensitive use of a weak digest.",
    "SEC-INSECURE-TEMPFILE": "Predictable or unsafe temporary-file handling.",
    "SEC-WEAK-RANDOM": "Security-sensitive use of non-cryptographic randomness.",
    "SEC-ASSERT-AUTH": "Authorization or authentication enforced only by assert.",
    "SEC-INSECURE-COOKIE": "Sensitive cookie lacks required transport protection.",
    "SEC-PICKLE-LOAD": "Unsafe deserialization through pickle.",
    "SEC-OPEN-REDIRECT": "Untrusted redirect target.",
    "SEC-LOG-FORGING": "Untrusted data can forge or corrupt log records.",
    "SEC-AUTHORIZATION": "Missing or incorrect authorization boundary.",
    "SEC-INPUT-VALIDATION": "Security-relevant input is insufficiently validated.",
    "SEC-INJECTION": "Untrusted data reaches an interpreter or command sink.",
    "SEC-SENSITIVE-DATA": "Sensitive data can be exposed or retained unsafely.",
    "SEC-UNSAFE-DESERIALIZATION": "Untrusted serialized data can instantiate unsafe values.",
    "REL-EMPTY-EXCEPT": "Failure is swallowed or converted into false success.",
    "REL-DEBUG-PRINT": "Debug output can leak data or pollute service logs.",
    "REL-UNBOUNDED-RETRY": "Retry or polling loop has no effective bound.",
    "REL-FLOAT-MONEY": "Binary floating point is used for monetary state.",
    "REL-NAIVE-DATETIME": "Timezone-naive time is used in a distributed boundary.",
    "REL-BLOCKING-ASYNC": "Blocking operation runs on an asynchronous execution path.",
    "REL-NONATOMIC-WRITE": "Durable state update is not atomic.",
    "COR-STATE-TRANSITION": "Change introduces an invalid state transition.",
    "COR-BOUNDARY-CONDITION": "Boundary or empty-input behavior is incorrect.",
    "COR-DATA-INTEGRITY": "Change can corrupt, drop, or partially persist data.",
    "COR-INVARIANT": "A required program or business invariant can be violated.",
    "API-COMPATIBILITY": "Externally observable API or schema compatibility break.",
    "REL-ERROR-HANDLING": "Error handling loses failure state or required context.",
    "REL-CONCURRENCY": "Concurrency ordering or synchronization is unsafe.",
    "REL-RESOURCE-LIFETIME": "Resource is leaked, reused, or released at the wrong time.",
    "REL-IDEMPOTENCY": "Retry or duplicate delivery can repeat a side effect.",
    "REL-TIMEOUT-RETRY": "Timeout or retry policy can amplify or hide failure.",
    "REL-OBSERVABILITY": "Material failure lacks an actionable operational signal.",
    "REL-PERFORMANCE": "Change introduces material unbounded or repeated work.",
    "DB-DATA-INTEGRITY": "Database change can violate durable data integrity.",
    "DB-TRANSACTION": "Transaction boundary permits partial or inconsistent state.",
    "DB-UNBOUNDED-QUERY": "Query can return or process an unbounded result set.",
    "TEST-COVERAGE": "Changed behavior lacks a meaningful regression or failure-path test.",
    "QUALITY-UNFINISHED": "Production behavior contains an unfinished TODO or placeholder.",
}

_SECURITY_RULES = {name for name in _RULE_DESCRIPTIONS if name.startswith("SEC-")}
_SHARED_RULES = {"DB-DATA-INTEGRITY", "DB-TRANSACTION"}

RULE_CATALOG: Dict[str, Dict[str, Any]] = {
    name: {
        "rule_id": name,
        "cwe": RULE_TO_CWE.get(name),
        "description": description,
        "workers": (
            ("security", "correctness-reliability")
            if name in _SHARED_RULES else
            ("security",) if name in _SECURITY_RULES else
            ("correctness-reliability",)
        ),
    }
    for name, description in _RULE_DESCRIPTIONS.items()
}

_CWE_RE = re.compile(r"^CWE-[0-9]+$", re.IGNORECASE)
_RULE_ID_RE = re.compile(r"^[A-Z][A-Z0-9_-]{1,79}$")
# Rule IDs a Skill may declare for project-specific findings.
_DECLARED_RULE_RE = re.compile(
    r"\b(?:SEC|REL|COR|DB|API|PERF|OBS|TEST|QUALITY|CODE)-[A-Z0-9][A-Z0-9_-]{1,79}\b"
)

# Optional, explicitly documented equivalence classes for model-produced
# labels.  Strict scoring keeps the original IDs; normalized scoring uses
# these families so a semantically correct finding is not lost only because
# the model chose a sibling CWE identifier.
CWE_ALIASES = {
    "CWE-396": "CWE-703",  # generic catch / improper exception handling
    "CWE-338": "CWE-330",  # weak PRNG / insufficient randomness
    "CWE-259": "CWE-798",  # hard-coded password / hard-coded credentials
    "CWE-321": "CWE-798",  # hard-coded key / hard-coded credentials
    "CWE-327": "CWE-328",  # broken crypto / weak hash
}


def canonical_cwe(rule_id: Any = "", cwe: Any = "") -> str:
    """Return a canonical CWE, preferring the trusted program rule mapping.

    The mapping is authoritative: a model-supplied CWE that disagrees with the
    managed rule catalog must not win, or a model could relabel a finding past
    the taxonomy gate.  Explicit CWE values are only used for rules without a
    program mapping (for example Skill-declared rules).
    """
    rule = str(rule_id or "").strip().upper()
    mapped = RULE_TO_CWE.get(rule, "")
    if mapped:
        return mapped.upper()
    explicit = str(cwe or "").strip().upper()
    return explicit if _CWE_RE.fullmatch(explicit) else ""


def is_valid_cwe(value: Any) -> bool:
    """Validate the transport format for an explicit CWE value."""
    return bool(_CWE_RE.fullmatch(str(value or "").strip()))


def worker_rule_catalog(worker: str) -> List[dict]:
    """Return the compact, role-scoped taxonomy exposed to one Worker."""
    return [
        {
            "rule_id": item["rule_id"], "cwe": item["cwe"],
            "description": item["description"],
        }
        for item in RULE_CATALOG.values()
        if worker in item["workers"]
    ]


def extract_declared_rule_ids(texts: Iterable[str]) -> Set[str]:
    """Extract project-specific rule IDs explicitly declared by active Skills."""
    values: Set[str] = set()
    for text in texts:
        values.update(_DECLARED_RULE_RE.findall(str(text or "").upper()))
    return {value for value in values if _RULE_ID_RE.fullmatch(value)}


def normalize_worker_identity(
    rule_id: Any, cwe: Any, worker: str, allowed_custom_rules: Iterable[str] = (),
) -> Tuple[str, str, bool]:
    """Normalize model taxonomy output and reject undeclared invented identities.

    Known catalog rules use their program-owned CWE mapping. Project-specific
    rules are accepted only when an active Skill declares them, and do not
    inherit a model-supplied CWE. Everything else is downgraded to a
    role-specific UNCLASSIFIED identity.
    """
    candidate = str(rule_id or "").strip().upper()
    catalog = RULE_CATALOG.get(candidate)
    if catalog and worker in catalog["workers"]:
        return candidate, str(catalog.get("cwe") or ""), True
    allowed = {str(value).strip().upper() for value in allowed_custom_rules}
    if candidate in allowed and _RULE_ID_RE.fullmatch(candidate):
        return candidate, "", True
    fallback = (
        "UNCLASSIFIED-SECURITY"
        if worker == "security" else "UNCLASSIFIED-CORRECTNESS"
    )
    return fallback, "", False


def normalized_cwe(rule_id: Any = "", cwe: Any = "") -> str:
    """Return a documented equivalence-class CWE for normalized scoring."""
    value = canonical_cwe(rule_id, cwe)
    return CWE_ALIASES.get(value, value)


def normalized_identity(rule_id: Any = "", cwe: Any = "") -> str:
    """Return the normalized scoring identity; strict identity is unchanged."""
    value = normalized_cwe(rule_id, cwe)
    if value:
        return value
    rule = str(rule_id or "").strip().upper()
    return "RULE:" + rule if rule else ""


def canonical_identity(rule_id: Any = "", cwe: Any = "") -> str:
    """Return the scoring identity, with a legacy rule fallback.

    Known rules and explicit CWE values are scored by CWE. Unknown legacy rule
    IDs remain matchable only against the exact same legacy ID; an arbitrary
    model-generated ID cannot match a CWE-labelled truth.
    """
    value = canonical_cwe(rule_id, cwe)
    if value:
        return value
    rule = str(rule_id or "").strip().upper()
    return "RULE:" + rule if rule else ""
