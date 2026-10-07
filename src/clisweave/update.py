"""cw update - update clisweave itself, and optionally the underlying
claude/codex/kimi/step CLIs, which each ship their own self-update command."""
import os
import shutil
import subprocess
import sys

TOOL_UPDATE_CMD = {
    "claude": ["claude", "update"],
    "codex": ["codex", "update"],
    # --yes keeps kimi's picker ("Install update now / Continue with current
    # version") from being the thing that fails `cw update`: without a friendly
    # TTY the selection aborts and the whole update errors out ("This operation
    # was aborted"). Confirmed live 2026-09-24.
    "kimi": ["kimi", "update", "--yes"],
    # step's updater answers in one shot -- no picker to dismiss the way
    # kimi's does, and no argument needed ("Usage: step update [version]",
    # checked against the installed binary).
    "step": ["step", "update"],
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

# The self-update is a `git pull` (or a pip upgrade) on the same network that
# severs long TLS transfers, and it used to get exactly one attempt: a pull
# that died on SSL_ERROR_SYSCALL was reported as a failure even though the
# very next try succeeds (seen live 2026-10-07). It now gets the same retry
# treatment as the flaky tool updaters.
SELF_UPDATE_RETRIES = 2

# Failures a retry cannot fix. A dirty worktree, a diverged branch or a
# rejected credential fail identically on every attempt, so retrying only
# repeats the same error -- and would earn a network hint for a problem that
# has nothing to do with the network.
GIT_FATAL_MARKERS = (
    "cannot pull with rebase",
    "cannot rebase:",
    "local changes would be overwritten",
    "not possible to fast-forward",
    "unrelated histories",
    "Permission denied",
    "Authentication failed",
    "could not read Username",
)

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

# Codex 0.160.1's native ``codex update`` launches the install script as
# ``curl ... | sh`` without pipefail.  If curl cannot establish TLS, the empty
# shell exits successfully and Codex prints its success banner.  Until the
# upstream updater propagates that failure, recognize curl's diagnostic in the
# captured output so ``cw update`` can report/retry it correctly.
TOOL_UPDATE_FAILURE_MARKERS = {
    "codex": ("curl: (",),
}


def detect_repo_dir(package_dir):
    """If clisweave was installed by symlinking into a git clone (the curl or
    git install path), return that clone's root so it can be `git pull`ed.
    Returns None for a pip install, where the package lives under
    site-packages with no .git anywhere nearby.

    `.git` is checked with exists, not isdir: a git worktree, a submodule and
    a `--separate-git-dir` clone all carry `.git` as a *file*. Taking the pip
    branch for those silently pointed `cw update` at PyPI instead of the
    clone the `cw` launcher actually runs -- and reported success."""
    repo_candidate = os.path.dirname(os.path.dirname(os.path.abspath(package_dir)))
    if os.path.exists(os.path.join(repo_candidate, ".git")):
        return repo_candidate
    return None


def run_update_command(argv, env=None, failure_markers=(), capture=False):
    """Run one updater, returning (exit code, captured output).

    Output is only captured when a caller needs to read it (failure markers,
    or `capture`): piped output can't be interactive, and these updaters own
    the terminal otherwise.

    A Windows npm/global shim (claude.cmd, codex.cmd, kimi.cmd) passes
    `shutil.which` but cannot be spawned directly -- PATH search only appends
    .exe -- so it goes through the shell. Either way a spawn failure becomes
    this tool's exit code rather than an exception that ends `cw update
    tools` before the remaining tools are even attempted."""
    command = list(argv)
    if os.name == "nt":
        found = (shutil.which(argv[0]) or "").lower()
        if found.endswith((".cmd", ".bat")):
            command = [os.environ.get("COMSPEC", "cmd.exe"), "/c", *command]
    try:
        if not failure_markers and not capture:
            return subprocess.run(command, env=env).returncode, ""

        result = subprocess.run(
            command, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="replace",
        )
        output = getattr(result, "stdout", "") or ""
        print(output, end="" if output.endswith("\n") or not output else "\n", flush=True)
        if result.returncode == 0 and any(marker in output for marker in failure_markers):
            print(
                f"cw update: {argv[0]} reported success after its installer failed",
                file=sys.stderr,
            )
            return 1, output
        return result.returncode, output
    except OSError as exc:
        print(f"cw update: could not run {argv[0]}: {exc}", file=sys.stderr)
        return 127, ""
    except ValueError as exc:
        print(f"cw update: invalid command for {argv[0]}: {exc}", file=sys.stderr)
        return 127, ""


def run_with_retries(argv, retries, proxy, failure_markers=(), give_up_markers=()):
    """Run one updater, retrying up to `retries` times. Returns
    (exit code, gave_up) -- `gave_up` marks a failure that a retry cannot fix,
    so callers don't dress it up as something a proxy would help with.

    The first attempt is always direct, so a proxy is only ever a fallback --
    the loop below forces it for retries by stripping no_proxy exemptions, the
    same way the tool updaters have always done."""
    last_code = 0
    for attempt in range(1 + retries):
        env = None
        if attempt:
            label = f"retry {attempt}/{retries}"
            if proxy:
                # Force the proxy even where no_proxy would exempt things;
                # a failure already proved the direct path is broken.
                env = dict(os.environ)
                env["http_proxy"] = env["https_proxy"] = proxy
                env.pop("no_proxy", None)
                env.pop("NO_PROXY", None)
                label += f" via {proxy}"
            print(f"  {label} ...", flush=True)
        last_code, output = run_update_command(
            argv, env=env, failure_markers=failure_markers,
            capture=bool(give_up_markers),
        )
        if last_code == 0:
            break
        if any(marker in output for marker in give_up_markers):
            return last_code, True
    return last_code, False


def update_self():
    """Update the clisweave install itself. Returns a process-style exit code."""
    package_dir = os.path.dirname(os.path.abspath(__file__))
    repo_dir = detect_repo_dir(package_dir)
    proxy = os.environ.get(UPDATE_PROXY_ENV)

    if repo_dir:
        print(f"Updating git install at {repo_dir} ...", flush=True)
        argv = ["git", "-C", repo_dir, "pull", "--ff-only"]
        give_up_markers = GIT_FATAL_MARKERS
    else:
        print("Updating pip install of clisweave ...", flush=True)
        argv = [sys.executable, "-m", "pip", "install", "--upgrade", "clisweave"]
        give_up_markers = ()

    code, gave_up = run_with_retries(argv, SELF_UPDATE_RETRIES, proxy,
                                     give_up_markers=give_up_markers)
    if code != 0 and gave_up:
        print(
            "cw update: this failure isn't transient, so it was not retried "
            "-- the message above is the real error",
            file=sys.stderr,
        )
    elif code != 0 and not proxy:
        print(
            f"  hint: the update itself could not reach its source. If you have a "
            f"working proxy, set {UPDATE_PROXY_ENV}=<proxy-url> and rerun -- failed "
            f"updates retry through it automatically.",
            file=sys.stderr,
        )
    return code


def update_tools():
    """Run each of claude/codex/kimi/step's own update command, skipping any
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
        proxy = os.environ.get(UPDATE_PROXY_ENV)
        if retries == 0 and proxy:
            # No per-tool retry policy, but the proxy fallback still earns one
            # attempt -- without this, codex (0 retries) could never reach it.
            retries = 1
        print(f"Updating {tool} ...", flush=True)
        last_code, _ = run_with_retries(argv, retries, proxy,
                                        TOOL_UPDATE_FAILURE_MARKERS.get(tool, ()))

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
        print(f"cw update: unexpected argument '{argv[0]}' (expected 'tools' or 'all')", file=sys.stderr)
        sys.exit(1)

    worst = 0
    if target in ("self", "all"):
        worst = max(worst, update_self())
    if target in ("tools", "all"):
        worst = max(worst, update_tools())

    sys.exit(worst)
