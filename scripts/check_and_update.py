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

Two hazards this script is built around, both observed in practice:

  * **Never move backwards.** GitHub's releases list is eventually consistent
    and its order is positional, not semantic, so a naive `releases[0]` can
    return an OLDER release than the one installed — which silently downgrades
    a working install (and, because this script lives inside the tree it
    resets, deletes the updater itself). Candidates are collected from both
    `/releases/latest` and the list, reduced to the highest SEMVER, and then
    applied only if the target is a strict descendant of the installed
    revision.

  * **A steady state must not flicker.** The "up to date" line reports the
    INSTALLED revision, so it stays identical whether or not the releases API
    has caught up yet.

Exit codes:
  0  already current, or updated cleanly
  1  update applied but the gate failed (previous revision restored)
  2  error (repo/API/git failure)

Usage:
  python3 scripts/check_and_update.py [--repo OWNER/REPO] [--repo-dir DIR]
                                      [--upstream-dir DIR] [--dry-run]
                                      [--tag vX.Y.Z]
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_REPO = "Triquetra/contextpilot-lcm-bridge"

# Stable tokens — the monitor hashes this output, so these strings must not
# carry anything that varies between ticks while state is unchanged.
SENTINEL_CURRENT = "BRIDGE_UP_TO_DATE"
SENTINEL_UPDATED = "BRIDGE_UPDATED"
SENTINEL_GATE_FAIL = "BRIDGE_UPDATE_GATE_FAILED"
SENTINEL_ERROR = "BRIDGE_UPDATE_CHECK_ERROR"

GIT_TIMEOUT = 120
GATE_TIMEOUT = 900
API_TIMEOUT = 30


def semver(tag: str) -> tuple:
    """Sort key for `vX.Y.Z` tags. Unparseable tags sort below everything."""
    core = tag.lstrip("vV").split("-")[0].split("+")[0]
    parts = core.split(".")
    try:
        return (0, tuple(int(p) for p in parts))
    except ValueError:
        return (-1, ())


def _api_get(url: str) -> Any:
    req = urllib.request.Request(url)
    token = os.environ.get("GITHUB_TOKEN", "")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    with urllib.request.urlopen(req, timeout=API_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def released_tags(repo: str) -> list:
    """Every published (non-draft, non-prerelease) tag, from BOTH endpoints.

    The list endpoint is eventually consistent and positionally ordered; the
    `latest` endpoint can also briefly lag. Unioning them and taking the
    highest semver means a fresh release is picked up as soon as either
    endpoint reports it, and an older release can never win on position.
    """
    tags = []
    errors = []
    try:
        rels = _api_get(f"https://api.github.com/repos/{repo}/releases?per_page=100")
        for rel in rels:
            if not rel.get("draft") and not rel.get("prerelease"):
                tags.append(rel["tag_name"])
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
        errors.append(f"list: {e}")
    try:
        rel = _api_get(f"https://api.github.com/repos/{repo}/releases/latest")
        if rel.get("tag_name"):
            tags.append(rel["tag_name"])
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as e:
        errors.append(f"latest: {e}")
    if not tags:
        if errors:
            raise RuntimeError("; ".join(errors))
        return []
    return sorted(set(tags), key=semver)


class Repo:
    def __init__(self, root: Path):
        self.root = root

    def git(self, *args, timeout=GIT_TIMEOUT):
        return subprocess.run(["git", *args], cwd=self.root, capture_output=True,
                              text=True, timeout=timeout)

    def head(self) -> str:
        r = self.git("rev-parse", "HEAD")
        if r.returncode != 0:
            raise RuntimeError(f"not a git checkout: {r.stderr.strip()}")
        return r.stdout.strip()

    def resolve(self, ref: str):
        r = self.git("rev-parse", f"{ref}^{{commit}}")
        return r.stdout.strip() if r.returncode == 0 else None

    def is_ancestor(self, ref: str, of: str) -> bool:
        return self.git("merge-base", "--is-ancestor", ref, of).returncode == 0

    def describe(self) -> str:
        """Nearest exact tag at HEAD, else plugin.yaml's version, else short sha."""
        r = self.git("describe", "--tags", "--exact-match", "HEAD")
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        yml = self.root / "plugin.yaml"
        if yml.is_file():
            for ln in yml.read_text(encoding="utf-8").splitlines():
                if ln.startswith("version:"):
                    return "v" + ln.split(":", 1)[1].strip().strip('"').strip("'")
        return self.head()[:7]


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


def run_gate(repo: "Repo", upstream_dir: Path):
    script = repo.root / "test_bridge.py"
    if not script.is_file():
        return 2, f"FAIL: {script} is missing from the release"
    r = subprocess.run(
        [sys.executable, str(script), "--upstream-dir", str(upstream_dir)],
        cwd=repo.root, capture_output=True, text=True, timeout=GATE_TIMEOUT,
    )
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--repo-dir", default="",
                    help="the bridge git checkout (default: this script's repo)")
    ap.add_argument("--upstream-dir", default="",
                    help="dir holding the installed contextpilot/ and hermes-lcm/ "
                         "(default: $HERMES_HOME/plugins)")
    ap.add_argument("--dry-run", action="store_true",
                    help="detect and report, but do not reset the checkout")
    ap.add_argument("--tag", default="", help="override the release tag to sync to")
    args = ap.parse_args()

    repo = Repo(Path(args.repo_dir).resolve() if args.repo_dir
                else Path(__file__).resolve().parent.parent)
    upstream_dir = Path(args.upstream_dir) if args.upstream_dir else (
        resolve_hermes_home() / "plugins")

    if not upstream_dir.is_dir():
        print(f"{SENTINEL_ERROR} upstream dir not found: {upstream_dir}")
        return 2

    try:
        installed = repo.head()
    except Exception as e:
        print(f"{SENTINEL_ERROR} {e}")
        return 2

    if args.tag:
        candidates = [args.tag]
    else:
        try:
            candidates = released_tags(args.repo)
        except (urllib.error.URLError, OSError, ValueError, RuntimeError) as e:
            print(f"{SENTINEL_ERROR} release lookup failed: {e}")
            return 2
    if not candidates:
        print(f"{SENTINEL_ERROR} no published release found in {args.repo}")
        return 2
    tag = candidates[-1]  # highest semver

    if repo.git("fetch", "--tags", "--force", "origin").returncode != 0:
        print(f"{SENTINEL_ERROR} git fetch --tags failed")
        return 2
    target = repo.resolve(tag)
    if target is None:
        print(f"{SENTINEL_ERROR} tag {tag} not found after fetch")
        return 2

    # Never move backwards: the installed revision is at or ahead of the newest
    # release (this also absorbs an API that has not caught up yet). Report the
    # INSTALLED revision so the monitor's steady state cannot flicker.
    if installed == target or repo.is_ancestor(target, installed):
        print(f"{SENTINEL_CURRENT} {repo.describe()} ({installed[:7]})")
        return 0

    # Only TRACKED modifications would be destroyed by reset --hard; untracked
    # files survive it untouched, so they must not block an update.
    dirty = repo.git("status", "--porcelain", "--untracked-files=no").stdout.strip()
    if dirty and not args.dry_run:
        # Local edits would be destroyed by the reset — never clobber silently.
        print(f"{SENTINEL_ERROR} checkout has local modifications; refusing to "
              f"overwrite: {dirty.splitlines()[0]}")
        return 2

    if args.dry_run:
        print(f"{SENTINEL_UPDATED} (dry-run) would move {installed[:7]} -> {tag}")
        return 0

    # ── Apply the update ──
    if repo.git("reset", "--hard", target).returncode != 0:
        print(f"{SENTINEL_ERROR} git reset --hard {tag} failed")
        return 2

    rc, output = run_gate(repo, upstream_dir)
    if rc == 0:
        print(f"{SENTINEL_UPDATED} {installed[:7]} -> {tag} ({target[:7]}); "
              f"compatibility gate passed; restart or reload plugins to load it")
        return 0

    # Gate failed: restore what was working, keep the failure visible.
    if repo.git("reset", "--hard", installed).returncode != 0:
        print(f"{SENTINEL_ERROR} gate failed AND restore to {installed[:7]} failed "
              f"— checkout is now at {tag}. Restore manually.")
        return 2
    first_fail = ""
    for ln in output.splitlines():
        if ln.strip().startswith("FAIL:"):
            first_fail = ln.strip()
            break
    print(f"{SENTINEL_GATE_FAIL} {tag} broke the compatibility gate; reverted "
          f"to {installed[:7]}. First failure: {first_fail or '(see gate output)'}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
