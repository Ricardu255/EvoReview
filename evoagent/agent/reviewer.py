"""Agentic reviewer facade composed from role-specific mixins."""
from collections import OrderedDict
import json
import threading
from typing import Any, Dict, Iterable, List, Optional, Set

from ..context_manager import ContextManager
from ..diff_parser import ParsedDiff
from ..gates import FindingGate
from ..llm import JsonChatClient
from ..models import ComponentKind, Finding
from ..modes import component, resolve_mode
from ..repository_tools import RepositoryToolSuite
from ..reviewer import LocalRuleReviewer, Reviewer
from ..store_contract import ReviewStore
from ..telemetry import ExecutionLedger
from .assignments import AssignmentsMixin
from .findings import FindingsMixin
from .lead import LeadMixin
from .memory_hooks import MemoryHooksMixin
from .orchestration import OrchestrationMixin
from .scanning import ScanningMixin
from .session import SessionMixin
from .tools import ToolsMixin


MAX_TRACKED_SUMMARIES = 128
SUMMARY_CHECKPOINT = "agentic-summary"


class AgenticReviewer(
    FindingsMixin, ScanningMixin, SessionMixin, MemoryHooksMixin,
    ToolsMixin, LeadMixin, AssignmentsMixin, OrchestrationMixin, Reviewer,
):
    name = "agentic-reviewer"

    def __init__(
        self, store: Optional[ReviewStore], llm_client: Optional[JsonChatClient],
        default_token_budget: int = 8000, default_time_budget: int = 60,
        input_cost_per_million: float = 0.0, output_cost_per_million: float = 0.0,
        enabled_roles: Optional[Set[str]] = None,
        scanners: Optional[List[Reviewer]] = None,
        scanner_provider=None,
        review_test_command: str = "",
        prompt_overlay: str = "",
        structured_config: Optional[Dict[str, Any]] = None,
        memory_manager=None,
        context_manager: Optional[ContextManager] = None,
        skill_provider=None,
    ):
        self.store = store
        self.client = llm_client
        self.default_token_budget = default_token_budget
        self.default_time_budget = default_time_budget
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million
        self.enabled_roles = enabled_roles or {
            "lead", "security", "correctness-reliability", "critic"
        }
        self.rules = LocalRuleReviewer()
        self.scanners = list(scanners or [])
        self.scanner_provider = scanner_provider
        self.review_test_command = review_test_command
        self.prompt_overlay = str(prompt_overlay or "").strip()
        self.structured_config = dict(structured_config or {})
        self.memory_manager = memory_manager
        self.context_manager = context_manager or ContextManager()
        self.skill_provider = skill_provider
        if self.structured_config:
            self.prompt_overlay += "\nStructured runtime policy:\n" + json.dumps(
                self.structured_config, ensure_ascii=False, sort_keys=True
            )
        self.gate = FindingGate()
        self._summaries: Dict[str, dict] = OrderedDict()
        self._summary_lock = threading.Lock()
        self.max_tracked_summaries = MAX_TRACKED_SUMMARIES
        self._memory_scopes: Dict[str, tuple] = {}
        self._memory_scope_lock = threading.Lock()

    def _token_budget(self, role: str) -> int:
        raw = (self.structured_config.get("budget_parameters") or {}).get(
            role, self.default_token_budget
        )
        try:
            return max(256, min(int(raw), self.default_token_budget * 4))
        except (TypeError, ValueError):
            return self.default_token_budget

    def review(self, diff: str, parsed: ParsedDiff) -> List[Finding]:
        raise RuntimeError(
            "agentic review requires review_with_context and a configured model"
        )

    def review_with_context(
        self, task_id: str, diff: str, parsed: ParsedDiff,
        repository: str = "", tenant_id: str = "default",
    ) -> List[Finding]:
        """Run a review while making its transient memory scope available to roles."""
        with self._memory_scope_lock:
            self._memory_scopes[task_id] = (tenant_id, repository)
        try:
            return self._review_with_context(
                task_id, diff, parsed, repository=repository, tenant_id=tenant_id,
            )
        finally:
            # Do not leave a stale tenant/repository binding behind when setup,
            # a model call, or a gate raises. Working observations themselves
            # retain their TTL so a resumed task can still use them.
            with self._memory_scope_lock:
                self._memory_scopes.pop(task_id, None)

    def _review_with_context(
        self, task_id: str, diff: str, parsed: ParsedDiff,
        repository: str = "", tenant_id: str = "default",
    ) -> List[Finding]:
        task = self.store.get(task_id, tenant_id) or {}
        task_input = task.get("input") or {}
        resolution = resolve_mode(task_input.get("mode"), self.client is not None)
        if self.client is None:
            raise RuntimeError("agentic review requires a configured model")
        self.context_manager.begin(task_id)
        ledger = ExecutionLedger(
            resolution.effective.value, self.input_cost_per_million,
            self.output_cost_per_million,
        )
        root = str(task_input.get("repository_root") or "")
        suite = RepositoryToolSuite(root, diff, parsed, ledger, self.review_test_command)
        enabled = set(task_input.get("enabled_agents") or self.enabled_roles)
        scanners = self.scanners + (
            list(self.scanner_provider(tenant_id)) if self.scanner_provider else []
        )
        available_skills = {
            skill.name: skill
            for skill in (list(self.skill_provider(tenant_id)) if self.skill_provider else [])
        }
        requested_skills = [str(value) for value in task_input.get("enabled_skills") or []]
        unknown_skills = set(requested_skills).difference(available_skills)
        if unknown_skills:
            raise ValueError(
                "unknown enabled Agent Skill(s): %s" % ", ".join(sorted(unknown_skills))
            )
        memory_query = self.context_manager.memory_query(diff, parsed.files)
        try:
            recalled = (
                self.memory_manager.recall(tenant_id, repository, memory_query)
                if self.memory_manager is not None else []
            )
        except Exception as exc:
            recalled = []
            ledger.trace(
                "context-manager", "memory_recall_failed", error=str(exc)[:1000],
            )
        self.context_manager.record_memory_recall(task_id, memory_query, recalled)
        memory_context = self.context_manager.format_memories(recalled)
        ledger.trace(
            "context-manager", "memory_recalled", count=len(recalled),
            repository=repository, tenant_id=tenant_id,
        )
        findings, collaboration, components = self._agentic(
            task_id, diff, parsed, suite, ledger, enabled, scanners, memory_context,
            available_skills, requested_skills,
        )
        gated = self.gate.apply(findings, parsed)
        ledger.trace("evidence-gate", "completed", **gated.checks)
        self._persist_task_memory(
            task_id, tenant_id, repository, findings, gated,
            parsed.files, collaboration,
        )
        execution = ledger.summary()
        execution["prompt_version"] = task_input.get("prompt_version", 0)
        execution["tenant_id"] = tenant_id
        context_management = self.context_manager.summary(task_id)
        execution["context_management"] = context_management
        summary = {
            "run_mode": resolution.to_dict(),
            "components": components + [
                component(ComponentKind.GATE, "finding-format-gate"),
                component(ComponentKind.GATE, "evidence-gate"),
                component(ComponentKind.GATE, "confidence-gate"),
                component(ComponentKind.GATE, "release-gate"),
            ],
            "execution": execution,
            "collaboration": collaboration,
            "gates": gated.checks,
            "rejected_findings": gated.rejected,
            "repository_context": {
                "available": suite.repository_available,
                "root_supplied": bool(root),
            },
            "context_management": context_management,
        }
        self._remember_summary(task_id, summary)
        if task_id:
            self.store.save_checkpoint(
                task_id, SUMMARY_CHECKPOINT, summary, "completed", 1
            )
        return gated.accepted

    def _remember_summary(self, task_id: str, summary: dict) -> None:
        with self._summary_lock:
            self._summaries[task_id] = summary
            self._summaries.move_to_end(task_id)
            while len(self._summaries) > self.max_tracked_summaries:
                self._summaries.popitem(last=False)

    def collaboration_summary(self, task_id: str) -> dict:
        with self._summary_lock:
            summary = self._summaries.get(task_id)
        if summary:
            return dict(summary)
        if task_id:
            checkpoint = (
                (self.store.load_checkpoints(task_id) or {}).get(SUMMARY_CHECKPOINT)
                or {}
            )
            if checkpoint.get("status") == "completed":
                return dict(checkpoint.get("state") or {})
        return {}

    def _model_diff(
        self, diff, context_key, label, focus_files=(), risk_domains=(),
    ):
        compressed = self.context_manager.compress_diff(
            diff, context_key, label, focus_files=focus_files,
            risk_domains=risk_domains,
        )
        return {
            "diff": self.context_manager.render_diff_view(compressed),
            "diff_context": self.context_manager.diff_metadata(compressed),
        }
