"""The queued diff must come from repository + number, not a webhook URL."""
import unittest

from evoagent.app.diff_fetch import fetch_initial_diff
from evoagent.errors import PermanentTaskError


class FakeClient:
    def __init__(self, has_pull_diff=True):
        self.has_pull_diff = has_pull_diff
        self.access = []
        self.pull = None
        self.diff_urls = []

    def ensure_repository_access(self, repository):
        self.access.append(repository)

    def __getattr__(self, name):
        if name == "fetch_pull_diff" and not self.has_pull_diff:
            raise AttributeError(name)
        if name == "fetch_pull_diff":
            def fetch(repository, number):
                self.pull = (repository, number)
                return "derived-diff"
            return fetch
        if name == "fetch_diff":
            def fetch(url):
                self.diff_urls.append(url)
                return "url-diff"
            return fetch
        raise AttributeError(name)


class DiffFetchTests(unittest.TestCase):
    def test_pull_request_ignores_the_webhook_diff_url(self):
        client = FakeClient()
        diff = fetch_initial_diff(client, {
            "repository": "org/repo", "pull_request": 12,
            "diff_url": "https://github.com/other/repo/pull/9.diff",
        })
        self.assertEqual("derived-diff", diff)
        self.assertEqual(("org/repo", 12), client.pull)
        self.assertEqual([], client.diff_urls)
        self.assertEqual(["org/repo"], client.access)

    def test_legacy_client_can_still_use_a_diff_url(self):
        client = FakeClient(has_pull_diff=False)
        diff = fetch_initial_diff(client, {
            "repository": "org/repo", "pull_request": 12,
            "diff_url": "https://github.com/org/repo/pull/12.diff",
        })
        self.assertEqual("url-diff", diff)
        self.assertEqual(
            ["https://github.com/org/repo/pull/12.diff"], client.diff_urls,
        )

    def test_missing_change_target_is_permanent(self):
        with self.assertRaises(PermanentTaskError):
            fetch_initial_diff(FakeClient(), {"repository": "org/repo"})


if __name__ == "__main__":
    unittest.main()
