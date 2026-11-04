"""Tests for summary and inline GitHub review-comment delivery."""
from types import SimpleNamespace
import unittest

from evoagent.app.comments import (
    ReviewCommentService, review_comment_marker,
)
from evoagent.domain.report import to_inline_markdown


REPORT = {
    "repository": "org/repo", "pull_request": 5, "summary": "summary",
    "risk": "high", "reviewer": "test",
    "findings": [{
        "rule_id": "SEC-EVAL", "severity": "critical", "title": "Dynamic execution",
        "explanation": "Input reaches eval.", "path": "app.py", "line": 7,
        "evidence": "eval(user_input)", "fix": "Use a parser.", "test": "Add a test.",
    }],
}


class _Store:
    def __init__(self):
        self.events = []
        self.updates = []

    def audit(self, *args):
        self.events.append(args)

    def update_task_input(self, task_id, values):
        self.updates.append((task_id, values))


class _Metrics:
    def __init__(self):
        self.counters = []

    def inc(self, name, value=1.0):
        self.counters.append((name, value))


class _Client:
    def __init__(self, inline_error=None, check_error=None):
        self.inline_error = inline_error
        self.check_error = check_error
        self.comments = []
        self.reviews = []
        self.checks = []
        self.updated_checks = []

    def upsert_comment(self, issue_url, body, marker):
        self.comments.append((issue_url, body, marker))

    def post_review(self, repository, number, body, comments):
        if self.inline_error:
            raise self.inline_error
        self.reviews.append((repository, number, body, comments))

    def create_check_run(self, repository, head_sha, conclusion, title, summary):
        if self.check_error:
            raise self.check_error
        self.checks.append((repository, head_sha, conclusion, title, summary))
        return {"id": 77}

    def update_check_run(self, repository, check_run_id, conclusion, title, summary):
        if self.check_error:
            raise self.check_error
        self.updated_checks.append(
            (repository, check_run_id, conclusion, title, summary),
        )
        return {"id": check_run_id}


def service(inline, client, checks=False):
    settings = SimpleNamespace(
        report_max_findings=50, inline_review_comments=inline,
        auto_post_checks=checks,
    )
    store, metrics = _Store(), _Metrics()
    return ReviewCommentService(settings, store, metrics, lambda _id: client), store, metrics


class InlineCommentTests(unittest.TestCase):
    def test_inline_comments_are_opted_in_and_target_the_changed_line(self):
        client = _Client()
        comments, store, metrics = service(True, client)
        self.assertTrue(comments.deliver(
            "https://github.test/issue", None, "task", "tenant", REPORT,
            "org/repo", 5,
        ))
        self.assertEqual(1, len(client.comments))
        self.assertEqual(1, len(client.reviews))
        repository, number, body, inline = client.reviews[0]
        self.assertEqual(("org/repo", 5), (repository, number))
        self.assertIn("1 actionable", body)
        self.assertEqual("app.py", inline[0]["path"])
        self.assertEqual(7, inline[0]["line"])
        self.assertEqual("RIGHT", inline[0]["side"])
        self.assertIn("Dynamic execution", inline[0]["body"])
        self.assertIn(("inline_comments_delivered_total", 1), metrics.counters)

    def test_inline_comments_are_disabled_by_default(self):
        client = _Client()
        comments, _store, _metrics = service(False, client)
        self.assertTrue(comments.deliver(
            "https://github.test/issue", None, "task", "tenant", REPORT,
            "org/repo", 5,
        ))
        self.assertEqual([], client.reviews)

    def test_inline_failure_does_not_fail_the_summary_comment(self):
        client = _Client(inline_error=RuntimeError("reviews unavailable"))
        comments, store, metrics = service(True, client)
        self.assertTrue(comments.deliver(
            "https://github.test/issue", None, "task", "tenant", REPORT,
            "org/repo", 5,
        ))
        self.assertEqual(1, len(client.comments))
        self.assertTrue(any(
            event[2] == "comment.inline_failed" for event in store.events
        ))
        self.assertIn(("inline_comments_failed_total", 1), metrics.counters)

    def test_inline_markdown_redacts_hardcoded_secret_evidence(self):
        body = to_inline_markdown({
            "rule_id": "SEC-HARDCODED-SECRET", "severity": "high",
            "title": "Secret", "explanation": "Hardcoded.",
            "path": "config.py", "line": 1,
            "evidence": "password = 'super-secret'",
            "fix": "Use env.", "test": "Remove secret.",
        })
        self.assertNotIn("super-secret", body)
        self.assertIn("***", body)

    def test_marker_is_stable_for_one_pr(self):
        first = review_comment_marker("tenant", "https://github.test/issue", "a")
        second = review_comment_marker("tenant", "https://github.test/issue/", "b")
        self.assertEqual(first, second)

    def test_check_run_is_opted_in_and_maps_high_risk_to_failure(self):
        client = _Client()
        comments, _store, metrics = service(False, client, checks=True)
        self.assertTrue(comments.deliver(
            "https://github.test/issue", None, "task", "tenant", REPORT,
            "org/repo", 5, "head-sha",
        ))
        self.assertEqual(1, len(client.checks))
        repository, head_sha, conclusion, title, summary = client.checks[0]
        self.assertEqual(("org/repo", "head-sha"), (repository, head_sha))
        self.assertEqual("failure", conclusion)
        self.assertIn("1 finding", title)
        self.assertIn(("check_runs_delivered_total", 1), metrics.counters)

    def test_check_run_failure_does_not_fail_the_summary_comment(self):
        client = _Client(check_error=RuntimeError("checks unavailable"))
        comments, store, metrics = service(False, client, checks=True)
        self.assertTrue(comments.deliver(
            "https://github.test/issue", None, "task", "tenant", REPORT,
            "org/repo", 5, "head-sha",
        ))
        self.assertEqual(1, len(client.comments))
        self.assertTrue(any(
            event[2] == "check_run.failed" for event in store.events
        ))
        self.assertIn(("check_runs_failed_total", 1), metrics.counters)

    def test_redelivery_does_not_duplicate_inline_or_check_run(self):
        client = _Client()
        comments, _store, _metrics = service(True, client, checks=True)
        task_input = {}
        for _ in range(2):
            self.assertTrue(comments.deliver(
                "https://github.test/issue", None, "task", "tenant", REPORT,
                "org/repo", 5, "head-sha", task_input,
            ))
        self.assertEqual(1, len(client.reviews))
        self.assertEqual(1, len(client.checks))
        self.assertEqual(1, len(client.updated_checks))
        self.assertEqual(77, task_input["check_run_id"])

    def test_check_run_is_disabled_without_head_sha(self):
        client = _Client()
        comments, _store, _metrics = service(False, client, checks=True)
        self.assertTrue(comments.deliver(
            "https://github.test/issue", None, "task", "tenant", REPORT,
            "org/repo", 5,
        ))
        self.assertEqual([], client.checks)


if __name__ == "__main__":
    unittest.main()
