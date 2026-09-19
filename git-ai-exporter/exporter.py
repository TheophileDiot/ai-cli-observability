#!/usr/bin/env python3
"""Export git-ai commit attribution to the AI-CLI OTel collector.

Only attribution metrics are exported. Token/cost/session telemetry already
reaches VictoriaMetrics natively from Claude Code and Codex, so re-exporting
`git-ai usage` numbers here would double-count them in ai-cli-overview.
"""

import fnmatch
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(os.environ.get("GITAI_SCAN_ROOT", Path.home() / "dev"))
WINDOW_DAYS = int(os.environ.get("GITAI_WINDOW_DAYS", "30"))
TIMEOUT = int(os.environ.get("GITAI_TIMEOUT", "300"))
GIT_AI = os.environ.get("GITAI_BIN", str(Path.home() / ".git-ai/bin/git-ai"))
CACHE = Path(os.environ.get(
    "GITAI_CACHE",
    Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "git-ai-stats.json"))
# A 30d window on a large repo costs minutes, but the numbers only move when
# HEAD moves. Recompute on a new HEAD, or once a day so the sliding window
# edge re-settles; otherwise replay the cached values.
CACHE_TTL = int(os.environ.get("GITAI_CACHE_TTL", 24 * 3600))
# Repos to leave out of the metrics, as comma-separated glob patterns matched
# against the directory name. git-ai's own `exclude_repositories` only stops
# future tracking; notes already written still show up in `git-ai stats`, so
# excluding a repo from the dashboard has to happen here as well.
EXCLUDE = [p.strip() for p in os.environ.get("GITAI_EXCLUDE_REPOS", "").split(",")
           if p.strip()]
# git's well-known empty tree: a range base that includes the root commit.
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def git(repo, *args):
    try:
        r = subprocess.run(("git", "-C", str(repo)) + args,
                           capture_output=True, text=True, timeout=TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def forge_of(repo):
    url = git(repo, "remote", "get-url", "origin")
    if not url:
        return "none"
    host = urlsplit(url).hostname if "://" in url else url.split("@")[-1].split(":")[0]
    return host or "none"


def window_range(repo):
    """Return 'base..HEAD' covering the window, or None if no commits in it."""
    since = f"{WINDOW_DAYS} days ago"
    if not git(repo, "log", "-1", f"--since={since}", "--format=%H"):
        return None
    # Newest commit strictly older than the window == the range base.
    base = git(repo, "rev-list", "-1", f"--before={since}", "HEAD")
    if not base:
        # The whole history fits inside the window. `A..B` excludes A, so using
        # the root commit here would drop it — and an initial import is usually
        # the largest commit in the repo. The empty tree has no such edge.
        base = EMPTY_TREE
    return f"{base}..HEAD"


def stats(repo, rng):
    # One slow or broken repo must never sink the whole export, so every
    # failure mode here degrades to "skip this repo" rather than raising.
    try:
        r = subprocess.run([GIT_AI, "stats", rng, "--json"], cwd=str(repo),
                           capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        print(f"warn: {repo.name}: git-ai stats timed out after {TIMEOUT}s",
              file=sys.stderr)
        return None
    except OSError as e:
        print(f"warn: {repo.name}: {e}", file=sys.stderr)
        return None
    if r.returncode != 0:
        return None
    try:
        parsed = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    # Valid JSON is not necessarily the schema we expect. A list or a scalar
    # would raise deep inside collect() and take the whole export with it.
    return parsed if isinstance(parsed, dict) else None


def points(name, rows):
    """rows: list of (attrs dict, int value)"""
    now = str(time.time_ns())
    # No `unit`: the OTLP-to-Prometheus translation turns unit "1" into a
    # `_ratio` suffix, and these are line counts, not ratios.
    return {
        "name": name,
        "gauge": {"dataPoints": [
            {"attributes": [{"key": k, "value": {"stringValue": str(v)}}
                            for k, v in sorted(a.items())],
             "timeUnixNano": now,
             "asInt": str(int(n))}
            for a, n in rows]},
    }


def collect(payloads, repo_name, forge, data):
    """Fold one repo's `git-ai stats` JSON into the metric row accumulator.

    Two schemas exist. `git-ai stats <commit>` puts the line counts at the top
    level; `git-ai stats <a>..<b>` nests them under `range_stats` and adds
    `authorship_stats`. We always query a range, but accept both so a
    single-commit payload is not silently reduced to commit counts.
    """
    base = {"repo": repo_name, "forge": forge}
    lines = data.get("range_stats") or data
    if not isinstance(lines, dict):
        lines = {}
    for field, metric in (
        ("ai_additions", "gitai_ai_additions"),
        ("ai_accepted", "gitai_ai_accepted"),
        ("human_additions", "gitai_human_additions"),
        ("unknown_additions", "gitai_unknown_additions"),
        ("git_diff_added_lines", "gitai_diff_added_lines"),
        ("git_diff_deleted_lines", "gitai_diff_deleted_lines"),
    ):
        if field in lines:
            payloads.setdefault(metric, []).append((base, lines[field]))

    for key, per in (lines.get("tool_model_breakdown") or {}).items():
        # Real payloads use "claude::claude-opus-5". The upstream README shows
        # "claude_code/claude-sonnet-5", so accept both rather than trusting
        # either: guessing wrong silently mislabels every model.
        sep = "::" if "::" in key else "/"
        tool, _, model = key.partition(sep)
        attrs = dict(base, tool=tool, model=model or "unknown")
        for field, metric in (("ai_additions", "gitai_tool_ai_additions"),
                              ("ai_accepted", "gitai_tool_ai_accepted")):
            if field in per:
                payloads.setdefault(metric, []).append((attrs, per[field]))

    # Coverage: how many commits actually carry a note. Without this a dashboard
    # cannot tell "no AI code" apart from "attribution never landed".
    a = data.get("authorship_stats") or {}
    if "total_commits" in a:
        payloads.setdefault("gitai_commits_total", []).append((base, a["total_commits"]))
    if "commits_with_authorship" in a:
        payloads.setdefault("gitai_commits_with_authorship", []).append(
            (base, a["commits_with_authorship"]))


def otel_config():
    endpoint = os.environ.get("GITAI_OTEL_ENDPOINT")
    headers = os.environ.get("GITAI_OTEL_HEADERS")
    if not (endpoint and headers):
        # Single source of truth for the bearer: the Claude Code env block.
        env = json.load(open(Path.home() / ".claude/settings.json")).get("env", {})
        endpoint = endpoint or env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        headers = headers or env.get("OTEL_EXPORTER_OTLP_HEADERS")
    if not (endpoint and headers):
        sys.exit("no OTLP endpoint/headers: set GITAI_OTEL_ENDPOINT + GITAI_OTEL_HEADERS")
    hdrs = {}
    for part in headers.split(","):
        k, _, v = part.partition("=")
        hdrs[k.strip()] = v.strip()
    return endpoint.rstrip("/") + "/v1/metrics", hdrs


def load_cache():
    try:
        return json.loads(CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(cache):
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(cache))
        tmp.replace(CACHE)          # atomic; a killed run never leaves a torn file
    except OSError as e:
        print(f"warn: cache write failed: {e}", file=sys.stderr)


def main():
    dry = "--dry-run" in sys.argv
    payloads, scanned, fresh, skipped, stale = {}, 0, 0, 0, 0
    cache = load_cache()
    now = time.time()
    for gitdir in sorted(ROOT.glob("*/.git")):
        repo = gitdir.parent
        if any(fnmatch.fnmatch(repo.name, pat) for pat in EXCLUDE):
            continue
        rng = window_range(repo)
        if not rng:
            continue
        head = git(repo, "rev-parse", "HEAD")
        hit = cache.get(str(repo))
        if hit and hit.get("head") == head and now - hit.get("at", 0) < CACHE_TTL:
            # Failures are cached too. A repo whose history is too large to walk
            # inside the timeout fails identically every run, so retrying it
            # hourly burns the full timeout forever for no new data.
            if hit.get("failed"):
                skipped += 1
                continue
            data = hit["data"]
        else:
            data = stats(repo, rng)
            if data is None:
                print(f"warn: stats failed for {repo.name}", file=sys.stderr)
                previous = (hit or {}).get("data")
                if previous is None:
                    cache[str(repo)] = {"head": head, "at": now, "failed": True}
                    continue
                # A repo that succeeded before and fails now is usually a
                # transient timeout. Discarding its numbers would blank the
                # dashboard for a full TTL, so keep serving the last good
                # values and flag them as stale instead.
                cache[str(repo)] = {"head": head, "at": now, "data": previous,
                                    "stale": True}
                data = previous
                stale += 1
            else:
                cache[str(repo)] = {"head": head, "at": now, "data": data}
                fresh += 1
        payloads.setdefault("gitai_stats_stale", []).append(
            ({"repo": repo.name, "forge": forge_of(repo)},
             1 if (cache.get(str(repo), {}).get("stale")) else 0))
        collect(payloads, repo.name, forge_of(repo), data)
        scanned += 1
    # Drop entries for repos that no longer exist, so the cache cannot grow forever.
    cache = {k: v for k, v in cache.items() if Path(k).exists()}
    save_cache(cache)   # perf-only state, so --dry-run warms it too

    if not payloads:
        print("nothing to export", file=sys.stderr)
        return 0

    body = {"resourceMetrics": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "git-ai-stats"}},
            {"key": "host.name", "value": {"stringValue": os.uname().nodename}},
        ]},
        "scopeMetrics": [{
            "scope": {"name": "git-ai-stats"},
            "metrics": [points(n, rows) for n, rows in sorted(payloads.items())],
        }],
    }]}

    if dry:
        json.dump(body, sys.stdout, indent=2)
        print(f"\n{scanned} repos ({fresh} recomputed, {skipped} known-failing), "
              f"{len(payloads)} metrics", file=sys.stderr)
        return 0

    url, hdrs = otel_config()
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **hdrs})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        print(f"{resp.status} — {scanned} repos ({fresh} recomputed, "
              f"{skipped} known-failing), {len(payloads)} metrics")
    return 0


if __name__ == "__main__":
    sys.exit(main())
