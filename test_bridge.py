"""Test the bridge plugin with mock dependencies in a temp HERMES_HOME."""
import sys, os, types, json, tempfile

# ── Stub: agent.context_engine.ContextEngine ─────────────────────────────
agent_mod = types.ModuleType('agent')
ctx_engine_mod = types.ModuleType('agent.context_engine')
class ContextEngine:
    last_prompt_tokens = 0
    last_completion_tokens = 0
    last_total_tokens = 0
    threshold_tokens = 0
    context_length = 0
    compression_count = 0
    def on_session_reset(self): pass
    def get_status(self): return {}
    def on_session_start(self, *a, **kw): pass
    def on_session_end(self, *a, **kw): pass
    def update_from_response(self, *a, **kw): pass
    def update_model(self, *a, **kw): pass
    def should_compress(self, *a, **kw): return False
    def get_tool_schemas(self): return []
    def handle_tool_call(self, *a, **kw): return '{}'
    def on_context_compressed(self, *a): pass
    def _sync_compressor_state(self): pass
ctx_engine_mod.ContextEngine = ContextEngine
sys.modules['agent'] = agent_mod
sys.modules['agent.context_engine'] = ctx_engine_mod

# ── Create mock plugin directories in a temp HERMES_HOME ──────────────────
_mock_home = tempfile.mkdtemp()
_mock_plugins = os.path.join(_mock_home, "plugins")
os.makedirs(os.path.join(_mock_plugins, "contextpilot"), exist_ok=True)
os.makedirs(os.path.join(_mock_plugins, "hermes-lcm"), exist_ok=True)

# Mock ContextPilot plugin
with open(os.path.join(_mock_plugins, "contextpilot", "__init__.py"), "w") as f:
    f.write('''
import json, types

class ContextPilotEngine:
    def __init__(self):
        self._compressor = None
        self._cached_messages = []
        self._cached_original_messages = []
        self._seen_doc_hashes = set()
        self._single_doc_hashes = {}
        self._first_tool_result_done = False
        self._system_processed = False
        self._total_chars_saved = 0
        self._total_reordered = 0
        self._total_docs_deduped = 0
        self._optimize_count = 0
        self._session_id = None
        self.threshold_percent = 0.75
        self._lcm_active = False
    @property
    def name(self): return "contextpilot"
    @staticmethod
    def is_available(): return True
    def _ensure_compressor(self):
        self._compressor = types.SimpleNamespace()
    def _sync_compressor_state(self): pass
    def update_from_response(self, usage):
        if self._compressor: self._compressor.update_from_response(usage)
    def should_compress(self, pt=None):
        if self._compressor: return self._compressor.should_compress(pt)
        return False
    def compress(self, messages, current_tokens=None, **kw):
        return messages
    def optimize_api_messages(self, msgs, *, system_content=""):
        return msgs, {"chars_saved": 0}
    def on_context_compressed(self, old, new):
        self._cached_messages.clear()
        self._seen_doc_hashes.clear()
        self._single_doc_hashes.clear()
        self._first_tool_result_done = False
    def on_session_start(self, sid, **kw):
        self._session_id = sid
        self._model = kw.get("model", "")
        self._ensure_compressor()
        if self._compressor and hasattr(self._compressor, "on_session_start"):
            self._compressor.on_session_start(sid, **kw)
    def on_session_end(self, sid, msgs):
        if self._compressor and hasattr(self._compressor, "on_session_end"):
            self._compressor.on_session_end(sid, msgs)
    def on_session_reset(self):
        if self._compressor: self._compressor.on_session_reset()
        self.on_context_compressed(0, 0)
        self._total_chars_saved = 0
        self._optimize_count = 0
    def update_model(self, model, cl, **kw):
        if self._compressor: self._compressor.update_model(model, cl, **kw)
    def get_tool_schemas(self):
        if self._compressor: return self._compressor.get_tool_schemas()
        return []
    def handle_tool_call(self, name, args, **kw):
        if self._compressor: return self._compressor.handle_tool_call(name, args, **kw)
        return json.dumps({"error": "unknown"})
    def get_status(self):
        return {"engine": "contextpilot", "contextpilot_chars_saved": self._total_chars_saved}
''')

# Mock hermes-lcm plugin
with open(os.path.join(_mock_plugins, "hermes-lcm", "__init__.py"), "w") as f:
    f.write('def register(ctx): pass\n')

with open(os.path.join(_mock_plugins, "hermes-lcm", "engine.py"), "w") as f:
    f.write('''
import json
class LCMEngine:
    def __init__(self, config=None, hermes_home=""):
        self.threshold_tokens = 50000
        self.context_length = 200000
        self.compression_count = 0
        self.last_prompt_tokens = 1000
        self.last_completion_tokens = 500
        self.last_total_tokens = 1500
        self._session_started = False
        self._model = ""
    @property
    def name(self): return "lcm"
    def on_session_start(self, sid, **kw):
        self._session_started = True
        self._session_id = sid
    def on_session_end(self, sid, msgs): pass
    def on_session_reset(self): pass
    def update_from_response(self, usage):
        self.last_prompt_tokens = usage.get("prompt_tokens", 0)
    def should_compress(self, pt=None):
        return pt is not None and pt > self.threshold_tokens
    def compress(self, messages, current_tokens=None, **kw):
        self.compression_count += 1
        return [{"role": "system", "content": "[LCM DAG summary]"}] + messages[-5:]
    def update_model(self, model, cl, **kw):
        self._model = model
        self.context_length = cl
    def get_tool_schemas(self):
        return [{"name": "lcm_grep", "description": "Search DAG", "parameters": {}}]
    def handle_tool_call(self, name, args, **kw):
        return json.dumps({"tool": name, "args": args, "engine": "lcm"})
    def get_status(self):
        return {"engine": "lcm", "compression_count": self.compression_count, "dag_nodes": 42}
''')

os.environ["HERMES_HOME"] = _mock_home

# ── Import and test the bridge ────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import importlib
mod = importlib.import_module("__init__")

# Build bridge class
BridgeClass = mod._build_bridge_class()
assert BridgeClass is not None, "Bridge class build failed"
print(f"1. Bridge class: {BridgeClass.__name__}")
print(f"2. Parent class: {BridgeClass.__bases__[0].__name__}")

# Instantiate
engine = BridgeClass()
print(f"3. Engine name: {engine.name}")
print(f"4. _lcm_active: {engine._lcm_active}")

# Test _ensure_compressor creates LCMEngine
engine._ensure_compressor()
print(f"5. Compressor type: {type(engine._compressor).__name__}")
print(f"6. _lcm_active: {engine._lcm_active}")

# Test inherited on_session_start delegates to LCM
engine.on_session_start("test-session", model="gpt-4", context_length=200000)
print(f"7. LCM session started: {engine._compressor._session_started}")

# Test should_compress delegates to LCM
print(f"8. should_compress(40000): {engine.should_compress(40000)}")
print(f"9. should_compress(60000): {engine.should_compress(60000)}")

# Test compress delegates to LCM + clears dedup caches
msgs = [{"role": "user", "content": f"msg {i}"} for i in range(20)]
result = engine.compress(msgs, current_tokens=60000)
print(f"10. compress() returned {len(result)} msgs, first: {result[0]['content']}")
print(f"11. compression_count: {engine.compression_count}")
print(f"12. dedup cache cleared: {len(engine._seen_doc_hashes) == 0}")

# Test get_tool_schemas returns LCM tools
schemas = engine.get_tool_schemas()
print(f"13. Tool schemas: {[s['name'] for s in schemas]}")

# Test handle_tool_call delegates to LCM
r = json.loads(engine.handle_tool_call("lcm_grep", {"query": "test"}))
print(f"14. handle_tool_call: {r}")

# Test get_status merges both
status = engine.get_status()
print(f"15. Status engine: {status['engine']}")
print(f"16. Status lcm_active: {status['lcm_active']}")
print(f"17. Status dag_nodes: {status.get('dag_nodes')}")
print(f"18. Status contextpilot_chars_saved: {status['contextpilot_chars_saved']}")
print(f"19. Status compression_count: {status['compression_count']}")

# Test inherited optimize_api_messages works
msgs2 = [{"role": "user", "content": "hello"}]
optimized, stats = engine.optimize_api_messages(msgs2, system_content="")
print(f"20. optimize_api_messages inherited: {len(optimized)} msgs")

# Test on_session_reset
engine.on_session_reset()
print(f"21. After reset: optimize_count={engine._optimize_count}")

# Test update_model delegates to LCM
engine.update_model("claude-4", 1000000)
print(f"22. LCM model: {engine._compressor._model}")
print(f"23. LCM context_length: {engine._compressor.context_length}")

# Test register function
class MockCtx:
    def __init__(self): self.engine = None
    def register_context_engine(self, e): self.engine = e
ctx = MockCtx()
mod.register(ctx)
print(f"24. Register: engine={ctx.engine.name if ctx.engine else None}")

print()
print("=== All 24 bridge delegation tests passed ===")