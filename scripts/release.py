#!/usr/bin/env python3
"""Validate the shared release version; print it or its curated release notes."""

import argparse
from pathlib import Path
import re
import tomllib


def notes(root: Path, version: str) -> str:
    changelog = (root / "CHANGELOG.md").read_text()
    sections = re.split(r"^## ", changelog, flags=re.MULTILINE)
    for section in sections[1:]:
        heading, _, body = section.partition("\n")
        if heading == version and body.strip():
            return body.strip()
    raise ValueError(f"CHANGELOG.md needs nonempty notes under '## {version}'")


def validate(root: Path, tag: str | None = None) -> str:
    version = (root / "VERSION").read_text().strip()
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise ValueError("VERSION must be a stable SemVer number, e.g. 0.1.0")
    if tag is not None and tag != f"v{version}":
        raise ValueError(f"tag {tag!r} does not match VERSION {version!r}")
    project = tomllib.loads((root / "pricing-exporter/pyproject.toml").read_text())
    lock = tomllib.loads((root / "pricing-exporter/uv.lock").read_text())
    locked = [p["version"] for p in lock["package"] if p["name"] == "pricing-exporter"]
    if project["project"]["version"] != version or locked != [version]:
        raise ValueError("VERSION, pricing-exporter/pyproject.toml and uv.lock must agree")
    notes(root, version)
    return version


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="require this tag to match VERSION")
    parser.add_argument("--notes", action="store_true", help="print this version's changelog section")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        version = validate(root, args.tag)
        print(notes(root, version) if args.notes else version)
    except (OSError, ValueError, KeyError) as error:
        parser.exit(1, f"release validation failed: {error}\n")
