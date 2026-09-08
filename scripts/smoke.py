#!/usr/bin/env python3
"""Smoke-test local Grafana and all three OTLP signal paths."""

from __future__ import annotations

import base64
from contextlib import suppress
import json
import os
from pathlib import Path
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from smoke_pricing import check as check_pricing

GRAFANA_URL = f"http://127.0.0.1:{os.getenv('GRAFANA_PORT', '3000')}"
OTLP_URL = f"http://127.0.0.1:{os.getenv('OTLP_HTTP_PORT', '4318')}"
OTEL_TOKEN = os.environ.get("OTEL_AUTH_TOKEN")
GRAFANA_USER = os.getenv("GRAFANA_ADMIN_USER", "admin")
GRAFANA_PASSWORD = os.environ.get("GRAFANA_ADMIN_PASSWORD")
SERVICE = "ai-cli-observability-smoke"


def request(
    url: str, *, data: dict | None = None, auth: str | None = None
) -> tuple[int, str]:
    headers = {}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    if auth:
        headers["Authorization"] = auth
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def wait_for(url: str, auth: str | None = None, attempts: int = 60) -> str:
    for _ in range(attempts):
        with suppress(OSError):
            status, body = request(url, auth=auth)
            if status == 200:
                return body
        time.sleep(1)
    raise RuntimeError(f"timed out waiting for {url}")


def post_signal(path: str, payload: dict) -> None:
    status, body = request(
        f"{OTLP_URL}{path}", data=payload, auth=f"Bearer {OTEL_TOKEN}"
    )
    if status != 200:
        raise RuntimeError(f"{path} returned {status}: {body}")


def retry_query(url: str, auth: str, needle: str) -> None:
    for _ in range(20):
        status, body = request(url, auth=auth)
        if status == 200 and needle in body:
            return
        time.sleep(1)
    raise RuntimeError(f"query did not return {needle}: {body}")


def main() -> int:
    if not OTEL_TOKEN or not GRAFANA_PASSWORD:
        print(
            "OTEL_AUTH_TOKEN and GRAFANA_ADMIN_PASSWORD are required", file=sys.stderr
        )
        return 2

    basic = base64.b64encode(f"{GRAFANA_USER}:{GRAFANA_PASSWORD}".encode()).decode()
    grafana_auth = f"Basic {basic}"
    wait_for(f"{GRAFANA_URL}/api/health")
    wait_for(f"{GRAFANA_URL}/api/search?query=AI%20CLI%20Tools", grafana_auth)

    unauthorized, _ = request(f"{OTLP_URL}/v1/logs", data={"resourceLogs": []})
    if unauthorized not in (401, 403):
        raise RuntimeError(
            f"OTLP endpoint accepted unauthenticated request: {unauthorized}"
        )

    now = str(time.time_ns())
    trace_id = uuid.uuid4().hex
    resource = {
        "attributes": [{"key": "service.name", "value": {"stringValue": SERVICE}}]
    }
    post_signal(
        "/v1/logs",
        {
            "resourceLogs": [
                {
                    "resource": resource,
                    "scopeLogs": [
                        {
                            "scope": {"name": "smoke"},
                            "logRecords": [
                                {
                                    "timeUnixNano": now,
                                    "body": {"stringValue": "smoke-ok"},
                                    "attributes": [
                                        {
                                            "key": "event.name",
                                            "value": {"stringValue": "smoke.event"},
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        },
    )
    post_signal(
        "/v1/metrics",
        {
            "resourceMetrics": [
                {
                    "resource": resource,
                    "scopeMetrics": [
                        {
                            "scope": {"name": "smoke"},
                            "metrics": [
                                {
                                    "name": "ai_cli_observability_smoke",
                                    "gauge": {
                                        "dataPoints": [
                                            {"timeUnixNano": now, "asInt": "1"}
                                        ]
                                    },
                                }
                            ],
                        }
                    ],
                }
            ]
        },
    )
    post_signal(
        "/v1/traces",
        {
            "resourceSpans": [
                {
                    "resource": resource,
                    "scopeSpans": [
                        {
                            "scope": {"name": "smoke"},
                            "spans": [
                                {
                                    "traceId": trace_id,
                                    "spanId": "0123456789abcdef",
                                    "name": "smoke-span",
                                    "kind": 1,
                                    "startTimeUnixNano": now,
                                    "endTimeUnixNano": str(int(now) + 1_000_000),
                                    "status": {"code": 1},
                                }
                            ],
                        }
                    ],
                }
            ]
        },
    )

    metrics_query = urllib.parse.urlencode(
        {"query": "ai_cli_observability_smoke", "nocache": "1"}
    )
    retry_query(
        f"{GRAFANA_URL}/api/datasources/proxy/uid/victoriametrics/api/v1/query?{metrics_query}",
        grafana_auth,
        "ai_cli_observability_smoke",
    )
    logs_query = urllib.parse.urlencode(
        {"query": f'{{service_name="{SERVICE}"}}', "limit": "10"}
    )
    retry_query(
        f"{GRAFANA_URL}/api/datasources/proxy/uid/victorialogs/select/logsql/query?{logs_query}",
        grafana_auth,
        "smoke-ok",
    )

    for uid in ("victoriametrics", "victorialogs", "victoriatraces"):
        status, body = request(
            f"{GRAFANA_URL}/api/datasources/uid/{uid}", auth=grafana_auth
        )
        if status != 200:
            raise RuntimeError(f"Grafana datasource {uid} unavailable: {body}")

    retry_query(
        f"{GRAFANA_URL}/api/datasources/proxy/uid/victoriatraces/api/traces/{trace_id}",
        grafana_auth,
        "smoke-span",
    )

    check_pricing(
        OTLP_URL,
        f"{GRAFANA_URL}/api/datasources/proxy/uid/victorialogs",
        Path(__file__).resolve().parents[1] / "grafana/dashboards/ai-cli-overview.json",
        OTEL_TOKEN,
        grafana_auth,
    )
    print("smoke passed: auth, dashboard, logs, metrics, traces, datasources, pricing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
