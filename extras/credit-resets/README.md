# Credit-reset add-on

This optional dashboard is separate because credit-reset timestamps are not
part of Codex, Claude Code, Gemini CLI, or OpenTelemetry.

It expects a custom Prometheus-compatible metric:

```text
ai_cli_credit_reset_timestamp_seconds{cli="claude"} 1760000000
ai_cli_credit_reset_timestamp_seconds{cli="codex"} 1760000000
```

Import `credit-resets.json` after your exporter exposes that metric to the
VictoriaMetrics datasource. Missing series render as no data; there are no
account-specific fallback offsets.
