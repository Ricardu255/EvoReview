"""Tests for the controlled benchmark corpus, metrics and observability."""
import json
import unittest
import urllib.error
from contextlib import contextmanager
from unittest.mock import patch

from evoagent.adapters.metrics import Metrics
from evoagent.adapters.observability import (
    AlertManager, AlertNotifier, Observability, trace_id_var,
)
from evoagent.domain.diff_parser import parse_unified_diff
from evoagent.evaluation.benchmark import (
    ContextRuleReviewer, _risk_scenarios, _risk_source, generate_controlled_pr_cases,
)
from evoagent.evaluation.harness import RULE_TO_CWE


def diff_for(line):
    return "--- a/app.py\n+++ b/app.py\n@@ -0,0 +1 @@\n+%s\n" % line


class ControlledBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases = generate_controlled_pr_cases()

    def test_generates_one_hundred_deterministic_cases(self):
        self.assertEqual(100, len(self.cases))
        self.assertEqual(self.cases, generate_controlled_pr_cases())
        self.assertEqual(
            ["pr-%04d" % number for number in range(1, 101)],
            [case["id"] for case in self.cases],
        )

    def test_case_metadata_and_splits(self):
        repositories = {case["repository"] for case in self.cases}
        self.assertEqual(10, len(repositories))
        validation = {case["repository"] for case in self.cases
                      if case["split"] == "validation"}
        holdout = {case["repository"] for case in self.cases
                   if case["split"] == "holdout"}
        self.assertEqual(8, len(validation))
        self.assertEqual(2, len(holdout))
        self.assertFalse(validation & holdout)

    def test_risk_and_clean_case_mix(self):
        risk = [case for case in self.cases if case["expected_findings"]]
        clean = [case for case in self.cases if not case["expected_findings"]]
        self.assertEqual(40, len(risk))
        self.assertEqual(60, len(clean))
        for case in risk:
            self.assertEqual("risk", case.get("kind", "risk"))
            expected = case["expected_findings"][0]
            self.assertIn(expected["cwe"], RULE_TO_CWE.values())
            self.assertIn(expected["severity"], {"critical", "high", "medium", "low"})
            self.assertTrue(case["repair_validation"]["risk_pattern"])
        for case in clean:
            self.assertEqual({}, case["repair_validation"])

    def test_every_diff_parses_and_labels_point_at_an_added_line(self):
        for case in self.cases:
            with self.subTest(case=case["id"]):
                parsed = parse_unified_diff(case["diff"])
                self.assertIn(case["path"] if "path" in case else list(
                    case["after_files"])[0],
                    parsed.files if "path" not in case else [case["path"]])
                if case["expected_findings"]:
                    expected = case["expected_findings"][0]
                    added = {(item.path, item.line) for item in parsed.added_lines}
                    self.assertIn(
                        (expected["path"], expected["start_line"]), added,
                    )
                self.assertTrue(case["after_files"])

    def test_risk_scenarios_have_exactly_forty_entries_and_known_cwes(self):
        scenarios = _risk_scenarios()
        self.assertEqual(40, len(scenarios))
        for scenario in scenarios:
            self.assertEqual(RULE_TO_CWE[scenario["rule_id"]], scenario["cwe"])
            before, after, needle = _risk_source(scenario)
            self.assertIn(needle, after)

    def test_supplemental_rules_fire_on_their_benchmark_scenarios(self):
        reviewer = ContextRuleReviewer()
        known = {rule_id for rule_id, _severity, _pattern in reviewer.RULES}
        checked = set()
        for scenario in _risk_scenarios():
            if scenario["rule_id"] not in known:
                continue
            diff = diff_for(scenario["line"].format(n=1))
            findings = reviewer.review(diff, parse_unified_diff(diff))
            self.assertIn(
                scenario["rule_id"], {item.rule_id for item in findings},
                msg=scenario["rule_id"],
            )
            checked.add(scenario["rule_id"])
        self.assertEqual(known, checked)

    def test_supplemental_rules_do_not_flag_safe_alternatives(self):
        reviewer = ContextRuleReviewer()
        for line in (
            "value = int(raw)",
            'api_key = os.environ["API_KEY"]',
            "digest = hashlib.sha256(value.encode()).hexdigest()",
            "response.set_cookie('sid', value, secure=True)",
            'cursor.execute("SELECT * FROM users WHERE id = ?", (value,))',
            "    for attempt in range(max_attempts):",
            'path = tempfile.NamedTemporaryFile(delete=False).name',
        ):
            with self.subTest(line=line):
                diff = diff_for(line)
                self.assertEqual([], reviewer.review(diff, parse_unified_diff(diff)))

    def test_findings_carry_location_severity_and_confidence(self):
        diff = diff_for("digest = hashlib.md5(value).hexdigest()")
        findings = ContextRuleReviewer().review(diff, parse_unified_diff(diff))
        self.assertEqual(1, len(findings))
        finding = findings[0]
        self.assertEqual("app.py", finding.path)
        self.assertEqual(1, finding.line)
        self.assertEqual("medium", finding.severity.value)
        self.assertEqual("SEC-WEAK-HASH", finding.rule_id)
        self.assertGreaterEqual(finding.confidence, 0.8)


class MetricsTests(unittest.TestCase):
    def test_counters_accumulate_and_render_in_sorted_order(self):
        metrics = Metrics()
        metrics.inc("zeta")
        metrics.inc("alpha", 2.5)
        metrics.inc("alpha", 0.5)
        output = metrics.prometheus()
        self.assertIn("# TYPE evoagent_alpha counter", output)
        self.assertIn("evoagent_alpha 3.0", output)
        self.assertIn("evoagent_zeta 1.0", output)
        self.assertLess(output.index("evoagent_alpha"), output.index("evoagent_zeta"))
        self.assertTrue(output.endswith("\n"))

    def test_timer_records_a_count_even_when_the_block_raises(self):
        metrics = Metrics()
        with metrics.timer("work"):
            pass
        with self.assertRaises(RuntimeError):
            with metrics.timer("work"):
                raise RuntimeError("boom")
        output = metrics.prometheus()
        self.assertIn("# TYPE evoagent_work_seconds summary", output)
        self.assertIn("evoagent_work_seconds_count 2", output)
        self.assertIn("evoagent_work_seconds_sum", output)

    def test_empty_metrics_render_a_newline(self):
        self.assertEqual("\n", Metrics().prometheus())


class _FakeSpan:
    def __init__(self):
        self.attributes = {}
        self.exceptions = []

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def record_exception(self, exc):
        self.exceptions.append(exc)


class _FakeTracer:
    def __init__(self):
        self.spans = []

    @contextmanager
    def start_as_current_span(self, name):
        span = _FakeSpan()
        self.spans.append((name, span))
        yield span


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.observability = Observability()
        self.observability.tracer = None

    def test_without_a_tracer_the_span_yields_none_and_sets_trace_id(self):
        with self.observability.span("review", trace_id="abc") as span:
            self.assertIsNone(span)
            self.assertEqual("abc", trace_id_var.get())
        self.assertEqual("", trace_id_var.get())

    def test_nested_spans_inherit_the_current_trace_id(self):
        with self.observability.span("outer", trace_id="abc"):
            with self.observability.span("inner") as span:
                self.assertIsNone(span)
                self.assertEqual("abc", trace_id_var.get())
            self.assertEqual("abc", trace_id_var.get())
        self.assertEqual("", trace_id_var.get())

    def test_exceptions_propagate_and_reset_the_trace_context(self):
        with self.assertRaises(RuntimeError):
            with self.observability.span("outer", trace_id="abc"):
                raise RuntimeError("boom")
        self.assertEqual("", trace_id_var.get())

    def test_tracer_receives_filtered_attributes_and_exceptions(self):
        tracer = _FakeTracer()
        self.observability.tracer = tracer
        with self.observability.span("review", trace_id="abc", tenant="t", empty=None):
            pass
        name, span = tracer.spans[0]
        self.assertEqual("review", name)
        self.assertEqual({"tenant": "t"}, span.attributes)

        with self.assertRaises(ValueError):
            with self.observability.span("failing"):
                raise ValueError("bad")
        self.assertEqual(1, len(tracer.spans[1][1].exceptions))


class _AlertStore:
    def __init__(self, total, failed, created=True):
        self.total = total
        self.failed = failed
        self.created = created
        self.calls = []
        self.alerts = []

    def dashboard_stats(self, tenant_id, window_seconds=None):
        self.calls.append((tenant_id, window_seconds))
        return {"tasks_total": self.total, "tasks_failed": self.failed}

    def create_alert(self, *args):
        self.alerts.append(args)
        return self.created


class AlertManagerTests(unittest.TestCase):
    def test_webhook_notifier_posts_json_and_swallows_failures(self):
        calls = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout):
            calls.append((request, timeout))
            return Response()

        notifier = AlertNotifier("https://alerts.test/hook", timeout=3)
        with patch("evoagent.adapters.observability.urllib.request.urlopen",
                   side_effect=fake_urlopen):
            self.assertTrue(notifier.notify("tenant-a", "key", "critical", "boom"))
        request, timeout = calls[0]
        self.assertEqual("https://alerts.test/hook", request.full_url)
        self.assertEqual(3, timeout)
        self.assertEqual({
            "tenant_id": "tenant-a", "alert_key": "key",
            "severity": "critical", "message": "boom",
        }, json.loads(request.data.decode("utf-8")))

        with patch("evoagent.adapters.observability.urllib.request.urlopen",
                   side_effect=urllib.error.URLError("down")):
            self.assertFalse(notifier.notify("tenant-a", "key", "critical", "boom"))

    def test_alert_manager_notifies_after_persisting(self):
        class Notifier:
            def __init__(self):
                self.calls = []

            def notify(self, *args):
                self.calls.append(args)

        store = _AlertStore(total=20, failed=7)
        notifier = Notifier()
        AlertManager(
            store, failure_rate=0.2, min_samples=10, notifier=notifier,
        ).evaluate("tenant-a")
        self.assertEqual(1, len(store.alerts))
        self.assertEqual(1, len(notifier.calls))
        self.assertEqual("review-failure-rate", notifier.calls[0][1])

    def test_already_open_alert_does_not_notify_again(self):
        class Notifier:
            def __init__(self):
                self.calls = []

            def notify(self, *args):
                self.calls.append(args)

        notifier = Notifier()
        store = _AlertStore(total=20, failed=7, created=False)
        AlertManager(store, notifier=notifier).evaluate("tenant-a")
        self.assertEqual(1, len(store.alerts))
        self.assertEqual([], notifier.calls)

    def test_below_minimum_samples_never_alerts(self):
        store = _AlertStore(total=9, failed=9)
        AlertManager(store, failure_rate=0.2, min_samples=10).evaluate("tenant")
        self.assertEqual([], store.alerts)
        self.assertEqual([("tenant", 900)], store.calls)

    def test_rate_at_or_below_threshold_never_alerts(self):
        for failed in (0, 1, 2):  # 20% is not "greater than" 20%
            store = _AlertStore(total=10, failed=failed)
            AlertManager(store, failure_rate=0.2, min_samples=10).evaluate("tenant")
            self.assertEqual([], store.alerts, msg=failed)

    def test_rate_above_threshold_creates_a_critical_alert(self):
        store = _AlertStore(total=20, failed=7)
        AlertManager(store, failure_rate=0.2, min_samples=10, window_seconds=60) \
            .evaluate("tenant-a")
        self.assertEqual([("tenant-a", 60)], store.calls)
        self.assertEqual("review-failure-rate", store.alerts[0][1])
        self.assertEqual("critical", store.alerts[0][2])
        self.assertIn("35.0%", store.alerts[0][3])
        self.assertIn("60 seconds", store.alerts[0][3])

    def test_window_is_clamped_to_at_least_one_second(self):
        store = _AlertStore(total=0, failed=0)
        AlertManager(store, window_seconds=0).evaluate("tenant")
        self.assertEqual([("tenant", 1)], store.calls)


if __name__ == "__main__":
    unittest.main()
