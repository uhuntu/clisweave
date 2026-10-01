#!/usr/bin/env python3
"""cbsessions - list CodeBuddy sessions from all three clients as one table.

The three CodeBuddy clients keep their own, mutually unaware stores:

  cli      ~/.codebuddy/projects/<slug>/<sessionId>.jsonl
  vscode   .../User/globalStorage/tencent-cloud.coding-copilot/
           genie-history/<b64(workspace)>/conversations/<cid>   (membership)
           message-queue/<hash>.json                            (timestamps)
           todos/<cid>.json  file-changes/<cid>/                (hints)
  desktop  ~/.../CodeBuddy/codebuddy-sessions.vscdb
           ItemTable key "session:<cid>" -> JSON metadata

Stdlib only. Read-only.
"""
import argparse
import base64
import json
import os
import re
import sqlite3
import sys
import time

CLI = "cli"
VSCODE = "vscode"
DESKTOP = "desktop"
CLIENTS = (CLI, VSCODE, DESKTOP)

HOME = os.path.expanduser("~")
CLI_ROOT = os.path.join(HOME, ".codebuddy", "projects")
VS_ROOT = os.path.join(HOME, "AppData", "Roaming", "Code", "User", "globalStorage",
                       "tencent-cloud.coding-copilot")
VS_GENIE = os.path.join(VS_ROOT, "genie-history")
VS_QUEUE = os.path.join(VS_ROOT, "message-queue")
VS_TODOS = os.path.join(VS_ROOT, "todos")
VS_CHANGES = os.path.join(VS_ROOT, "file-changes")
DESKTOP_DB = os.path.join(HOME, "AppData", "Roaming", "CodeBuddy", "codebuddy-sessions.vscdb")

TAG_BLOCK = re.compile(r"<(system-reminder|local-command-stdout|local-command-stderr)"
                       r"\b[^>]*>.*?</\1>", re.S)
TAG_ANY = re.compile(r"<[^>]*>")
WS = re.compile(r"\s+")


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
    return time.strftime("%m-%d %H:%M", time.localtime(epoch_ms / 1000.0))


def read_jsonl(path):
    """Tolerant jsonl reader -- a corrupt line never kills the whole file."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


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
            raw_ask = ""
            for obj in read_jsonl(path):
                if sid is None:
                    sid = obj.get("sessionId") or fn[: -len(".jsonl")]
                if not cwd:
                    cwd = obj.get("cwd")
                ts = obj.get("timestamp")
                if isinstance(ts, (int, float)):
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
                    if not raw_ask and text.strip():
                        raw_ask = WS.sub(" ", text.strip())
            if not first:
                first = last = int(os.path.getmtime(path) * 1000)
            # prompts are wrapped in injected <system-reminder>/<local-command-*>
            # scaffolding; when cleaning leaves nothing behind fall back to the
            # raw text so a session with real prompts isn't labelled empty
            if not title:
                title = first_ask[:120]
            if not title:
                if not asks:
                    title = "(no messages)"
                elif "local-command-" in raw_ask:
                    # every prompt was a command result fed back into the session
                    title = "%d prompt(s), command output only" % asks
                else:
                    title = "%d prompt(s), no plain text" % asks
            out.append({
                "client": CLI, "id": sid or fn, "cwd": cwd,
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
    p = os.path.join(VS_TODOS, cid + ".json")
    if not os.path.isfile(p):
        return "", 0
    try:
        obj = json.load(open(p, encoding="utf-8"))
    except Exception:
        return "", 0
    todos = obj.get("todos") or []
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
    for root, _dirs, files in os.walk(d):
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
    if os.path.isdir(VS_QUEUE):
        for fn in sorted(os.listdir(VS_QUEUE)):
            p = os.path.join(VS_QUEUE, fn)
            if not fn.endswith(".json") or not os.path.isfile(p):
                continue
            try:
                obj = json.load(open(p, encoding="utf-8"))
            except Exception:
                continue
            for cid, conv in (obj.get("conversations") or {}).items():
                if not isinstance(conv, dict):
                    continue
                u = conv.get("updatedAt") or obj.get("lastUpdated")
                if isinstance(u, (int, float)):
                    stamps[cid] = max(stamps.get(cid, 0), u)
                items = conv.get("items") or []
                stamps.setdefault(cid + ":items", len(items))
                owners.setdefault(cid, None)

    out = []
    for cid in sorted(set(owners) | set(k for k in stamps if not k.endswith(":items"))):
        updated = stamps.get(cid)
        nitems = stamps.get(cid + ":items")
        created = created_hint.get(cid) or updated
        todo_hint, ntodos = _todo_hint(cid)
        files = _changed_files(cid)
        notes = []
        if not nitems:
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
            "turns": nitems or 0, "note": ", ".join(notes),
        })
    return out


# ---------------------------------------------------------------- desktop

def collect_desktop():
    if not os.path.isfile(DESKTOP_DB):
        return []
    out = []
    con = sqlite3.connect("file:%s?mode=ro" % DESKTOP_DB, uri=True)
    try:
        rows = list(con.execute("select key, value from ItemTable"))
    except Exception:
        rows = []
    finally:
        con.close()
    for key, val in rows:
        if not str(key).startswith("session:"):
            continue
        try:
            obj = json.loads(val)
        except Exception:
            continue
        out.append({
            "client": DESKTOP, "id": obj.get("conversationId") or str(key)[len("session:"):],
            "cwd": obj.get("cwd"),
            "title": (obj.get("title") or "").strip() or "(no title)",
            "title_source": "stored",
            "created": obj.get("createdAt"), "updated": obj.get("updatedAt") or obj.get("createdAt"),
            "turns": None, "note": obj.get("status") or "",
        })
    return out


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
        s = s[: w - 1] + "\u2026" if w > 1 else s[:w]
    return s.rjust(w) if align == "r" else s.ljust(w)


def _header():
    line = []
    total = 0
    for head, w, _a in COLUMNS:
        line.append(fit(head, w, "l"))
        total += w + 2
    print("  ".join(line).rstrip())
    return total - 2


def _row_line(i, r, show_id_full=False):
    vals = [
        fit(i, 4, "r"),
        fit(r["client"], 7, "l"),
        fit(r["id"] if show_id_full else r["id"][:8], 8, "l"),
        fit(ms(r["updated"]) or "-", 11, "l"),
        fit(r["turns"] if r["turns"] is not None else "?", 5, "r"),
        fit(short_cwd(r["cwd"]), 26, "l"),
        fit(r["title"], 46, "l"),
    ]
    print("  ".join(vals).rstrip())
    if r["note"]:
        print("      note: %s" % r["note"])


def render(rows, show_id_full=False):
    width = _header()
    print("-" * width)
    for i, r in enumerate(rows, 1):
        _row_line(i, r, show_id_full)
    print("-" * width)
    counts = {}
    for r in rows:
        counts[r["client"]] = counts.get(r["client"], 0) + 1
    print("%d session(s): %s" % (
        len(rows), ", ".join("%s=%d" % (c, counts.get(c, 0)) for c in CLIENTS if counts.get(c))))


# ------------------------------------------------------- cross-client clusters

def cluster_rows(rows, window_min):
    """The three clients mint their own ids, so nothing links them. The only
    signal is proximity: same cwd, and timestamps within `window_min` of each
    other. A client contributes at most one row per cluster -- the same client
    can't hold one conversation twice, so two nearby rows from the same client
    are genuinely separate sessions and must not be folded together."""
    window = window_min * 60 * 1000
    buckets = {}
    for r in rows:
        key = (r["cwd"] or "").replace("\\", "/").rstrip("/").lower()
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
    width = _header()
    print("-" * width)
    n = 0
    for c in shown:
        span = ms(c["lo"]) + " -> " + ms(c["hi"])
        print("\n[%s]  %s  (%d client(s): %s)" % (
            short_cwd(c["cwd"]), span, len(c["clients"]), "/".join(sorted(c["clients"]))))
        for i, r in enumerate(sorted(c["rows"], key=lambda x: x["updated"] or 0), 1):
            _row_line("%d." % i, r, show_id_full)
    print("\n-" * 1 + "-" * (width - 1))
    print("%d cluster(s) shown; %d rows (%d in multi-client clusters)%s" % (
        len(shown),
        sum(len(c["rows"]) for c in clusters),
        sum(len(c["rows"]) for c in clusters if len(c["rows"]) > 1),
        "" if multi_only else "  [all clusters, incl. singletons]"))


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="cbsessions",
        description="Unified session list across CodeBuddy CLI / VS Code extension / desktop app.")
    ap.add_argument("--client", choices=CLIENTS, action="append", default=[],
                    help="restrict to one client (repeatable)")
    ap.add_argument("--cwd", metavar="PREFIX",
                    help="only sessions whose cwd starts with PREFIX (`.` = current dir)")
    ap.add_argument("--limit", default="20",
                    help="max rows, or `all` (default 20)")
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

    want = set(a.client or CLIENTS)
    rows = []
    if CLI in want:
        rows += collect_cli()
    if VSCODE in want:
        rows += collect_vscode()
    if DESKTOP in want:
        rows += collect_desktop()

    if a.cwd:
        prefix = os.getcwd() if a.cwd == "." else a.cwd
        prefix = prefix.replace("\\", "/").rstrip("/").lower()
        rows = [r for r in rows
                if (r["cwd"] or "").replace("\\", "/").rstrip("/").lower().startswith(prefix)]

    reverse = True
    if a.sort == "cwd":
        rows.sort(key=lambda r: (r["cwd"] or "").lower())
        reverse = False
    elif a.sort == "tool":
        rows.sort(key=lambda r: (r["client"], r["id"]))
        reverse = False
    elif a.sort == "created":
        rows.sort(key=lambda r: r["created"] or 0)
    else:
        rows.sort(key=lambda r: r["updated"] or 0)

    if reverse:
        rows.reverse()

    limit = None if a.limit == "all" else int(a.limit) if a.limit.isdigit() else 20

    if a.cluster:
        # cluster over the full set first -- truncating rows beforehand would
        # drop the very rows that link two clients
        clusters = cluster_rows(rows, a.window)
        if a.as_json:
            json.dump([{
                "cwd": c["cwd"], "start": c["lo"], "end": c["hi"],
                "clients": sorted(c["clients"]), "sessions": c["rows"],
            } for c in clusters], sys.stdout, ensure_ascii=False, indent=2, default=str)
            print()
            return 0
        if not clusters:
            print("(no sessions)")
            return 0
        shown_all = a.all_clusters
        render_clusters(clusters, a.full_id, multi_only=not shown_all)
        return 0

    if limit is not None:
        rows = rows[:limit]

    if a.as_json:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2, default=str)
        print()
    elif rows:
        render(rows, a.full_id)
    else:
        print("(no sessions)")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    sys.exit(main())
