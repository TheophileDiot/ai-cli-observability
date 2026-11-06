"""Release boundaries: reject mismatched versions and incomplete release notes."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import release


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "pricing-exporter").mkdir()
        (self.root / "VERSION").write_text("0.1.0\n")
        (self.root / "pricing-exporter/pyproject.toml").write_text(
            '[project]\nname = "pricing-exporter"\nversion = "0.1.0"\n'
        )
        (self.root / "pricing-exporter/uv.lock").write_text(
            '[[package]]\nname = "pricing-exporter"\nversion = "0.1.0"\n'
        )
        (self.root / "CHANGELOG.md").write_text(
            "# Changelog\n\n## 0.1.0\n\nFirst release.\n\n## 0.0.0\n\nOld notes.\n"
        )

    def test_matching_stable_release_and_its_notes(self):
        self.assertEqual(release.validate(self.root, "v0.1.0"), "0.1.0")
        self.assertEqual(release.notes(self.root, "0.1.0"), "First release.")

    def test_rejects_noncanonical_versions(self):
        for version in ("v0.1.0", "01.1.0", "0.1", "0.1.0-rc.1", "0.1.0+build"):
            with self.subTest(version=version):
                (self.root / "VERSION").write_text(version + "\n")
                with self.assertRaises(ValueError):
                    release.validate(self.root)

    def test_tag_must_match_version(self):
        for tag in ("v0.2.0", "0.1.0", "v0.1.0-rc.1"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                release.validate(self.root, tag)

    def test_package_and_lock_must_match(self):
        for path in ("pyproject.toml", "uv.lock"):
            file = self.root / "pricing-exporter" / path
            original = file.read_text()
            file.write_text(original.replace('"0.1.0"', '"0.0.0"'))
            with self.subTest(path=path), self.assertRaises(ValueError):
                release.validate(self.root)
            file.write_text(original)

    def test_missing_or_empty_notes_block_release(self):
        for text in ("# Changelog\n", "## 0.1.0\n\n## 0.0.0\nOld notes.\n"):
            (self.root / "CHANGELOG.md").write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                release.validate(self.root)


if __name__ == "__main__":
    unittest.main()
