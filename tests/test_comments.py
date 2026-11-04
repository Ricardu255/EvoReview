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

    def audit(self, *args):
        self.events.append(args)


class _Metrics:
    def __init__(self):
        self.counters = []

    def inc(self, name, value=1.0):
        self.counters.append((name, value))


class _Client:
    def __init__(self, inline_error=None):
        self.inline_error = inline_error
        self.comments = []
        self.reviews = []

    def upsert_comment(self, issue_url, body, marker):
        self.comments.append((issue_url, body, marker))

    def post_review(self, repository, number, body, comments):
        if self.inline_error:
            raise self.inline_error
        self.reviews.append((repository, number, body, comments))


def service(inline, client):
    settings = SimpleNamespace(
        report_max_findings=50, inline_review_comments=inline,
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


if __name__ == "__main__":
    unittest.main()
