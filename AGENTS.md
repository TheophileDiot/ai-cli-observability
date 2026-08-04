# Repository Guidelines

## Project Structure & Module Organization

`compose.yaml` defines the local OpenTelemetry, VictoriaMetrics, VictoriaLogs,
VictoriaTraces, Grafana, and pricing-exporter stack. Collector routing and
enrichment live in `collector/config.yaml`. Grafana provisioning files are under
`grafana/provisioning/`, while dashboard JSON lives in `grafana/dashboards/`.
`pricing-exporter/` contains the Python exporter, its `pyproject.toml`, lockfile,
Dockerfile, and unit tests. `scripts/smoke.py` performs end-to-end validation.
Client examples belong in `clients/<cli>/`; optional integrations belong in
`extras/`.

## Build, Test, and Development Commands

- `docker compose config -q`: validate the rendered Compose configuration.
- `docker compose up -d --build`: build the exporter and start the stack.
- `uv --directory pricing-exporter sync --frozen`: install the exact locked
  Python dependencies.
- `uv --directory pricing-exporter run python -m unittest discover -s tests`:
  run pricing tests.
- `jq empty grafana/dashboards/*.json extras/credit-resets/*.json`: validate
  dashboard JSON.
- `python3 scripts/smoke.py`: verify authentication, telemetry ingestion,
  dashboard provisioning, and datasources after loading `.env`.
- `docker compose down`: stop services without deleting volumes.

## Coding Style & Naming Conventions

Use four spaces, type hints, `snake_case` functions, `PascalCase` classes, and
`UPPER_CASE` constants in Python. Follow existing stdlib-first patterns and keep
I/O boundaries explicit. Use two-space indentation in YAML. Preserve pinned
dependency versions and container digests. Keep dashboard JSON machine-valid;
avoid manual reformatting unrelated to the change.

## Testing Guidelines

Tests use Python `unittest`. Name files `test_*.py`, test methods `test_*`, and
add the smallest regression covering changed pricing logic. Run static checks
before the runtime smoke test. Smoke records must retain the
`ai-cli-observability-smoke` service name so generated telemetry is identifiable.

## Commit & Pull Request Guidelines

Use Conventional Commits with short imperative subjects, such as
`fix: update Gemini pricing fallback`. Keep each commit focused. Pull requests
should explain behavior and configuration changes, list checks run, link
relevant issues, and include sanitized screenshots for dashboard changes.

## Security & Configuration

Never commit `.env`, tokens, telemetry, private endpoints, or Grafana exports
containing sensitive labels. Start from `.env.example`. Keep OTLP and Grafana
loopback-bound unless a TLS proxy and network access controls are configured.
Never use `docker compose down -v` unless permanent local data deletion is
explicitly intended.
