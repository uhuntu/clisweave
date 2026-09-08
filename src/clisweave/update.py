"""ai update - update clisweave itself, and optionally the underlying
claude/codex/kimi CLIs, which each ship their own self-update command."""
import os
import shutil
import subprocess
import sys

TOOL_UPDATE_CMD = {
    "claude": ["claude", "update"],
    "codex": ["codex", "update"],
    "kimi": ["kimi", "update"],
}

# claude's updater has no fallback mirror, so it's prone to hitting its own
# internal download deadline on a slow-but-working connection -- confirmed
# 2026-08-19: a failed `claude update` (TelemetrySafeError: exceeded the
# total deadline) succeeded on a bare retry with no other change. codex and
# kimi don't need this: codex already falls back to GitHub Releases
# internally, and neither has shown this failure pattern.
TOOL_UPDATE_RETRIES = {"claude": 2}

# claude's updater fetches from a single Google-Cloud-fronted host with no
# fallback mirror (unlike codex, which falls back to GitHub Releases when
# its primary source stalls), so it fails more often -- especially on
# networks that block/throttle Google Cloud IPs. Worth a pointed hint
# rather than just the raw error.
TOOL_UPDATE_HINTS = {
    "claude": (
        "claude's updater has no fallback mirror and can fail on networks that "
        "block/throttle Google Cloud IPs (downloads.claude.ai). If this keeps "
        "happening, try routing the update through a proxy, or download the "
        "release binary directly from "
        "https://downloads.claude.ai/claude-code-releases/<version>/linux-x64/claude "
        "via a proxy and install it manually."
    ),
}


def detect_repo_dir(package_dir):
    """If clisweave was installed by symlinking into a git clone (the curl or
    git install path), return that clone's root so it can be `git pull`ed.
    Returns None for a pip install, where the package lives under
    site-packages with no .git anywhere nearby."""
    repo_candidate = os.path.dirname(os.path.dirname(os.path.abspath(package_dir)))
    if os.path.isdir(os.path.join(repo_candidate, ".git")):
        return repo_candidate
    return None


def update_self():
    """Update the clisweave install itself. Returns a process-style exit code."""
    package_dir = os.path.dirname(os.path.abspath(__file__))
    repo_dir = detect_repo_dir(package_dir)

    if repo_dir:
        print(f"Updating git install at {repo_dir} ...", flush=True)
        result = subprocess.run(["git", "-C", repo_dir, "pull", "--ff-only"])
    else:
        print("Updating pip install of clisweave ...", flush=True)
        result = subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "clisweave"])

    return result.returncode


def update_tools():
    """Run each of claude/codex/kimi's own update command, skipping any
    that aren't installed. Returns 0 unless one that IS installed fails."""
    worst = 0
    for tool, argv in TOOL_UPDATE_CMD.items():
        if shutil.which(tool) is None:
            print(f"{tool}: not installed, skipping")
            continue

        retries = TOOL_UPDATE_RETRIES.get(tool, 0)
        result = None
        for attempt in range(1 + retries):
            if attempt == 0:
                print(f"Updating {tool} ...", flush=True)
            else:
                print(f"Updating {tool} (retry {attempt}/{retries}) ...", flush=True)
            result = subprocess.run(argv)
            if result.returncode == 0:
                break

        if result.returncode != 0:
            worst = result.returncode
            hint = TOOL_UPDATE_HINTS.get(tool)
            if hint:
                print(f"  hint: {hint}", file=sys.stderr)
    return worst


def cmd_update(argv):
    if not argv:
        target = "self"
    elif argv == ["tools"]:
        target = "tools"
    elif argv == ["all"]:
        target = "all"
    else:
        print(f"ai update: unexpected argument '{argv[0]}' (expected 'tools' or 'all')", file=sys.stderr)
        sys.exit(1)

    worst = 0
    if target in ("self", "all"):
        worst = max(worst, update_self())
    if target in ("tools", "all"):
        worst = max(worst, update_tools())

    sys.exit(worst)
