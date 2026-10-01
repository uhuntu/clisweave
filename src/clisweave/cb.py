"""cb - every CodeBuddy session, from all three clients, in one place.

CodeBuddy ships as a CLI, a VS Code extension and a desktop app, and the
three keep their own, mutually unaware stores:

  cli      ~/.codebuddy/projects/<slug>/<sessionId>.jsonl
  vscode   <VS Code>/User/globalStorage/tencent-cloud.coding-copilot/
             genie-history/<b64(workspace)>/conversations/<cid>  (membership)
             message-queue/<hash>.json                           (timestamps)
             todos/<cid>.json  file-changes/<cid>/               (hints)
  desktop  <CodeBuddy>/codebuddy-sessions.vscdb
             ItemTable key "session:<cid>" -> JSON metadata

Only the CLI keeps a transcript. The other two keep metadata, so their rows
can be listed but not resumed or handed off -- `cb resume` says so rather
than starting a fresh, empty session under a resume's name.

`cb` is deliberately separate from `ai`: `ai` weaves tools whose sessions can
all be resumed and handed to one another; these three cannot.
"""
import argparse
import base64
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time

from .sessions import harden_console_output, read_json

CLI = "cli"
VSCODE = "vscode"
DESKTOP = "desktop"
CLIENTS = (CLI, VSCODE, DESKTOP)

HOME = os.path.expanduser("~")


def _app_support():
    """Where Electron apps keep per-user data on this OS. Windows is the
    layout observed on a real install; macOS and Linux follow Electron's
    standard convention and have not been checked against one."""
    if os.name == "nt":
        return os.environ.get("APPDATA") or os.path.join(HOME, "AppData", "Roaming")
    if sys.platform == "darwin":
        return os.path.join(HOME, "Library", "Application Support")
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config")


APP_SUPPORT = _app_support()
CLI_ROOT = os.path.join(HOME, ".codebuddy", "projects")
VS_ROOT = os.path.join(APP_SUPPORT, "Code", "User", "globalStorage",
                       "tencent-cloud.coding-copilot")
VS_GENIE = os.path.join(VS_ROOT, "genie-history")
VS_QUEUE = os.path.join(VS_ROOT, "message-queue")
VS_TODOS = os.path.join(VS_ROOT, "todos")
VS_CHANGES = os.path.join(VS_ROOT, "file-changes")
DESKTOP_DB = os.path.join(APP_SUPPORT, "CodeBuddy", "codebuddy-sessions.vscdb")

# Its own cache: `ai` and `cb` number different rows, and a listing from one
# must never become the other's `resume <N>`.
LIST_CACHE_FILE = os.path.join(HOME, ".cache", "clisweave", "cb_last_list.json")

VSCODE_HISTORY_CMD = "tencentcloud.codingcopilot.chatHistory"

TAG_BLOCK = re.compile(r"<(system-reminder|local-command-stdout|local-command-stderr)"
                       r"\b[^>]*>.*?</\1>", re.S)
TAG_ANY = re.compile(r"<[^>]*>")
WS = re.compile(r"\s+")

USAGE = """Usage: cb [sessions] [--client C] [--cwd PREFIX] [--limit N|all] [--sort S]
                  [--cluster [--window MIN] [--all-clusters]] [--full-id] [--json]
       cb full [...]                    same as `cb sessions --limit all`
       cb resume <N|[client:]id> [--cwd DIR] [--to C] [--history] [--dry-run]

Lists CodeBuddy sessions from the CLI, the VS Code extension and the desktop
app as one table. `cb resume N` takes a row number from the last listing.

  cli      resumes for real: `codebuddy -r <id>` in the session's directory
  vscode   no per-conversation entry point -- opens the workspace
  desktop  no per-session entry point either -- launches the app on the folder
"""


# ---------------------------------------------------------------- helpers

def clean_text(s):
    """Collapse whitespace and strip injected <...> scaffolding from a prompt."""
    if not s:
        return ""
    s = TAG_BLOCK.sub(" ", s)
    s = TAG_ANY.sub(" ", s)
    return WS.sub(" ", s).strip()


def content_text(content):
    """A content field is a list of {"type": ..., "text": ...} blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for blk in content:
            if isinstance(blk, dict):
                t = blk.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return " ".join(parts)
    return ""


def norm_path(p):
    return (p or "").replace("\\", "/").rstrip("/").lower()


def short_cwd(cwd):
    """Shorten a cwd for display: collapse the home prefix."""
    if not cwd:
        return "?"
    cwd = cwd.replace("\\", "/")
    home = HOME.replace("\\", "/")
    if cwd.lower().startswith(home.lower()):
        cwd = "~" + cwd[len(home):]
    return cwd.rstrip("/") or "/"


def ms(epoch_ms):
    if not epoch_ms:
        return ""
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(epoch_ms / 1000.0))
    except (OverflowError, OSError, ValueError):
        return ""


def read_jsonl(path):
    """Tolerant jsonl reader -- a corrupt line never kills the whole file."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield obj


def decode_workspace(name):
    """Undo the extension's dir naming: base64 of the workspace path with
    trailing '_' characters standing in for '=' padding (so "/" -> "Lw__")."""
    i = len(name)
    while i > 0 and name[i - 1] == "_":
        i -= 1
    body, pads = name[:i], len(name) - i
    s = body + "=" * pads
    s += "=" * ((-len(s)) % 4)
    try:
        return base64.b64decode(s, validate=True).decode("utf-8", "replace")
    except Exception:
        return None


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# ---------------------------------------------------------------- cli

def collect_cli():
    out = []
    if not os.path.isdir(CLI_ROOT):
        return out
    for slug in sorted(os.listdir(CLI_ROOT)):
        pdir = os.path.join(CLI_ROOT, slug)
        if not os.path.isdir(pdir):
            continue
        for fn in sorted(os.listdir(pdir)):
            if not fn.endswith(".jsonl"):
                continue
            path = os.path.join(pdir, fn)
            sid = None
            cwd = None
            title = ""
            title_was_ai = False
            first = last = None
            asks = 0
            first_ask = ""
            saw_command_output = False
            try:
                for obj in read_jsonl(path):
                    if sid is None:
                        sid = obj.get("sessionId") or fn[: -len(".jsonl")]
                    if not cwd:
                        cwd = obj.get("cwd")
                    ts = _num(obj.get("timestamp"))
                    if ts is not None:
                        first = ts if first is None else min(first, ts)
                        last = ts if last is None else max(last, ts)
                    t = obj.get("type")
                    if t == "ai-title":
                        v = obj.get("aiTitle")
                        if isinstance(v, str) and v.strip():
                            title = v.strip()
                            title_was_ai = True
                    elif t == "message" and obj.get("role") == "user":
                        asks += 1
                        text = content_text(obj.get("content"))
                        if not first_ask:
                            first_ask = clean_text(text)
                        if "local-command-" in text:
                            saw_command_output = True
                mtime = int(os.path.getmtime(path) * 1000)
            except OSError:
                continue
            if first is None:
                first = last = mtime
            # prompts are wrapped in injected <system-reminder>/<local-command-*>
            # scaffolding; when cleaning leaves nothing behind, say why the
            # row has no text rather than calling a session with prompts empty
            if not title:
                title = first_ask[:120]
            if not title:
                if not asks:
                    title = "(no messages)"
                elif saw_command_output:
                    title = "%d prompt(s), command output only" % asks
                else:
                    title = "%d prompt(s), no plain text" % asks
            out.append({
                "client": CLI, "id": sid or fn[: -len(".jsonl")], "cwd": cwd,
                "title": title,
                "title_source": "ai-title" if title_was_ai else "first-prompt",
                "created": first, "updated": last or first,
                "turns": asks, "note": "",
            })
    return out


# ---------------------------------------------------------------- vscode

def _todo_hint(cid):
    """No title is persisted for extension sessions; the todo list is the
    closest thing to 'what this conversation was about'."""
    obj = read_json(os.path.join(VS_TODOS, cid + ".json"))
    if not isinstance(obj, dict):
        return "", 0
    todos = obj.get("todos")
    if not isinstance(todos, list):
        return "", 0
    first = ""
    for t in todos:
        if isinstance(t, dict) and t.get("content"):
            first = str(t["content"]).strip()
            break
    return first, len(todos)


def _changed_files(cid):
    d = os.path.join(VS_CHANGES, cid)
    if not os.path.isdir(d):
        return 0
    n = 0
    for _root, _dirs, files in os.walk(d):
        n += len(files)
    return n


def collect_vscode():
    if not os.path.isdir(VS_GENIE):
        return []
    # cwd per conversation comes from which workspace dir lists it
    owners = {}
    created_hint = {}
    for ws in sorted(os.listdir(VS_GENIE)):
        cd = os.path.join(VS_GENIE, ws, "conversations")
        if not os.path.isdir(cd):
            continue
        cwd = decode_workspace(ws)
        for cid in os.listdir(cd):
            owners[cid] = cwd
            p = os.path.join(cd, cid)
            if os.path.isdir(p):
                created_hint[cid] = int(os.path.getmtime(p) * 1000)

    # timestamps come from the message-queue snapshots
    stamps = {}
    nitems = {}
    if os.path.isdir(VS_QUEUE):
        for fn in sorted(os.listdir(VS_QUEUE)):
            p = os.path.join(VS_QUEUE, fn)
            if not fn.endswith(".json") or not os.path.isfile(p):
                continue
            obj = read_json(p)
            if not isinstance(obj, dict):
                continue
            convs = obj.get("conversations")
            if not isinstance(convs, dict):
                continue
            for cid, conv in convs.items():
                if not isinstance(conv, dict):
                    continue
                u = _num(conv.get("updatedAt")) or _num(obj.get("lastUpdated"))
                if u is not None:
                    stamps[cid] = max(stamps.get(cid, 0), u)
                items = conv.get("items")
                nitems.setdefault(cid, len(items) if isinstance(items, list) else 0)
                owners.setdefault(cid, None)

    out = []
    for cid in sorted(owners):
        updated = stamps.get(cid)
        created = created_hint.get(cid) or updated
        turns = nitems.get(cid, 0)
        todo_hint, ntodos = _todo_hint(cid)
        files = _changed_files(cid)
        notes = []
        if not turns:
            notes.append("no messages persisted")
        if ntodos:
            notes.append("%d todos" % ntodos)
        if files:
            notes.append("%d file snapshots" % files)
        out.append({
            "client": VSCODE, "id": cid, "cwd": owners.get(cid),
            "title": todo_hint[:120] or "(untitled; no title stored)",
            "title_source": "todo-hint" if todo_hint else "-",
            "created": created, "updated": updated or created,
            "turns": turns, "note": ", ".join(notes),
        })
    return out


# ---------------------------------------------------------------- desktop

def collect_desktop():
    if not os.path.isfile(DESKTOP_DB):
        return []
    rows = []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DESKTOP_DB.replace("\\", "/"), uri=True)
        try:
            rows = list(con.execute("select key, value from ItemTable"))
        finally:
            con.close()
    except sqlite3.Error:
        return []
    out = []
    for key, val in rows:
        if not str(key).startswith("session:"):
            continue
        try:
            obj = json.loads(val)
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        title = obj.get("title")
        created = _num(obj.get("createdAt"))
        out.append({
            "client": DESKTOP,
            "id": obj.get("conversationId") or str(key)[len("session:"):],
            "cwd": obj.get("cwd"),
            "title": title.strip() if isinstance(title, str) and title.strip() else "(no title)",
            "title_source": "stored",
            "created": created, "updated": _num(obj.get("updatedAt")) or created,
            "turns": None, "note": obj.get("status") if isinstance(obj.get("status"), str) else "",
        })
    return out


COLLECTORS = {CLI: collect_cli, VSCODE: collect_vscode, DESKTOP: collect_desktop}


def collect(clients=None):
    rows = []
    for c in CLIENTS:
        if not clients or c in clients:
            rows += COLLECTORS[c]()
    return rows


# ---------------------------------------------------------------- cache

def write_list_cache(rows):
    """Atomic for the same reason `ai`'s is: a truncated cache reads back as
    'nothing listed yet' for a listing that plainly happened."""
    try:
        os.makedirs(os.path.dirname(LIST_CACHE_FILE), exist_ok=True)
        tmp = "%s.%d.tmp" % (LIST_CACHE_FILE, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)
        os.replace(tmp, LIST_CACHE_FILE)
    except OSError:
        pass  # best-effort -- resume-by-number just won't work this time


def read_list_cache():
    rows = read_json(LIST_CACHE_FILE)
    if not isinstance(rows, list):
        return []
    return [r for r in rows if isinstance(r, dict)
            and r.get("client") in CLIENTS and isinstance(r.get("id"), str)]


# ---------------------------------------------------------------- render

COLUMNS = [
    ("#", 4, "l"),
    ("CLIENT", 7, "l"),
    ("ID", 8, "l"),
    ("UPDATED", 11, "l"),
    ("TURNS", 5, "r"),
    ("CWD", 26, "l"),
    ("TITLE", 46, "l"),
]


def fit(s, w, align):
    s = "" if s is None else str(s)
    if len(s) > w:
        s = s[: w - 1] + "…" if w > 1 else s[:w]
    return s.rjust(w) if align == "r" else s.ljust(w)


ID_WIDTH = 8


def _id_width(rows, show_id_full):
    """The ID column is 8 wide, which is enough to tell rows apart -- unless
    the full id was asked for, when it grows to the longest id shown."""
    if not show_id_full:
        return ID_WIDTH
    return max([ID_WIDTH] + [len(r["id"]) for r in rows])


def _header(idw=ID_WIDTH):
    cols = [(h, idw if h == "ID" else w) for h, w, _a in COLUMNS]
    print("  ".join(fit(h, w, "l") for h, w in cols).rstrip())
    return sum(w + 2 for _h, w in cols) - 2


def _row_line(i, r, idw=ID_WIDTH):
    vals = [
        fit(i, 4, "r"),
        fit(r["client"], 7, "l"),
        fit(r["id"], idw, "l"),
        fit(ms(r["updated"]) or "-", 11, "l"),
        fit(r["turns"] if r["turns"] is not None else "?", 5, "r"),
        fit(short_cwd(r["cwd"]), 26, "l"),
        fit(r["title"], 46, "l"),
    ]
    print("  ".join(vals).rstrip())
    if r["note"]:
        print("      note: %s" % r["note"])


def render(rows, show_id_full=False):
    idw = _id_width(rows, show_id_full)
    width = _header(idw)
    print("-" * width)
    for i, r in enumerate(rows, 1):
        _row_line(i, r, idw)
    print("-" * width)
    counts = {}
    for r in rows:
        counts[r["client"]] = counts.get(r["client"], 0) + 1
    print("%d session(s): %s" % (
        len(rows), ", ".join("%s=%d" % (c, counts[c]) for c in CLIENTS if counts.get(c))))


# ------------------------------------------------------- cross-client clusters

def cluster_rows(rows, window_min):
    """The three clients mint their own ids, so nothing links them. The only
    signal is proximity: same cwd, and timestamps within `window_min` of each
    other. A client contributes at most one row per cluster -- the same client
    can't hold one conversation twice, so two nearby rows from the same client
    are genuinely separate sessions and must not be folded together. This is
    a guess about identity, not a fact about it."""
    window = window_min * 60 * 1000
    buckets = {}
    for r in rows:
        key = norm_path(r["cwd"])
        if key:
            buckets.setdefault(key, []).append(r)

    clusters = []
    for items in buckets.values():
        items.sort(key=lambda r: r["updated"] or r["created"] or 0)
        open_cs = []
        for r in items:
            t = r["updated"] or r["created"] or 0
            best = None
            for c in open_cs:
                if r["client"] in c["clients"]:
                    continue
                if c["lo"] - window <= t <= c["hi"] + window:
                    if best is None or abs(t - c["hi"]) < abs(t - best["hi"]):
                        best = c
            if best is None:
                best = {"rows": [], "clients": set(), "lo": t, "hi": t, "cwd": r["cwd"]}
                open_cs.append(best)
            best["rows"].append(r)
            best["clients"].add(r["client"])
            best["lo"] = min(best["lo"], t)
            best["hi"] = max(best["hi"], t)
        clusters.extend(open_cs)
    clusters.sort(key=lambda c: c["hi"], reverse=True)
    return clusters


def render_clusters(clusters, show_id_full=False, multi_only=True):
    shown = [c for c in clusters if len(c["rows"]) > 1] if multi_only else clusters
    idw = _id_width([r for c in shown for r in c["rows"]], show_id_full)
    width = _header(idw)
    print("-" * width)
    for c in shown:
        print("\n[%s]  %s -> %s  (%d client(s): %s)" % (
            short_cwd(c["cwd"]), ms(c["lo"]), ms(c["hi"]),
            len(c["clients"]), "/".join(sorted(c["clients"]))))
        for i, r in enumerate(sorted(c["rows"], key=lambda x: x["updated"] or 0), 1):
            _row_line("%d." % i, r, idw)
    print("\n" + "-" * width)
    print("%d cluster(s) shown; %d rows (%d in multi-client clusters)%s" % (
        len(shown),
        sum(len(c["rows"]) for c in clusters),
        sum(len(c["rows"]) for c in clusters if len(c["rows"]) > 1),
        "" if multi_only else "  [all clusters, incl. singletons]"))


# ---------------------------------------------------------------- sessions

def sort_rows(rows, key):
    if key == "cwd":
        rows.sort(key=lambda r: (r["cwd"] or "").lower())
    elif key == "tool":
        rows.sort(key=lambda r: (r["client"], r["id"]))
    elif key == "created":
        rows.sort(key=lambda r: r["created"] or 0, reverse=True)
    else:
        rows.sort(key=lambda r: r["updated"] or 0, reverse=True)
    return rows


def cmd_sessions(argv, prog="cb sessions"):
    ap = argparse.ArgumentParser(
        prog=prog,
        description="Unified session list across CodeBuddy CLI / VS Code extension / desktop app.")
    ap.add_argument("--client", choices=CLIENTS, action="append", default=[],
                    help="restrict to one client (repeatable)")
    ap.add_argument("--cwd", metavar="PREFIX",
                    help="only sessions whose cwd starts with PREFIX (`.` = current dir)")
    ap.add_argument("--limit", default="20", help="max rows, or `all` (default 20)")
    ap.add_argument("--sort", choices=("updated", "created", "cwd", "tool"), default="updated")
    ap.add_argument("--full-id", action="store_true", help="print full session ids")
    ap.add_argument("--cluster", action="store_true",
                    help="group rows that are probably one conversation split "
                         "across clients (same cwd + close in time)")
    ap.add_argument("--window", metavar="MINUTES", type=float, default=120,
                    help="time proximity window for --cluster (default 120)")
    ap.add_argument("--all-clusters", action="store_true",
                    help="with --cluster, also show single-row groups")
    ap.add_argument("--json", action="store_true", dest="as_json",
                    help="machine-readable output instead of a table")
    a = ap.parse_args(argv)

    rows = collect(set(a.client))

    if a.cwd:
        prefix = norm_path(os.getcwd() if a.cwd == "." else a.cwd)
        rows = [r for r in rows if norm_path(r["cwd"]).startswith(prefix)]

    sort_rows(rows, a.sort)

    if a.cluster:
        # cluster over the full set first -- truncating rows beforehand would
        # drop the very rows that link two clients
        clusters = cluster_rows(rows, a.window)
        if a.as_json:
            json.dump([{
                "cwd": c["cwd"], "start": c["lo"], "end": c["hi"],
                "clients": sorted(c["clients"]), "sessions": c["rows"],
            } for c in clusters], sys.stdout, ensure_ascii=False, indent=2)
            print()
        elif clusters:
            render_clusters(clusters, a.full_id, multi_only=not a.all_clusters)
        else:
            print("(no sessions)")
        return 0

    if a.limit != "all":
        rows = rows[:int(a.limit)] if a.limit.isdigit() else rows[:20]

    # the numbers on screen are the numbers `cb resume` will accept
    write_list_cache(rows)

    if a.as_json:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
        print()
    elif rows:
        render(rows, a.full_id)
    else:
        print("(no sessions)")
    return 0


# ---------------------------------------------------------------- resume

def find_cli_exe():
    """.cmd wins over a bare name: on Windows npm drops a .ps1 and a .cmd, and
    only the .cmd can be spawned without a shell."""
    for name in ("codebuddy.cmd", "codebuddy.exe", "codebuddy", "cbc.cmd", "cbc"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _reg_protocol_exe():
    """Read the codebuddy:// handler out of HKCU, Windows only."""
    if os.name != "nt":
        return None
    try:
        out = subprocess.run(
            ["reg", "query", r"HKCU\Software\Classes\codebuddy\shell\open\command"],
            capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return None
    for line in out.splitlines():
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
    for cand in (os.path.join(base, "Programs", "CodeBuddy", "CodeBuddy.exe") if base else None,
                 shutil.which("CodeBuddy.exe"), shutil.which("codebuddy-desktop")):
        if cand and os.path.isfile(cand):
            return cand
    return None


def _id_or_cwd(row, tail):
    tail = tail.lower()
    return row["id"].lower().startswith(tail) or tail in (row["cwd"] or "").lower()


def resolve(target):
    """`<N>` is a row of the last listing; `<client>:<prefix>` or a bare
    prefix is matched against every session on disk. Returns (row, error)."""
    t = target.strip()
    if t.isdigit():
        cache = read_list_cache()
        if not cache:
            return None, "no session list cached yet -- run `cb` first"
        n = int(t)
        if not 1 <= n <= len(cache):
            return None, "row %d out of range (last listing had %d rows)" % (n, len(cache))
        return cache[n - 1], None

    rows = collect()
    if ":" in t:
        client, tail = t.split(":", 1)
        if client not in CLIENTS:
            return None, "unknown client %r (use cli, vscode or desktop)" % client
        hits = [r for r in rows if r["client"] == client and _id_or_cwd(r, tail)]
    else:
        hits = [r for r in rows if _id_or_cwd(r, t)]

    if not hits:
        return None, "no session matches %r" % target
    if len(hits) > 1:
        same_client = len({r["client"] for r in hits}) == 1
        hint = "give more of the id" if same_client \
            else "prefix with `<client>:` and give more of the id"
        return None, "%r matches %d sessions (%s) -- %s" % (
            target, len(hits), ", ".join("%s:%s" % (r["client"], r["id"][:8]) for r in hits[:6]),
            hint)
    return hits[0], None


def describe(row):
    print("target  : [%s] %s" % (row["client"], row["id"]))
    print("cwd     : %s" % (row["cwd"] or "(unknown)"))
    print("title   : %s" % row["title"])


def pick_cwd(row, override=None):
    cwd = override or row["cwd"]
    if cwd and os.path.isdir(cwd):
        return cwd
    if cwd:
        print("warn    : %s is not an existing directory" % cwd)
    return None


def run_interactive(cmd, cwd):
    """Run the CLI in this terminal and pass its exit code through. The child
    owns the console; Ctrl+C reaches both of us, so don't print a traceback."""
    try:
        return subprocess.call(cmd, cwd=cwd), None
    except KeyboardInterrupt:
        return 130, None
    except OSError as e:
        return 127, "could not run %s: %s" % (cmd[0], e)


def do_cli(row, override, dry_run, _history):
    exe = find_cli_exe()
    if not exe:
        return 1, "no codebuddy executable on PATH"
    cwd = pick_cwd(row, override)
    if not cwd:
        # a session is tied to the directory it began in; resuming from
        # elsewhere would find nothing to resume
        return 1, "no usable directory for this session; pass --cwd"
    cmd = [exe, "-r", row["id"]]
    print("exec    : %s  (in %s)" % (" ".join(cmd), cwd))
    if dry_run:
        return 0, None
    return run_interactive(cmd, cwd)


def do_vscode(row, override, dry_run, history):
    code = shutil.which("code") or shutil.which("code.cmd")
    if not code:
        return 1, "no `code` executable on PATH"
    cwd = pick_cwd(row, override)
    if not cwd:
        return 1, "no usable directory for this session; pass --cwd"
    cmd = [code, cwd]
    if history:
        cmd += ["--command", VSCODE_HISTORY_CMD]
    print("exec    : %s" % " ".join(cmd))
    print("note    : the extension has no command that opens one conversation; "
          "its history panel is as close as it gets")
    if dry_run:
        return 0, None
    try:
        subprocess.Popen(cmd)
    except OSError as e:
        return 1, "could not run code: %s" % e
    return 0, None


def do_desktop(row, override, dry_run, _history):
    exe = find_desktop_exe()
    if not exe:
        return 1, "CodeBuddy desktop executable not found"
    cwd = pick_cwd(row, override)
    if not cwd:
        return 1, "no usable directory for this session; pass --cwd"
    print("exec    : %s %s" % (exe, cwd))
    print("note    : no route opens one desktop conversation by id; "
          "use the app's own session picker afterwards")
    if dry_run:
        return 0, None
    try:
        subprocess.Popen([exe, cwd])
    except OSError as e:
        return 1, "could not run %s: %s" % (exe, e)
    return 0, None


DO = {CLI: do_cli, VSCODE: do_vscode, DESKTOP: do_desktop}


def cmd_resume(argv, prog="cb resume"):
    ap = argparse.ArgumentParser(
        prog=prog, description="Resume / jump into a CodeBuddy session found by `cb`.")
    ap.add_argument("target",
                    help="row number from the last `cb` listing, or `[client:]<id-prefix>`")
    ap.add_argument("--cwd", metavar="DIR", help="run in DIR instead of the session's own directory")
    ap.add_argument("--to", choices=CLIENTS, dest="to",
                    help="open the row's directory in another client instead of its own")
    ap.add_argument("--history", action="store_true",
                    help="(vscode) also open the extension's conversation history panel")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="print what would run, touch nothing")
    a = ap.parse_args(argv)

    row, err = resolve(a.target)
    if err:
        print("cb: %s" % err, file=sys.stderr)
        return 1

    describe(row)
    dest = a.to or row["client"]

    if dest == CLI and row["client"] != CLI:
        # those clients keep no transcript locally, so there is nothing to
        # hand over: this would be a brand new, empty CLI session
        print("cb: a %s row carries no local transcript, so it can't be resumed "
              "in the cli" % row["client"], file=sys.stderr)
        return 1

    code, err = DO[dest](row, a.cwd, a.dry_run, a.history)
    if err:
        print("cb: %s" % err, file=sys.stderr)
    return code or 0


# ---------------------------------------------------------------- entry

def main(argv=None):
    harden_console_output()
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0
    sub = "sessions"
    if args and not args[0].startswith("-"):
        sub, args = args[0], args[1:]
    if sub == "sessions":
        return cmd_sessions(args)
    if sub == "full":
        return cmd_sessions(["--limit", "all"] + args, prog="cb full")
    if sub == "resume":
        return cmd_resume(args)
    print("cb: unknown command '%s' (try sessions, full or resume)" % sub, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
