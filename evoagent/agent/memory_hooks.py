"""Task-scoped Working Memory hooks."""


class MemoryHooksMixin:
    def _memory_hooks(self, task_id, role):
        """Return task-scoped Working Memory read/write hooks for one role loop."""
        def scope():
            with self._memory_scope_lock:
                return self._memory_scopes.get(task_id)

        def supplier():
            values = scope()
            if self.memory_manager is None or not values:
                return None
            tenant_id, repository = values
            # Lead is the authorized coordination point. Workers and Critic
            # only see their own transient observations, preserving the
            # hierarchy and Critic's independent review boundary.
            memories = self.memory_manager.recall_working(
                tenant_id, repository, task_id, limit=12,
                agent="" if role == "lead" else role,
            )
            if not memories:
                return None
            context = self.context_manager.format_memories(memories)
            context["trust"] = (
                "untrusted, task-scoped tool observations; verify before making a claim"
            )
            return context

        def sink(agent, observation):
            values = scope()
            if self.memory_manager is not None and values:
                self.memory_manager.remember_observation(
                    values[0], values[1], task_id, agent, observation,
                )

        return supplier, sink

    def _persist_task_memory(
        self, task_id, tenant_id, repository, findings, gated, files, collaboration,
    ):
        """Turn verified decisions into reusable episodes and clear Working Memory."""
        if self.memory_manager is None:
            return
        try:
            for finding in findings:
                gate = finding.gate or {}
                self.memory_manager.remember_finding(
                    tenant_id, repository, task_id, finding.to_dict(),
                    bool(gate.get("passed")), gate.get("reasons") or (),
                )
            accepted = [
                {
                    "rule_id": item.rule_id, "path": item.path, "line": item.line,
                    "severity": item.severity.value, "confidence": item.confidence,
                }
                for item in gated.accepted[:50]
            ]
            self.memory_manager.consolidate_task(tenant_id, repository, task_id, {
                "schema_version": 1, "files": list(files)[:100],
                "accepted_findings": accepted,
                "rejected_findings": list(gated.rejected)[:50],
                "gate_checks": dict(gated.checks),
                "agent_roles": list(collaboration.get("roles") or []),
            })
        except Exception:
            # Memory must enrich a review, not turn a completed review into a failure.
            return
