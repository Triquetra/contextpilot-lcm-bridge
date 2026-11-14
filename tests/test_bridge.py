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

REPO_ROOT = Path(__file__).resolve().parent.parent
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

    # ── Host compressor-contract compliance (pre-existing defect) ──
    # agent_init assigns THIS engine object to agent.context_compressor, but
    # tui_gateway/session_compression.py drives it through ContextCompressor's
    # private interface. Without these two methods every compression-config save
    # raised AttributeError — leaving the engine holding partially-applied host
    # config, after the other keys had already been written.
    engine2 = BridgeClass()
    engine2._ensure_compressor()
    check(engine2._coerce_threshold_tokens_cap(4096) == 4096
          and engine2._coerce_threshold_tokens_cap(0) is None
          and engine2._coerce_threshold_tokens_cap("bogus") is None,
          "32. _coerce_threshold_tokens_cap matches core semantics")
    check(engine2._effective_threshold_percent(100_000, 0.5) == 0.75
          and engine2._effective_threshold_percent(1_000_000, 0.5) == 0.5,
          "33. _effective_threshold_percent small-context floor")

    # ── Host compression-kwargs contract ──
    # The host decides which compaction kwargs to send by inspecting THIS
    # engine's compress() signature (agent/conversation_compression.py::
    # _supported_compression_kwargs). A `**kwargs` signature reads as "accepts
    # everything", so the full candidate set — including kwargs added to the
    # host after the pinned upstreams — arrives here. Blind-forwarding them to
    # LCM (whose compress() stops at `force`) raised
    # `TypeError: CompactionMixin.compress() got an unexpected keyword argument
    # 'bypass_cooldown'` on every provider-proven overflow recovery, which
    # killed the turn instead of compacting.
    engine3 = BridgeClass()
    engine3._ensure_compressor()
    engine3.on_session_start("ci-session-kwargs", model="gpt-4", context_length=200000)
    host_kwargs = dict(
        current_tokens=40000, focus_topic="auth bug", force=False,
        bypass_cooldown=True, memory_context="provider note",
    )
    try:
        out3 = engine3.compress(small, **host_kwargs)
        ok_kw, err_kw = len(out3) == len(small), ""
    except TypeError as exc:
        ok_kw, err_kw = False, f"{type(exc).__name__}: {exc}"
    check(ok_kw,
          "34a. host kwargs beyond LCM's signature (bypass_cooldown/"
          "memory_context) are dropped, not forwarded", err_kw)
    try:
        engine3.compress(small, current_tokens=40000, bogus_future_kwarg=1)
        ok_unknown, err_unknown = True, ""
    except TypeError as exc:
        ok_unknown, err_unknown = False, f"{type(exc).__name__}: {exc}"
    check(ok_unknown, "34b. an unknown host kwarg is dropped too", err_unknown)
    # Accepted kwargs must still reach LCM. Record what the delegation passes;
    # functools.wraps keeps LCM's signature visible to inspect.signature (which
    # follows __wrapped__), so the filter still sees the real contract.
    import functools
    seen_kwargs: dict = {}
    real_compress = engine3._compressor.compress

    @functools.wraps(real_compress)
    def _recording_compress(messages, **kw):
        seen_kwargs.update(kw)
        return real_compress(messages, **kw)

    engine3._compressor.compress = _recording_compress
    engine3.compress(small, current_tokens=40000, force=True, bypass_cooldown=True)
    check(seen_kwargs.get("current_tokens") == 40000
          and seen_kwargs.get("force") is True
          and "bypass_cooldown" not in seen_kwargs,
          "34c. filtering keeps accepted kwargs and drops the rest",
          repr(seen_kwargs))

    # ── agent_init order: the host window must reach LCM ──
    # agent_init gives the engine the real window in update_model() BEFORE
    # get_tool_schemas() builds LCM inside the bridge, then forwards the ENGINE's own
    # context_length into on_session_start(). Building the inner engine late made the
    # inherited _sync_compressor_state adopt its zero window, so LCM kept
    # context_length=0 / threshold_tokens=0 and should_compress() stayed False forever:
    # compaction fired only through forced overflow recovery, AFTER the request already
    # exceeded the model window (live: 1,037,032 tokens on a 1M model, 0.5 threshold).
    # The engine above cannot show this — it builds its compressor first.
    WINDOW = 1_000_000
    engine4 = BridgeClass()
    engine4.update_model(model="deepseek-v4.1-flash", context_length=WINDOW,
                         base_url="https://api.deepseek.com", api_key="x",
                         provider="deepseek")
    engine4.get_tool_schemas()          # builds LCM here, exactly like agent_init
    check(engine4._compressor.context_length == WINDOW,
          "36a. LCM learned the window even though it was built after update_model",
          f"lcm.context_length={engine4._compressor.context_length}")
    window_threshold = int(WINDOW * engine4._compressor.context_threshold)
    check(engine4._compressor.threshold_tokens == window_threshold
          and engine4.should_compress(window_threshold - 1_000) is False
          and engine4.should_compress(window_threshold + 1_000) is True,
          "36b. should_compress triggers at the threshold, not only on overflow",
          f"lcm.threshold_tokens={engine4._compressor.threshold_tokens}")
    # The host forwards OUR context_length into on_session_start (agent_init:2007).
    engine4.on_session_start("ci-session-window", model="deepseek-v4.1-flash",
                             context_length=getattr(engine4, "context_length", 0))
    check(engine4._compressor.context_length == WINDOW,
          "36c. on_session_start keeps the window (host forwards the engine's own value)",
          f"lcm.context_length={engine4._compressor.context_length}")
    # ── The visible knob: compression.threshold written at runtime ──
    engine4.threshold_percent = 0.5
    check(engine4.should_compress(int(WINDOW * 0.45)) is False
          and engine4.should_compress(int(WINDOW * 0.55)) is True,
          "36d. a host compression.threshold change reaches LCM's trigger",
          f"lcm.context_threshold={engine4._compressor.context_threshold} "
          f"threshold_tokens={engine4._compressor.threshold_tokens}")

    # ── Structural guard: the plugin directory's *.py glob contract ──
    # Hermes' plugin loader (plugins/plugin_loader.py:117) executes EVERY
    # `*.py` in a plugin directory — not just `__init__.py` — inside the host
    # process, because the repo root IS the plugin directory. A self-test that
    # installs a stub `agent` module at import time therefore replaces the real
    # `agent` package in the running Hermes interpreter: the bridge then
    # subclasses the stub ABC, every `from agent import ...` fails, and other
    # plugins fail to load ("'agent' is not a package"). Keep test code out of
    # the root — it belongs in a subdirectory, which the loader never globs.
    root_py = sorted(p.name for p in REPO_ROOT.glob("*.py"))
    check(root_py == ["__init__.py"],
          "35. plugin root holds only __init__.py (loader execs every *.py here)",
          f"found: {root_py}")

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
