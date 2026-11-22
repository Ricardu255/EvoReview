"""Checkpointed orchestration for lossless, metered PR reviews.

Ported from the parallel EvoAgnet branch and adapted to the layered layout:
the engine lives beside :class:`~evoagent.agent.reviewer.AgenticReviewer`, the
meter persists through the application store's checkpoint port, and per-batch
token usage is streamed to the tenant quota recorder so a paused task still
accounts for the work it already performed.
"""

import hashlib
import json
from copy import copy
from threading import Lock
from types import SimpleNamespace

from ..adapters.llm import ModelResponseError
from ..domain.models import ReviewReport, TaskState, TraceEvent
from ..domain.modes import BATCHED_ENGINE as ENGINE
from ..errors import PermanentTaskError
from ..time_utils import utc_now
from .harness import ReviewHarness
from .review_batches import parse_batch, plan_batches, token_bound
from .reviewer import AgenticReviewer
from .runtime import RuntimeBudgetExceeded, RuntimeCancelled
from .telemetry import ExecutionLedger


SNAPSHOT = "review-v2-snapshot"
PLAN = "review-v2-plan"
METER = "review-v2-meter"
RESULT = "review-v2-result-"


class TaskBudgetPaused(RuntimeBudgetExceeded):
    pass


class BatchInputTooLarge(RuntimeBudgetExceeded):
    pass


class ReviewConflict(ValueError):
    pass


def checkpoint(checkpoints, name):
    return (checkpoints.get(name) or {}).get("state") or {}


def report_revision(report):
    return hashlib.sha256(json.dumps(report, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class BatchStore:
    """Scope the existing agent checkpoints to one batch on the parent task."""

    def __init__(self, store, task, batch_id, checkpoints):
        self.store = store
        self.task = task
        self.prefix = f"review-v2-batch-{batch_id}:"
        self.checkpoints = {
            name[len(self.prefix):]: value for name, value in checkpoints.items()
            if name.startswith(self.prefix)
        }

    def get(self, _task_id, _tenant_id=None):
        return self.task

    def load_checkpoints(self, _task_id):
        return self.checkpoints

    def save_checkpoint(self, _task_id, name, state, status="completed", attempt=1, error=""):
        self.store.save_checkpoint(self.task["id"], self.prefix + name, state, status, attempt, error)
        self.checkpoints[name] = {"state": state, "status": status}


class MeteredClient:
    """Pre-charges estimated tokens, validates the action envelope, settles
    against ledger reality after each call, and pauses the task when the
    budget runs out instead of failing it."""

    lossless = True

    def __init__(self, client, context, overview, meter, limit, persist, cancelled):
        self.client = client
        self.context = context
        self.overview = overview
        self.meter = meter
        self.limit = limit
        self.persist = persist
        self.cancelled = cancelled
        self.lock = Lock()

    def complete_role(self, role, system, user, ledger, output_cap, role_remaining, allow_tools):
        with self.lock:
            correction = ""
            before = 0
            error = None
            for attempt in range(2):
                if self.cancelled():
                    raise RuntimeCancelled("Review cancelled")
                # Account for both messages, framing and their final JSON escaping.
                prompt = system + correction
                spendable = self.limit - self.meter["charged_tokens"]
                capacity = min(role_remaining - before, self.context.context_window_tokens - 64)
                user, estimated = self._fit_auxiliary(prompt, user, min(capacity, spendable))
                if estimated > self.context.input_token_budget or estimated + 128 > capacity:
                    raise BatchInputTooLarge(f"{role} request exceeds its configured input/role budget")
                if estimated + 128 > spendable:
                    if error is not None:
                        # Preserve the retryable protocol error and let the runner pause.
                        raise TaskBudgetPaused(f"Additional tokens required to retry {role}") from error
                    raise TaskBudgetPaused(f"Additional tokens required before calling {role}")
                output = min(output_cap, capacity - estimated, spendable - estimated)
                if attempt == 0 and output >= 256:
                    output = min(output, max(256, output_cap // 2))
                reserved = estimated + output
                self.meter["charged_tokens"] += reserved
                self.meter["reserved_tokens"] = reserved
                self.persist(self.meter)
                start_calls = len(ledger.model_calls)
                action = None
                error = None
                try:
                    action = self.client.complete_json(role, prompt, user, ledger, max_tokens=output)
                    kind = str(action.get("action", "")).strip().lower() if isinstance(action, dict) else ""
                    if kind not in ({"tool", "final"} if allow_tools else {"final"}):
                        raise ModelResponseError(role, "invalid_action")
                    if kind == "tool" and not isinstance(action.get("tool"), str):
                        raise ModelResponseError(role, "invalid_tool_action")
                    if kind == "final":
                        phase = json.loads(json.loads(user)["task"]).get("phase")
                        required = {
                            "delegate": "delegations", "assess-workers": "revision_requests",
                            "finalize": "accepted_finding_indices", "overview": "review_focus",
                        }.get(phase)
                        if role != "lead":
                            required = "decisions" if role == "critic" else "findings"
                        if required and not isinstance(action.get(required), list):
                            raise ModelResponseError(role, "invalid_action_payload")
                except ModelResponseError as exc:
                    error = exc
                finally:
                    calls = ledger.model_calls[start_calls:]
                    actual = sum(call.input_tokens + call.output_tokens for call in calls)
                    charged = actual or reserved
                    self.meter["charged_tokens"] += charged - reserved
                    self.meter["reported_tokens"] += actual
                    self.meter["reserved_tokens"] = 0
                    self.meter["calls"] += 1
                    self.persist(self.meter)
                    before += charged
                if error is None:
                    return action
                ledger.trace(role, "model_response_rejected", code=error.code, attempt=attempt + 1)
                correction = "\nReturn the required complete JSON object with the action and its phase-specific fields."
            raise error

    @staticmethod
    def _fit_auxiliary(prompt, user, capacity):
        managed = json.loads(user)
        task = json.loads(managed["task"])
        for key in (None, "working_memory", "recalled_memory", "change_overview", "worker_results"):
            if key and (key != "worker_results" or "candidate_findings" in task):
                task.pop(key, None)
            managed["task"] = json.dumps(task, ensure_ascii=False)
            user = json.dumps(managed, ensure_ascii=False)
            estimated = token_bound(json.dumps([prompt, user], ensure_ascii=False))
            if estimated + 256 <= capacity:
                return user, estimated
        while managed.get("observations"):
            managed["observations"].pop(0)
            user = json.dumps(managed, ensure_ascii=False)
            estimated = token_bound(json.dumps([prompt, user], ensure_ascii=False))
            if estimated + 256 <= capacity:
                break
        return user, estimated


class BatchReview:
    """Drive one AgenticReviewer per planned batch, reusing completed batches
    across pauses and restarts until the whole diff is covered losslessly."""

    def __init__(self, store, reviewer_factory, budget, snapshot_factory=None, quota_recorder=None):
        self.store = store
        self.reviewer_factory = reviewer_factory
        self.budget = budget
        self.snapshot_factory = snapshot_factory
        self.quota_recorder = quota_recorder

    def _save(self, task_id, name, value):
        self.store.save_checkpoint(task_id, name, value)

    def _event(self, task, state, message):
        step = max((event["step"] for event in task.get("trace", [])), default=0) + 1
        event = TraceEvent(step, state, message, utc_now())
        task.setdefault("trace", []).append(event.to_dict())
        return event

    def _record_usage(self, tenant_id, summary):
        if self.quota_recorder is None or not summary:
            return
        execution = summary.get("execution") or {}
        try:
            self.quota_recorder(
                tenant_id,
                int(execution.get("total_tokens", 0) or 0),
                float(execution.get("cost_usd", 0) or 0.0),
            )
        except Exception:
            pass

    def run(self, task, diff):
        task_id = task["id"]
        tenant_id = task.get("tenant_id") or "default"
        saved = self.store.load_checkpoints(task_id)
        snapshot = checkpoint(saved, SNAPSHOT)
        plan = checkpoint(saved, PLAN)
        meter = checkpoint(saved, METER) or {
            "charged_tokens": 0, "reported_tokens": 0, "reserved_tokens": 0, "calls": 0,
        }
        if meter["reserved_tokens"]:
            # An interrupted provider request may have been billed before the worker died.
            meter["reserved_tokens"] = 0
            meter["calls"] += 1
            self._save(task_id, METER, meter)
        done = self._completed(saved)
        current = None
        reviewer = None
        budget = snapshot.get("task_budget", self.budget) + int(task["input"].get("additional_budget_tokens", 0))

        def configure_client(reviewer, overview):
            metered = MeteredClient(
                reviewer.client, reviewer.context_manager, overview, meter, budget,
                lambda value: self._save(task_id, METER, value),
                lambda: self.store.is_cancelled(task_id),
            )
            # Collaborators read the client through the runtime backref; keep
            # both references pointing at the metered wrapper.
            reviewer.client = metered
            reviewer.runtime.client = metered

        try:
            if self.store.is_cancelled(task_id):
                raise RuntimeCancelled("Review cancelled")
            if not snapshot:
                if self.snapshot_factory is None:
                    raise ReviewConflict("The task's model and version snapshot is missing")
                snapshot = self.snapshot_factory(task)
                self._save(task_id, SNAPSHOT, snapshot)
                budget = snapshot.get("task_budget", self.budget) + int(task["input"].get("additional_budget_tokens", 0))
            expected_diff = task["input"].get("diff_sha256")
            if expected_diff and hashlib.sha256(diff.encode("utf-8")).hexdigest() != expected_diff:
                raise ReviewConflict("The stored Diff no longer matches the task fingerprint")
            if not plan:
                self.store.transition(task_id, self._event(task, TaskState.PLANNING, "Planning Diff batches"))
                batches, metadata = plan_batches(diff, snapshot["batch_tokens"], task["input"].get("repository_root", ""))
                plan = {"batches": batches, "metadata": metadata}
                self._save(task_id, PLAN, plan)
            self.store.transition(task_id, self._event(task, TaskState.EXECUTING, "Reviewing planned batches"))
            overview = {
                "text_batches": len(plan["batches"]),
                "file_count": len({path for batch in plan["batches"] for path in batch["files"]}),
                "files": list(dict.fromkeys(
                    path for batch in plan["batches"] for path in batch["files"]
                ))[:35],
                "risk_evidence": "Repository tools when available; otherwise risk is unknown until reviewed.",
            }
            if len(plan["batches"]) > 1:
                guidance = checkpoint(saved, "review-v2-overview")
                if not guidance:
                    reviewer = self.reviewer_factory(BatchStore(self.store, task, 0, saved), snapshot)
                    configure_client(reviewer, overview)
                    ledger = ExecutionLedger("agentic", reviewer.input_cost_per_million, reviewer.output_cost_per_million)
                    response = reviewer._run_lead("overview", {
                        "inventory": overview,
                        "structural_signals": [item for batch in plan["batches"][:10]
                                               for item in batch.get("structural_signals", [])[:2]],
                        "instruction": (
                            'Return {"action":"final","review_focus":["..."]}. From the inventory, identify '
                            'cross-file checks to consider while reviewing batches. State uncertainty when context is missing. '
                            'All text batches will be reviewed.'
                        ),
                    }, None, ledger, f"{task_id}:overview")
                    guidance = {"review_focus": [str(item)[:200] for item in response["review_focus"][:8]],
                                "execution": ledger.summary()}
                    self._save(task_id, "review-v2-overview", guidance)
                    self._record_usage(tenant_id, guidance)
                overview["lead_focus"] = guidance["review_focus"]
            index = 0
            while index < len(plan["batches"]):
                batch = plan["batches"][index]
                current = batch["id"]
                if current in done:
                    index += 1
                    continue
                if self.store.is_cancelled(task_id):
                    raise RuntimeCancelled("Review cancelled")
                self._save(task_id, "review-v2-current", {"id": current, "status": "running"})
                scope = BatchStore(self.store, task, current, saved)
                reviewer = self.reviewer_factory(scope, snapshot)
                configure_client(reviewer, {**overview, "batch_id": current,
                                            "structural_signals": batch.get("structural_signals", [])})
                batch_key = f"{task_id}:{current}"
                parsed = parse_batch(batch["diff"])
                try:
                    findings = reviewer.review_with_context(
                        batch_key, batch["diff"], parsed, task["repository"], tenant_id,
                    )
                except BatchInputTooLarge:
                    if not self._split_batch(plan, index):
                        raise
                    overview["text_batches"] = len(plan["batches"])
                    self._save(task_id, PLAN, plan)
                    self.store.transition(task_id, self._event(
                        task, TaskState.EXECUTING, f"Split batch {current} to fit the request budget",
                    ))
                    continue
                result = {"findings": [finding.to_dict() for finding in findings],
                          "summary": reviewer.collaboration_summary(batch_key)}
                self._save(task_id, RESULT + str(current), result)
                self._record_usage(tenant_id, result["summary"])
                done[current] = result
                index += 1
            if self.store.is_cancelled(task_id):
                raise RuntimeCancelled("Review cancelled")
            self.store.transition(task_id, self._event(task, TaskState.REVIEWING, "Merging completed batches"))
            guide = checkpoint(self.store.load_checkpoints(task_id), "review-v2-overview")
            report = self.build_report(task, done, plan, meter, guide=guide)
            self.store.succeed(task_id, report, self._event(task, TaskState.SUCCESS, "Review completed"))
            if done and reviewer is not None:
                reviewer._persist_task_memory(task_id, tenant_id, task["repository"], report.findings,
                                              SimpleNamespace(accepted=report.findings, rejected=[], checks={}),
                                              report.files_reviewed, report.collaboration)
            return report
        except TaskBudgetPaused as exc:
            if self.store.is_cancelled(task_id):
                self.store.cancel(task_id, self._event(task, TaskState.CANCELLED, "Review cancelled"))
                return None
            self._save(task_id, "review-v2-current", {"id": current, "status": "paused"})
            self.store.transition(task_id, self._event(task, TaskState.PAUSED_BUDGET, str(exc)))
            if self.store.is_cancelled(task_id):
                self.store.cancel(task_id, self._event(task, TaskState.CANCELLED, "Review cancelled"))
            return None
        except RuntimeCancelled as exc:
            self._save(task_id, "review-v2-current", {"id": current, "status": "cancelled"})
            self.store.cancel(task_id, self._event(task, TaskState.CANCELLED, str(exc)))
            return None
        except Exception as exc:
            self._save(task_id, "review-v2-current", {"id": current, "status": "failed",
                       "error_code": getattr(exc, "code", "input_budget" if isinstance(exc, BatchInputTooLarge) else None)})
            self.store.fail(task_id, str(exc), self._event(task, TaskState.FAILED, "Review failed"))
            if not isinstance(exc, (ValueError, RuntimeBudgetExceeded)):
                try:
                    self.store.record_failure_case(task_id, "execution_error", {"error": str(exc)[:1000]})
                except Exception:
                    pass
            if isinstance(exc, (ValueError, ModelResponseError, RuntimeBudgetExceeded)):
                # Deterministic engine failures (bad envelope, exhausted
                # budget validation, diff mismatch) must not burn queue
                # retries; straight to the dead-letter queue instead.
                raise PermanentTaskError(str(exc)) from exc
            raise

    @staticmethod
    def _split_batch(plan, index):
        batch = plan["batches"][index]
        size = token_bound(batch["diff"])
        if size < 320:
            return False
        try:
            children, _ = plan_batches(batch["diff"], size // 2)
        except ValueError:
            return False
        if len(children) < 2:
            return False
        next_id = plan.get("next_batch_id", max(item["id"] for item in plan["batches"]) + 1)
        for child in children:
            child["id"] = next_id
            next_id += 1
            child["structural_signals"] = [signal for signal in batch.get("structural_signals", [])
                                           if any(span["path"] == signal["path"] and span["side"] == "new"
                                                  and span["start"] <= signal["line"] <= span["end"]
                                                  for span in child["ranges"])]
            for span in child["ranges"]:
                original = next((item for item in batch["ranges"] if (
                    (item["path"], item["side"]) == (span["path"], span["side"])
                    and item["start"] <= span["start"] <= span["end"] <= item["end"]
                )), None)
                if original is None:
                    return False
                span["hunk"] = original["hunk"]
                span["offset"] += original["offset"]
        plan["next_batch_id"] = next_id
        plan["batches"][index:index + 1] = children
        return True

    @staticmethod
    def _completed(saved):
        return {int(name[len(RESULT):]): value["state"] for name, value in saved.items()
                if name.startswith(RESULT) and value["status"] == "completed"}

    @staticmethod
    def coverage(done, plan, current=None):
        current = current or {}
        batches = [{k: v for k, v in batch.items() if k != "diff"}
                   for batch in plan.get("batches", [])]
        for batch in batches:
            bid = batch["id"]
            batch["status"] = ("completed" if bid in done else current.get("status", "pending")
                               if bid == current.get("id") else "pending")
            if bid in done:
                batch["findings"] = len(done[bid].get("findings", []))
        return {"complete": bool(plan) and len(done) == len(batches), "completed": len(done),
                "total": len(batches), "batches": batches, "metadata": plan.get("metadata", [])}

    @staticmethod
    def build_report(task, done, plan, meter, guide=None):
        summaries = [done[bid]["summary"] for bid in sorted(done)]
        findings = AgenticReviewer._merge(
            ReviewHarness._finding_from_dict(finding)
            for bid in sorted(done) for finding in done[bid]["findings"]
        )
        files = list(dict.fromkeys(path for batch in plan.get("batches", [])
                                   if batch["id"] in done for path in batch["files"]))
        coverage = BatchReview.coverage(done, plan)
        risk = ReviewHarness._risk(findings) if coverage["complete"] and coverage["total"] else "unknown"
        summary = f"Reviewed {coverage['completed']}/{coverage['total']} text batches. Found {len(findings)} actionable issue(s)."
        first = summaries[0] if summaries else {}
        guide = guide or {}
        runs = summaries + ([guide] if guide else [])
        execution = {}
        for key in ("llm_calls", "tool_calls", "total_tokens", "cost_usd", "input_tokens",
                    "output_tokens", "duration_ms", "failed_model_calls", "failed_tool_calls"):
            execution[key] = sum(item.get("execution", {}).get(key, 0) for item in runs)
        for key in ("model_call_log", "tool_call_log"):
            execution[key] = [row for item in runs for row in item.get("execution", {}).get(key, [])]
        execution.update({"coverage": coverage, "budget": dict(meter)})
        first_execution = first.get("execution") or {}
        for key in ("prompt_version", "tenant_id"):
            if first_execution.get(key) is not None:
                execution[key] = first_execution[key]
        if first.get("repository_context") is not None:
            execution["repository_context"] = first["repository_context"]
        collaboration = dict(first.get("collaboration") or {})
        if guide:
            collaboration["lead_focus"] = guide["review_focus"]
        if len(summaries) > 1:
            collaboration["batches"] = [item.get("collaboration") or {} for item in summaries]
            collaboration["roles"] = list(dict.fromkeys(
                role for item in collaboration["batches"] for role in item.get("roles", [])
            ))
        return ReviewReport(task["repository"], task.get("pull_request"), summary, risk,
                            findings, files, "agentic-reviewer", collaboration,
                            first.get("run_mode") or {}, first.get("components") or [], execution)

    def details(self, task, may_review):
        task = copy(task)
        if task["input"].get("review_engine") != ENGINE:
            task.update(coverage=None, confirmation=None)
            return task
        saved = self.store.load_checkpoints(task["id"])
        plan, done, meter = checkpoint(saved, PLAN), self._completed(saved), checkpoint(saved, METER)
        coverage = self.coverage(done, plan, checkpoint(saved, "review-v2-current"))
        for batch in coverage["batches"]:
            active = checkpoint(saved, f"review-v2-batch-{batch['id']}:agentic-lead-session")
            batch["phase"] = (active.get("session") or {}).get("phase")
        confirmation = checkpoint(saved, "review-v2-confirmation") or None
        snapshot = checkpoint(saved, SNAPSHOT)
        state = task["state"]
        task.update(coverage=coverage, confirmation=confirmation, versions={
            key: snapshot.get(key) for key in ("provider", "model", "prompt_version", "skill_versions")
        }, budget={"limit": snapshot.get("task_budget", self.budget) + int(task["input"].get("additional_budget_tokens", 0)), **meter},
            actions={
                "accept": may_review and state == "SUCCESS" and coverage["complete"] and coverage["total"] > 0 and not confirmation,
                "resume": may_review and state in {"FAILED", "PAUSED_BUDGET"} and not task["cancel_requested"],
                "cancel": may_review and state in {"PENDING", "PLANNING", "EXECUTING", "REVIEWING", "PAUSED_BUDGET"}
                          and not task["cancel_requested"],
            })
        if task["report"] is not None:
            task["report_revision"] = report_revision(task["report"])
        elif plan:
            task["partial_report"] = self.build_report(task, done, plan, meter,
                                                      checkpoint(saved, "review-v2-overview")).to_dict()
        return task
