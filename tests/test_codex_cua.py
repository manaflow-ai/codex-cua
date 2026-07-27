"""Unit tests for codex-cua tree parsing. Run: python3 -m unittest discover tests"""

import importlib.machinery
import importlib.util
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_loader(
    "codex_cua",
    importlib.machinery.SourceFileLoader("codex_cua", str(Path(__file__).resolve().parents[1] / "bin" / "codex-cua")),
)
cua = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cua)

# Two renderings of the same Calculator window: unfocused (terse) and focused
# (descriptions present). Element numbering differs between the snapshots, which
# is why --match resolves against a fresh tree instead of a remembered index.
UNFOCUSED = """Computer Use state
<app_state>
App=com.apple.calculator (pid 21639)
0 unknown Secondary Actions: Raise
\t1 split group main, SidebarNavigationSplitView
\t\t21 button Seven
\t\t22 button Eight
\t\t23 button Nine
"""

FOCUSED = """Computer Use state
0 standard window Calculator, ID: main
\t\t\t21 button Description: 7, ID: Seven
\t\t\t22 button Description: 8, ID: Eight
\t\t\t23 button Description: 9, ID: Nine
"""


class TreeMatchesTest(unittest.TestCase):
    def test_returns_index_and_line(self):
        self.assertEqual(cua.tree_matches(UNFOCUSED, "Nine"), [("23", "23 button Nine")])

    def test_matches_both_renderings(self):
        for tree in (UNFOCUSED, FOCUSED):
            self.assertEqual([i for i, _ in cua.tree_matches(tree, "Nine")], ["23"])

    def test_button_prefix_only_matches_terse_rendering(self):
        self.assertTrue(cua.tree_matches(UNFOCUSED, "button Nine"))
        self.assertFalse(cua.tree_matches(FOCUSED, "button Nine"))

    def test_skips_lines_without_an_element_index(self):
        self.assertEqual(cua.tree_matches(UNFOCUSED, "calculator"), [])

    def test_case_sensitivity_is_opt_in(self):
        self.assertTrue(cua.tree_matches(UNFOCUSED, "nine"))
        self.assertFalse(cua.tree_matches(UNFOCUSED, "nine", case_sensitive=True))

    def test_reports_every_hit_so_ambiguity_can_be_detected(self):
        self.assertEqual(len(cua.tree_matches(UNFOCUSED, "button")), 3)

    def test_bad_pattern_fails_loudly(self):
        with self.assertRaises(SystemExit):
            cua.tree_matches(UNFOCUSED, "button (")


class ParsePointTest(unittest.TestCase):
    def test_parses_pairs(self):
        self.assertEqual(cua.parse_point("12, 34.5", "--from"), (12.0, 34.5))

    def test_rejects_wrong_arity(self):
        with self.assertRaises(SystemExit):
            cua.parse_point("12", "--from")

    def test_rejects_non_numeric(self):
        with self.assertRaises(SystemExit):
            cua.parse_point("a,b", "--to")


if __name__ == "__main__":
    unittest.main()
