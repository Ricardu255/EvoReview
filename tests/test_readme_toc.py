"""Keep the README heading hierarchy and table of contents consistent."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _headings(path):
    headings = []
    in_fence = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and line.startswith("## "):
            headings.append(line[3:].strip())
    return headings


def _anchor(title):
    value = title.lower()
    value = re.sub(r"[^\w\s-]", "", value, flags=re.UNICODE)
    return re.sub(r"\s+", "-", value.strip())


class ReadmeTocTests(unittest.TestCase):
    def test_english_toc_matches_level_two_headings(self):
        path = ROOT / "README.md"
        headings = _headings(path)
        self.assertIn("Table of Contents", headings)
        expected = {_anchor(item) for item in headings if item != "Table of Contents"}
        text = path.read_text(encoding="utf-8")
        toc = re.findall(r"^- \[[^\]]+\]\(#([^)]+)\)$", text, flags=re.MULTILINE)
        self.assertEqual(expected, set(toc))

    def test_chinese_toc_matches_level_two_headings(self):
        path = ROOT / "docs" / "README.zh-CN.md"
        headings = _headings(path)
        self.assertIn("目录", headings)
        expected = {_anchor(item) for item in headings if item != "目录"}
        text = path.read_text(encoding="utf-8")
        toc = re.findall(r"^- \[[^\]]+\]\(#([^)]+)\)$", text, flags=re.MULTILINE)
        self.assertEqual(expected, set(toc))


if __name__ == "__main__":
    unittest.main()
