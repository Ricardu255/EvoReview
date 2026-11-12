"""Contract checks for the public OpenAPI document and key responses."""
import inspect
import json
import re
import unittest

from evoagent import api as api_module
from evoagent.openapi import build_openapi

from test_api_routes import FakeService, Handler, settings


# Browser-flow endpoints (302 redirect / GitHub callback with one-time state)
# and dashboard static assets. They are intentionally excluded from the
# Bearer-token REST contract.
BROWSER_ONLY_ROUTES = frozenset({"/github/install", "/github/setup"})
STATIC_ASSET_PREFIX = "/assets/"


def _non_rest(path):
    return path in BROWSER_ONLY_ROUTES or path.startswith(STATIC_ASSET_PREFIX)


def _implemented_routes():
    """Collect every path the router can answer, from three sources.

    Sources: module-level regex routes, module-level string path constants,
    and ``path == "..."`` literals inside the handler. Keeping this derived
    (not hand-copied) is what makes the sync check trustworthy.
    """
    patterns = []
    literals = set()
    for value in vars(api_module).values():
        if isinstance(value, re.Pattern) and value.pattern.startswith("^/"):
            patterns.append(value.pattern)
        elif isinstance(value, str) and re.fullmatch(
            r"/(?:v1|api|webhooks|health|metrics|openapi|github)[a-z0-9/_.-]*", value,
        ):
            literals.add(value)
    source = inspect.getsource(api_module)
    literals.update(re.findall(r'path == "(/[a-z0-9/_.-]+)"', source))
    return patterns, literals


def _sample_for(pattern):
    """Build a request path that the given route regex accepts."""
    def repl(match):
        text = match.group(0)
        if "0-9a-f" in text:
            return "0a1b2c3d"
        if "\\d" in text:
            return "42"
        return "sample"
    return re.sub(r"\([^)]+\)", repl, pattern).lstrip("^").rstrip("$")


def _route_template(pattern):
    """Turn a route regex into an OpenAPI-style path template."""
    return re.sub(r"\([^)]+\)", "{x}", pattern.lstrip("^").rstrip("$"))


def _path_matcher(template):
    """Turn an OpenAPI path template into a regex over concrete paths."""
    return re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", template) + "$")


def _resolve(spec, schema):
    while isinstance(schema, dict) and "$ref" in schema:
        name = schema["$ref"].split("/")[-1]
        schema = spec["components"]["schemas"][name]
    return schema


def assert_matches(test, value, schema, spec, path="$"):
    schema = _resolve(spec, schema)
    if not isinstance(schema, dict):
        return
    expected = schema.get("type")
    if expected == "object":
        test.assertIsInstance(value, dict, path)
        for name in schema.get("required", []):
            test.assertIn(name, value, "%s.%s" % (path, name))
        properties = schema.get("properties") or {}
        for name, child in properties.items():
            if name in value:
                assert_matches(test, value[name], child, spec, "%s.%s" % (path, name))
    elif expected == "array":
        test.assertIsInstance(value, list, path)
        child = schema.get("items")
        if isinstance(child, dict):
            for index, item in enumerate(value):
                assert_matches(test, item, child, spec, "%s[%d]" % (path, index))
    elif expected == "string":
        test.assertIsInstance(value, str, path)
    elif expected == "integer":
        test.assertIsInstance(value, int, path)
        test.assertNotIsInstance(value, bool, path)
    elif expected == "number":
        test.assertIsInstance(value, (int, float), path)
    elif expected == "boolean":
        test.assertIsInstance(value, bool, path)


class OpenApiSpecTests(unittest.TestCase):
    def setUp(self):
        self.spec = build_openapi()

    def test_document_metadata_and_security_scheme(self):
        self.assertEqual("3.0.3", self.spec["openapi"])
        self.assertTrue(self.spec["info"]["title"])
        self.assertEqual(
            "bearer", self.spec["components"]["securitySchemes"]["bearerAuth"]["scheme"],
        )
        self.assertTrue(self.spec["paths"])

    def test_all_refs_resolve_and_document_is_json_serializable(self):
        refs = []

        def collect(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    refs.append(value["$ref"])
                for child in value.values():
                    collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)

        collect(self.spec)
        schemas = self.spec["components"]["schemas"]
        for ref in refs:
            self.assertIn(ref.split("/")[-1], schemas, ref)
        json.dumps(self.spec)

    def test_every_operation_is_well_formed(self):
        operation_ids = []
        methods = {"get", "post", "put", "patch", "delete"}
        for path, item in self.spec["paths"].items():
            for method, operation in item.items():
                if method not in methods:
                    continue
                with self.subTest(path=path, method=method):
                    self.assertTrue(operation.get("operationId"))
                    operation_ids.append(operation["operationId"])
                    self.assertTrue(operation.get("responses"))
                    self.assertIn("tags", operation)
        self.assertEqual(len(operation_ids), len(set(operation_ids)))

    def test_every_api_route_is_documented_in_openapi(self):
        """Adding a route without documenting it must fail this test."""
        patterns, literals = _implemented_routes()
        documented = set(self.spec["paths"]) | BROWSER_ONLY_ROUTES
        undocumented = []
        for pattern in patterns:
            template = _route_template(pattern)
            covered = template in literals or any(
                re.fullmatch(re.sub(r"\{[^}]+\}", "[^/]+", op), template)
                for op in documented
            )
            if not covered:
                undocumented.append(template)
        for literal in sorted(literals):
            if literal not in documented and not _non_rest(literal):
                undocumented.append(literal)
        self.assertEqual([], undocumented,
                         "routes implemented but missing from OpenAPI paths")

    def test_every_openapi_path_is_implemented_by_the_router(self):
        """A typo in the hand-maintained spec must fail this test."""
        patterns, literals = _implemented_routes()
        ghosts = []
        for path in self.spec["paths"]:
            if path in literals:
                continue
            matcher = _path_matcher(path)
            if not any(matcher.match(_sample_for(p)) for p in patterns):
                ghosts.append(path)
        self.assertEqual([], ghosts,
                         "OpenAPI paths with no matching route in api.py")

    def test_expected_public_paths_exist(self):
        expected = {
            "/openapi.json", "/health", "/health/detail", "/metrics",
            "/v1/auth/login", "/v1/auth/token", "/v1/reviews",
            "/webhooks/github", "/v1/tasks/{task_id}",
            "/v1/tasks/{task_id}/report", "/v1/tasks/{task_id}/sarif",
            "/v1/tasks/{task_id}/feedback",
            "/v1/tasks/{task_id}/fix", "/v1/tasks/{task_id}/cancel",
            "/v1/tasks/{task_id}/resume", "/v1/tasks/{task_id}/comment",
            "/v1/repository-grants", "/v1/users", "/v1/tenant-quota", "/v1/tenants",
            "/v1/users/{username}/password", "/v1/users/{username}/active",
            "/v1/platform/users/{username}/platform-admin",
            "/v1/skills/reload", "/v1/skills/{name}/versions",
            "/v1/skills/{name}/versions/{version}/activate",
            "/v1/evolution/auto", "/v1/evolution/propose",
            "/v1/evolution/status", "/v1/evolution/runs",
            "/v1/skill-evolution/auto", "/v1/skill-evolution/propose",
            "/v1/skill-evolution/status", "/v1/skill-evolution/runs",
            "/v1/skill-evolution/{name}/versions",
            "/v1/skill-evolution/{name}/versions/{version}/activate",
            "/v1/evaluation/cases", "/api/dashboard", "/api/tasks",
            "/api/skills", "/api/failures", "/api/audit", "/api/alerts",
            "/api/alerts/{id}/resolve", "/api/deployments/llm-review",
            "/v1/deployments/llm-review", "/api/queue/dead-letters",
            "/v1/queue/dead-letters/replay",
        }
        self.assertEqual(expected, set(self.spec["paths"]))

    def test_health_dashboard_and_error_responses_match_the_contract(self):
        service = FakeService()
        config = settings()

        def call(path):
            handler = Handler(service, config, path)
            handler.do_GET()
            return handler

        health = call("/health")
        assert_matches(
            self, health.body,
            self.spec["paths"]["/health"]["get"]["responses"]["200"]
            ["content"]["application/json"]["schema"],
            self.spec, "health",
        )

        dashboard = call("/api/dashboard")
        assert_matches(
            self, dashboard.body,
            self.spec["paths"]["/api/dashboard"]["get"]["responses"]["200"]
            ["content"]["application/json"]["schema"],
            self.spec, "dashboard",
        )

        missing = call("/v1/tasks/abcdef01")
        self.assertEqual(404, missing.status)
        assert_matches(
            self, missing.body,
            self.spec["paths"]["/v1/tasks/{task_id}"]["get"]["responses"]["404"]
            ["content"]["application/json"]["schema"],
            self.spec, "error",
        )

    def test_openapi_route_is_served_without_auth(self):
        service = FakeService()
        handler = Handler(service, settings(), "/openapi.json")
        handler.do_GET()
        self.assertEqual(200, handler.status)
        self.assertEqual(self.spec["openapi"], handler.body["openapi"])


if __name__ == "__main__":
    unittest.main()
