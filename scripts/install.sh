#!/usr/bin/env bash
#
# install.sh — symlink this repo into $HERMES_HOME/plugins/contextpilot-lcm-bridge
#
# Usage:
#   ./scripts/install.sh
#
# HERMES_HOME resolution order:
#   1. $HERMES_HOME            (if set and non-empty)
#   2. $LOCALAPPDATA/hermes    (Windows)
#   3. $HOME/.hermes           (POSIX)
#
# Manual copy fallback (no symlink privileges, or if this script's symlink
# step fails):
#   mkdir -p "$HERMES_HOME/plugins"
#   cp -R . "$HERMES_HOME/plugins/contextpilot-lcm-bridge"
# (PowerShell equivalent: Copy-Item -Recurse "$env:HERMES_HOME\plugins\contextpilot-lcm-bridge")
#
# No hardcoded user or machine paths in this script.

set -euo pipefail

PLUGIN_NAME="contextpilot-lcm-bridge"

# --- Resolve repo root (this script lives in <repo>/scripts/) ----------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# --- Resolve HERMES_HOME ------------------------------------------------------
HERMES_HOME="${HERMES_HOME:-}"
if [[ -z "$HERMES_HOME" ]]; then
  if [[ -n "${LOCALAPPDATA:-}" ]]; then
    HERMES_HOME="$LOCALAPPDATA/hermes"
  elif [[ -n "${HOME:-}" ]]; then
    HERMES_HOME="$HOME/.hermes"
  else
    echo "error: HERMES_HOME is not set and no fallback location could be determined." >&2
    echo "Set HERMES_HOME to your Hermes home directory and re-run this script." >&2
    exit 1
  fi
  echo "note: HERMES_HOME not set; using $HERMES_HOME"
fi

PLUGINS_DIR="$HERMES_HOME/plugins"
TARGET="$PLUGINS_DIR/$PLUGIN_NAME"

# On MSYS/git-bash, use a Windows-style target so native tools (Python,
# Hermes) can follow the symlink; keep the POSIX form everywhere else.
SYMLINK_TARGET="$REPO_ROOT"
if command -v cygpath >/dev/null 2>&1; then
  SYMLINK_TARGET="$(cygpath -w "$REPO_ROOT")"
fi

mkdir -p "$PLUGINS_DIR"

# --- Handle an existing target -------------------------------------------------
if [[ -L "$TARGET" ]]; then
  EXISTING="$(readlink "$TARGET" || true)"
  if [[ "$EXISTING" == "$REPO_ROOT" || "$EXISTING" == "$SYMLINK_TARGET" ]]; then
    echo "already installed: $TARGET -> $EXISTING"
    exit 0
  fi
  echo "error: $TARGET is already a symlink to $EXISTING (not this repo)." >&2
  echo "Remove it manually, then re-run this script." >&2
  exit 1
elif [[ -e "$TARGET" ]]; then
  BACKUP="$TARGET.bak.$(date +%Y%m%d%H%M%S)"
  echo "backing up existing plugin directory to $BACKUP"
  mv "$TARGET" "$BACKUP"
fi

# --- Create the symlink ---------------------------------------------------------
# On MSYS, force a real native symlink (winsymlinks:nativestrict fails loudly
# instead of silently copying, which plain `ln -s` does on some setups).
if command -v cygpath >/dev/null 2>&1; then
  if MSYS=winsymlinks:nativestrict ln -s "$SYMLINK_TARGET" "$TARGET" 2>/dev/null; then
    :
  elif ln -s "$SYMLINK_TARGET" "$TARGET" 2>/dev/null; then
    if [[ ! -L "$TARGET" ]]; then
      # MSYS silently copied instead of symlinking — treat as copy fallback.
      echo "note: native symlinks unavailable; files were copied (manual fallback)."
      echo "installed (copy): $TARGET"
      exit 0
    fi
  else
    echo "warning: could not create symlink (missing privileges?)." >&2
    echo "Manual copy fallback:" >&2
    echo "  mkdir -p \"$PLUGINS_DIR\"" >&2
    echo "  cp -R \"$REPO_ROOT\" \"$TARGET\"" >&2
    exit 1
  fi
else
  ln -s "$SYMLINK_TARGET" "$TARGET"
fi

echo "installed: $TARGET -> $SYMLINK_TARGET"

# --- Next steps -----------------------------------------------------------------
cat <<EOF

Next steps:
  1. Add "$PLUGIN_NAME" to plugins.enabled in your Hermes config.
  2. Set context.engine to "$PLUGIN_NAME".
  3. Restart Hermes (or reload plugins) and verify with: hermes plugins
EOF
