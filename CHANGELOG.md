# Changelog

Entries describe the changes prepared for each version. A version is published
only when its tag and GitHub Release exist. See [RELEASING.md](RELEASING.md).

## 0.1.0

- Establish one version for the collector configuration, pricing exporter,
  provisioned dashboards, optional integrations and client examples.
- Add CI for locked Python dependencies, unit tests, public-data guards, Compose
  configuration and the full OTLP/Grafana/pricing smoke test.
- Publish the pricing exporter to GHCR for Linux amd64 and arm64 only after CI
  succeeds. Release notes record its immutable manifest digest and source commit.
- Include a source bundle, release manifest, per-file hashes and asset checksums
  so consumers can deploy matching collector, pricing and dashboard revisions.
- Preserve local builds as the default. An optional Compose override uses the
  published image by version or digest for both pricing services.

### Compatibility and operations

This is the initial versioned baseline, not a pricing-rule or telemetry-schema
change. Existing volumes are retained. Pricing files are written at exporter
startup and read at collector startup: update the exporter first, then
force-recreate the collector and Grafana. Back up persistent data before upgrades.
The live exchange rate and upstream Grafana.com uploads are not frozen by a
release. Read RELEASING.md for upgrade, rollback and registry-access requirements.
