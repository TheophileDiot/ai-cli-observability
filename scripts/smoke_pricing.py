#!/usr/bin/env python3
"""Exercise real OTTL pricing and Grafana queries with synthetic OTLP records."""

import argparse
import json
import math
import os
from pathlib import Path
import time
import urllib.parse
import urllib.request
import uuid


def check(collector_url, logs_url, dashboard_path, token, logs_auth=None):
    run_id = uuid.uuid4().hex
    service = "ai-cli-observability-smoke"
    base = {
        "input_token_count": 1000,
        "cached_token_count": 200,
        "cache_write_token_count": 100,
        "output_token_count": 100,
    }
    cases = [
        ("astra", "gpt-6-astra", {}, 0.01345),
        ("sol", "gpt-5.6-sol", {}, 0.00538),
        ("terra", "gpt-5.6-terra", {}, 0.00289),
        ("dated-terra", "gpt-5.6-terra-2026-09-08", {}, 0.00289),
        ("luna", "gpt-5.6-luna", {}, 0.000289),
        ("alias", "gpt-5.6", {}, 0.00538),
        ("fast", "gpt-6-astra", {"service_tier": "fast"}, 0.0269),
        ("flex", "gpt-6-astra", {"service_tier": "flex"}, 0.006725),
        ("priority", "gpt-6-astra", {"service_tier": "priority"}, 0.0269),
        (
            "long-fast",
            "gpt-6-astra",
            {
                "input_token_count": 300000,
                "cached_token_count": 100000,
                "cache_write_token_count": 50000,
                "output_token_count": 1000,
                "service_tier": "fast",
            },
            9.05,
        ),
        ("boundary", "gpt-6-astra", {"input_token_count": 272000}, 2.72345),
        ("old-client", "gpt-6-astra", {"cache_write_token_count": None}, 0.0132),
        ("legacy-fast", "gpt-5.4", {"service_tier": "fast"}, 0.0071),
        ("native", "gpt-6-astra", {"cost_usd": 0.123}, 0.123),
        ("unknown", "codex-auto-review", {}, None),
        ("invalid", "gpt-6-astra", {"cached_token_count": 1100}, None),
    ]
    now = str(time.time_ns())

    def record(attrs, body):
        attrs = {**attrs, "smoke_run": run_id}
        return {
            "timeUnixNano": now,
            "body": {"stringValue": body},
            "attributes": [
                {"key": k, "value": {"stringValue": str(v)}}
                for k, v in attrs.items()
                if v is not None
            ],
        }

    records = [
        record(
            {
                **base,
                **extra,
                "model": model,
                "smoke_case": name,
                "event.name": "codex.sse_event",
                "event.kind": "response.completed",
            },
            "codex.sse_event",
        )
        for name, model, extra, _ in cases
    ]
    for event, cost in (("api_request", 0.02), ("claude_code.api_request", 0.03)):
        records.append(
            record(
                {
                    "event.name": event,
                    "model": "claude-sonnet-5",
                    "cost_usd": cost,
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "duration_ms": 50,
                },
                "claude_code.api_request",
            )
        )
    payload = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": service}}
                    ]
                },
                "scopeLogs": [{"scope": {"name": "smoke"}, "logRecords": records}],
            }
        ]
    }
    req = urllib.request.Request(
        collector_url.rstrip("/") + "/v1/logs",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as response:
        assert response.status == 200

    def query(expr):
        params = urllib.parse.urlencode({"query": f'smoke_run:="{run_id}" ' + expr})
        req = urllib.request.Request(
            logs_url.rstrip("/") + "/select/logsql/query?" + params,
            headers={"Authorization": logs_auth} if logs_auth else {},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            return [json.loads(line) for line in response.read().decode().splitlines()]

    for _ in range(30):
        rows = query(
            "smoke_case:* | fields smoke_case, effective_cost_usd, cost_source"
        )
        if len(rows) == len(cases):
            break
        time.sleep(1)
    actual = {r["smoke_case"]: r for r in rows}
    errors = []
    for name, _, _, expected in cases:
        row = actual.get(name, {})
        if expected is None:
            ok = (
                "effective_cost_usd" not in row
                and row.get("cost_source") == "unsupported"
            )
        else:
            ok = math.isclose(
                float(row.get("effective_cost_usd", "nan")), expected, rel_tol=1e-9
            )
            ok = ok and row.get("cost_source") == (
                "native" if name == "native" else "estimated"
            )
        if not ok:
            errors.append(f"{name}: expected {expected}, received {row}")

    def panels(items):
        for panel in items:
            yield panel
            yield from panels(panel.get("panels", []))

    dashboard = json.loads(Path(dashboard_path).read_text())
    for panel in panels(dashboard["panels"]):
        if panel.get("id") not in (107, 108):
            continue
        expr = (
            panel["targets"][0]["expr"]
            .replace("${service:regex}", service)
            .replace("${eur_rate}", "1")
        )
        result = query(expr)
        field, expected = ("cost_eur", 0.05) if panel["id"] == 107 else ("requests", 2)
        if not result or not math.isclose(
            float(result[0].get(field, "nan")), expected, rel_tol=1e-9
        ):
            errors.append(
                f"panel {panel['id']}: expected {field}={expected}, received {result}"
            )
    if errors:
        raise AssertionError("\n".join(errors))
    print(f"pricing smoke passed: {len(cases)} cost cases and 2 dashboard queries")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collector-url", default="http://127.0.0.1:4318")
    parser.add_argument("--logs-url", required=True)
    parser.add_argument("--dashboard", required=True)
    args = parser.parse_args()
    check(
        args.collector_url, args.logs_url, args.dashboard, os.environ["OTEL_AUTH_TOKEN"]
    )
