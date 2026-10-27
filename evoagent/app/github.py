"""GitHub webhook intake and installation-scoped clients."""
import uuid
from typing import Any, Callable, Dict, Optional

from ..config import Settings
from ..store_contract import StoreProtocol


class GitHubApplicationService:
    def __init__(
        self,
        settings: Settings,
        store: StoreProtocol,
        queue: Any,
        policy: Any,
        chat_client_provider: Callable[[], Any],
        default_client: Any,
        client_for_installation: Callable[[int], Any],
        create_deferred_task: Callable[..., str],
        metrics: Any,
    ):
        self.settings = settings
        self.store = store
        self.queue = queue
        self.policy = policy
        self.chat_client_provider = chat_client_provider
        self.default_client = default_client
        self.client_for_installation = client_for_installation
        self.create_deferred_task = create_deferred_task
        self.metrics = metrics

    def handle_github_pull_request(
        self, payload: Dict[str, Any], delivery_id: str,
        payload_sha256: str, tenant_id: str = "",
    ) -> Dict[str, Any]:
        installation_id = (payload.get("installation") or {}).get("id")
        tenant_id = tenant_id or (
            self.store.installation_tenant(installation_id) if installation_id else None
        ) or self.settings.default_tenant_id
        action = payload.get("action")
        supported = action in {"opened", "reopened", "synchronize"}
        pull = payload.get("pull_request") or {}
        repository = (payload.get("repository") or {}).get("full_name", "")
        number, diff_url = payload.get("number"), pull.get("diff_url")
        if supported:
            self.policy.require_agentic_model(self.chat_client_provider())
            if not repository or not isinstance(number, int) or not diff_url:
                raise ValueError("invalid GitHub pull_request payload")
            self.policy.authorize_repository(tenant_id, repository)
        claim_token = uuid.uuid4().hex
        if not self.store.claim_webhook(
            delivery_id, tenant_id, "pull_request", payload_sha256, claim_token,
        ):
            existing = self.store.get_webhook(delivery_id) or {}
            return {
                "duplicate": True, "task_id": existing.get("task_id"),
                "state": "PENDING" if existing.get("task_id") else "ACCEPTED",
            }
        try:
            if not supported:
                self.store.complete_webhook(delivery_id, None, claim_token)
                return {
                    "ignored": True,
                    "reason": "unsupported pull_request action: %s" % action,
                }
            # A retry after a crash reuses the same task even if the process died
            # between task creation and binding the delivery record.
            task_id = str(uuid.uuid5(
                uuid.NAMESPACE_URL,
                "evoagent:webhook:%s:%s" % (tenant_id, delivery_id),
            ))
            queue_payload = {
                "task_id": task_id, "repository": repository, "pull_request": number,
                "github_issue_url": pull.get("issue_url", ""),
                "installation_id": installation_id, "tenant_id": tenant_id,
                "diff_url": diff_url,
            }
            if not self.store.get(task_id, tenant_id):
                self.create_deferred_task(
                    repository, number, "github-webhook", tenant_id,
                    queue_payload, task_id=task_id,
                )
            self.store.bind_webhook_task(delivery_id, task_id, claim_token)
            self.queue.submit(queue_payload, message_id=task_id)
            self.store.complete_webhook(delivery_id, task_id, claim_token)
            self.metrics.inc("reviews_enqueued_total")
            return {
                "task_id": task_id, "state": "PENDING",
                "queue": self.queue.backend,
                "will_post_to_github": self.settings.auto_post_review,
            }
        except Exception:
            self.store.release_webhook(delivery_id, claim_token)
            raise

    def github_client_for_installation(self, installation_id: Optional[int] = None) -> Any:
        if installation_id is None:
            return self.default_client
        return self.client_for_installation(installation_id)
