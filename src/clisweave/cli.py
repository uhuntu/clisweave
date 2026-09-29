"""ai - unified wrapper for the claude / codex / kimi / step CLIs.
Normalizes a handful of common flags across the four tools and passes
everything else straight through.
"""
import sys

from . import __version__, search, sessions, update

TOOLS = ("claude", "codex", "kimi", "step")

# USAGE is an f-string built from TOOLS, search.JUDGE_CMD and
# search.DEFAULT_JUDGE for the same reason SUBCOMMANDS exists below: the
# search line hardcoded [--judge claude|codex|kimi] and so advertised a judge
# list that was wrong for the two commits after step joined it, the yolo table
# and the examples named three tools when there were four, and "asks claude"
# kept naming a judge that was no longer the default. Nothing here spells out
# something the code can say.
USAGE = f"""Usage: ai <{'|'.join(TOOLS)}> [common-options] [prompt] [-- extra native args]
       ai sessions [--tool T] [--limit N|all] [--cwd] [--all]
       ai full [--tool T] [--cwd] [--all]
       ai search <topic> [--tool {'|'.join(TOOLS)}] [--judge {'|'.join(search.JUDGE_CMD)}] [--why] [--all]
       ai resume <{'|'.join(TOOLS)}|N> [session-id-or-prefix] [--cwd <dir>]
       ai <N> <{'|'.join(TOOLS)}> [native-options]
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
  step    -> --approval-mode auto + --non-interactive-approval allow

Examples:
  ai claude -p "summarize this repo"
  ai codex -p -m o3 "fix the failing test"
  ai kimi -c
  ai step -p "summarize this repo"
  ai claude -- --agent reviewer "look at this diff"
  ai sessions --limit 10
  ai sessions --limit all   # same as `ai full`
  ai full                   # everything, no default 15-row cutoff
  ai resume kimi 97946bc7
  ai resume 3         # resume row 3 from the last `ai`/`ai sessions` listing
  ai resume 2 --cwd /path/to/other-project   # can't relocate row 2 in place -- hands off to a fresh session there
  ai 3 codex          # continue row 3 in a new Codex session with context
  ai update           # update clisweave itself
  ai update tools     # update claude, codex, kimi, and step (whichever are installed)
  ai update all       # both of the above
  ai search "the nfc frequency lock issue"   # asks {search.DEFAULT_JUDGE} (the default judge)
  ai search "katago" --judge codex   # judge with a different tool
  ai search "katago" --why           # each hit's reason, in full
  ai stats            # session counts per tool, oldest/newest, top directories
"""

# Everything `ai <x>` accepts that is not a tool. The unknown-tool error names
# this list, and the test asserts it matches what main() actually dispatches,
# so adding a subcommand cannot leave the message stale the way it was when
# `stats` and `step` were both missing from it.
SUBCOMMANDS = ("sessions", "full", "search", "resume", "update", "stats")


class UsageError(Exception):
    """Bad arguments to `ai <tool> ...`. Caught by main() and reported
    cleanly; kept separate from sys.exit so build_command stays a pure,
    testable function."""


def build_command(tool, rest):
    """Normalize `rest` (the args after the tool name) into the native
    command to run. Pure function, no I/O — raises UsageError on bad
    input instead of exiting, so it's easy to unit test."""
    if tool not in TOOLS:
        raise UsageError(f"unknown tool '{tool}' (expected one of {', '.join(TOOLS)}, "
                        f"{', '.join(SUBCOMMANDS)})")

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
    elif tool == "step":
        cmd = ["step"]
        if print_:
            cmd.append("-p")
        if continue_session:
            cmd.append("-c")
        if model:
            cmd += ["--model", model]
        if add_dirs:
            # step has no per-workspace directory flag of its own; the closest
            # it has is --session-dir, which is where sessions live rather
            # than what to work on.
            raise UsageError("--add-dir is not supported for step")
        if yolo:
            # step's approval modes are confirm/auto/strict, and "auto" is the
            # auto-approve one. With no approval UI (a -p run) approval falls
            # back to --non-interactive-approval, which denies by default, so
            # -y has to cover both.
            cmd += ["--approval-mode", "auto", "--non-interactive-approval", "allow"]
    else:  # kimi
        cmd = ["kimi"]
        if print_:
            if not trailing:
                # claude and codex both accept `-p` with the prompt on
                # stdin; kimi's -p does not read stdin at all, so a bare
                # `-p` here can only be a mistake (it used to reach kimi as
                # a value-less option, and in a handoff it collided with the
                # seed's own -p). --model/--add-dir already reject a missing
                # value the same way.
                raise UsageError("-p requires a prompt (kimi's -p does not read stdin)")
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
    if sessions.ROW_NUMBER_RE.match(tool):
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
