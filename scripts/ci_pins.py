"""Print a pinned upstream commit from upstream-pins.json.

Usage: python scripts/ci_pins.py <contextpilot|hermes-lcm>
"""
import json
import sys
from pathlib import Path

pins = json.loads((Path(__file__).resolve().parent.parent / "upstream-pins.json").read_text(encoding="utf-8"))
key = sys.argv[1] if len(sys.argv) > 1 else ""
if key not in pins:
    sys.exit(f"error: unknown upstream {key!r}; expected one of {sorted(pins)}")
print(pins[key]["commit"])
