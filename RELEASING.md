# Releases and consumer updates

## Version contract

`VERSION` is the version of the **whole stack**: Compose, collector enrichment,
pricing exporter and its locked model catalog, dashboards, client examples and
optional integrations. The exporter project version and its entry in `uv.lock`
must match. `python3 scripts/release.py` checks this and requires useful notes in
`CHANGELOG.md`. A release's source bundle, commit, file hashes and image digest
identify the matching components; they do not have independent release trains.

Start at `0.1.0` while telemetry and dashboard contracts evolve. Use stable SemVer
tags `vMAJOR.MINOR.PATCH`, without leading zeros. During `0.x`, use a minor bump
for incompatible configuration, fields, queries or storage changes, and a patch
for compatible fixes, pricing refreshes and dependency updates. Record breaking
changes and migration steps. Move to `1.0.0` when the public configuration and
telemetry contracts are ready for stability guarantees. Prerelease tags and
moving image aliases (`latest`, `0`, `0.1`) are not published.

Third-party image versions, Grafana's dashboard revision integers, and
`pricing_as_of` retain their own meanings; do not replace them with the stack
version. Change `pricing_as_of` when pricing rules change (see AGENTS.md).
SQLite pricing history and past ingested records remain historical.

## Prepare and publish (maintainers)

1. In a PR, update `VERSION`, `pricing-exporter/pyproject.toml`, the exporter entry
   in `pricing-exporter/uv.lock`, and add a nonempty `## X.Y.Z` section to
   `CHANGELOG.md`. Include changed model prices, compatibility, migration and
   rollback limits. Keep dependency hashes and third-party image digests pinned.
2. Run the checks in README.md and merge only after all CI jobs pass. CI runs the
   public-data hooks, a committed-history secret scan and unit tests, validates
   both Compose modes, and builds and smoke-tests the stack on native Linux amd64
   and arm64 runners. Pricing tests
   check every collector price-file reference against the exporter; smoke tests
   exercise real collector enrichment and dashboard queries.
3. After explicit authorization to publish, create an annotated tag on the
   reviewed commit from `main`, then push **only that tag**. For the first release
   (these commands publish; preparing a PR does not run them):

   ```bash
   git switch main
   git pull --ff-only
   python3 scripts/release.py --tag v0.1.0
   git tag -a v0.1.0 -m "Release v0.1.0"
   git push origin refs/tags/v0.1.0
   ```

4. The release workflow rejects malformed or mismatched tags, lightweight tags,
   commits outside `main`, and an existing GitHub Release. It reruns the same CI
   workflow at the tag, then builds and pushes
   `ghcr.io/theophilediot/ai-cli-observability-pricing-exporter:vX.Y.Z` for
   `linux/amd64` and `linux/arm64`. The image uses the same checkout and locked
   inputs tested by CI; the final multi-platform build happens afterward. OCI
   labels record the source repository, commit and version.
5. Only after a successful push does it create a GitHub Release with curated
   changelog notes, GitHub-generated change notes, the image manifest digest,
   source commit and assets:

   | Asset | Purpose |
   | --- | --- |
   | `ai-cli-observability-vX.Y.Z.tar.gz` | Tracked sources, including collector and dashboards |
   | `release-manifest.json` | Version, source commit, platforms and image reference with digest |
   | `release-files.sha256` | SHA-256 of every tracked file, relative to the bundle root |
   | `SHA256SUMS` | SHA-256 of the three assets above |

Never move or reuse a published version tag. Enable GitHub tag rules and immutable
releases if available; these administrative settings are not configured by this
PR. The workflow does not enforce registry tag immutability: pin the digest from
a successful release rather than relying on an image tag alone.

### Permissions and first publication

No personal registry token is required. Validation uses `contents: read`; only
publication requests `contents: write` and `packages: write` on `GITHUB_TOKEN`.
Actions must be enabled, pinned actions allowed, and repository/package policy
must permit these permissions. If the package already exists, grant this
repository Actions access to it. See GitHub's
[Container registry documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry).

**New GHCR packages default to private.** After first publication, the package
owner must review access and make it public for anonymous pulls, or provide
authenticated read access. Test an anonymous pull before announcing public
availability. This PR does not change registry visibility. A production push
cannot be validated without publishing a tag.

If CI fails, publication is skipped. If image publication succeeds but release
creation fails, an image may exist without a release: do not deploy it yet.
Inspect the failure and any draft release before retrying. A failed run can
rebuild and replace the image tag until a GitHub Release exists; the digest in
the successful release is the consumer reference. If the Release already exists,
make a new patch release rather than overwriting assets.

## Follow releases (consumers)

Use GitHub **Watch → Custom → Releases**. Read compatibility and pricing notes
before updating. Deploy the source bundle or checkout at the selected tag, with
dashboards and collector configuration from that same version. Grafana.com IDs
are independently uploaded and may lag or lead a release: import tagged JSON
when reproducibility matters.

Download all four assets into one directory. Run `sha256sum -c SHA256SUMS` there
(`shasum -a 256 -c SHA256SUMS` on macOS), extract the bundle, then run
`sha256sum -c ../release-files.sha256` inside its root. Obtain checksums from the
trusted GitHub Release. Keep the deployment in the same directory/project and
preserve `.env`, local overrides and persistent volumes.

### Use a published pricing image

Docker Compose **2.24.4 or newer** is required for `compose.release.yaml`'s `!reset`
tag, which removes both local build definitions. `compose.yaml` still builds
locally by default. Set `PRICING_IMAGE` in `.env` (or export it) to the **complete
`pricing_image` value from `release-manifest.json`**:

```text
ghcr.io/theophilediot/ai-cli-observability-pricing-exporter:vX.Y.Z@sha256:<digest-from-release>
```

For every command using the published image, include both Compose files. Both
`pricing-init` and `pricing-exporter` use the identical reference:

```bash
docker compose -f compose.yaml -f compose.release.yaml config --images
docker compose -f compose.yaml -f compose.release.yaml pull
```

### Upgrade and rollback

Back up persistent data and record the previous tag and digest. Review local
changes before switching versions; never reset them away. For a Git deployment,
fetch tags and check out the selected release in the same directory, for example
`git switch --detach v0.1.0` once published. Set `PRICING_IMAGE` to that release's
manifest value, then run:

```bash
docker compose -f compose.yaml -f compose.release.yaml up -d --no-build --force-recreate --wait --wait-timeout 180
set -a
. ./.env
set +a
python3 scripts/smoke.py
```

This recreates the stack, so plan for a brief ingestion interruption. Compose
runs the volume-ownership initializer, waits for the new pricing exporter to be
healthy, then starts the collector with its new price files. Grafana restarts to
load provisioned dashboards. A changed bind-mounted file alone does not reliably
update all three components.

For local builds, instead run:

```bash
docker compose up -d --build --force-recreate --wait --wait-timeout 180
```

Do not mix a new collector/dashboard checkout with an older exporter image.
Historical costs are not recalculated; the live exchange rate is not pinned.
To roll back, restore the previous full checkout and its image digest together,
then repeat recreation and smoke checks. Keep volumes unless a release's storage
migration requires restoring a backup: code rollback cannot undo a migration.
Never use `docker compose down -v` for a routine update.

## Operating cost

Each PR runs file/unit checks and two complete native test stacks; each release
reruns them and adds a multi-platform build. This uses Actions runner time, image
pulls and temporary disk space. GHCR keeps each version, so retain digests still
used by consumers when setting cleanup policies. No continuously running service
is added. Test startup needs network access for pinned images and the Grafana
plugin; the live exchange-rate refresh retains its existing external dependency.
