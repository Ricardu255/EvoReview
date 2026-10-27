"""Narrow runtime and collaborator base for the agent review engine."""
import threading


class ReviewRuntime:
    """Shared state that role collaborators may depend on."""

    def __init__(
        self, store, client, context_manager, memory_manager, rules,
        default_token_budget, default_time_budget, prompt_overlay,
        structured_config,
    ):
        self.store = store
        self.client = client
        self.context_manager = context_manager
        self.memory_manager = memory_manager
        self.rules = rules
        self.default_token_budget = default_token_budget
        self.default_time_budget = default_time_budget
        self.prompt_overlay = prompt_overlay
        self.structured_config = structured_config
        self.memory_scopes = {}
        self.memory_scope_lock = threading.Lock()

    def token_budget(self, role: str) -> int:
        raw = (self.structured_config.get("budget_parameters") or {}).get(
            role, self.default_token_budget,
        )
        try:
            return max(256, min(int(raw), self.default_token_budget * 4))
        except (TypeError, ValueError):
            return self.default_token_budget

    def model_diff(self, diff, context_key, label, focus_files=(), risk_domains=()):
        compressed = self.context_manager.compress_diff(
            diff, context_key, label, focus_files=focus_files,
            risk_domains=risk_domains,
        )
        return {
            "diff": self.context_manager.render_diff_view(compressed),
            "diff_context": self.context_manager.diff_metadata(compressed),
        }


class RuntimeBackref:
    """Explicit read-only view of the shared ReviewRuntime."""

    def __init__(self, runtime):
        self.runtime = runtime

    @property
    def store(self):
        return self.runtime.store

    @property
    def client(self):
        return self.runtime.client

    @property
    def context_manager(self):
        return self.runtime.context_manager

    @property
    def memory_manager(self):
        return self.runtime.memory_manager

    @property
    def rules(self):
        return self.runtime.rules

    @property
    def default_token_budget(self):
        return self.runtime.default_token_budget

    @property
    def default_time_budget(self):
        return self.runtime.default_time_budget

    @property
    def prompt_overlay(self):
        return self.runtime.prompt_overlay

    @property
    def structured_config(self):
        return self.runtime.structured_config

    @property
    def memory_scopes(self):
        return self.runtime.memory_scopes

    @property
    def memory_scope_lock(self):
        return self.runtime.memory_scope_lock

    def token_budget(self, role: str) -> int:
        return self.runtime.token_budget(role)

    def model_diff(self, diff, context_key, label, focus_files=(), risk_domains=()):
        return self.runtime.model_diff(
            diff, context_key, label, focus_files=focus_files,
            risk_domains=risk_domains,
        )
