# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Read `AGENTS.md` too — it holds the style, testing, and commit conventions. This file covers
architecture and the non-obvious constraints.

## Commands

```bash
docker compose config -q                                              # validate compose
docker compose up -d --build                                          # build + start stack
docker compose down                                                   # stop, keep volumes
jq empty grafana/dashboards/*.json extras/credit-resets/*.json        # validate dashboard JSON

uv --directory pricing-exporter sync --frozen                         # install locked deps
uv --directory pricing-exporter run python -m unittest discover -s tests
uv --directory pricing-exporter run python -m unittest tests.test_pricing.PricingTests.test_current_codex_prices_are_exported  # single test

set -a; . ./.env; set +a; python3 scripts/smoke.py                    # runtime end-to-end check
```

`docker compose down -v` permanently deletes all local telemetry and Grafana state. Never run it
unless the user explicitly asks for that deletion.

## Architecture

Three AI CLIs (Codex, Claude Code, Gemini CLI) push OTLP/HTTP to one collector, which fans the
signals out to the VictoriaMetrics stack behind Grafana:

- `/v1/logs` → VictoriaLogs (`otlp_http/vlogs`, uses `logs_endpoint` — the generic `endpoint`
  key would re-append `/v1/logs`)
- `/v1/metrics` → VictoriaMetrics via `prometheusremotewrite`
- `/v1/traces` → VictoriaTraces (`traces_endpoint`, same path-append reason)

Only port 4318 (collector) and 3000 (Grafana) are published, both bound to `127.0.0.1`. Storage
services are Compose-network-internal. Ingestion auth is a bearer token (`OTEL_AUTH_TOKEN`);
Grafana has its own admin password.

### Cost estimation flow

This is the core of the repo and it spans three files.

1. `pricing-exporter/exporter.py` reads LiteLLM's **local, pinned** cost map
   (`LITELLM_LOCAL_MODEL_COST_MAP=true`, no network), upserts prices into SQLite
   (`model_pricing` + append-only `pricing_history`), and writes one plain-text file per price
   into the shared `pricing-files` volume — e.g. `/pricing/GPT55_IN` containing `1.25`.
2. `collector/config.yaml` reads those files with the collector's `${file:/pricing/NAME}`
   syntax inside OTTL `transform` statements, computing `cost_usd_estimated` from token counts
   on Codex `codex.sse_event` and Gemini `gemini_cli.api_response` log records.
3. Grafana panels aggregate `effective_cost_usd` from VictoriaLogs and multiply by the
   `usd_eur_exchange_rate` gauge that the exporter also serves on `:9101`.

Claude Code emits native cost, so no collector-side estimation exists for it.

Every enriched record carries `cost_source` (`native` | `estimated` | `unsupported`) and a
hardcoded `pricing_as_of` date string, so old telemetry stays interpretable when prices move.
Bump that date in `collector/config.yaml` when pricing rules change.

### Constraints that will bite you

- **Processor order in the logs pipeline is load-bearing.** `transform/vl_field_names` deletes
  `event.name` / `event.kind` / `service.name` after renaming them to underscore forms for
  VictoriaLogs. It must stay *after* both enrichment processors, which match on the dotted names.
- **OTTL rule order is load-bearing.** Codex tier rules go long-context → priority → standard;
  Gemini goes specific model → family fallback, with every later rule guarded by
  `cost_usd_estimated == nil` so specifics always win.
- **Delta metrics are silently dropped** by `prometheusremotewrite`. Client configs must force
  cumulative temporality (see the Claude Code example) or no metric lands at all.
- **Dashboard field names use underscores** (`service_name`, `event_name`, `event_kind`), not
  the OTel dotted names.

### Adding a model price

1. Add the env-var → `(litellm model key, provider, direction)` triple to `OTEL_ENV_MAP` in
   `exporter.py`. If LiteLLM's pinned map lacks the model, add official list prices to
   `OFFICIAL_PRICE_FALLBACKS` (fills only missing fields; marks source `openai-official`).
2. Add OTTL rules in `collector/config.yaml` referencing `${file:/pricing/NEW_VAR}`, placed so
   ordering above holds.
3. Extend `pricing-exporter/tests/test_pricing.py` —
   `test_every_otel_price_has_a_model_cost` already asserts every `OTEL_ENV_MAP` entry resolves.
4. Rebuild: price files are only written at exporter startup and on refresh, so
   `docker compose up -d --build` is required for the collector to see new files.

## Security

Prompt/tool-content logging is disabled in every client example; keep it that way unless the
user explicitly opts in. Never commit `.env`, tokens, real telemetry, private hostnames, or
Grafana exports carrying private labels. Compose port bindings must stay loopback unless the
user is deliberately fronting the collector with a TLS proxy.
