"""Static checks for the console's task-action controls.

There is no browser harness in this repository, so these tests pin the
element IDs and API routes that the handlers rely on.
"""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "evoagent" / "web"


class WebAssetTests(unittest.TestCase):
    def test_task_action_controls_exist(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        for element_id in (
            "cancel-task", "resume-task", "redeliver-comment", "create-fix",
        ):
            with self.subTest(element_id=element_id):
                self.assertIn('id="%s"' % element_id, html)

    def test_task_action_handlers_use_the_expected_routes(self):
        source = (WEB / "app.js").read_text(encoding="utf-8")
        for route in ("/cancel", "/resume", "/comment"):
            with self.subTest(route=route):
                self.assertIn(route, source)
        self.assertIn('id="cancel-task"', (WEB / "index.html").read_text(encoding="utf-8"))
        self.assertIn("$(\"#resume-task\").addEventListener", source)
        self.assertIn("$(\"#redeliver-comment\").addEventListener", source)

    def test_task_filters_are_wired(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        source = (WEB / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="task-state-filter"', html)
        self.assertIn('id="task-repository-filter"', html)
        self.assertIn('params.set("state", state)', source)
        self.assertIn('params.set("repository", repository)', source)

    def test_prompt_versions_are_rendered(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        source = (WEB / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="prompt-versions"', html)
        self.assertIn("/v1/skills/llm-review/versions", source)
        self.assertIn('$("#prompt-versions").textContent', source)

    def test_admin_view_is_wired(self):
        html = (WEB / "index.html").read_text(encoding="utf-8")
        source = (WEB / "app.js").read_text(encoding="utf-8")
        for element_id in (
            "view-admin", "admin-users", "admin-grants", "admin-alerts",
            "admin-audit", "user-form", "grant-form",
        ):
            with self.subTest(element_id=element_id):
                self.assertIn('id="%s"' % element_id, html)
        for route in (
            "/v1/users", "/v1/repository-grants", "/api/alerts", "/api/audit",
        ):
            with self.subTest(route=route):
                self.assertIn(route, source)


if __name__ == "__main__":
    unittest.main()
