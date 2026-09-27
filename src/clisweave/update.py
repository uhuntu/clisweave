"""ai update - update clisweave itself, and optionally the underlying
claude/codex/kimi CLIs, which each ship their own self-update command."""
import os
import shutil
import subprocess
import sys

TOOL_UPDATE_CMD = {
    "claude": ["claude", "update"],
    "codex": ["codex", "update"],
    # --yes keeps kimi's picker ("Install update now / Continue with current
    # version") from being the thing that fails `ai update`: without a friendly
    # TTY the selection aborts and the whole update errors out ("This operation
    # was aborted"). Confirmed live 2026-09-24.
    "kimi": ["kimi", "update", "--yes"],
}

# If this is set, failed updates retry through this proxy. Confirmed live
# 2026-09-24: this network severs TLS transfers past ~60MB while short requests
# pass -- codex's ~146MB asset dies mid-download every time, resume ignored,
# and its GitHub Releases fallback dies the same way. The same transfer through
# a working local proxy completed in under a minute.
UPDATE_PROXY_ENV = "CLISWEAVE_UPDATE_PROXY"

# claude's updater has no fallback mirror, so it's prone to hitting its own
# internal download deadline on a slow-but-working connection -- confirmed
# 2026-08-19: a failed `claude update` (TelemetrySafeError: exceeded the
# total deadline) succeeded on a bare retry with no other change. kimi needs
# the same treatment for a different reason: its update check
# (code.kimi.com/kimi-code/latest) intermittently hangs at connect time (2 of
# 5 attempts exceeded 10s on 2026-09-24), and a bare retry usually gets through.
TOOL_UPDATE_RETRIES = {"claude": 2, "kimi": 2}

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
    "codex": (
        "codex's asset is ~146MB from releases.openai.com (it does fall back to "
        "GitHub Releases, but a network that severs long TLS transfers kills "
        "that too, and resume won't help). If you have a working proxy, set "
        f"{UPDATE_PROXY_ENV}=<proxy-url> and rerun -- failed updates retry "
        "through it automatically."
    ),
}


def detect_repo_dir(package_dir):
    """If clisweave was installed by symlinking into a git clone (the curl or
    git install path), return that clone's root so it can be `git pull`ed.
    Returns None for a pip install, where the package lives under
    site-packages with no .git anywhere nearby.

    `.git` is checked with exists, not isdir: a git worktree, a submodule and
    a `--separate-git-dir` clone all carry `.git` as a *file*. Taking the pip
    branch for those silently pointed `ai update` at PyPI instead of the
    clone the `ai` launcher actually runs -- and reported success."""
    repo_candidate = os.path.dirname(os.path.dirname(os.path.abspath(package_dir)))
    if os.path.exists(os.path.join(repo_candidate, ".git")):
        return repo_candidate
    return None


def run_update_command(argv, env=None):
    """Run one updater, returning its exit code.

    A Windows npm/global shim (claude.cmd, codex.cmd, kimi.cmd) passes
    `shutil.which` but cannot be spawned directly -- PATH search only appends
    .exe -- so it goes through the shell. Either way a spawn failure becomes
    this tool's exit code rather than an exception that ends `ai update
    tools` before the remaining tools are even attempted."""
    command = list(argv)
    if os.name == "nt":
        found = (shutil.which(argv[0]) or "").lower()
        if found.endswith((".cmd", ".bat")):
            command = [os.environ.get("COMSPEC", "cmd.exe"), "/c", *command]
    try:
        return subprocess.run(command, env=env).returncode
    except OSError as exc:
        print(f"ai update: could not run {argv[0]}: {exc}", file=sys.stderr)
        return 127
    except ValueError as exc:
        print(f"ai update: invalid command for {argv[0]}: {exc}", file=sys.stderr)
        return 127


def update_self():
    """Update the clisweave install itself. Returns a process-style exit code."""
    package_dir = os.path.dirname(os.path.abspath(__file__))
    repo_dir = detect_repo_dir(package_dir)

    if repo_dir:
        print(f"Updating git install at {repo_dir} ...", flush=True)
        return run_update_command(["git", "-C", repo_dir, "pull", "--ff-only"])
    print("Updating pip install of clisweave ...", flush=True)
    return run_update_command([sys.executable, "-m", "pip", "install", "--upgrade", "clisweave"])


def update_tools():
    """Run each of claude/codex/kimi's own update command, skipping any
    that aren't installed. Returns 0 unless one that IS installed fails.

    A failed update is retried (per-tool counts in TOOL_UPDATE_RETRIES), and
    when UPDATE_PROXY_ENV is set the retries go through that proxy -- the
    first attempt is always direct, so a proxy is only ever a fallback."""
    worst = 0
    for tool, argv in TOOL_UPDATE_CMD.items():
        if shutil.which(tool) is None:
            print(f"{tool}: not installed, skipping")
            continue

        retries = TOOL_UPDATE_RETRIES.get(tool, 0)
        if retries == 0 and os.environ.get(UPDATE_PROXY_ENV):
            # No per-tool retry policy, but the proxy fallback still earns one
            # attempt -- without this, codex (0 retries) could never reach it.
            retries = 1
        proxy = os.environ.get(UPDATE_PROXY_ENV)
        last_code = 0
        for attempt in range(1 + retries):
            env = None
            label = f"(retry {attempt}/{retries})" if attempt else ""
            if attempt and proxy:
                # Force the proxy even where no_proxy would exempt things;
                # a failure already proved the direct path is broken.
                env = dict(os.environ)
                env["http_proxy"] = env["https_proxy"] = proxy
                env.pop("no_proxy", None)
                env.pop("NO_PROXY", None)
                label = f"(retry {attempt}/{retries} via {proxy})"
            print(f"Updating {tool} {label}".rstrip() + " ...", flush=True)
            last_code = run_update_command(argv, env=env)
            if last_code == 0:
                break

        if last_code != 0:
            worst = last_code
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
