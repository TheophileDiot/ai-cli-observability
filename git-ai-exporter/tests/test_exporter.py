"""Tests for the git-ai attribution exporter.

Run: cd git-ai-exporter/tests && PYTHONPATH=.. python3 -m unittest test_exporter
"""

import subprocess
import sys
import contextlib
import io
import json
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


# What `git-ai stats <a>..<b>` really returns: line counts nested under
# range_stats, not at the top level as in the single-commit payload.
RANGE_SAMPLE = {
    "authorship_stats": {"total_commits": 3, "commits_with_authorship": 2,
                         "commits_without_authorship": ["abc", "def"]},
    "range_stats": {
        "human_additions": 12,
        "unknown_additions": 4,
        "ai_additions": 31,
        "ai_accepted": 20,
        "git_diff_added_lines": 47,
        "git_diff_deleted_lines": 9,
        # Verbatim key shape from real `git-ai stats` output.
        "tool_model_breakdown": {"claude::claude-opus-5":
                                 {"ai_additions": 31, "ai_accepted": 20}},
    },
}


class TestRangeSchema(unittest.TestCase):
    """Regression: range payloads were reduced to commit counts only.

    The exporter always queries a range, so reading the single-commit schema
    silently dropped every line-level metric.
    """

    def rows(self, data):
        out = {}
        exporter.collect(out, "komodo", "example.com", data)
        return out

    def test_range_line_counts_are_read(self):
        p = self.rows(RANGE_SAMPLE)
        self.assertEqual(p["gitai_ai_additions"][0][1], 31)
        self.assertEqual(p["gitai_ai_accepted"][0][1], 20)
        self.assertEqual(p["gitai_human_additions"][0][1], 12)
        self.assertEqual(p["gitai_unknown_additions"][0][1], 4)

    def test_range_tool_breakdown_is_read(self):
        """Real payloads separate tool and model with '::', not '/'.

        Fixtures copied from the upstream README used '/', so this passed while
        the exporter mislabelled every real model as "unknown".
        """
        attrs = dict(self.rows(RANGE_SAMPLE)["gitai_tool_ai_additions"][0][0])
        self.assertEqual(attrs["tool"], "claude")
        self.assertEqual(attrs["model"], "claude-opus-5")

    def test_readme_slash_form_still_parses(self):
        p = self.rows({"range_stats": {"tool_model_breakdown":
                                       {"claude_code/claude-sonnet-5": {"ai_additions": 1}}}})
        attrs = dict(p["gitai_tool_ai_additions"][0][0])
        self.assertEqual(attrs["tool"], "claude_code")
        self.assertEqual(attrs["model"], "claude-sonnet-5")

    def test_range_still_reports_coverage(self):
        p = self.rows(RANGE_SAMPLE)
        self.assertEqual(p["gitai_commits_total"][0][1], 3)
        self.assertEqual(p["gitai_commits_with_authorship"][0][1], 2)

    def test_single_commit_schema_still_works(self):
        p = self.rows(SAMPLE)
        self.assertEqual(p["gitai_ai_additions"][0][1], 76)


class TestWindowRange(unittest.TestCase):
    """`A..B` excludes A, so the range base must never be the root commit."""

    def setUp(self):
        self._git = exporter.git

    def tearDown(self):
        exporter.git = self._git

    def test_uses_pre_window_commit_when_one_exists(self):
        exporter.git = lambda repo, *a: "oldsha" if "rev-list" in a else "newsha"
        self.assertEqual(exporter.window_range(Path("/r")), "oldsha..HEAD")

    def test_falls_back_to_empty_tree_not_root(self):
        """History entirely inside the window: the initial commit must count.

        Using the root commit as base drops it, and an initial import is
        usually the largest commit in the repo.
        """
        def fake(repo, *a):
            if "rev-list" in a:
                return ""          # no commit older than the window
            return "somesha"       # but the repo does have commits
        exporter.git = fake
        self.assertEqual(exporter.window_range(Path("/r")),
                         f"{exporter.EMPTY_TREE}..HEAD")

    def test_empty_repo_is_skipped(self):
        exporter.git = lambda repo, *a: ""
        self.assertIsNone(exporter.window_range(Path("/r")))


class TestMalformedPayloads(unittest.TestCase):
    """Valid JSON in an unexpected shape must skip the repo, not kill the run."""

    def parse(self, stdout):
        orig = exporter.subprocess.run
        exporter.subprocess.run = lambda *a, **k: type(
            "R", (), {"returncode": 0, "stdout": stdout})()
        try:
            return exporter.stats(Path("/r"), "a..HEAD")
        finally:
            exporter.subprocess.run = orig

    def test_list_payload_rejected(self):
        self.assertIsNone(self.parse("[]"))

    def test_scalar_payload_rejected(self):
        self.assertIsNone(self.parse("42"))

    def test_dict_payload_accepted(self):
        self.assertEqual(self.parse('{"a":1}'), {"a": 1})

    def test_null_range_stats_does_not_raise(self):
        out = {}
        exporter.collect(out, "r", "f", {"range_stats": None, "ai_additions": 3})
        self.assertEqual(out["gitai_ai_additions"][0][1], 3)


class TestStaleData(unittest.TestCase):
    """A transient failure must not erase the last good numbers."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "repo" / ".git").mkdir(parents=True)
        self._saved = (exporter.ROOT, exporter.CACHE, exporter.stats,
                       exporter.window_range, exporter.git, exporter.forge_of)
        exporter.ROOT = root
        exporter.CACHE = root / "cache.json"
        exporter.window_range = lambda repo: "base..HEAD"
        exporter.git = lambda repo, *a: "sha1"
        exporter.forge_of = lambda repo: "example.com"

    def tearDown(self):
        (exporter.ROOT, exporter.CACHE, exporter.stats,
         exporter.window_range, exporter.git, exporter.forge_of) = self._saved
        self.tmp.cleanup()

    def run_once(self):
        argv = sys.argv
        sys.argv = ["exporter.py", "--dry-run"]
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), \
                 contextlib.redirect_stderr(io.StringIO()):
                exporter.main()
        finally:
            sys.argv = argv
        out = buf.getvalue().strip()
        return json.loads(out) if out else None   # nothing to export

    @staticmethod
    def value(payload, name):
        for m in payload["resourceMetrics"][0]["scopeMetrics"][0]["metrics"]:
            if m["name"] == name:
                return int(m["gauge"]["dataPoints"][0]["asInt"])
        return None

    def test_previous_values_survive_a_transient_failure(self):
        exporter.stats = lambda repo, rng: {"range_stats": {"ai_additions": 42}}
        first = self.run_once()
        self.assertEqual(self.value(first, "gitai_ai_additions"), 42)
        self.assertEqual(self.value(first, "gitai_stats_stale"), 0)

        # Same repo, new HEAD, and the refresh now fails.
        exporter.git = lambda repo, *a: "sha2"
        exporter.stats = lambda repo, rng: None
        second = self.run_once()
        self.assertEqual(self.value(second, "gitai_ai_additions"), 42,
                         "a transient failure erased the last good values")
        self.assertEqual(self.value(second, "gitai_stats_stale"), 1,
                         "stale data must be flagged")

    def test_first_ever_failure_is_marked_failed(self):
        exporter.stats = lambda repo, rng: None
        self.run_once()
        cache = json.loads(exporter.CACHE.read_text())
        self.assertTrue(list(cache.values())[0]["failed"])


class TestCacheWrite(unittest.TestCase):
    def test_temp_file_is_process_unique(self):
        """A shared .tmp lets concurrent runs rename each other's partials."""
        with tempfile.TemporaryDirectory() as d:
            orig = exporter.CACHE
            exporter.CACHE = Path(d) / "c.json"
            try:
                exporter.save_cache({"a": 1})
                self.assertEqual(json.loads(exporter.CACHE.read_text()), {"a": 1})
            finally:
                exporter.CACHE = orig
        import inspect
        self.assertIn("getpid", inspect.getsource(exporter.save_cache))


class TestOtlpShape(unittest.TestCase):
    def test_no_unit_field(self):
        """unit "1" makes the Prometheus translation append `_ratio`."""
        self.assertNotIn("unit", exporter.points("m", [({"repo": "x"}, 1)]))

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
