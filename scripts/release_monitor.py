#!/usr/bin/env python3
"""Upstream release monitor for the ContextPilot-LCM bridge.

Detects NEW versioned releases of EfficientContext/ContextPilot and
stephenschoettler/hermes-lcm, re-tests the bridge against them, and:

  - on PASS: bumps the bridge version (patch), updates upstream-pins.json,
    commits, tags a new bridge release, pushes, and creates a GitHub release
    noting the new upstream compatibility.
  - on FAIL: opens (or comments on) a GitHub issue titled
    "Upstream release breaks bridge", with per-upstream isolation results.

CI is the authoritative gate — there is no local watchdog. This script is
invoked daily by .github/workflows/upstream-release-monitor.yml.

Usage:
  python scripts/release_monitor.py [--dry-run] [--repo OWNER/REPO]

Exit codes:
  0  no new releases (pins current) or pass path completed
  1  fail path completed (issue opened) — run is intentionally red
  2  error (API failure, clone failure, dirty tree, ...)
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PINS_PATH = REPO_ROOT / "upstream-pins.json"
PLUGIN_YAML_PATH = REPO_ROOT / "plugin.yaml"
README_PATH = REPO_ROOT / "README.md"
TEST_SCRIPT = REPO_ROOT / "tests" / "test_bridge.py"

UPSTREAMS = {
    "contextpilot": {
        "repo": "EfficientContext/ContextPilot",
        "display": "ContextPilot",
    },
    "hermes-lcm": {
        "repo": "stephenschoettler/hermes-lcm",
        "display": "hermes-lcm",
    },
}

API_BASE = "https://api.github.com"
TOKEN = os.environ.get("GITHUB_TOKEN", "")


# ── GitHub REST helpers (stdlib only; GITHUB_TOKEN optional for reads) ──────

def api_get(path: str) -> dict:
    req = urllib.request.Request(API_BASE + path)
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def api_post(path: str, payload: dict) -> dict:
    req = urllib.request.Request(API_BASE + path, method="POST")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data=json.dumps(payload).encode("utf-8"), timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── Detection ───────────────────────────────────────────────────────────────

def latest_release(repo: str) -> dict | None:
    """Newest non-draft, non-prerelease release, or None."""
    releases = api_get(f"/repos/{repo}/releases?per_page=100")
    for r in releases:
        if not r.get("draft") and not r.get("prerelease"):
            return r
    return None


def release_commit(repo: str, tag: str) -> str:
    """Resolve a release tag to its commit SHA, dereferencing annotated tags."""
    ref = api_get(f"/repos/{repo}/git/refs/tags/{tag}")
    obj = ref["object"]
    if obj["type"] == "commit":
        return obj["sha"]
    if obj["type"] == "tag":
        tag_obj = api_get(f"/repos/{repo}/git/tags/{obj['sha']}")
        return tag_obj["object"]["sha"]
    raise RuntimeError(f"unexpected tag object type {obj['type']} for {repo}@{tag}")


def commit_date(repo: str, sha: str) -> str:
    c = api_get(f"/repos/{repo}/commits/{sha}")
    return c["commit"]["committer"]["date"]


def compare_status(repo: str, base: str, head: str) -> str:
    c = api_get(f"/repos/{repo}/compare/{base}...{head}")
    return c.get("status", "")


def detect_new_release(key: str, pin: dict) -> dict | None:
    """Return release info if the pinned commit does not already cover the
    latest release; None otherwise.

    A release is covered when:
      - it is the exact release already recorded in the pin, or
      - the pinned commit is a descendant of (or equal to) the release commit
        (compare status 'behind'/'equal' — the pin is newer than the release),
        or
      - the release was published before the pinned commit's own date
        (the pin is newer than the release, so the release is stale).
    """
    repo = UPSTREAMS[key]["repo"]
    rel = latest_release(repo)
    if rel is None:
        return None
    tag = rel["tag_name"]
    if pin.get("release") == tag:
        return None
    sha = release_commit(repo, tag)
    status = compare_status(repo, pin["commit"], sha)
    if status in ("behind", "equal"):
        return None
    published = rel["published_at"]
    pin_date = commit_date(repo, pin["commit"])
    if published <= pin_date:
        return None
    return {
        "tag": tag,
        "commit": sha,
        "published_at": published,
        "name": rel.get("name") or tag,
    }


# ── Test execution ──────────────────────────────────────────────────────────

def clone_upstreams(workdir: Path, commits: dict) -> None:
    """Clone each upstream and check out the exact commit (blob:none filter).

    Clone base is overridable via BRIDGE_UPSTREAM_CLONE_BASE (default
    https://github.com) — used by tests and mirror setups.
    """
    base = os.environ.get("BRIDGE_UPSTREAM_CLONE_BASE", "https://github.com")
    for key, sha in commits.items():
        repo = UPSTREAMS[key]["repo"]
        dst = workdir / key
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        try:
            subprocess.run(
                ["git", "clone", "--quiet", "--filter=blob:none",
                 f"{base}/{repo}.git", str(dst)],
                check=True, capture_output=True, text=True, timeout=600,
            )
            subprocess.run(
                ["git", "-C", str(dst), "fetch", "--quiet", "origin", sha],
                check=True, capture_output=True, text=True, timeout=300,
            )
            subprocess.run(
                ["git", "-C", str(dst), "checkout", "--quiet", sha],
                check=True, capture_output=True, text=True, timeout=300,
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(
                f"clone/checkout failed for {key}@{sha[:12]}: "
                f"{(e.stderr or e.stdout or '').strip()[-800:]}"
            ) from e


def run_test(upstream_dir: Path, expect: dict) -> tuple[int, str]:
    """Run test_bridge.py against the checkouts, asserting exact commits."""
    cmd = [sys.executable, str(TEST_SCRIPT), "--upstream-dir", str(upstream_dir)]
    for key, sha in expect.items():
        cmd += ["--expect-commit", f"{key}={sha}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def test_commits_at(workdir: Path, commits: dict) -> tuple[int, str]:
    """Clone the given commits into a FRESH workdir and run the test there."""
    clone_upstreams(workdir, commits)
    return run_test(workdir, commits)


# ── Version / pins updates ──────────────────────────────────────────────────

def bump_patch(version: str) -> str:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)$", version.strip())
    if not m:
        raise ValueError(f"cannot patch-bump version {version!r}")
    return f"{m.group(1)}.{m.group(2)}.{int(m.group(3)) + 1}"


def read_plugin_version() -> str:
    text = PLUGIN_YAML_PATH.read_text(encoding="utf-8")
    m = re.search(r"^version:\s*[\"']?([^\"'\n]+)[\"']?\s*$", text, re.MULTILINE)
    if not m:
        raise RuntimeError("plugin.yaml has no version line")
    return m.group(1).strip()


def write_plugin_version(version: str) -> None:
    text = PLUGIN_YAML_PATH.read_text(encoding="utf-8")
    new_text = re.sub(
        r"^version:.*$", f'version: "{version}"', text, count=1, flags=re.MULTILINE,
    )
    PLUGIN_YAML_PATH.write_text(new_text, encoding="utf-8")


def write_pins(pins: dict) -> None:
    PINS_PATH.write_text(
        json.dumps(pins, indent=2, sort_keys=False) + "\n", encoding="utf-8",
    )


# ── README compatibility table ──────────────────────────────────────────────

README_TABLE_HEADER = (
    "| Component | Pinned Commit | Test Date | Status | Upstream Release Tag |\n"
    "| :--- | :--- | :--- | :--- | :--- |\n"
)


def render_readme_table(pins: dict) -> str:
    """Render the compatibility table rows from the pins, in UPSTREAMS order.

    The README table is a VIEW of upstream-pins.json. It must be regenerated
    whenever the pins change, or the published table contradicts the pins that
    CI actually tests against (and users read the table to decide whether an
    upstream bump is safe).
    """
    rows = ""
    for key in UPSTREAMS:
        pin = pins[key]
        display = UPSTREAMS[key]["display"]
        commit = pin["commit"][:7]
        tested = pin.get("tested_at", "")
        release = pin.get("release")
        tag = f"`{release}`" if release else f"none (pinned commit {commit})"
        rows += f"| **{display}** | `{commit}` | {tested} | Verified | {tag} |\n"
    return README_TABLE_HEADER + rows


def write_readme_table(pins: dict) -> None:
    """Replace the compatibility table in README.md in place.

    Raises if the anchor or table shape is not found — a silent no-op here is
    exactly the bug this function exists to prevent.
    """
    text = README_PATH.read_text(encoding="utf-8")
    marker = "| Component | Pinned Commit | Test Date | Status | Upstream Release Tag |"
    start = text.find(marker)
    if start < 0:
        raise RuntimeError("README.md has no compatibility table header to update")
    end = text.find("\n\n", start)
    if end < 0:
        raise RuntimeError("could not find the end of the README compatibility table")
    README_PATH.write_text(
        text[:start] + render_readme_table(pins).rstrip("\n") + text[end:],
        encoding="utf-8",
    )


# ── Git / release / issue actions ───────────────────────────────────────────

def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True, timeout=300)


def push_url(repo: str) -> str:
    if TOKEN:
        return f"https://x-access-token:{TOKEN}@github.com/{repo}.git"
    return f"https://github.com/{repo}.git"


def _push_remote(repo: str) -> str:
    """Push remote, overridable via BRIDGE_PUSH_REMOTE (used by tests)."""
    return os.environ.get("BRIDGE_PUSH_REMOTE", "") or push_url(repo)


def do_pass_path(repo: str, pins: dict, new_releases: dict, dry_run: bool) -> None:
    """Bump version, update pins, commit, tag, push, create release."""
    old_version = read_plugin_version()
    new_version = bump_patch(old_version)
    today = date.today().isoformat()
    for key, rel in new_releases.items():
        pins[key]["commit"] = rel["commit"]
        pins[key]["release"] = rel["tag"]
        pins[key]["tested_at"] = today

    summary = ", ".join(
        f"{key} {new_releases[key]['tag']} ({new_releases[key]['commit'][:7]})"
        for key in new_releases
    )
    release_notes = (
        f"Compatibility update — bridge re-tested against new upstream releases.\n\n"
        f"| Upstream | Release | Commit | Result |\n"
        f"| :--- | :--- | :--- | :--- |\n"
    )
    for key in UPSTREAMS:
        rel = new_releases.get(key)
        if rel:
            release_notes += f"| {key} | {rel['tag']} | {rel['commit'][:7]} | passed |\n"
        else:
            release_notes += f"| {key} | (unchanged) | {pins[key]['commit'][:7]} | passed |\n"
    release_notes += (
        f"\nPins updated in `upstream-pins.json`. Verified by `test_bridge.py` "
        f"(30 assertions) in the release-monitor workflow."
    )

    if dry_run:
        print(f"[dry-run] would bump plugin.yaml: {old_version} -> {new_version}")
        print(f"[dry-run] would update upstream-pins.json: {json.dumps(pins, indent=2)}")
        print(f"[dry-run] would regenerate README.md compatibility table:")
        print(render_readme_table(pins))
        print(f"[dry-run] would commit + tag v{new_version} + push + create release")
        print(f"[dry-run] release body:\n{release_notes}")
        return

    write_plugin_version(new_version)
    write_pins(pins)
    write_readme_table(pins)
    git(REPO_ROOT, "config", "user.name", "github-actions[bot]")
    git(REPO_ROOT, "config", "user.email", "github-actions[bot]@users.noreply.github.com")
    git(REPO_ROOT, "add", "plugin.yaml", "upstream-pins.json", "README.md")
    git(REPO_ROOT, "commit", "-m",
        f"chore: bump pins to {summary} (bridge v{new_version})")
    git(REPO_ROOT, "tag", f"v{new_version}")
    git(REPO_ROOT, "push", _push_remote(repo), "master", "--tags")
    api_post(f"/repos/{repo}/releases", {
        "tag_name": f"v{new_version}",
        "name": f"v{new_version}",
        "body": release_notes,
    })
    print(f"PASS: bridge v{new_version} released with pins {summary}")


def do_fail_path(repo: str, new_releases: dict, isolation: dict,
                 combined_output: str, dry_run: bool) -> None:
    """Open (or comment on) the 'Upstream release breaks bridge' issue."""
    title = "Upstream release breaks bridge"
    rows = "".join(
        f"| {key} | {rel['tag']} | {rel['commit'][:7]} | "
        f"{'FAILED' if isolation.get(key) != 'passed' else 'passed'} |\n"
        for key, rel in new_releases.items()
    )
    body = (
        "The daily release monitor detected new upstream releases and the "
        "bridge self-test FAILED against them.\n\n"
        f"| Upstream | Release | Commit | Result |\n"
        f"| :--- | :--- | :--- | :--- |\n{rows}\n"
    )
    if isolation:
        body += "\nIsolation results (each upstream tested alone at its new release):\n"
        for key, result in isolation.items():
            body += f"- {key}: {result}\n"
    body += (
        "\n<details><summary>Test output (truncated)</summary>\n\n"
        f"```\n{combined_output[-4000:]}\n```\n\n</details>\n\n"
        "Per the kill-switch policy in the README, a breaking upstream change "
        "may deprecate this bridge. Investigate before updating pins."
    )

    if dry_run:
        print(f"[dry-run] would open issue: {title}")
        print(body)
        return

    issues = api_get(f"/repos/{repo}/issues?state=open&per_page=100")
    existing = [
        i for i in issues
        if i.get("title") == title and "pull_request" not in i
    ]
    if existing:
        api_post(f"/repos/{repo}/issues/{existing[0]['number']}/comments", {"body": body})
        print(f"FAIL: commented on existing issue #{existing[0]['number']}")
    else:
        api_post(f"/repos/{repo}/issues", {"title": title, "body": body})
        print("FAIL: opened issue 'Upstream release breaks bridge'")


# ── Main ────────────────────────────────────────────────────────────────────

def main() -> int:
    global API_BASE
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="detect + test, but do not bump/tag/release/open issues")
    ap.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"),
                    help="OWNER/REPO of the bridge repo (default: GITHUB_REPOSITORY)")
    ap.add_argument("--api-base", default=API_BASE, help=argparse.SUPPRESS)
    args = ap.parse_args()
    API_BASE = args.api_base
    if not args.repo:
        print("error: --repo OWNER/REPO required (or GITHUB_REPOSITORY env)")
        return 2

    pins = json.loads(PINS_PATH.read_text(encoding="utf-8"))

    # 1) Detect new releases
    new_releases: dict = {}
    for key in UPSTREAMS:
        rel = detect_new_release(key, pins[key])
        if rel:
            new_releases[key] = rel
            print(f"detected: {key} {rel['tag']} ({rel['commit'][:7]}) "
                  f"published {rel['published_at']}")
        else:
            print(f"current:  {key} pin {pins[key]['commit'][:7]} covers latest release")

    if not new_releases:
        print("no new upstream releases — pins are current, nothing to do")
        return 0

    # 2) Test the bridge against the new release commits
    test_commits = {
        key: (new_releases[key]["commit"] if key in new_releases else pins[key]["commit"])
        for key in UPSTREAMS
    }
    workdir = Path(tempfile.mkdtemp(prefix="bridge_monitor_"))
    try:
        rc, output = test_commits_at(workdir, test_commits)
        print(f"combined test exit: {rc}")
        if rc == 0:
            do_pass_path(args.repo, pins, new_releases, args.dry_run)
            return 0

        # 3) Fail path: isolate which upstream broke (fresh workdir per test)
        isolation: dict = {}
        for key in new_releases:
            if len(new_releases) == 1:
                isolation[key] = "FAILED (combined test)"
                print(f"isolation {key}: {isolation[key]}")
                continue
            iso_commits = {
                k: (new_releases[k]["commit"] if k == key else pins[k]["commit"])
                for k in UPSTREAMS
            }
            iso_workdir = Path(tempfile.mkdtemp(prefix="bridge_monitor_iso_"))
            try:
                iso_rc, iso_out = test_commits_at(iso_workdir, iso_commits)
            finally:
                shutil.rmtree(iso_workdir, ignore_errors=True)
            isolation[key] = "passed" if iso_rc == 0 else "FAILED"
            print(f"isolation {key}: {isolation[key]}")
        do_fail_path(args.repo, new_releases, isolation, output, args.dry_run)
        return 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
