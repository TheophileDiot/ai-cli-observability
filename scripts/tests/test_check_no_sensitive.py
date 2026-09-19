"""Tests for the public-repo sensitive-data guard.

A security guard that stops firing fails silently, so each rule gets a case.
Run: cd scripts/tests && PYTHONPATH=.. python3 -m unittest test_check_no_sensitive
"""

import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import check_no_sensitive as guard  # noqa: E402


def check(text, rules=None):
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
        fh.write(text)
        path = fh.name
    try:
        return guard.scan(path, rules if rules is not None else guard.load_rules())
    finally:
        Path(path).unlink()


class TestBuiltinRules(unittest.TestCase):
    def assertBlocked(self, text):
        self.assertTrue(check(text), f"should have been blocked: {text!r}")

    def assertAllowed(self, text):
        self.assertFalse(check(text), f"should have been allowed: {text!r}")

    def test_cgnat_blocked(self):
        self.assertBlocked("collector at 100.64.0.13")  # allow-sensitive

    def test_rfc1918_blocked(self):
        self.assertBlocked("traefik pinned to 10.40.40.250")  # allow-sensitive
        self.assertBlocked("host 192.168.1.10")  # allow-sensitive

    def test_bearer_token_blocked(self):
        self.assertBlocked("Authorization=Bearer 25beefaaaaaaaaaaaaaaaaaaaa")  # allow-sensitive

    def test_real_email_blocked(self):
        self.assertBlocked("contact: real.person@somecorp.io")  # allow-sensitive

    def test_public_ip_allowed(self):
        """Only private ranges are secrets; 8.8.8.8 is not."""
        self.assertAllowed("resolver 8.8.8.8")

    def test_clone_urls_allowed(self):
        self.assertAllowed("git clone git@github.com:org/repo.git")
        self.assertAllowed("git clone git@gitlab.com:group/proj.git")

    def test_example_domains_allowed(self):
        self.assertAllowed("contact: someone@example.com")
        self.assertAllowed("endpoint: https://otel.example.com")

    def test_pragma_exempts_line(self):
        self.assertAllowed("Bearer abcdefghijklmnopqrstuvwx   allow-sensitive")


class TestFailClosed(unittest.TestCase):
    """A guard that cannot load its rules must block, not wave the commit through."""

    def with_patterns(self, text):
        original = guard.PATTERN_FILE
        with tempfile.TemporaryDirectory() as d:
            pf = Path(d) / ".sensitive-patterns"
            pf.write_text(text)
            guard.PATTERN_FILE = pf
            try:
                return guard.main([__file__])
            finally:
                guard.PATTERN_FILE = original

    def test_bad_regex_blocks(self):
        self.assertEqual(self.with_patterns("broken=[unclosed\n"), 2)

    def test_missing_equals_blocks(self):
        self.assertEqual(self.with_patterns("no-separator-here\n"), 2)

    def test_valid_rules_do_not_block(self):
        self.assertEqual(self.with_patterns("# just a comment\nok=\\bzzz\\b\n"), 0)


class TestLocalPatternFile(unittest.TestCase):
    def test_local_patterns_are_applied(self):
        rules = [("private hostname", __import__("re").compile(r"\bsecret\.example\.net\b"))]
        self.assertTrue(check("host: secret.example.net", rules))

    @unittest.skipUnless(
        guard.PATTERN_FILE.exists(),
        "no .sensitive-patterns in this checkout: the deployment rules that "
        "would prove this claim are not present. The pre-commit hook is the "
        "real enforcement; it runs with the operator's rules loaded.")
    def test_guard_ships_without_private_literals(self):
        """The committed script must not contain the strings it exists to hide.

        Asserted by running the guard over its own source, so this test needs
        no private literals of its own — an earlier version hardcoded them and
        would itself have leaked on commit.

        This can only hold where the deployment rules exist, hence the skip:
        in a clean checkout the builtin rules alone cannot see a private
        hostname, so a pass would mean nothing.
        """
        self.assertFalse(guard.scan(guard.__file__, guard.load_rules()))


if __name__ == "__main__":
    unittest.main()
