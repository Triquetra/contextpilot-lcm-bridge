"""ContextPilot-LCM Bridge — a thin subclass that combines both plugins.

Inherits ContextPilotEngine (per-turn dedup/reorder/telemetry/SDK patching)
and overrides only the compaction delegation to use LCMEngine instead of the
built-in ContextCompressor. All of ContextPilot's optimization pipeline is
inherited untouched. All of hermes-lcm's DAG compaction and retrieval tools
are delegated to directly.

Zero lines of upstream code copied. When either plugin updates, the
improvements flow through automatically as long as the ContextEngine ABC
interface stays stable.

Requires: contextpilot and hermes-lcm plugins already installed in Hermes.
"""

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger("contextpilot_lcm_bridge")

# ── Import ContextPilotEngine from the sibling contextpilot plugin ──────────
# Hermes loads plugins under the hermes_plugins namespace. When this plugin's
# __init__.py runs, sibling plugins may or may not be loaded yet, so we try
# the namespace import first and fall back to loading from the plugin directory.

ContextPilotEngine = None


def _resolve_hermes_home():
    """Resolve HERMES_HOME, handling both Windows-native and MSYS paths."""
    # Try Hermes's own resolver first (correct in all cases)
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except ImportError:
        pass
    # Fall back to env var
    val = os.environ.get("HERMES_HOME", "").strip()
    if val:
        p = Path(val)
        if p.exists():
            return str(p)
        # On MSYS/bash, /c/... paths need conversion to Windows drive form
        if val.startswith("/c/"):
            win_path = "C:\\" + val[3:].replace("/", "\\")
            if Path(win_path).exists():
                return win_path
        return val
    # Final fallback: platform default
    return str(Path.home() / ".hermes")


def _import_contextpilot_engine():
    """Import ContextPilotEngine from the installed contextpilot plugin.

    The plugin is NOT enabled as a context engine (the bridge is the only
    enabled context plugin). We load ContextPilot directly from its plugin
    directory as a standalone module — its __init__.py uses _load_submodule()
    for its own subpackages, so it works without being loaded as a package.
    """
    hermes_home = _resolve_hermes_home()
    cp_dir = Path(hermes_home) / "plugins" / "contextpilot"
    if not cp_dir.exists():
        logger.error(
            "[contextpilot-lcm] contextpilot plugin not found at %s", cp_dir
        )
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "contextpilot_plugin", str(cp_dir / "__init__.py"),
        submodule_search_locations=[str(cp_dir)],
    )
    if spec and spec.loader:
        mod = importlib.util.module_from_spec(spec)
        sys.modules["contextpilot_plugin"] = mod
        spec.loader.exec_module(mod)
        return getattr(mod, "ContextPilotEngine", None)
    return None


# ── Import LCMEngine from the sibling hermes-lcm plugin ─────────────────────

def _import_lcm_engine():
    """Import LCMEngine from the installed hermes-lcm plugin.

    The plugin is NOT enabled as a context engine (the bridge is the only
    enabled context plugin). We load it directly from its plugin directory
    as a proper package so relative imports (from .config, from .dag, etc.)
    resolve correctly.
    """
    hermes_home = _resolve_hermes_home()
    lcm_dir = Path(hermes_home) / "plugins" / "hermes-lcm"
    if not lcm_dir.exists():
        logger.error(
            "[contextpilot-lcm] hermes-lcm plugin not found at %s", lcm_dir
        )
        return None
    import importlib.util
    # Register the plugin dir as a package so `from .config import ...` works
    # inside engine.py. The package name must not collide with anything on sys.path.
    pkg_name = "hermes_lcm_bridge_loaded"
    spec = importlib.util.spec_from_file_location(
        pkg_name, str(lcm_dir / "__init__.py"),
        submodule_search_locations=[str(lcm_dir)],
    )
    if not (spec and spec.loader):
        return None
    pkg_mod = importlib.util.module_from_spec(spec)
    sys.modules[pkg_name] = pkg_mod
    pkg_mod.__path__ = [str(lcm_dir)]
    spec.loader.exec_module(pkg_mod)
    # Now import the engine submodule from the loaded package
    try:
        engine_spec = importlib.util.spec_from_file_location(
            f"{pkg_name}.engine", str(lcm_dir / "engine.py"),
        )
        engine_mod = importlib.util.module_from_spec(engine_spec)
        engine_mod.__package__ = pkg_name
        sys.modules[f"{pkg_name}.engine"] = engine_mod
        engine_spec.loader.exec_module(engine_mod)
        return getattr(engine_mod, "LCMEngine", None)
    except Exception as e:
        logger.error("[contextpilot-lcm] Failed to import LCMEngine: %s", e)
        return None


# ── Bridge engine ────────────────────────────────────────────────────────────

# Defer the class definition so __init__.py can be imported even if the
# sibling plugins aren't loaded yet (Hermes loads plugins in arbitrary order).
# The actual subclass is built at register() time.

_BridgeClass = None


def _build_bridge_class():
    """Build the bridge subclass, importing both dependencies."""
    CPE = _import_contextpilot_engine()
    if CPE is None:
        logger.error(
            "[contextpilot-lcm] ContextPilot plugin not found — "
            "install it first: hermes plugins install EfficientContext/ContextPilot"
        )
        return None

    LCMEngine = _import_lcm_engine()
    if LCMEngine is None:
        logger.error(
            "[contextpilot-lcm] hermes-lcm plugin not found — "
            "install it first: hermes plugins install stephenschoettler/hermes-lcm"
        )
        return None

    class ContextPilotLCMBridge(CPE):
        """ContextPilot + hermes-lcm bridge.

        Inherits all of ContextPilotEngine (optimize_api_messages, sanitizer
        hook, OpenAI SDK patching, telemetry, prefix replay, dedup, reorder)
        and overrides only the compaction delegation to use LCMEngine.
        """

        @property
        def name(self):
            return "contextpilot-lcm-bridge"

        def __init__(self):
            # Don't call super().__init__() — it sets _compressor = None and
            # ContextPilot's fields. We replicate that here (same fields) but
            # add our _lcm_active flag.
            super().__init__()
            self._lcm_active = False

        def _ensure_compressor(self):
            """Override: create LCMEngine instead of ContextCompressor."""
            if self._compressor is not None:
                return

            hermes_home = os.environ.get("HERMES_HOME", "")
            try:
                self._compressor = LCMEngine(config=None, hermes_home=hermes_home)
                self._lcm_active = True
                logger.info("[contextpilot-lcm] LCMEngine initialized for compaction")
            except Exception as e:
                logger.warning(
                    "[contextpilot-lcm] LCMEngine init failed (%s), "
                    "falling back to built-in ContextCompressor", e,
                )
                # Fall back to parent's _ensure_compressor (creates ContextCompressor)
                self._lcm_active = False
                super()._ensure_compressor()
                return

            self._sync_compressor_state()

        def compress(self, messages, current_tokens=None, **kwargs):
            """Delegate to LCM, then clear ContextPilot's stale dedup caches."""
            self._ensure_compressor()
            result = self._compressor.compress(
                messages, current_tokens=current_tokens, **kwargs,
            )
            self.compression_count = getattr(self._compressor, "compression_count", 0)
            # After compaction, message structure changed — clear dedup state.
            self.on_context_compressed(0, len(result))
            return result

        def get_status(self):
            """Merge LCM's rich status with ContextPilot's savings metrics."""
            if self._compressor and hasattr(self._compressor, "get_status"):
                status = self._compressor.get_status()
            else:
                status = super().get_status()
            status["engine"] = "contextpilot-lcm-bridge"
            status["lcm_active"] = getattr(self, "_lcm_active", False)
            status["contextpilot_chars_saved"] = getattr(self, "_total_chars_saved", 0)
            status["contextpilot_docs_reordered"] = getattr(self, "_total_reordered", 0)
            status["contextpilot_docs_deduped"] = getattr(self, "_total_docs_deduped", 0)
            status["contextpilot_optimize_count"] = getattr(self, "_optimize_count", 0)
            return status

    return ContextPilotLCMBridge


def register(ctx):
    """Hermes plugin entry point."""
    global _BridgeClass
    if _BridgeClass is None:
        _BridgeClass = _build_bridge_class()
    if _BridgeClass is None:
        logger.error("[contextpilot-lcm] Bridge not built — dependencies missing")
        return
    engine = _BridgeClass()
    ctx.register_context_engine(engine)
    logger.info("[contextpilot-lcm] Bridge registered as context engine")