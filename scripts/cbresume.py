#!/usr/bin/env python3
"""cbresume - jump into a session listed by cbsessions.py.

Capability per client (verified against what each client actually exposes,
not guessed):

  cli      real resume: `codebuddy -r <sessionId>` run in the session's cwd
  vscode   no per-conversation entry point. The extension contributes only
           `chatHistory` / `clearSession`, so we can open the workspace and
           optionally bring up its history panel -- you pick the row.
  desktop  no per-session entry point either. `codebuddy://` IS registered
           (-> CodeBuddy.exe --open-url), but neither product.json nor the
           bundled genie extension defines any route for it, so at best we
           launch the app on the right folder.

Stdlib only.
"""
import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cbsessions as cb  # noqa: E402

VSCODE_HISTORY_CMD = "tencentcloud.codingcopilot.chatHistory"


# ---------------------------------------------------------------- locate clients

def find_cli_exe():
    """.cmd wins over .ps1: a .ps1 can't be exec'd by CreateProcess."""
    for name in ("codebuddy.cmd", "codebuddy.exe", "cbc.cmd", "codebuddy"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _reg_protocol_exe():
    """Read the codebuddy:// handler out of HKCU if it's registered."""
    try:
        out = subprocess.run(
            ["reg", "query", r"HKCU\Software\Classes\codebuddy\shell\open\command"],
            capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    for line in out.splitlines():
        line = line.strip()
        if '"' not in line:
            continue
        first = line.split('"')[1]
        if first.lower().endswith(".exe"):
            return first
    return None


def find_desktop_exe():
    p = _reg_protocol_exe()
    if p and os.path.isfile(p):
        return p
    base = os.environ.get("LOCALAPPDATA", "")
    for cand in (os.path.join(base, "Programs", "CodeBuddy", "CodeBuddy.exe"),
                 shutil.which("CodeBuddy.exe")):
        if cand and os.path.isfile(cand):
            return cand
    return None


# ---------------------------------------------------------------- resolve target

def load_rows(clients, cwd_filter, sort):
    want = set(clients or cb.CLIENTS)
    rows = []
    if cb.CLI in want:
        rows += cb.collect_cli()
    if cb.VSCODE in want:
        rows += cb.collect_vscode()
    if cb.DESKTOP in want:
        rows += cb.collect_desktop()

    if cwd_filter:
        prefix = os.getcwd() if cwd_filter == "." else cwd_filter
        prefix = prefix.replace("\\", "/").rstrip("/").lower()
        rows = [r for r in rows
                if (r["cwd"] or "").replace("\\", "/").rstrip("/").lower().startswith(prefix)]

    reverse = sort in ("updated", "created")
    key = (lambda r: r["updated"] or 0) if sort == "updated" \
        else (lambda r: r["created"] or 0) if sort == "created" \
        else (lambda r: (r["cwd"] or "").lower())
    rows.sort(key=key)
    if reverse:
        rows.reverse()
    return rows


def resolve(rows, target):
    """`<N>` row number, `<client>:<prefix>`, or a bare unique id prefix."""
    t = target.strip()
    if ":" in t:
        client, tail = t.split(":", 1)
        if client not in cb.CLIENTS:
            return None, "unknown client %r (use cli/vscode/desktop)" % client
        hits = [r for r in rows if r["client"] == client and _id_or_cwd(r, tail)]
    elif t.isdigit():
        n = int(t)
        if not 1 <= n <= len(rows):
            return None, "row %d out of range (1..%d)" % (n, len(rows))
        return rows[n - 1], None
    else:
        hits = [r for r in rows if _id_or_cwd(r, t.lower())]

    if not hits:
        return None, "no session matches %r" % target
    if len(hits) > 1:
        clients = sorted({r["client"] for r in hits})
        hint = "give more of the id" if len(clients) == 1 \
            else "prefix with `<client>:` and give more of the id"
        return None, "%r matches %d sessions (%s) -- %s" % (
            target, len(hits), ", ".join("%s:%s" % (r["client"], r["id"][:8]) for r in hits[:6]),
            hint)
    return hits[0], None


def _id_or_cwd(row, tail):
    tail = tail.lower()
    return row["id"].lower().startswith(tail) or tail in (row["cwd"] or "").lower()


# ---------------------------------------------------------------- actions

def describe(row):
    print("target  : [%s] %s" % (row["client"], row["id"]))
    print("cwd     : %s" % (row["cwd"] or "(unknown)"))
    print("title   : %s" % row["title"])


def pick_cwd(row, override=None):
    cwd = override or row["cwd"]
    if not cwd or not os.path.isdir(cwd):
        if row["cwd"]:
            print("warn    : %s is not an existing directory" % (override or row["cwd"]))
        return None
    return cwd


def do_cli(row, override, dry_run):
    exe = find_cli_exe()
    if not exe:
        return 1, "no codebuddy executable on PATH"
    cwd = pick_cwd(row, override)
    cmd = [exe, "-r", row["id"]]
    print("exec    : %s  (in %s)" % (" ".join(cmd), cwd or os.getcwd()))
    if dry_run:
        return 0, None
    try:
        return (subprocess.call(cmd, cwd=cwd) if cwd else 1), None
    except Exception as e:
        return 1, str(e)


def do_vscode(row, override, dry_run, open_history):
    code = shutil.which("code") or shutil.which("code.cmd")
    if not code:
        return 1, "no `code` executable on PATH"
    cwd = pick_cwd(row, override)
    if not cwd:
        return 1, "this %s session has no usable cwd; pass --cwd" % row["client"]
    cmd = [code, cwd]
    if open_history:
        cmd += ["--command", VSCODE_HISTORY_CMD]
    print("exec    : %s" % " ".join(cmd))
    print("note    : the extension has no command that opens one conversation; "
          "history panel is as close as it gets")
    if dry_run:
        return 0, None
    try:
        subprocess.Popen(cmd)
    except Exception as e:
        return 1, str(e)
    return 0, None


def do_desktop(row, override, dry_run):
    exe = find_desktop_exe()
    if not exe:
        return 1, "CodeBuddy desktop executable not found"
    cwd = pick_cwd(row, override)
    if not cwd:
        return 1, "this desktop session has no usable cwd; pass --cwd"
    print("exec    : %s %s" % (exe, cwd))
    print("note    : no route found to open one desktop conversation by id; "
          "use the app's own session picker afterwards")
    if dry_run:
        return 0, None
    try:
        subprocess.Popen([exe, cwd])
    except Exception as e:
        return 1, str(e)
    return 0, None


DO = {cb.CLI: do_cli, cb.VSCODE: do_vscode, cb.DESKTOP: do_desktop}


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="cbresume",
        description="Resume / jump into a CodeBuddy session found by cbsessions.py.")
    ap.add_argument("target",
                    help="row number from the listing, `[client:]<id-prefix>`, or a cwd fragment")
    ap.add_argument("--cwd", metavar="DIR", help="run in DIR instead of the session's own cwd")
    ap.add_argument("--client", choices=cb.CLIENTS, action="append", default=[],
                    help="restrict the candidate rows to one client (repeatable)")
    ap.add_argument("--cwd-filter", metavar="PREFIX", dest="cwd_filter",
                    help="restrict candidates to sessions under PREFIX (`.` = here)")
    ap.add_argument("--sort", choices=("updated", "created", "cwd"), default="updated",
                    help="row ordering used to resolve <N>; must match how you listed (default updated)")
    ap.add_argument("--to", choices=cb.CLIENTS, dest="to",
                    help="hand the row off to another client instead of its own")
    ap.add_argument("--history", action="store_true",
                    help="(vscode) also open the extension's conversation history panel")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="print what would run, touch nothing")
    a = ap.parse_args(argv)

    rows = load_rows(a.client, a.cwd_filter, a.sort)
    if not rows:
        print("(no sessions)")
        return 1
    row, err = resolve(rows, a.target)
    if err:
        print("error: %s" % err)
        return 1

    describe(row)
    dest = a.to or row["client"]

    if dest == cb.CLI and row["client"] != cb.CLI:
        # those clients keep no transcript locally, so there is nothing to
        # hand over -- resuming would start a brand new, empty CLI session
        print("skip    : a %s row carries no local transcript, so it can't be "
              "resumed in the cli; `%s <cwd> -p <prompt>` is a new session" % (row["client"], "codebuddy"))
        return 1

    handler = DO[dest]
    if dest == cb.CLI:
        code, err = handler(row, a.cwd, a.dry_run)
    elif dest == cb.VSCODE:
        code, err = handler(row, a.cwd, a.dry_run, a.history)
    else:
        code, err = handler(row, a.cwd, a.dry_run)
    if err:
        print("error: %s" % err)
    return code or 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
