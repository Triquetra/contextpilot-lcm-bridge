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

### Option 1: Symlink (Recommended)
If you are developing or managing plugins via a separate directory, symlink this repository into your Hermes plugins folder:

```bash
ln -s /path/to/contextpilot-lcm-bridge $HERMES_HOME/plugins/contextpilot-lcm-bridge
```

### Option 2: Manual Fallback
Copy the contents of this repository directly into a new folder named `contextpilot-lcm-bridge` within your Hermes plugins directory:
`$HERMES_HOME/plugins/contextpilot-lcm-bridge/`

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
