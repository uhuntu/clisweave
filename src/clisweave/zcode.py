"""Reader for the ZCode session store: a SQLite database under
~/.zcode/cli/db (opencode-style `session`/`message`/`part` tables, WAL mode)
shared by the ZCode desktop app and the `zcode` terminal CLI.

Unlike the file-backed tools there is no per-session transcript file -- the
conversation lives in the `part` table as typed JSON rows (text, reasoning,
tool, file, ...), the cwd is `session.directory`, and the title is a stored
one ZCode's own auto-titler keeps updated.

Two executables answer to the name `zcode`: the desktop app (an Electron
binary, e.g. /usr/bin/zcode) and a terminal CLI that often shadows it on
PATH (~/.local/bin/zcode, v0.16.9 here). They are told apart by their file
headers -- the CLI ships as a script wrapper, the desktop as an ELF binary
-- because probing either with --help/--version has a side effect on the
desktop one: it boots the GUI (checked against both installs). When the
CLI is the one on PATH, resume and handoff seeding are real:
`zcode --resume <sess_...>` reopens one session, and `zcode -p <seed>`
persists a headless run that can then be resumed -- the same two-step a
kimi handoff needs. Only the desktop falls back to the workspace deep
link, which opens the right workspace but no particular session.

The store belongs to a live app, so every connection is read-only (`mode=ro`
-- the same contract cb.py's desktop collector uses) and a missing file or
table reads as an empty store rather than an error. Rows still being written
are invisible to a query mid-transaction at worst, never half-read: SQLite
transactions are atomic.
"""
import json
import os
import shutil
import sqlite3
import urllib.parse

from . import common



ZCODE_DB = os.path.join(common.HOME, ".zcode", "cli", "db", "db.sqlite")

# The only route back into a specific workspace: registered in zcode.desktop
# (MimeType=x-scheme-handler/zcode), and the app.asar routes table carries
# `workspace/open?path=${encoded}` -- with the path fully percent-encoded,
# JavaScript's encodeURIComponent spelling. No session-id route exists.
ZCODE_WORKSPACE_LINK = "zcode://workspace/open?path=%s"

# How many messages the title fallback looks at before giving up -- the
# opening of the conversation is where a request would be; this mirrors the
# bound every other tool's title scan carries (common.TITLE_SCAN_LINES).
TITLE_SCAN_MESSAGES = 50


def _connect():
    """A read-only connection to the store, or None when there is nothing
    to read.

    `mode=ro` never creates the file and never writes (not even the WAL
    checkpoints a read-write connection performs), which is what sharing a
    database with a running app requires. The backslash swap keeps the URI
    legal on Windows."""
    if not os.path.isfile(ZCODE_DB):
        return None
    try:
        return sqlite3.connect("file:%s?mode=ro" % ZCODE_DB.replace("\\", "/"), uri=True)
    except sqlite3.Error:
        return None


def _query(query, params=()):
    """All rows of a query, or [] when the store is missing or the schema is
    not the one this reader knows. A future ZCode that renames a column is a
    store this tool can't see -- the same treatment an absent store gets --
    not a traceback over every command at once."""
    con = _connect()
    if con is None:
        return []
    try:
        return con.execute(query, params).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _json(data):
    if not isinstance(data, str):
        return {}
    try:
        value = json.loads(data)
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _epoch(ms):
    """The store keeps times as epoch milliseconds; the listing works in
    seconds. A NULL (a session row flushed before its clock stamp) reads as
    0, which `relative_time` renders as "?" -- the same answer an absent
    timestamp deserves."""
    try:
        return int((ms or 0) / 1000)
    except (TypeError, ValueError):
        return 0


def _session_row(sid):
    for row in _query("SELECT id, directory, title, time_created, time_updated, parent_id "
                      "FROM session WHERE id = ?", (sid,)):
        return row
    return None


def zcode_light_records(show_all=False):
    """One record per ZCode session.

    Sessions with a `parent_id` are subagent runs ZCode started for itself --
    the same category as step's `subagent-` files -- and are left out unless
    --all asks for them."""
    records = []
    for sid, directory, title, created, updated, parent in _query(
            "SELECT id, directory, title, time_created, time_updated, parent_id FROM session"):
        if not isinstance(sid, str) or not sid:
            continue
        if parent and not show_all:
            continue
        records.append({
            "tool": "zcode", "id": sid,
            "ts": _epoch(updated if updated is not None else created),
            "path": None,  # the transcript is in the store, not in a file
            "cwd": directory if isinstance(directory, str) and directory else None,
            "title": _row_title(sid, title),
            "started": _epoch(created),
        })
    return records


def zcode_resolve(prefix, show_all=False):
    matches = []
    for r in zcode_light_records(show_all=True):
        if r["id"].startswith(prefix):
            matches.append(r["id"])
    return sorted(set(matches))


def zcode_session_cwd(sid):
    row = _session_row(sid)
    if row is None:
        return None
    return row[1] if isinstance(row[1], str) and row[1] else None


def _message_parts(message_id):
    """The typed part rows of one message, in stored order."""
    return [_json(data) for (data,) in _query(
        "SELECT data FROM part WHERE message_id = ? ORDER BY sequence", (message_id,))]


def _iter_messages(sid):
    """(role, message_id) per message of a session, in conversation order.

    The role lives inside message.data; the message's words live in its part
    rows. A message whose data failed to parse still yields -- with a None
    role it simply never matches a role filter downstream."""
    for mid, data in _query(
            "SELECT id, data FROM message WHERE session_id = ? ORDER BY sequence", (sid,)):
        yield _json(data).get("role"), mid


def _part_text(part):
    """What a `text` part says, or "" -- see common.block_text for why the
    string type is checked rather than assumed."""
    if part.get("type") != "text":
        return ""
    text = part.get("text")
    return text if isinstance(text, str) else ""


def _first_prompt(sid):
    """(genuine_title, first_prompt_at_all) from the session's user messages.

    ZCode titles its own sessions, so this is only the fallback for a row
    whose stored title never got written -- but the fallback must then be as
    good as the other tools': injected reminders, pasted transcripts and
    captionless screenshots name nothing (the same filters claude's and
    step's title readers apply)."""
    fallback = None
    title = None
    for i, (role, mid) in enumerate(_iter_messages(sid)):
        if i >= TITLE_SCAN_MESSAGES or (title is not None and fallback is not None):
            break
        if role != "user":
            continue
        for part in _message_parts(mid):
            text = _part_text(part)
            if not text.strip():
                continue
            stripped = " ".join(text.split())
            if common.is_image_only(stripped) or common._is_injected_or_pasted(text):
                continue
            if fallback is None:
                fallback = stripped[:70]
            if title is None and not common.is_trivial_title(stripped):
                title = stripped[:70]
                break
    return title, fallback


def _row_title(sid, stored):
    """The stored title when it is a real one, else the first genuine
    prompt. ZCode keeps `session.title` current through its own auto-titler,
    so this normally returns without a message scan."""
    title = common._as_title(stored)
    if title:
        return title
    genuine, fallback = _first_prompt(sid)
    return common._title_or_placeholder(genuine, fallback)


def zcode_title_and_cwd(sid):
    """(title, cwd) for the row renderer -- the stored title (with the
    first-prompt fallback) and the session's recorded directory."""
    row = _session_row(sid)
    if row is None:
        return "(no title)", None
    _sid, directory, title, _created, _updated, _parent = row
    return _row_title(sid, title), (directory if isinstance(directory, str) else None)


# Probed once per process: whether this interpreter's SQLite can test the
# role inside SQLite (json_extract). Python 3.9/3.10 can bundle a SQLite
# built without JSON1, and turn counting then parses the rows here instead.
_json1 = None


def _has_json1():
    global _json1
    if _json1 is None:
        _json1 = False
        con = _connect()
        if con is not None:
            try:
                con.execute("SELECT json_extract('{}', '$.a')")
                _json1 = True
            except sqlite3.Error:
                pass
            finally:
                con.close()
    return _json1


def zcode_session_turns(sid):
    """How many user messages the session holds, or None when it holds none.

    A turn is the user speaking, the same semantics the other tools' byte
    counts use -- but there are no bytes to count here, so this is a real
    query. Zero reads as None, the convention session_turns documents: an
    empty count and an uncountable store render the same way."""
    if _has_json1():
        rows = _query("SELECT json_extract(data, '$.role') FROM message WHERE session_id = ?",
                      (sid,))
        turns = sum(1 for (role,) in rows if role == "user")
    else:
        turns = sum(1 for role, _mid in _iter_messages(sid) if role == "user")
    return turns or None


def zcode_handoff_messages(sid):
    """Every user and assistant message of a session, in order, as the
    (role, text) pairs a handoff export is built from.

    A `tool` part is a call and its result -- what happened, which the
    receiving session can reproduce -- and a `reasoning` part is the model's
    thinking. Both are left out so the export is what was said, not
    everything that happened, the same line step's and claude's exporters
    hold."""
    messages = []
    for role, mid in _iter_messages(sid):
        if role not in ("user", "assistant"):
            continue
        texts = []
        for part in _message_parts(mid):
            text = _part_text(part)
            if text.strip():
                texts.append(text)
        if texts:
            messages.append((role, "\n\n".join(texts)))
    return messages


def _tool_part_text(part):
    """Human-meaningful text from a `tool` part: the command a shell call
    carries, else the tool name and its input. A term often appears only
    inside a call's arguments, which is why step's and codex's snippets and
    scans reach into theirs too."""
    state = part.get("state")
    state = state if isinstance(state, dict) else {}
    name = part.get("tool") if isinstance(part.get("tool"), str) else ""
    arguments = state.get("input")
    if not isinstance(arguments, dict):
        arguments = {}
    command = arguments.get("command") or arguments.get("cmd")
    if isinstance(command, list):
        command = " ".join(str(piece) for piece in command)
    if isinstance(command, str) and command.strip():
        return command.strip()
    body = json.dumps(arguments, ensure_ascii=False, default=str) if arguments else ""
    return " ".join(piece for piece in (name, body) if piece)[:400]


def _all_texts(sid, with_tool_output=False):
    """Every text this session's content offers, in conversation order:
    user and assistant text parts, plus tool calls -- and their outputs when
    `with_tool_output` (for the literal scan, where a term may appear only in
    a result; codex's scan reads outputs for the same reason)."""
    texts = []
    for role, mid in _iter_messages(sid):
        for part in _message_parts(mid):
            kind = part.get("type")
            if kind == "text":
                text = _part_text(part)
                if text.strip():
                    texts.append(text)
            elif kind == "tool":
                text = _tool_part_text(part)
                if text:
                    texts.append(text)
                if with_tool_output:
                    state = part.get("state")
                    output = state.get("output") if isinstance(state, dict) else None
                    if isinstance(output, str) and output.strip():
                        texts.append(output)
    return texts


def zcode_snippet(sid, max_messages=12, max_chars=800):
    """Longer excerpt than a title, for `ai search` -- sampled evenly across
    the conversation (common.sample_stride, see its docstring for why first-N
    is not enough), text plus tool calls, thinking left out (it runs long and
    the substance of a session is in what was said and done)."""
    texts = []
    for text in _all_texts(sid):
        flat = " ".join(text.split())
        if flat:
            texts.append(flat)
    return common.join_with_fair_budget(common.sample_stride(texts, max_messages), max_chars)


def zcode_contains(sid, pattern):
    """True if the pattern matches any conversation text of the session:
    what was said, what tool calls carried, and what they returned. This is
    the SQLite-store twin of the literal file scan the other tools get."""
    return any(pattern.search(text) for text in _all_texts(sid, with_tool_output=True))


def zcode_last_message(sid):
    """What the session ended on: the last thing anyone actually said.

    Walks backwards from the newest message, past tool calls, reasoning and
    injected reminders, to the newest text part either party spoke -- the
    same filters session_last_message applies to the file-backed tools."""
    rows = _query(
        "SELECT m.data, p.data FROM part p JOIN message m ON p.message_id = m.id "
        "WHERE p.session_id = ? ORDER BY m.sequence DESC, p.sequence DESC LIMIT 40", (sid,))
    for mdata, pdata in rows:
        role = _json(mdata).get("role")
        if role not in ("user", "assistant"):
            continue
        text = _part_text(_json(pdata))
        if not text.strip():
            continue
        if common.is_image_only(text) or common._is_injected_or_pasted(text):
            continue
        return " ".join(text.split())
    return None


def zcode_cli_on_path():
    """True when the `zcode` on PATH is the terminal CLI -- the one that
    resumes a session by id and seeds one headlessly -- rather than the
    desktop app's launcher.

    Told apart by file header, never by running them: the CLI ships as a
    script wrapper (`#!`), the desktop as an ELF binary, and asking either
    --help or --version boots the desktop's GUI as a side effect (checked
    against both installs). A machine with only the desktop reads False,
    and every caller falls back to the deep link."""
    exe = shutil.which("zcode")
    if not exe:
        return False
    try:
        with open(exe, "rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def zcode_newest_session_since(started):
    """The newest zcode session created at/after `started` (epoch seconds)
    whose directory is the current one -- the session a just-finished
    `zcode -p` seed run persisted, recovered from the store the same way
    kimi's is (the CLI prints no id hint of its own)."""
    best_id = None
    best_ts = None
    for r in zcode_light_records(show_all=True):
        cwd = r.get("cwd")
        if not cwd or os.path.realpath(cwd) != os.path.realpath(os.getcwd()):
            continue
        ts = r.get("ts") or 0
        if ts < started - 2:
            continue
        if best_ts is None or ts >= best_ts:
            best_id, best_ts = r["id"], ts
    return best_id


def zcode_workspace_link(directory):
    """The deep link that opens ZCode on a workspace directory. There is no
    session-level route -- the app's own task list picks up from there."""
    return ZCODE_WORKSPACE_LINK % urllib.parse.quote(directory or "", safe="")
