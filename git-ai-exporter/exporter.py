#!/usr/bin/env python3
"""Export git-ai commit attribution to the AI-CLI OTel collector.

Only attribution metrics are exported. Token/cost/session telemetry already
reaches VictoriaMetrics natively from Claude Code and Codex, so re-exporting
`git-ai usage` numbers here would double-count them in ai-cli-overview.
"""

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
        base = git(repo, "rev-list", "--max-parents=0", "-1", "HEAD")
        if not base:
            return None
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
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def points(name, rows):
    """rows: list of (attrs dict, int value)"""
    now = str(time.time_ns())
    return {
        "name": name,
        "unit": "1",
        "gauge": {"dataPoints": [
            {"attributes": [{"key": k, "value": {"stringValue": str(v)}}
                            for k, v in sorted(a.items())],
             "timeUnixNano": now,
             "asInt": str(int(n))}
            for a, n in rows]},
    }


def collect(payloads, repo_name, forge, data):
    """Fold one repo's `git-ai stats` JSON into the metric row accumulator."""
    base = {"repo": repo_name, "forge": forge}
    for field, metric in (
        ("ai_additions", "gitai_ai_additions"),
        ("ai_accepted", "gitai_ai_accepted"),
        ("human_additions", "gitai_human_additions"),
        ("unknown_additions", "gitai_unknown_additions"),
        ("git_diff_added_lines", "gitai_diff_added_lines"),
        ("git_diff_deleted_lines", "gitai_diff_deleted_lines"),
    ):
        if field in data:
            payloads.setdefault(metric, []).append((base, data[field]))

    for key, per in (data.get("tool_model_breakdown") or {}).items():
        tool, _, model = key.partition("/")
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
        tmp = CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache))
        tmp.replace(CACHE)          # atomic; a killed run never leaves a torn file
    except OSError as e:
        print(f"warn: cache write failed: {e}", file=sys.stderr)


def main():
    dry = "--dry-run" in sys.argv
    payloads, scanned, fresh = {}, 0, 0
    cache = load_cache()
    now = time.time()
    for gitdir in sorted(ROOT.glob("*/.git")):
        repo = gitdir.parent
        rng = window_range(repo)
        if not rng:
            continue
        head = git(repo, "rev-parse", "HEAD")
        hit = cache.get(str(repo))
        if hit and hit.get("head") == head and now - hit.get("at", 0) < CACHE_TTL:
            data = hit["data"]
        else:
            data = stats(repo, rng)
            if data is None:
                print(f"warn: stats failed for {repo.name}", file=sys.stderr)
                continue
            cache[str(repo)] = {"head": head, "at": now, "data": data}
            fresh += 1
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
        print(f"\n{scanned} repos ({fresh} recomputed), {len(payloads)} metrics",
              file=sys.stderr)
        return 0

    url, hdrs = otel_config()
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **hdrs})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        print(f"{resp.status} — {scanned} repos ({fresh} recomputed), "
              f"{len(payloads)} metrics")
    return 0


if __name__ == "__main__":
    sys.exit(main())
