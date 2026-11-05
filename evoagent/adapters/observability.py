"""Tracing helpers that degrade cleanly when OpenTelemetry is not installed."""
import json
import logging
import urllib.error
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar


trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
logger = logging.getLogger("evoagent")


class AlertNotifier:
    """Best-effort webhook delivery for persisted alerts.

    Failure must never change the review outcome, so network errors are logged
    and swallowed.  The webhook URL is operator configuration, not user input.
    """

    def __init__(self, url: str = "", timeout: int = 5):
        self.url = str(url or "").strip()
        self.timeout = max(1, int(timeout))

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def notify(self, tenant_id: str, alert_key: str, severity: str, message: str) -> bool:
        if not self.url:
            return False
        payload = json.dumps({
            "tenant_id": tenant_id, "alert_key": alert_key,
            "severity": severity, "message": message,
        }, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=payload, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "EvoReview"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout):
                return True
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            logger.warning("alert webhook delivery failed: %s", exc)
            return False


class Observability:
    def __init__(self, service_name: str = "evoagent", endpoint: str = ""):
        self.tracer = None
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
            if endpoint:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
            trace.set_tracer_provider(provider)
            self.tracer = trace.get_tracer(service_name)
        except ImportError:
            self.tracer = None

    @contextmanager
    def span(self, name: str, trace_id: str = "", **attributes):
        token = trace_id_var.set(trace_id or trace_id_var.get())
        if self.tracer:
            with self.tracer.start_as_current_span(name) as span:
                for key, value in attributes.items():
                    if value is not None:
                        span.set_attribute(key, value)
                try:
                    yield span
                except Exception as exc:
                    span.record_exception(exc)
                    raise
                finally:
                    trace_id_var.reset(token)
        else:
            try:
                yield None
            finally:
                trace_id_var.reset(token)


class AlertManager:
    def __init__(
        self, store, failure_rate: float = .2, min_samples: int = 10,
        window_seconds: int = 900, notifier: AlertNotifier = None,
    ):
        self.store = store
        self.failure_rate = failure_rate
        self.min_samples = min_samples
        self.window_seconds = max(1, int(window_seconds))
        self.notifier = notifier or AlertNotifier()

    def evaluate(self, tenant_id: str) -> None:
        # A sliding window keeps an old incident from paging forever and lets
        # the alert reflect the current health of the deployment.
        stats = self.store.dashboard_stats(
            tenant_id, window_seconds=self.window_seconds,
        )
        if stats["tasks_total"] >= self.min_samples:
            rate = stats["tasks_failed"] / stats["tasks_total"]
            if rate > self.failure_rate:
                message = (
                    "Review failure rate %.1f%% exceeds the %.1f%% threshold "
                    "in the last %d seconds."
                    % (rate * 100, self.failure_rate * 100, self.window_seconds)
                )
                created = self.store.create_alert(
                    tenant_id, "review-failure-rate", "critical", message,
                )
                # Notify only when the incident opens. Re-evaluating an
                # already-open alert must not page again on every review.
                if created:
                    self.notifier.notify(
                        tenant_id, "review-failure-rate", "critical", message,
                    )
