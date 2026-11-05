"""Contract checks for the public OpenAPI document and key responses."""
import json
import unittest

from evoagent.openapi import build_openapi

from test_api_routes import FakeService, Handler, settings


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
