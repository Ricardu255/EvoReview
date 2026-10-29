"""Execute reviews, shadow candidates and queued deliveries."""
import hashlib
import json
import uuid
from typing import Any, Callable, Dict, Optional

from ..errors import PermanentTaskError
from ..domain.finding_identity import canonical_identity
from ..domain.models import TaskState, TraceEvent
from ..domain.report import to_markdown
from ..ports.stores import StoreProtocol
from ..time_utils import utc_now
from .policies import ReviewPolicy


def mark_dead_letter(store, metrics, payload: Dict[str, Any], error: str) -> None:
    """Mark a pending task failed and raise an alert for a dead-letter delivery."""
    task_id = payload.get("task_id", "")
    tenant_id = payload.get("tenant_id", "default")
    task = store.get(task_id, tenant_id) if task_id else None
    if task and task.get("state") not in {
        TaskState.SUCCESS.value, TaskState.FAILED.value, TaskState.CANCELLED.value,
    }:
        step = max(
            [int(item.get("step", 0)) for item in task.get("trace", [])] or [0]
        ) + 1
        store.fail(
            task_id, error,
            TraceEvent(
                step, TaskState.FAILED,
                "Task entered the dead-letter queue: %s" % error, utc_now(),
            ),
        )
    store.create_alert(
        tenant_id, "dlq:%s" % (task_id or "unknown"), "critical",
        "Task %s entered the dead-letter queue: %s" % (task_id, error),
    )
    metrics.inc("dead_letters_total")


class ReviewExecutionService:
    def __init__(
        self,
        settings: Any,
        store: StoreProtocol,
        policy: ReviewPolicy,
        observability: Any,
        releases: Any,
        alerts: Any,
        metrics: Any,
        build_agentic_reviewer: Callable[..., Any],
        harness_factory: Callable[[Any], Any],
        github_client_for_installation: Callable[[Optional[int]], Any],
    ):
        self.settings = settings
        self.store = store
        self.policy = policy
        self.observability = observability
        self.releases = releases
        self.alerts = alerts
        self.metrics = metrics
        self.build_agentic_reviewer = build_agentic_reviewer
        self.harness_factory = harness_factory
        self.github_client_for_installation = github_client_for_installation

    def _execution_assignment(self, tenant_id: str, task_id: str) -> dict:
        assignment = self.releases.assignment(tenant_id, "llm-review", task_id)
        active = self.store.get_active_skill_version("llm-review", tenant_id)
        return {
            "release_lane": assignment["lane"], "shadow": assignment["shadow"],
            "prompt_version": assignment.get(
                "prompt_version", active["version"] if active else 0
            ),
            "shadow_version": assignment.get("shadow_version"),
        }

    @staticmethod
    def _release_result(report) -> dict:
        return {
            "prompt_version": report.execution.get("prompt_version"),
            "finding_keys": sorted({
                json.dumps([
                    canonical_identity(item.rule_id, item.cwe), item.path, item.line,
                ])
                for item in report.findings
            }),
        }

    def _run_shadow(
        self, task_id, repository, pull_request, diff, tenant_id, task_input, primary,
    ):
        shadow_id = str(uuid.uuid5(
            uuid.NAMESPACE_URL, "evoagent:shadow:" + task_id,
        ))
        if self.store.load_checkpoints(task_id).get(
            "shadow-observed", {}
        ).get("status") == "completed":
            return
        candidate, failed = None, False
        if not self.store.get(shadow_id, tenant_id):
            self.store.create(shadow_id, repository, pull_request, {
                **task_input, "source": "shadow", "shadow": False,
                "primary_task_id": task_id,
                "prompt_version": task_input["shadow_version"],
            }, tenant_id)
            self.store.save_task_payload(shadow_id, diff)
        try:
            reviewer = self.build_agentic_reviewer(
                tenant_id, task_input["shadow_version"], shadow=True,
            )
            report = self.harness_factory(reviewer).run(
                shadow_id, repository, pull_request, diff, tenant_id,
            )
            candidate = self._release_result(report)
        except Exception:
            failed = True
        self.releases.observe_shadow(
            tenant_id, "llm-review", task_id, task_input["release_lane"],
            self._release_result(primary), candidate, failed,
        )
        self.store.save_checkpoint(
            task_id, "shadow-observed", {"task_id": shadow_id, "failed": failed},
        )

    def run_review(
        self, task_id: str, repository: str, pull_request: Optional[int],
        diff: str, tenant_id: str,
    ):
        task = self.store.get(task_id, tenant_id)
        if not task:
            raise ValueError("task not found")
        task_input = task.get("input") or {}
        if "prompt_version" not in task_input:
            task_input.update(self._execution_assignment(tenant_id, task_id))
            self.store.update_task_input(task_id, task_input)
        reviewer = self.build_agentic_reviewer(
            tenant_id, task_input["prompt_version"],
        )
        harness = self.harness_factory(reviewer)
        report = harness.run(task_id, repository, pull_request, diff, tenant_id)
        if task_input.get("shadow"):
            try:
                self._run_shadow(
                    task_id, repository, pull_request, diff, tenant_id,
                    task_input, report,
                )
            except Exception:
                # Shadow infrastructure must not change a successful primary result.
                self.metrics.inc("shadow_observation_errors_total")
        return report

    def process_queued(self, payload: Dict[str, Any]) -> None:
        task_id = payload["task_id"]
        task = self.store.get(task_id)
        if not task:
            raise PermanentTaskError("task record no longer exists")
        tenant_id = task.get("tenant_id") or "default"
        if (
            payload.get("tenant_id", tenant_id) != tenant_id
            or payload["repository"] != task["repository"]
        ):
            raise PermanentTaskError(
                "queue payload does not match the task's tenant and repository"
            )
        diff = self.store.get_task_payload(task_id)
        if diff is None and payload.get("diff_url"):
            client = self.github_client_for_installation(
                payload.get("installation_id")
            )
            client.ensure_repository_access(payload["repository"])
            diff = client.fetch_diff(payload["diff_url"])
            self.policy.validate_review(payload["repository"], diff)
            encoded = diff.encode("utf-8")
            self.store.save_task_payload(task_id, diff)
            self.store.update_task_input(task_id, {
                "diff_pending": False, "diff_bytes": len(encoded),
                "diff_sha256": hashlib.sha256(encoded).hexdigest(),
            })
        if diff is None:
            raise PermanentTaskError("task payload no longer exists")
        try:
            with self.observability.span(
                "review.async", task_id, task_id=task_id, tenant_id=tenant_id,
            ), self.metrics.timer("review_duration"):
                report = self.run_review(
                    task_id, payload["repository"], payload.get("pull_request"),
                    diff, tenant_id,
                )
            self.metrics.inc("reviews_total")
            lane = (task.get("input") or {}).get("release_lane", "stable")
            self.releases.observe(tenant_id, "llm-review", False, lane)
            if payload.get("github_issue_url") and self.settings.auto_post_review:
                client = self.github_client_for_installation(
                    payload.get("installation_id")
                )
                client.upsert_comment(
                    payload["github_issue_url"], to_markdown(report.to_dict()),
                    "<!-- evoagent-review:%s -->" % task_id,
                )
        except Exception:
            self.metrics.inc("reviews_failed_total")
            lane = (task.get("input") or {}).get("release_lane", "stable")
            self.releases.observe(tenant_id, "llm-review", True, lane)
            self.alerts.evaluate(tenant_id)
            raise

    def on_dead_letter(self, payload: Dict[str, Any], error: str) -> None:
        mark_dead_letter(self.store, self.metrics, payload, error)
