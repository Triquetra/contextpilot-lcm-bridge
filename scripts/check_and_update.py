#!/usr/bin/env python3
"""Self-updater for the ContextPilot-LCM bridge.

The bridge is installed as a git clone symlinked into $HERMES_HOME/plugins/, so
updating it is `git reset --hard <release-tag>` — no copy step, no drift.

Detects the latest published release of the bridge repo, and when it differs
from the installed checkout:

  1. fetches tags and hard-resets the checkout to the release tag
  2. runs the repo's authoritative compatibility gate (test_bridge.py) against
     the plugins ACTUALLY INSTALLED in $HERMES_HOME/plugins
  3. on gate FAILURE, restores the previous revision and reports loudly — a
     broken bridge must not be left serving a live engine
  4. on success, reports the version transition

Intended to run as a Hermes cron `monitor_script`: stdout is hashed each tick
and the agent is woken only when the output CHANGES. Output is therefore
deterministic — no timestamps, no volatile counters — so a steady state stays
silent and only a real update (or a gate failure) wakes the agent.

Exit codes:
  0  already current, or updated cleanly
  1  update applied but the gate failed (previous revision restored)
  2  error (repo/API/git failure)

Usage:
  python3 scripts/check_and_update.py [--repo OWNER/REPO] [--upstream-dir DIR]
                                      [--dry-run] [--tag vX.Y.Z]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEST_SCRIPT = REPO_ROOT / "test_bridge.py"
DEFAULT_REPO = "Triquetra/contextpilot-lcm-bridge"

# Stable tokens — the monitor hashes this output, so these strings must not
# carry anything that varies between ticks while state is unchanged.
SENTINEL_CURRENT = "BRIDGE_UP_TO_DATE"
SENTINEL_UPDATED = "BRIDGE_UPDATED"
SENTINEL_GATE_FAIL = "BRIDGE_UPDATE_GATE_FAILED"
SENTINEL_ERROR = "BRIDGE_UPDATE_CHECK_ERROR"


def resolve_hermes_home() -> Path:
    """Same precedence the bridge itself uses: hermes_constants, env, default."""
    try:
        from hermes_constants import get_hermes_home  # type: ignore
        return Path(str(get_hermes_home()))
    except ImportError:
        pass
    val = os.environ.get("HERMES_HOME", "").strip()
    if val:
        return Path(val)
    return Path.home() / ".hermes"


def git(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True,
                          text=True, timeout=timeout)


def head() -> str:
    r = git("rev-parse", "HEAD")
    if r.returncode != 0:
        raise RuntimeError(f"not a git checkout: {r.stderr.strip()}")
    return r.stdout.strip()


def latest_release_tag(repo: str) -> str | None:
    """Newest non-draft, non-prerelease tag, or None if the repo has none."""
    url = f"https://api.github.com/repos/{repo}/releases?per_page=100"
    req = urllib.request.Request(url)
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    with urllib.request.urlopen(req, timeout=30) as resp:
        releases = json.loads(resp.read().decode("utf-8"))
    for rel in releases:
        if not rel.get("draft") and not rel.get("prerelease"):
            return rel["tag_name"]
    return None


def run_gate(upstream_dir: Path) -> tuple[int, str]:
    r = subprocess.run(
        [sys.executable, str(TEST_SCRIPT), "--upstream-dir", str(upstream_dir)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=900,
    )
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--upstream-dir", default="",
                    help="dir holding the installed contextpilot/ and hermes-lcm/ "
                         "(default: $HERMES_HOME/plugins)")
    ap.add_argument("--dry-run", action="store_true",
                    help="detect and report, but do not reset the checkout")
    ap.add_argument("--tag", default="", help="override the release tag to sync to")
    args = ap.parse_args()

    upstream_dir = Path(args.upstream_dir) if args.upstream_dir else (
        resolve_hermes_home() / "plugins")
    if not upstream_dir.is_dir():
        print(f"{SENTINEL_ERROR} upstream dir not found: {upstream_dir}")
        return 2

    try:
        installed = head()
    except Exception as e:
        print(f"{SENTINEL_ERROR} {e}")
        return 2

    try:
        tag = args.tag or latest_release_tag(args.repo)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"{SENTINEL_ERROR} release lookup failed: {e}")
        return 2
    if not tag:
        print(f"{SENTINEL_ERROR} no published release found in {args.repo}")
        return 2

    if git("fetch", "--tags", "--force", "origin").returncode != 0:
        print(f"{SENTINEL_ERROR} git fetch --tags failed")
        return 2
    r = git("rev-parse", f"{tag}^{{commit}}")
    if r.returncode != 0:
        print(f"{SENTINEL_ERROR} tag {tag} not found after fetch")
        return 2
    target = r.stdout.strip()

    # Only TRACKED modifications would be destroyed by reset --hard; untracked
    # files survive it untouched, so they must not block an update.
    dirty = git("status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty and not args.dry_run:
        # Local edits would be destroyed by the reset — never clobber silently.
        print(f"{SENTINEL_ERROR} checkout has local modifications; refusing to "
              f"overwrite: {dirty.splitlines()[0]}")
        return 2

    if installed == target:
        print(f"{SENTINEL_CURRENT} {tag} ({installed[:7]})")
        return 0

    if args.dry_run:
        print(f"{SENTINEL_UPDATED} (dry-run) would move {installed[:7]} -> {tag}")
        return 0

    # ── Apply the update ──
    if git("reset", "--hard", target).returncode != 0:
        print(f"{SENTINEL_ERROR} git reset --hard {tag} failed")
        return 2

    rc, output = run_gate(upstream_dir)
    if rc == 0:
        print(f"{SENTINEL_UPDATED} {installed[:7]} -> {tag} ({target[:7]}); "
              f"compatibility gate passed; restart or reload plugins to load it")
        return 0

    # Gate failed: restore what was working, keep the failure visible.
    if git("reset", "--hard", installed).returncode != 0:
        print(f"{SENTINEL_ERROR} gate failed AND restore to {installed[:7]} failed "
              f"— checkout is now at {tag}. Restore manually.")
        return 2
    last_fail = ""
    for line in output.splitlines():
        if line.strip().startswith("FAIL:"):
            last_fail = line.strip()
    print(f"{SENTINEL_GATE_FAIL} {tag} broke the compatibility gate; reverted "
          f"to {installed[:7]}. First failure: {last_fail or '(see gate output)'}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
