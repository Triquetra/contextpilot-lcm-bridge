"""Bridge self-test against REAL pinned upstream plugin code.

This is the authoritative compatibility gate. It loads the actual
ContextPilot and hermes-lcm plugin sources (checked out at the commits
pinned in upstream-pins.json) into a temp HERMES_HOME, builds the bridge
subclass against them, and asserts the delegation contract end to end.

Usage:
    python test_bridge.py [--upstream-dir DIR] [--env-dir DIR] [--verify-pins]

  --upstream-dir   Directory containing the `contextpilot/` and `hermes-lcm/`
                   plugin checkouts. Default: $HERMES_HOME/plugins.
  --env-dir        Temp HERMES_HOME to copy the plugins into. Default: a fresh
                   temp dir (deleted on exit).
  --verify-pins    Require each plugin checkout to be a git repo whose HEAD
                   is at (or a descendant of) the pinned commit. CI always
                   passes this; local runs may omit it.
  --expect-commit KEY=SHA
                   Require the plugin checkout for KEY to be exactly at SHA.
                   Repeatable. Used by the release monitor to prove the test
                   ran against the exact release commits it detected.

Exit code 0 = all assertions passed; non-zero = bridge is broken against the
pinned upstreams (or the pins themselves are not checked out).
"""
import argparse
import importlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
PINS = json.loads((REPO_ROOT / "upstream-pins.json").read_text(encoding="utf-8"))

FAILURES: list[str] = []
CHECKS_PASSED = 0


def check(cond: bool, label: str, detail: str = "") -> None:
    global CHECKS_PASSED
    if cond:
        CHECKS_PASSED += 1
        print(f"  ok: {label}")
    else:
        msg = f"FAIL: {label}"
        if detail:
            msg += f" — {detail}"
        print(f"  {msg}")
        FAILURES.append(msg)


# ── Stub the Hermes host `agent` package (ContextEngine ABC) ────────────────
# The real plugins import `from agent.context_engine import ContextEngine`.
# CI has no Hermes install, so provide a faithful minimal ABC. The bridge's
# contract is tested through the real upstream code, not through this stub.
_agent = types.ModuleType("agent")
_ctx_engine = types.ModuleType("agent.context_engine")


class ContextEngine:
    last_prompt_tokens = 0
    last_completion_tokens = 0
    last_total_tokens = 0
    threshold_tokens = 0
    context_length = 0
    compression_count = 0

    def on_session_reset(self):
        pass

    def get_status(self):
        return {}

    def on_session_start(self, *a, **kw):
        pass

    def on_session_end(self, *a, **kw):
        pass

    def update_from_response(self, *a, **kw):
        pass

    def update_model(self, *a, **kw):
        pass

    def should_compress(self, *a, **kw):
        return False

    def get_tool_schemas(self):
        return []

    def handle_tool_call(self, *a, **kw):
        return "{}"

    def on_context_compressed(self, *a):
        pass

    def _sync_compressor_state(self):
        pass


_ctx_engine.ContextEngine = ContextEngine
sys.modules["agent"] = _agent
sys.modules["agent.context_engine"] = _ctx_engine


def _git_head(repo: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return None


def _pin_ok(repo: Path, pin: str) -> tuple[bool, str]:
    head = _git_head(repo)
    if head is None:
        return False, f"{repo.name} is not a git checkout (HEAD unknown)"
    if head == pin:
        return True, f"HEAD == pin {pin[:12]}"
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", pin, "HEAD"],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            return True, f"HEAD {head[:12]} is a descendant of pin {pin[:12]}"
    except Exception:
        pass
    return False, f"HEAD {head[:12]} is NOT at or after pin {pin[:12]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--upstream-dir", default=os.environ.get("HERMES_HOME", ""))
    ap.add_argument("--env-dir", default="")
    ap.add_argument("--verify-pins", action="store_true")
    ap.add_argument("--expect-commit", action="append", default=[])
    args = ap.parse_args()

    upstream_dir = Path(args.upstream_dir or "").resolve()
    if not upstream_dir.is_dir():
        print(f"error: upstream dir not found: {upstream_dir}")
        return 2

    cp_src = upstream_dir / "contextpilot"
    lcm_src = upstream_dir / "hermes-lcm"
    for name, src in (("contextpilot", cp_src), ("hermes-lcm", lcm_src)):
        if not (src / "__init__.py").is_file():
            print(f"error: {name} plugin not found at {src}")
            return 2

    # Exact-commit assertions (release monitor uses these)
    for spec in args.expect_commit:
        key, _, sha = spec.partition("=")
        src = {"contextpilot": cp_src, "hermes-lcm": lcm_src}.get(key)
        if src is None:
            print(f"error: unknown --expect-commit key {key!r}")
            return 2
        head = _git_head(src)
        if head != sha:
            print(f"error: {key} HEAD {head or '(not a git checkout)'} != expected {sha}")
            return 2
        print(f"expect-commit {key}: HEAD == {sha[:12]}")

    # Pin verification (CI always checks out the pins explicitly)
    if args.verify_pins:
        for key, src in (("contextpilot", cp_src), ("hermes-lcm", lcm_src)):
            ok, detail = _pin_ok(src, PINS[key]["commit"])
            print(f"pin {key}: {detail}")
            if not ok:
                print(f"error: {key} checkout does not match pin {PINS[key]['commit']}")
                return 2

    # Build the temp HERMES_HOME with the real plugin sources
    if args.env_dir:
        env_home = Path(args.env_dir).resolve()
        env_home.mkdir(parents=True, exist_ok=True)
        keep = True
    else:
        env_home = Path(tempfile.mkdtemp(prefix="bridge_ci_"))
        keep = False
    plugins_dir = env_home / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    for name, src in (("contextpilot", cp_src), ("hermes-lcm", lcm_src)):
        dst = plugins_dir / name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))
    os.environ["HERMES_HOME"] = str(env_home)
    # Never let ContextPilot try to pip self-install in CI
    os.environ["CONTEXTPILOT_PLUGIN_BOOTSTRAP"] = "1"
    print(f"HERMES_HOME: {env_home}")

    # ── Import the bridge and build the class against REAL upstream code ──
    sys.path.insert(0, str(REPO_ROOT))
    bridge = importlib.import_module("__init__")
    BridgeClass = bridge._build_bridge_class()
    check(BridgeClass is not None, "1. bridge class built from real upstream code")
    if BridgeClass is None:
        print("\nerror: bridge class build failed — see log lines above")
        return 1

    check(BridgeClass.__name__ == "ContextPilotLCMBridge", "2. class name")
    check(BridgeClass.__bases__[0].__name__ == "ContextPilotEngine",
          "3. inherits real ContextPilotEngine", BridgeClass.__bases__[0].__name__)

    engine = BridgeClass()
    check(engine.name == "contextpilot-lcm-bridge", "4. engine name", engine.name)
    check(engine._lcm_active is False, "5. _lcm_active False before compressor init")

    engine._ensure_compressor()
    check(type(engine._compressor).__name__ == "LCMEngine",
          "6. compressor is real LCMEngine", type(engine._compressor).__name__)
    check(engine._lcm_active is True, "7. _lcm_active True after init")

    # ── Threshold / model delegation (real LCM config: 0.35 of context) ──
    engine.update_model("gpt-4", 200000)
    check(engine.context_length == 200000, "8. context_length propagated",
          str(engine.context_length))
    check(engine.threshold_tokens == 70000, "9. threshold_tokens = 0.35 * 200000",
          str(engine.threshold_tokens))
    check(engine.should_compress(40000) is False, "10. should_compress(40000) False")
    check(engine.should_compress(80000) is True, "11. should_compress(80000) True")

    # ── Session lifecycle delegation ──
    engine.on_session_start("ci-session", model="gpt-4", context_length=200000)
    check(engine._compressor.bound_session_id == "ci-session",
          "12. LCM session bound", engine._compressor.bound_session_id)

    # ── Compaction delegation: small backlog is a noop ──
    small = [{"role": "user", "content": f"msg {i}"} for i in range(20)]
    r = engine.compress(small, current_tokens=40000)
    check(len(r) == 20, "13. small compress returns messages unchanged", str(len(r)))
    check(engine.compression_count == 0, "14. no compaction below threshold",
          str(engine.compression_count))
    check(len(engine._seen_doc_hashes) == 0, "15. dedup caches cleared after compress")

    # ── Compaction delegation: large backlog actually compacts ──
    big = [{"role": "user", "content": f"message number {i} " + "y" * 190}
           for i in range(2000)]
    r2 = engine.compress(big, current_tokens=150000, force=True)
    check(len(r2) < len(big), "16. large backlog compacted",
          f"{len(big)} -> {len(r2)} msgs")
    check(engine.compression_count == 1, "17. compression_count incremented",
          str(engine.compression_count))
    check(engine._compressor._last_compression_status == "compacted",
          "18. LCM reports compacted", engine._compressor._last_compression_status)
    check(len(engine._seen_doc_hashes) == 0, "19. dedup caches cleared after compaction")

    # ── Tool surface delegation ──
    schemas = engine.get_tool_schemas()
    names = [s.get("name") for s in schemas]
    # The exact tool set is owned by the pinned upstream LCM engine, so assert
    # the CONTRACT (namespacing, uniqueness, the core tools) instead of a frozen
    # list. A hardcoded list goes red on every upstream release that adds a
    # tool, and that red gets misread as a broken bridge rather than a stale
    # expectation — which is exactly what the v0.5.0 pin bump produced.
    engine_names = [s.get("name") for s in engine._compressor.get_tool_schemas()]
    core_tools = {"lcm_grep", "lcm_recall", "lcm_expand", "lcm_status"}
    check(bool(names) and names == engine_names,
          f"20a. all {len(engine_names)} LCM tools exposed (matches upstream engine)",
          ",".join(names))
    check(core_tools.issubset(set(names)) and len(names) == len(set(names)),
          "20b. core LCM tools present, names unique", ",".join(names))

    import json as _json
    out = engine.handle_tool_call("lcm_grep", {"query": "test"})
    try:
        parsed = _json.loads(out)
        check(parsed.get("query") == "test", "21. lcm_grep delegated (query echoed)")
    except Exception:
        check(False, "21. lcm_grep delegated (JSON response)", out[:120])

    # ── Status merge ──
    status = engine.get_status()
    check(status.get("engine") == "contextpilot-lcm-bridge", "22. status engine name",
          str(status.get("engine")))
    check(status.get("lcm_active") is True, "23. status lcm_active")
    check(status.get("compression_count") == 1, "24. status compression_count",
          str(status.get("compression_count")))
    check("contextpilot_chars_saved" in status, "25. status has ContextPilot metrics")
    check("threshold_tokens" in status, "26. status has LCM metrics")

    # ── Inherited ContextPilot pipeline ──
    msgs2 = [{"role": "user", "content": "hello"}]
    optimized, stats = engine.optimize_api_messages(msgs2, system_content="")
    check(len(optimized) == 1, "27. optimize_api_messages inherited")
    check(isinstance(stats, dict) and "chars_saved" in stats,
          "28. optimize stats dict")

    # ── Reset ──
    engine.on_session_reset()
    check(engine._optimize_count == 0, "29. reset clears ContextPilot counters",
          str(engine._optimize_count))

    # ── register() entry point ──
    class MockCtx:
        def __init__(self):
            self.engine = None

        def register_context_engine(self, e):
            self.engine = e

    ctx = MockCtx()
    bridge.register(ctx)
    check(ctx.engine is not None and ctx.engine.name == "contextpilot-lcm-bridge",
          "30. register() registers the bridge engine")

    # ── Init-order regression: tools must exist BEFORE on_session_start ──
    # Hermes injects engine tools from get_tool_schemas() (agent_init.py:2909)
    # before on_session_start() (:2933) builds the compressor. An inherited
    # implementation returns [] while _compressor is None, silently dropping
    # every lcm_* tool from the tool payload — a failure that is invisible to
    # every check above, because they all run AFTER _ensure_compressor().
    fresh = BridgeClass()
    pre_names = [s.get("name") for s in fresh.get_tool_schemas()]
    check(fresh._compressor is not None,
          "31a. get_tool_schemas() builds LCM before session start")
    check(bool(pre_names),
          "31b. tools exposed on the pre-session call "
          "(the agent-init order that drops them)", ",".join(pre_names) or "(none)")
    pre_call = fresh.handle_tool_call("lcm_status", {})
    try:
        _json.loads(pre_call)
        check(True, "31c. handle_tool_call works pre-session")
    except Exception:
        check(False, "31c. handle_tool_call works pre-session", pre_call[:120])

    if not keep:
        shutil.rmtree(env_home, ignore_errors=True)

    print()
    if FAILURES:
        print(f"=== {len(FAILURES)} bridge test(s) FAILED ===")
        for f in FAILURES:
            print(f"  {f}")
        return 1
    print(f"=== All {CHECKS_PASSED} bridge delegation tests passed "
          f"against pinned upstreams ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
