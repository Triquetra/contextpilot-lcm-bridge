# GitHub Actions Workflows

## bridge-self-test.yml

The authoritative compatibility gate — there is no local watchdog.

- **Triggers:** push to `master`, pull requests, weekly schedule (Mon 06:00 UTC), manual dispatch.
- **Runners:** `ubuntu-latest` and `windows-latest`.
- **What it does:** checks out the bridge, resolves the pinned upstream commits from
  `upstream-pins.json` (via `scripts/ci_pins.py`), checks out `EfficientContext/ContextPilot`
  and `stephenschoettler/hermes-lcm` at exactly those commits, then runs
  `test_bridge.py --upstream-dir upstream --verify-pins`.
- **Failure means:** the bridge no longer works against the pinned upstreams — fix or
  re-pin before merging.

## upstream-release-monitor.yml

Daily upstream release watcher (06:00 UTC, plus manual dispatch).

- Queries the GitHub API for the newest non-draft, non-prerelease release of both upstreams.
- A release is "new" when the pinned commit does not already cover it (pin is not a
  descendant of the release commit and the release is newer than the pin).
- Clones both upstreams at the new release commits and runs `test_bridge.py` against them.
- **Pass path:** patch-bumps `plugin.yaml`, updates `upstream-pins.json`, commits, tags
  `vX.Y.Z`, pushes, and creates a GitHub release noting the new upstream compatibility.
- **Fail path:** opens (or comments on) the issue "Upstream release breaks bridge" with
  per-upstream isolation results. The workflow run is intentionally red so the failure is
  visible.
- **No-op:** when pins already cover the latest releases, the run exits 0 with a clear
  "pins are current" message.

All API calls use the built-in `GITHUB_TOKEN`; no secrets are hardcoded.
