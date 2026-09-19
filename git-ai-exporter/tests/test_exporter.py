"""Tests for the git-ai attribution exporter.

Run: cd git-ai-exporter/tests && PYTHONPATH=.. python3 -m unittest test_exporter
"""

import subprocess
import sys
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import exporter  # noqa: E402


SAMPLE = {
    "human_additions": 28,
    "ai_additions": 76,
    "ai_accepted": 47,
    "unknown_additions": 2,
    "git_diff_added_lines": 104,
    "git_diff_deleted_lines": 34,
    "tool_model_breakdown": {
        "claude_code/claude-sonnet-5": {"ai_additions": 76, "ai_accepted": 47},
    },
    "authorship_stats": {"total_commits": 7, "commits_with_authorship": 3},
}


class TestCollect(unittest.TestCase):
    def rows(self, data, repo="komodo", forge="git.example.io"):
        out = {}
        exporter.collect(out, repo, forge, data)
        return out

    def test_totals(self):
        p = self.rows(SAMPLE)
        self.assertEqual(p["gitai_ai_additions"][0][1], 76)
        self.assertEqual(p["gitai_ai_accepted"][0][1], 47)
        self.assertEqual(p["gitai_human_additions"][0][1], 28)

    def test_coverage_metrics(self):
        """Without these a dashboard cannot tell 'no AI code' from 'no notes'."""
        p = self.rows(SAMPLE)
        self.assertEqual(p["gitai_commits_total"][0][1], 7)
        self.assertEqual(p["gitai_commits_with_authorship"][0][1], 3)

    def test_tool_model_split(self):
        attrs = dict(self.rows(SAMPLE)["gitai_tool_ai_additions"][0][0])
        self.assertEqual(attrs["tool"], "claude_code")
        self.assertEqual(attrs["model"], "claude-sonnet-5")

    def test_model_without_slash_is_kept(self):
        p = self.rows({"tool_model_breakdown": {"cursor": {"ai_additions": 1}}})
        attrs = dict(p["gitai_tool_ai_additions"][0][0])
        self.assertEqual(attrs["tool"], "cursor")
        self.assertEqual(attrs["model"], "unknown")

    def test_missing_fields_do_not_become_zeros(self):
        """An absent field must be absent, not reported as a real zero."""
        p = self.rows({"ai_additions": 5})
        self.assertNotIn("gitai_human_additions", p)

    def test_repo_and_forge_labels(self):
        attrs = dict(self.rows(SAMPLE)["gitai_ai_additions"][0][0])
        self.assertEqual(attrs["repo"], "komodo")
        self.assertEqual(attrs["forge"], "git.example.io")


class TestOtlpShape(unittest.TestCase):
    def test_gauge_point(self):
        dp = exporter.points("m", [({"repo": "x"}, 3)])["gauge"]["dataPoints"][0]
        self.assertEqual(dp["asInt"], "3")          # OTLP ints are strings
        self.assertEqual(dp["attributes"][0]["key"], "repo")
        self.assertEqual(dp["attributes"][0]["value"]["stringValue"], "x")


class TestFailureIsolation(unittest.TestCase):
    def test_stats_timeout_is_not_fatal(self):
        """Regression: one slow repo used to abort the entire export run."""
        orig = exporter.subprocess.run

        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="git-ai stats", timeout=1)

        exporter.subprocess.run = boom
        try:
            self.assertIsNone(exporter.stats(Path("/nonexistent"), "a..HEAD"))
        finally:
            exporter.subprocess.run = orig

    def test_git_helper_survives_timeout(self):
        orig = exporter.subprocess.run

        def boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)

        exporter.subprocess.run = boom
        try:
            self.assertEqual(exporter.git(Path("/nonexistent"), "rev-parse"), "")
        finally:
            exporter.subprocess.run = orig


class TestNegativeCache(unittest.TestCase):
    """A repo too large to walk inside the timeout fails identically every run.

    Without a cached failure the exporter pays the full timeout for it on every
    tick, so the retry must stop until HEAD moves.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "slowrepo" / ".git").mkdir(parents=True)
        self.cache = root / "cache.json"
        self.calls = []

        self._saved = (exporter.ROOT, exporter.CACHE, exporter.stats,
                       exporter.window_range, exporter.git, exporter.forge_of)
        exporter.ROOT = root
        exporter.CACHE = self.cache
        exporter.window_range = lambda repo: "base..HEAD"
        exporter.git = lambda repo, *a: "deadbeef"
        exporter.forge_of = lambda repo: "example.com"

        def failing_stats(repo, rng):
            self.calls.append(repo)
            return None

        exporter.stats = failing_stats

    def run_once(self):
        """Invoke a dry run, swallowing the payload it prints to stdout."""
        argv = sys.argv
        sys.argv = ["exporter.py", "--dry-run"]
        try:
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(io.StringIO()):
                exporter.main()
        finally:
            sys.argv = argv

    def tearDown(self):
        (exporter.ROOT, exporter.CACHE, exporter.stats,
         exporter.window_range, exporter.git, exporter.forge_of) = self._saved
        self.tmp.cleanup()

    def test_failing_repo_is_not_retried(self):
        self.run_once()
        self.assertEqual(len(self.calls), 1, "first run should attempt the repo")
        self.run_once()
        self.assertEqual(len(self.calls), 1,
                         "second run retried a known-failing repo")

    def test_failure_is_retried_when_head_moves(self):
        self.run_once()
        exporter.git = lambda repo, *a: "cafebabe"   # new HEAD
        self.run_once()
        self.assertEqual(len(self.calls), 2,
                         "a moved HEAD must invalidate the cached failure")


if __name__ == "__main__":
    unittest.main()
