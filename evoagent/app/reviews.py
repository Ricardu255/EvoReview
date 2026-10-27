"""Review task lifecycle: create, enqueue, execute, resume and cancel."""
import hashlib
import json
import uuid
from typing import Any, Callable, Dict, Optional

from ..config import Settings
from ..errors import PermanentTaskError
from ..finding_identity import canonical_identity
from ..modes import RunMode
from ..models import TaskState, TraceEvent
from ..report import to_markdown
from ..store_contract import StoreProtocol
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


class ReviewsApplicationService:
    def __init__(
        self,
        settings: Settings,
        store: StoreProtocol,
        queue: Any,
        policy: ReviewPolicy,
        observability: Any,
        releases: Any,
        alerts: Any,
        metrics: Any,
        build_agentic_reviewer: Callable[..., Any],
        harness_factory: Callable[[Any], Any],
        github_client_for_installation: Callable[[Optional[int]], Any],
        chat_client_provider: Callable[[], Any],
    ):
        self.settings = settings
        self.store = store
        self.queue = queue
        self.policy = policy
        self.observability = observability
        self.releases = releases
        self.alerts = alerts
        self.metrics = metrics
        self.build_agentic_reviewer = build_agentic_reviewer
        self.harness_factory = harness_factory
        self.github_client_for_installation = github_client_for_installation
        self.chat_client_provider = chat_client_provider

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

    def create_task(
        self, repository: str, diff: str, pull_request: Optional[int], source: str,
        tenant_id: str = "default", repository_root: str = "",
        enabled_agents: Optional[list] = None,
        enabled_skills: Optional[list] = None,
    ) -> str:
        task_id = str(uuid.uuid4())
        encoded = diff.encode("utf-8")
        assignment = self._execution_assignment(tenant_id, task_id)
        self.store.create(task_id, repository, pull_request, {
            "source": source, "diff_bytes": len(encoded),
            "diff_sha256": hashlib.sha256(encoded).hexdigest(),
            **assignment,
            "mode": RunMode.AGENTIC.value,
            "repository_root": repository_root,
            "enabled_agents": enabled_agents or [],
            "enabled_skills": enabled_skills or [],
        }, tenant_id)
        self.store.save_task_payload(task_id, diff)
        return task_id

    def create_deferred_task(
        self, repository: str, pull_request: Optional[int], source: str,
        tenant_id: str, payload: Dict[str, Any], task_id: Optional[str] = None,
    ) -> str:
        task_id = task_id or str(uuid.uuid4())
        assignment = self._execution_assignment(tenant_id, task_id)
        self.store.create(task_id, repository, pull_request, {
            "source": source, "diff_pending": True,
            "repository_root": self.policy.resolve_repository_root(
                repository, "", tenant_id,
            ),
            **assignment,
            **payload,
            "mode": RunMode.AGENTIC.value,
        }, tenant_id)
        return task_id

    def create_review(
        self, repository: str, diff: str, pull_request: Optional[int] = None,
        source: str = "api", tenant_id: str = "default", mode: str = "",
        repository_root: str = "", enabled_agents: Optional[list] = None,
        enabled_skills: Optional[list] = None,
    ) -> Dict[str, Any]:
        self.policy.validate_review(repository, diff)
        RunMode.parse(mode) if mode else None
        self.policy.require_agentic_model(self.chat_client_provider())
        repository_root = self.policy.resolve_repository_root(
            repository, repository_root, tenant_id,
        )
        self.policy.validate_enabled_agents(enabled_agents)
        self.policy.validate_enabled_skills(enabled_skills, tenant_id)
        self.policy.authorize_repository(tenant_id, repository)
        task_id = self.create_task(
            repository, diff, pull_request, source, tenant_id,
            repository_root, enabled_agents, enabled_skills,
        )
        try:
            with self.observability.span(
                "review", task_id, task_id=task_id, tenant_id=tenant_id,
                repository=repository,
            ), self.metrics.timer("review_duration"):
                report = self.run_review(
                    task_id, repository, pull_request, diff, tenant_id,
                )
            self.metrics.inc("reviews_total")
            lane = (self.store.get(task_id, tenant_id).get("input") or {}).get(
                "release_lane", "stable"
            )
            self.releases.observe(tenant_id, "llm-review", False, lane)
            return {
                "task_id": task_id, "state": "SUCCESS",
                "report": report.to_dict(),
            }
        except Exception:
            task = self.store.get(task_id, tenant_id) or {}
            lane = (task.get("input") or {}).get("release_lane", "stable")
            self.releases.observe(tenant_id, "llm-review", True, lane)
            self.alerts.evaluate(tenant_id)
            raise

    def enqueue_review(
        self, repository: str, diff: str, pull_request: Optional[int] = None,
        source: str = "api", github_issue_url: str = "",
        installation_id: Optional[int] = None,
        tenant_id: str = "default", mode: str = "",
        repository_root: str = "", enabled_agents: Optional[list] = None,
        enabled_skills: Optional[list] = None,
    ) -> Dict[str, Any]:
        self.policy.validate_review(repository, diff)
        RunMode.parse(mode) if mode else None
        self.policy.require_agentic_model(self.chat_client_provider())
        repository_root = self.policy.resolve_repository_root(
            repository, repository_root, tenant_id,
        )
        self.policy.validate_enabled_agents(enabled_agents)
        self.policy.validate_enabled_skills(enabled_skills, tenant_id)
        self.policy.authorize_repository(tenant_id, repository)
        task_id = self.create_task(
            repository, diff, pull_request, source, tenant_id,
            repository_root, enabled_agents, enabled_skills,
        )
        self.queue.submit({
            "task_id": task_id, "repository": repository,
            "pull_request": pull_request,
            "github_issue_url": github_issue_url,
            "installation_id": installation_id,
            "tenant_id": tenant_id,
        }, message_id=task_id)
        self.metrics.inc("reviews_enqueued_total")
        return {
            "task_id": task_id, "state": "PENDING",
            "queue": self.queue.backend,
        }

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

    def resume_task(self, task_id: str, tenant_id: Optional[str] = None) -> dict:
        task = self.store.get(task_id, tenant_id)
        if not task:
            raise ValueError("task not found")
        if task["state"] == "SUCCESS":
            return {"task_id": task_id, "state": "SUCCESS", "report": task["report"]}
        if task["state"] not in {"FAILED", "CANCELLED"}:
            raise ValueError("only failed or cancelled tasks can be resumed")
        diff = self.store.get_task_payload(task_id)
        if diff is None:
            raise ValueError("task payload is no longer available")
        if not self.store.prepare_resume(task_id, task.get("tenant_id", "default")):
            raise ValueError("task state changed; resume was not applied")
        try:
            self.queue.submit({
                "task_id": task_id, "repository": task["repository"],
                "pull_request": task.get("pull_request"),
                "tenant_id": task.get("tenant_id", "default"),
            }, message_id=task_id)
        except Exception as exc:
            step = max([int(item["step"]) for item in task.get("trace", [])] or [0]) + 1
            self.store.fail(task_id, str(exc), TraceEvent(
                step, TaskState.FAILED, "Resume delivery failed: %s" % exc, utc_now(),
            ))
            raise
        return {"task_id": task_id, "state": "PENDING", "resumed": True}

    def cancel_task(self, task_id: str, tenant_id: Optional[str] = None) -> bool:
        return self.store.request_cancel(task_id, tenant_id)
