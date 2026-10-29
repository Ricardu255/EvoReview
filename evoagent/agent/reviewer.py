"""Agentic reviewer facade composed from explicit role collaborators."""
from collections import OrderedDict
import json
import threading
from typing import Any, Dict, List, Optional, Set

from .context_manager import ContextManager
from ..domain.diff_parser import ParsedDiff
from ..domain.gates import FindingGate
from ..adapters.llm import JsonChatClient
from ..domain.models import ComponentKind, Finding
from ..domain.modes import component, resolve_mode
from .repository_tools import RepositoryToolSuite
from .reviewers import LocalRuleReviewer, Reviewer
from ..store_contract import ReviewStore
from .telemetry import ExecutionLedger
from .assignments import AssignmentRunner
from .collaborators import ReviewRuntime
from .findings import FindingPolicy
from .lead import LeadRunner
from .memory_hooks import MemoryHooks
from .orchestration import ReviewOrchestrator
from .scanning import ScanRunner
from .session import SessionManager
from .tools import ToolPolicy


MAX_TRACKED_SUMMARIES = 128
SUMMARY_CHECKPOINT = "agentic-summary"


class AgenticReviewer(Reviewer):
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

        self.runtime = ReviewRuntime(
            store, llm_client, self.context_manager, memory_manager, self.rules,
            default_token_budget, default_time_budget, self.prompt_overlay,
            self.structured_config,
        )
        self._memory_scopes = self.runtime.memory_scopes
        self._memory_scope_lock = self.runtime.memory_scope_lock
        self.findings = FindingPolicy()
        self.tools = ToolPolicy(self.runtime)
        self.memory = MemoryHooks(self.runtime)
        self.sessions = SessionManager(self.runtime, self.findings)
        self.scanner = ScanRunner(self.runtime, self.findings)
        self.lead = LeadRunner(self.runtime, self.memory)
        self.assignments = AssignmentRunner(
            self.runtime, self.memory, self.tools, self.sessions,
        )
        self.orchestrator = ReviewOrchestrator(
            self.runtime, self.scanner, self.sessions, self.lead,
            self.assignments, self.findings,
        )

    def _token_budget(self, role: str) -> int:
        return self.runtime.token_budget(role)

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
        return self.runtime.model_diff(
            diff, context_key, label, focus_files=focus_files,
            risk_domains=risk_domains,
        )

    # Compatibility wrappers; collaborators own the implementations.
    def _agentic(self, *args, **kwargs):
        return self.orchestrator._agentic(*args, **kwargs)

    def _scan(self, *args, **kwargs):
        return self.scanner._scan(*args, **kwargs)

    def _attach_diff_ast_evidence(self, *args, **kwargs):
        return self.scanner._attach_diff_ast_evidence(*args, **kwargs)

    def _session_candidates(self, *args, **kwargs):
        return self.sessions._session_candidates(*args, **kwargs)

    def _load_lead_session(self, *args, **kwargs):
        return self.sessions._load_lead_session(*args, **kwargs)

    def _save_lead_session(self, *args, **kwargs):
        return self.sessions._save_lead_session(*args, **kwargs)

    def _memory_hooks(self, *args, **kwargs):
        return self.memory._memory_hooks(*args, **kwargs)

    def _persist_task_memory(self, *args, **kwargs):
        return self.memory._persist_task_memory(*args, **kwargs)

    @staticmethod
    def _skill_tool_permissions(worker, skills):
        return ToolPolicy._skill_tool_permissions(worker, skills)

    @staticmethod
    def _register_skill_resource_tool(tools, selected_skills):
        return ToolPolicy._register_skill_resource_tool(tools, selected_skills)

    def _run_lead(self, *args, **kwargs):
        return self.lead._run_lead(*args, **kwargs)

    def _run_critic(self, *args, **kwargs):
        return self.lead._run_critic(*args, **kwargs)

    def _run_pending_assignments(self, *args, **kwargs):
        return self.assignments._run_pending_assignments(*args, **kwargs)

    @staticmethod
    def _apply_critic(result, candidates):
        return FindingPolicy._apply_critic(result, candidates)

    @staticmethod
    def _apply_lead_final(decision, candidates):
        return FindingPolicy._apply_lead_final(decision, candidates)

    @staticmethod
    def _restore_findings(values):
        return FindingPolicy._restore_findings(values)

    @staticmethod
    def _public_decision(result):
        return FindingPolicy._public_decision(result)

    @staticmethod
    def _merge(findings):
        return FindingPolicy._merge(findings)

    @staticmethod
    def _scanner_name(name):
        return FindingPolicy._scanner_name(name)
