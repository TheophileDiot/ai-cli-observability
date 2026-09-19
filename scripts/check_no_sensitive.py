#!/usr/bin/env python3
"""Block private data from reaching this public repository.

gitleaks catches generic secrets (API keys, private keys). This catches what it
cannot know: addresses and identifiers that are private to *your* deployment.

Two tiers, deliberately:

* Built-in rules below are generic — private address ranges, bearer tokens,
  email addresses. Publishing them reveals nothing.
* Deployment-specific strings (your hostnames, private repo names, accounts)
  belong in `.sensitive-patterns`, which is gitignored. A denylist of private
  identifiers is itself sensitive: committing it would publish the exact
  strings it exists to suppress. Copy `.sensitive-patterns.example` to start.

Pre-commit passes staged files as arguments; a non-zero exit blocks the commit.
"""

import re
import sys
from pathlib import Path

PATTERN_FILE = Path(__file__).resolve().parent.parent / ".sensitive-patterns"

# Generic, safe to publish.
BUILTIN = [
    ("CGNAT address (NetBird/Tailscale range)",
     r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"),
    ("private RFC1918 address",
     r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"),
    ("bearer token", r"Bearer\s+[A-Za-z0-9._~+/-]{16,}"),
    ("authorization header", r"Authorization\s*[=:]\s*[A-Za-z0-9._~+/-]{16,}"),
    # `git@github.com:` in a clone URL is not a contact address, and neither is
    # anything at example.com/org, so both are exempt.
    ("email address",
     r"\b(?!git@(?:github\.com|gitlab\.com|bitbucket\.org)\b)"
     r"[A-Za-z0-9._%+-]+@(?!example\.(?:com|org)\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
]

# A line carrying this marker is exempt, for documentation that must show the
# shape of a value. Keep these rare and obviously fake.
PRAGMA = "allow-sensitive"


def load_rules():
    rules = [(label, re.compile(p, re.IGNORECASE)) for label, p in BUILTIN]
    if PATTERN_FILE.exists():
        for n, raw in enumerate(PATTERN_FILE.read_text().splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            label, _, pattern = line.partition("=")
            if not pattern:
                print(f"{PATTERN_FILE}:{n}: expected 'label=regex'", file=sys.stderr)
                continue
            try:
                rules.append((label.strip(), re.compile(pattern.strip(), re.IGNORECASE)))
            except re.error as e:
                print(f"{PATTERN_FILE}:{n}: bad regex: {e}", file=sys.stderr)
    return rules


def scan(path, rules):
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, IsADirectoryError):
        return []
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        if PRAGMA in line:
            continue
        for label, rx in rules:
            m = rx.search(line)
            if m:
                hits.append((n, label, m.group(0)))
    return hits


def main(paths):
    rules = load_rules()
    if not PATTERN_FILE.exists():
        print(f"note: {PATTERN_FILE.name} not found — generic rules only. "
              "Copy .sensitive-patterns.example to catch your own identifiers.",
              file=sys.stderr)
    failed = False
    for path in paths:
        for n, label, found in scan(path, rules):
            print(f"{path}:{n}: {label}: {found}")
            failed = True
    if failed:
        print("\nThis repository is PUBLIC. Replace the values above with "
              f"placeholders, or mark the line '{PRAGMA}' if it is a fake example.",
              file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
