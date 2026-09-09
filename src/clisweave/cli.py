"""ai - unified wrapper for claude / codex / kimi CLIs.
Normalizes a handful of common flags across the three tools and passes
everything else straight through.
"""
import sys

from . import __version__, search, sessions, update

USAGE = """Usage: ai <claude|codex|kimi> [common-options] [prompt] [-- extra native args]
       ai sessions [--tool T] [--limit N|all] [--cwd] [--all]
       ai full [--tool T] [--cwd] [--all]
       ai search <topic> [--tool T] [--judge claude|codex|kimi]
       ai resume <claude|codex|kimi|N> [session-id-or-prefix] [--cwd <dir>]
       ai <N> <claude|codex|kimi> [native-options]
       ai update [tools|all]
       ai stats [--tool T]

Common options (translated per-tool, all optional):
  -p, --print              Non-interactive: print response and exit
  -c, --continue           Continue the most recent session in this directory
  -m, --model <model>      Model to use
  --add-dir <dir>          Additional workspace directory (repeatable)
  -y, --yolo               Auto-approve tool calls (per-tool semantics differ,
                            see notes below)

Anything after a literal `--`, or any flag this wrapper doesn't recognize,
is passed through unchanged to the underlying CLI.

Per-tool --yolo mapping:
  claude  -> --dangerously-skip-permissions
  codex   -> --approve-for-me   (auto-approve, still sandboxed)
  kimi    -> -y/--yolo

Examples:
  ai claude -p "summarize this repo"
  ai codex -p -m o3 "fix the failing test"
  ai kimi -c
  ai claude -- --agent reviewer "look at this diff"
  ai sessions --limit 10
  ai sessions --limit all   # same as `ai full`
  ai full                   # everything, no default 15-row cutoff
  ai resume kimi 97946bc7
  ai resume 3         # resume row 3 from the last `ai`/`ai sessions` listing
  ai resume 2 --cwd /path/to/other-project   # resume row 2, but force this directory
  ai 3 codex          # continue row 3 in a new Codex session with context
  ai update           # update clisweave itself
  ai update tools     # update claude, codex, and kimi (whichever are installed)
  ai update all       # both of the above
  ai search "the nfc frequency lock issue"   # asks claude to find relevant sessions
  ai stats            # session counts per tool, oldest/newest, top directories
"""

TOOLS = ("claude", "codex", "kimi")


class UsageError(Exception):
    """Bad arguments to `ai <tool> ...`. Caught by main() and reported
    cleanly; kept separate from sys.exit so build_command stays a pure,
    testable function."""


def build_command(tool, rest):
    """Normalize `rest` (the args after the tool name) into the native
    command to run. Pure function, no I/O — raises UsageError on bad
    input instead of exiting, so it's easy to unit test."""
    if tool not in TOOLS:
        raise UsageError(f"unknown tool '{tool}' (expected claude, codex, or kimi, or sessions/full/search/resume/update)")

    print_ = False
    continue_session = False
    model = None
    add_dirs = []
    yolo = False
    trailing = []

    i = 0
    while i < len(rest):
        a = rest[i]
        if a in ("-p", "--print"):
            print_ = True
            i += 1
        elif a in ("-c", "--continue"):
            continue_session = True
            i += 1
        elif a in ("-m", "--model"):
            if i + 1 >= len(rest):
                raise UsageError("--model requires a value")
            model = rest[i + 1]
            i += 2
        elif a == "--add-dir":
            if i + 1 >= len(rest):
                raise UsageError("--add-dir requires a value")
            add_dirs.append(rest[i + 1])
            i += 2
        elif a in ("-y", "--yolo"):
            yolo = True
            i += 1
        elif a == "--":
            trailing.extend(rest[i + 1:])
            break
        else:
            trailing.append(a)
            i += 1

    cmd = []
    if tool == "claude":
        cmd = ["claude"]
        if print_:
            cmd.append("-p")
        if continue_session:
            cmd.append("--continue")
        if model:
            cmd += ["--model", model]
        for d in add_dirs:
            cmd += ["--add-dir", d]
        if yolo:
            cmd.append("--dangerously-skip-permissions")
    elif tool == "codex":
        if print_:
            cmd = ["codex", "exec"]
            if continue_session:
                cmd += ["resume", "--last"]
        else:
            cmd = ["codex"]
        if model:
            cmd += ["-m", model]
        for d in add_dirs:
            cmd += ["--add-dir", d]
        if yolo:
            cmd.append("--approve-for-me")
    else:  # kimi
        cmd = ["kimi"]
        if print_:
            cmd.append("-p")
        if continue_session:
            cmd.append("-c")
        if model:
            cmd += ["-m", model]
        for d in add_dirs:
            cmd += ["--add-dir", d]
        if yolo:
            cmd.append("-y")

    cmd += trailing
    return cmd


def main():
    argv = sys.argv[1:]

    if argv and argv[0] in ("-h", "--help"):
        print(USAGE)
        return
    if argv and argv[0] in ("-v", "--version"):
        print(f"clisweave {__version__}")
        return

    if not argv:
        sessions.cmd_list(["--limit", "15"])
        return

    tool, rest = argv[0], argv[1:]

    if tool == "sessions":
        sessions.cmd_list(rest)
        return
    if tool == "full":
        # `ai full` is shorthand for `ai sessions --limit all`.
        sessions.cmd_list(["--limit", "all", *rest])
        return
    if tool == "resume":
        sessions.cmd_resume(rest)
        return
    if tool.isdigit():
        # `ai <N>` is shorthand for `ai resume <N>`.
        sessions.cmd_resume(argv)
        return
    if tool == "update":
        update.cmd_update(rest)
        return
    if tool == "search":
        search.cmd_search(rest)
        return
    if tool == "stats":
        sessions.cmd_stats(rest)
        return

    try:
        cmd = build_command(tool, rest)
    except UsageError as e:
        print(f"ai: {e}", file=sys.stderr)
        sys.exit(1)

    sessions.exec_or_die(cmd)


if __name__ == "__main__":
    main()
