# ContextPilot-LCM Bridge

A thin integration bridge that combines the optimization pipeline of **ContextPilot** with the advanced DAG-based compaction of **hermes-lcm**.

## Overview

The `contextpilot-lcm-bridge` is not a standalone engine, but a subclass that wires together two existing Hermes plugins. It inherits the full telemetry, deduplication, and reordering pipeline from `ContextPilotEngine` but overrides the compaction delegation to use `LCMEngine` from `hermes-lcm` instead of the default `ContextCompressor`.

**Key characteristics:**
- **Zero Upstream Copying:** No code from the upstream plugins is duplicated.
- **Inherited Pipeline:** Maintains all of ContextPilot's per-turn dedup and SDK patching.
- **Advanced Compaction:** Leverages hermes-lcm's retrieval tools for actual context reduction.

## Requirements

This bridge requires the following plugins to be already installed in your Hermes environment:
- `contextpilot`
- `hermes-lcm`

## Installation

### Option 1: Clone + symlink (recommended)

The bridge installs as a git checkout symlinked into the plugins directory, so
updates are a `git reset --hard <tag>` with no copy step and no drift:

```bash
git clone https://github.com/Triquetra/contextpilot-lcm-bridge.git ~/projects/contextpilot-lcm-bridge
ln -s ~/projects/contextpilot-lcm-bridge "$HERMES_HOME/plugins/contextpilot-lcm-bridge"
```

Or run `scripts/install.sh`, which resolves `$HERMES_HOME` and does the same
(backing up an existing directory first).

### Option 2: Manual fallback

Copy the contents of this repository directly into a new folder named
`contextpilot-lcm-bridge` within your Hermes plugins directory:
`$HERMES_HOME/plugins/contextpilot-lcm-bridge/`

> An installed copy does **not** update itself. Note that a manual copy carries
> no `.git`, so `scripts/check_and_update.py` (below) cannot run against it.

## Updating an installed copy

`scripts/check_and_update.py` syncs an installed checkout to the latest
published release of this repository:

1. detects the newest non-prerelease release tag
2. hard-resets the checkout to that tag
3. runs `test_bridge.py` — the authoritative compatibility gate — against the
   plugins **actually installed** in `$HERMES_HOME/plugins`
4. on gate failure, restores the previous revision and reports the first failing
   assertion, so a broken bridge is never left serving a live engine

It refuses to touch a checkout with tracked local modifications, because
`reset --hard` would destroy them.

Exit codes: `0` current or updated, `1` gate failed and rolled back, `2` error.
It is designed to run as a Hermes cron `monitor_script`, so its stdout is
deterministic (stable sentinels, no timestamps): a steady state hashes
identically and stays silent, and only a real update or a gate failure wakes the
agent.

```bash
python3 scripts/check_and_update.py            # sync if a new release exists
python3 scripts/check_and_update.py --dry-run  # report only
```

## Compatibility Table

This bridge is pinned to specific upstream commits to ensure stability. Support for other versions is not guaranteed.

| Component | Pinned Commit | Test Date | Status | Upstream Release Tag |
| :--- | :--- | :--- | :--- | :--- |
| **ContextPilot** | `7f3b62b` | 2026-09-25 | Verified | `v0.5.0` |
| **hermes-lcm** | `854e869` | 2026-08-22 | Verified | none (pinned commit 854e869) |

## Maintenance & Scope

### Scope and Limitations
This is a "glue" plugin. It does not implement its own compression logic; it only manages the delegation between two other engines. If either upstream plugin changes its internal class structure or `ContextEngine` ABC interface, this bridge may break.

### Kill-Switch Policy
Due to the fragile nature of subclassing external plugins, this bridge operates under a **kill-switch policy**:
If an upstream update to `ContextPilot` or `hermes-lcm` introduces a breaking change to the inheritance or delegation pattern, this bridge will be considered deprecated immediately. Users are encouraged to check the compatibility table before updating upstream dependencies.

## License

Distributed under the [MIT License](LICENSE).
