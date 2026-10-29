import unittest

from evoagent.domain.diff_parser import parse_unified_diff


DIFF = """diff --git a/app.py b/app.py
index 123..456 100644
--- a/app.py
+++ b/app.py
@@ -2,3 +2,4 @@ def run():
 keep = True
-old = 1
+new = 2
+eval(user_input)
 tail = 3
"""


class DiffParserTests(unittest.TestCase):
    def test_parses_added_line_numbers(self):
        parsed = parse_unified_diff(DIFF)
        self.assertEqual(["app.py"], parsed.files)
        self.assertEqual([(3, "new = 2"), (4, "eval(user_input)")], [(x.line, x.content) for x in parsed.added_lines])

    def test_added_line_starting_with_plus_plus_is_not_a_file_header(self):
        diff = """--- a/tool.sh
+++ b/tool.sh
@@ -1,2 +1,4 @@
 line1
+echo hello
+++ --flag value
+echo done
 tail
"""
        parsed = parse_unified_diff(diff)
        self.assertEqual(["tool.sh"], parsed.files)
        self.assertEqual(
            [(2, "echo hello"), (3, "++ --flag value"), (4, "echo done")],
            [(x.line, x.content) for x in parsed.added_lines],
        )

    def test_deleted_line_starting_with_minus_minus_is_not_a_file_header(self):
        diff = """--- a/tool.sh
+++ b/tool.sh
@@ -1,3 +1,2 @@
 line1
--- --flag value
 tail
"""
        parsed = parse_unified_diff(diff)
        self.assertEqual(["tool.sh"], parsed.files)
        self.assertEqual([], parsed.added_lines)


if __name__ == "__main__":
    unittest.main()

