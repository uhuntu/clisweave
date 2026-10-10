"""Reader for the step CLI's session store: one JSONL per session under
~/.stepcode/agent/sessions (or $STEP_CODING_AGENT_SESSION_DIR), grouped by
the encoded cwd -- the layout claude uses, and the same "every
non-alphanumeric is a dash" encoding, so decode_project_dir_name reads it.
The authoritative id and cwd are the `session` record on the file's first
line; the filename carries a timestamp prefix, except for the
`subagent-<uuid>` files step's own subagents write, which have none."""
import calendar
import glob
import json
import os
import re
import time

from . import common



STEP_SESSIONS = os.environ.get(
    "STEP_CODING_AGENT_SESSION_DIR",
    os.path.join(common.HOME, ".stepcode", "agent", "sessions"))

# step's own session-file naming: `<timestamp>_<id>.jsonl`, timestamp in the
# form it writes headers with ("2026-09-27T03:20:47.723Z", colons and dot
# turned into dashes). Anything before the first underscore that looks like
# one is the prefix; what follows is the id.
STEP_FILE_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[^_]*_(.+)$")

# step marks a pasted image with a *text* block reading "[Image #1]" rather
# than claude's "[Image: source: ...]" spelling.
STEP_IMAGE_PREFIX = "[Image #"


def parse_step_timestamp(value):
    """step's ISO-8601-with-Z timestamps ("2026-09-27T03:20:47.723Z") to
    seconds since epoch, or 0. Truncated to whole seconds, which is all the
    listing's relative time needs."""
    if not isinstance(value, str):
        return 0
    try:
        return calendar.timegm(time.strptime(value[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0


def step_session_header(path):
    """(id, cwd, started, name) from a step session file's opening record, or
    (None, None, 0, None). Reads only as far as the first session record --
    the header is the first line step writes.

    `name` is the display name set with `step --name` or /name. The format
    keeps it on the header, so it is there from the first line rather than
    being discovered later the way kimi's own generated name is."""
    for i, d in enumerate(common.read_jsonl(path)):
        if i > 20:
            break
        if isinstance(d, dict) and d.get("type") == "session":
            sid = d.get("id")
            cwd = d.get("cwd")
            name = d.get("name")
            return (sid if isinstance(sid, str) else None,
                    cwd if isinstance(cwd, str) else None,
                    parse_step_timestamp(d.get("timestamp")),
                    name if isinstance(name, str) else None)
    return None, None, 0, None


def step_session_id_from_filename(path):
    """The id to use for a session file whose header never gave one.

    step names its session files `<timestamp>_<id>.jsonl`, so the id is
    still in the name -- but only the id: keeping the timestamp as well put
    `2026-09-27T` in the row's ID column, stopped a uuid prefix from
    resolving here (step_resolve matches on the start of the id), and handed
    `step --resume` a string that is neither the path it accepts nor the
    partial uuid it accepts. A name with no timestamp in front is kept whole
    -- `subagent-<uuid>` in particular, since the subagent filter keys on the
    id starting with that and a headerless subagent file would otherwise
    become an ordinary-looking session."""
    stem = os.path.splitext(os.path.basename(path))[0]
    m = STEP_FILE_NAME_RE.match(stem)
    return m.group(1) if m else stem


def step_light_records(show_all=False):
    """One record per step session, newest write wins per id.

    step's own subagents write `subagent-<uuid>.jsonl` files: a session the
    tool started for itself, holding another session's context rather than a
    request of anyone's. Like codex's approval reviews, those are left out
    unless --all asks for them."""
    by_id = {}
    if not os.path.isdir(STEP_SESSIONS):
        return []
    # Both layouts step writes: its own default is per-project subdirectories
    # (<session-dir>/<encoded-cwd>/<file>.jsonl), but point the store at a
    # fresh directory -- $STEP_CODING_AGENT_SESSION_DIR, which this module
    # reads through and the README offers as the way to relocate the store,
    # or step's own --session-dir -- and it writes the file straight at the
    # root. Only `*/*.jsonl` was globbed, so with either set every session
    # step created was invisible here: `ai sessions --tool step` printed an
    # empty table, and a title lookup or a handoff into that store read
    # nothing.
    for path in (glob.glob(os.path.join(STEP_SESSIONS, "*", "*.jsonl"))
                 + glob.glob(os.path.join(STEP_SESSIONS, "*.jsonl"))):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        sid, cwd, started, name = step_session_header(path)
        if not sid:
            sid = step_session_id_from_filename(path)
        if not show_all and sid.startswith("subagent-"):
            continue
        prev = by_id.get(sid)
        if prev is not None and prev["ts"] >= mtime:
            continue
        by_id[sid] = {
            "tool": "step", "id": sid, "ts": mtime, "path": path,
            # The cwd is in the header; the encoded directory is only the
            # fallback, for a session whose header was never flushed.
            "cwd": cwd or common.decode_project_dir_name(os.path.basename(os.path.dirname(path))),
            "started": started,
            "name": name,
        }
    return list(by_id.values())


def step_resolve(prefix):
    matches = []
    for r in step_light_records(show_all=True):
        if r["id"].startswith(prefix):
            matches.append(r["id"])
    return sorted(set(matches))


def step_session_cwd(sid):
    for r in step_light_records(show_all=True):
        if r["id"] == sid:
            return r["cwd"]
    return None


def _step_message_text(message):
    """The text of a step message: a plain string, or its first text block.

    A `thinking` block holds the model's reasoning and a `toolCall` block
    holds a call, so neither is what the user (or the assistant) said."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return common.block_text(block)
    return None


def step_title(path):
    """First genuine user prompt in a step session.

    step's record shape is its own -- a message record carrying role and
    content blocks -- but the question this answers is the one claude's
    reader answers, so the same injected/paste/trivial filters apply, and a
    session whose every message is noise falls back to its first prompt the
    same way."""
    fallback = None
    title = None
    try:
        with common.open_text(path) as fh:
            for i, line in enumerate(fh):
                if i > common.TITLE_SCAN_LINES:
                    break
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if not isinstance(d, dict) or d.get("type") != "message":
                    continue
                message = common.dict_field(d, "message")
                if message.get("role") != "user":
                    continue
                text = common.unwrap_openclaw_ctx(_step_message_text(message) or "")
                if not text.strip():
                    continue
                stripped = " ".join(text.split())
                if stripped.startswith(STEP_IMAGE_PREFIX):
                    continue  # a captionless paste names nothing
                if fallback is None:
                    fallback = stripped[:70]
                if title is None and not common._is_injected_or_pasted(text) and not common.is_trivial_title(stripped):
                    title = stripped[:70]
    except OSError:
        pass
    return common._title_or_placeholder(title, fallback)


def _step_display_name(path):
    """The session's display name, if it has one, else None.

    step writes it to the header ({"type":"session",...,"name":"refactor auth
    flow"}), and a `session_info` record carries the same thing when /name
    sets it after the fact. Only a real name counts: the selector shows it in
    place of the first message, so a session named with whitespace or a
    placeholder is better left to its own first prompt."""
    _sid, _cwd, _started, name = step_session_header(path)
    if name and name.strip():
        return " ".join(name.split())[:70]
    for i, d in enumerate(common.read_jsonl(path)):
        if i > 200:
            break
        if not isinstance(d, dict) or d.get("type") != "session_info":
            continue
        for key in ("name", "displayName", "title"):
            value = d.get(key)
            if isinstance(value, str) and value.strip():
                return " ".join(value.split())[:70]
    return None


def step_title_with_name(path):
    """step_title, falling back to the session's own display name.

    A session opened as `step --name "refactor auth flow"` with no prompt yet
    -- or one every message of which is noise -- names itself, and that name
    describes the work where a seed prompt or `(no title)` would not."""
    title = step_title(path)
    if title == "(no title)":
        return common._title_or_placeholder(_step_display_name(path), "")
    return title


def step_handoff_messages(path):
    """Every user and assistant message in a step session, in order, as the
    (role, text) pairs `ai handoff` exports.

    A toolResult message is the tool's own output, which the receiving session
    can reproduce by running the tool again; the thinking blocks are the
    model's reasoning about it. Both are left out so the export is what was
    said, not everything that happened."""
    messages = []
    for d in common.read_jsonl(path):
        if not isinstance(d, dict) or d.get("type") != "message":
            continue
        message = common.dict_field(d, "message")
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        texts = []
        for block in common.list_field(message, "content"):
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = common.block_text(block)
            if text.strip():
                texts.append(text)
        if texts:
            messages.append((role, "\n\n".join(texts)))
    return messages


def _step_tool_call_text(block):
    """Human-meaningful text from a step toolCall block.

    A shell call's command, else the tool name and its arguments: a call's
    arguments are often the only place a term appears anywhere in a session,
    which is why codex's function_call inputs are scanned too."""
    name = block.get("name") if isinstance(block.get("name"), str) else ""
    arguments = block.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except Exception:
            pass
    command = None
    if isinstance(arguments, dict):
        command = arguments.get("command") or arguments.get("cmd")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
    if isinstance(command, str) and command.strip():
        return command.strip()
    body = arguments if isinstance(arguments, str) else json.dumps(
        arguments, ensure_ascii=False, default=str)
    return " ".join(part for part in (name, body) if part)[:400]


def step_snippet(path, max_messages=12, max_chars=800):
    """Longer excerpt than step_title's single prompt, for `ai search`.

    Includes assistant text and the model's thinking, not just user prompts,
    for the reason claude_snippet and kimi_snippet do: the substance of an
    agentic session is usually in the responses. Also includes tool calls,
    which is where a term often lives and nowhere else. Thinking blocks are
    truncated per block -- they run long, and one would otherwise take the
    whole budget.
    Sampled evenly across the whole conversation via sample_stride -- see
    its docstring."""
    texts = []
    try:
        with common.open_text(path) as fh:
            for i, line in enumerate(fh):
                if i > 20000:
                    break
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if not isinstance(d, dict) or d.get("type") != "message":
                    continue
                message = common.dict_field(d, "message")
                if message.get("role") not in ("user", "assistant", "toolResult"):
                    continue
                for block in common.list_field(message, "content"):
                    if not isinstance(block, dict):
                        continue
                    kind = block.get("type")
                    if kind == "text":
                        text = common.block_text(block).strip().replace("\n", " ")
                        if text and not text.startswith(STEP_IMAGE_PREFIX):
                            texts.append(text)
                    elif kind == "toolCall":
                        text = _step_tool_call_text(block).strip().replace("\n", " ")
                        if text:
                            texts.append(text)
                    elif kind == "thinking":
                        text = block.get("thinking")
                        if isinstance(text, str) and text.strip():
                            texts.append(text.strip().replace("\n", " ")[:400])
    except OSError:
        pass
    return common.join_with_fair_budget(common.sample_stride(texts, max_messages), max_chars)
