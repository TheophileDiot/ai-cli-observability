# git-ai exporter

Exports [git-ai](https://github.com/git-ai-project/git-ai) commit attribution —
which lines of each commit were written by an agent, and by which tool and model —
into the same VictoriaMetrics instance backing the AI CLI dashboards.

The matching dashboard ships provisioned from
[`grafana/dashboards/git-ai-attribution.json`](../grafana/dashboards/git-ai-attribution.json)
and is published on Grafana.com as
[Git AI - Code Attribution](https://grafana.com/grafana/dashboards/25803) (ID `25803`).

Attribution answers a question token telemetry cannot: not *how much did the
agents cost*, but *how much of the code that shipped did they actually write*.

## What it does not export

`git-ai usage` also reports tokens, cost and session counts. Those are **not**
exported here: Claude Code and Codex already push them natively over OTLP, and
re-exporting would double-count them in the existing dashboards. This exporter
emits attribution only.

## How it reaches the collector

It pushes OTLP/JSON to the existing authenticated collector ingress — no new
port, router or credential. Endpoint and bearer are read from
`GITAI_OTEL_ENDPOINT` / `GITAI_OTEL_HEADERS`, falling back to the `env` block of
`~/.claude/settings.json` so the bearer has exactly one home on the machine.

The collector is published on a NetBird address only, so the systemd unit skips
the run when the tunnel is down rather than failing against a dead endpoint.

## Metrics

All gauges, labelled `repo` and `forge`:

| Metric                                                | Meaning                                 |
| ----------------------------------------------------- | --------------------------------------- |
| `gitai_ai_additions`                                  | Lines added by an agent in the window   |
| `gitai_ai_accepted`                                   | Agent lines that survived to the commit |
| `gitai_human_additions`                               | Lines added by a human                  |
| `gitai_unknown_additions`                             | Lines with no attribution record        |
| `gitai_diff_added_lines` / `gitai_diff_deleted_lines` | Raw git diff totals                     |
| `gitai_commits_total`                                 | Commits in the window                   |
| `gitai_commits_with_authorship`                       | Commits carrying a git-ai note          |

`gitai_tool_ai_additions` and `gitai_tool_ai_accepted` add `tool` and `model`.

`gitai_repo_failed` is emitted for **every** repo considered, including ones that
produced no numbers. An earlier version emitted nothing for a failing repo, which
made "the exporter is broken" indistinguishable from "this repo had no commits",
"this repo is excluded", and "this repo always fails". Health is stated, never
inferred from a series going missing.

Export the last two as a ratio before reading anything else. Attribution only
exists for commits made on a machine where git-ai is installed and the agent has
been restarted since; without that coverage ratio, "no AI code" and "attribution
never landed" look identical on a graph.

## Excluding repos

git-ai's own `exclude_repositories` stops it tracking a repo, but notes already
written stay in git and still appear in `git-ai stats`. Keeping a repo out of the
dashboard therefore needs `GITAI_EXCLUDE_REPOS` here as well.

Worth excluding: anything auto-committed by a timer or a bot. A notes vault whose
commits are agent-written prose will otherwise dominate every panel and make the
AI percentage meaningless.

## What the numbers mean

- The window is a **rolling 30 days**, recomputed each run. The time series
  therefore show how a trailing aggregate drifts, not activity on that day.
- Range mode reports the **net diff** across the window, not the sum of
  per-commit churn: a line added then rewritten counts once, not twice. This is
  the honest figure for "how much code is there", not "how much typing happened".
- "Committed locally" is the limit of what this can claim. The exporter reads each
  repo's local HEAD and knows nothing about merges, pull requests or deployments,
  so none of these numbers establish that the code shipped.

## Coverage caveats

- Only commits made on this machine are attributed. On a repo with other
  contributors, the AI percentage reflects *your share of the commits*, not the
  project's AI content.
- GitHub squash-merge rewrites commits server-side and drops the notes from the
  target branch. `git ai ci github install` fixes this; without it, treat
  post-merge numbers on shared repos as a floor.

## Configuration

| Variable              | Default                | Purpose                              |
| --------------------- | ---------------------- | ------------------------------------ |
| `GITAI_SCAN_ROOT`     | `~/dev`                | Directory of repos to scan           |
| `GITAI_WINDOW_DAYS`   | `30`                   | Rolling window                       |
| `GITAI_CACHE_TTL`     | `86400`                | Recompute a repo at least this often |
| `GITAI_TIMEOUT`       | `300`                  | Per-command timeout, seconds         |
| `GITAI_OTEL_ENDPOINT` | Claude `settings.json` | Collector base URL                   |
| `GITAI_OTEL_HEADERS`  | Claude `settings.json` | `Authorization=Bearer …`             |

A 30-day window over a large repo costs minutes, so results are cached against
`HEAD` and replayed until it moves or the TTL expires. A cold first pass takes
several minutes; steady-state runs are seconds.

Two cases the cache handles deliberately:

- A repo that **fails on its first attempt** is recorded as failed and not retried
  until `HEAD` moves. Some histories cannot be walked inside any sane timeout, and
  retrying one hourly costs the full timeout every tick for data that never arrives.
- A repo that **succeeded before and fails now** keeps serving its last good values
  with `gitai_stats_stale` set to 1. Dropping them would blank the dashboard for a
  whole TTL over what is usually a transient timeout. Alert on `gitai_stats_stale`
  rather than on a metric going missing.

## Usage

```bash
./exporter.py --dry-run     # print the OTLP payload, push nothing
./exporter.py               # push to the collector

cd tests && PYTHONPATH=.. python3 -m unittest test_exporter
```

## Install

```bash
cp systemd/git-ai-exporter.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now git-ai-exporter.timer
```
