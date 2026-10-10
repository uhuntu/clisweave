"""Reader for Kimi CLI's session store: ~/.kimi-code/session_index.jsonl
plus each session's state.json and agents/main/wire.jsonl."""
import calendar
import json
import os
import time

from . import common



def kimi_index():
    path = os.path.join(common.KIMI_HOME, "session_index.jsonl")
    return list(common.read_jsonl(path))


def parse_kimi_timestamp(value):
    """kimi-code's state.json has used several schemas over time: epoch
    milliseconds (numeric, current) and ISO-8601 strings (older sessions,
    e.g. "2026-07-20T01:49:19.177Z"). Handle both; returns seconds since
    epoch, or 0 if missing/unparseable.

    A number is read as milliseconds only when it is large enough to be some:
    epoch *seconds* (1755161559) is a value these files have carried too, and
    dividing it by 1000 lands in January 1970 -- which sorted the session to
    the bottom of every listing and rendered it as "20833d ago"."""
    if not value or isinstance(value, bool):
        return 0
    number = None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        try:
            number = float(str(value).strip())
        except ValueError:
            number = None
    if number is not None:
        if abs(number) < 1e11:
            number *= 1000.0
        return number / 1000.0
    try:
        return calendar.timegm(time.strptime(str(value)[:19], "%Y-%m-%dT%H:%M:%S"))
    except ValueError:
        return 0


def kimi_light_records(show_all):
    records = []
    for entry in kimi_index():
        sid = entry.get("sessionId")
        sdir = entry.get("sessionDir")
        if not sid or not sdir:
            continue
        state = common.read_json(os.path.join(sdir, "state.json")) or {}
        if state.get("archived") and not show_all:
            continue
        ts = parse_kimi_timestamp(state.get("updatedAt") or state.get("createdAt"))
        # older sessions use "workDir" instead of "cwd" in state.json; the
        # index itself also records workDir as a last-resort fallback.
        cwd = state.get("cwd") or state.get("workDir") or entry.get("workDir")
        records.append({"tool": "kimi", "id": sid, "ts": ts, "cwd": cwd, "dir": sdir})
    return records


# kimi records a name of its own in state.json, along with how it got it:
# "generated" is one kimi wrote, "replaceable" is a placeholder it will
# replace once there is something to go on (a session opened by an
# uncaptioned screenshot sits at "[image]"), and sessions from before that
# schema just say "New Session". Only a real name is worth showing -- and
# only as a last resort, because it is derived from whatever kimi saw first
# (often the same screenshot or paste the wire scan already rejects), so an
# actual user prompt always beats it.
KIMI_PLACEHOLDER_TITLES = {"new session", "[image]"}


def kimi_stored_title(sdir):
    """kimi's own name for a session, or None when it has none worth showing
    -- see KIMI_PLACEHOLDER_TITLES."""
    state = common.read_json(os.path.join(sdir, "state.json")) or {}
    title = state.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    if title.strip().lower() in KIMI_PLACEHOLDER_TITLES:
        return None
    if state.get("titleKind") != "generated" and not state.get("isCustomTitle"):
        return None
    return " ".join(title.split())[:70]


def kimi_title(sdir):
    """First *genuine* user prompt in the session's wire log.

    Skips injected/pasted prompts the same way claude_title_and_cwd does
    (see its docstring), including a bare acknowledgement ("yes") replying
    to content this scan can't read: a kimi session can just as easily be
    opened by a reminder, a pasted terminal transcript, or a one-word reply
    as by a real question, and a title taken from those is unrecognizable in
    the listing. Falls back to the first prompt when every one in the
    window is noise."""
    wire = os.path.join(sdir, "agents", "main", "wire.jsonl")
    fallback = None
    title = None
    try:
        with common.open_text(wire) as fh:
            for i, line in enumerate(fh):
                if i > common.TITLE_SCAN_LINES or title:
                    break
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "turn.prompt":
                    continue
                for block in common.list_field(d, "input"):
                    if not (isinstance(block, dict) and block.get("type") == "text"):
                        continue
                    raw = common.unwrap_openclaw_ctx(common.block_text(block))
                    stripped = " ".join(raw.split())
                    if not stripped:
                        continue
                    if fallback is None and not common.is_image_only(stripped):
                        fallback = stripped[:70]
                    # as with claude: the un-flattened text, so a later
                    # line's prompt can't reject a real opening question
                    if title is None and not common._is_injected_or_pasted(raw) and not common.is_trivial_title(stripped):
                        title = stripped[:70]
                    break
    except OSError:
        pass
    resolved = common._title_or_placeholder(title, fallback)
    if resolved == "(no title)":
        # Nothing in the wire log to name it by: newer kimi builds keep a
        # screenshot paste's prompt in context.append_message rather than
        # turn.prompt, so a session opened by an image has no text here at
        # all. kimi names such a session itself -- show its name rather than
        # a blank row (see kimi_stored_title for what it won't show).
        return kimi_stored_title(sdir) or resolved
    return resolved


def kimi_snippet(sdir, max_messages=12, max_chars=800):
    """Longer excerpt than kimi_title's single-prompt title, for `ai search`.
    Same wider window as claude_snippet -- a topic that only shows up
    partway into the conversation should still be visible to the judge.

    Includes assistant response text, not just user prompts: in agentic
    sessions the user often gives short directives while the assistant's
    own prose describes what was actually done and found -- see
    claude_snippet's docstring for a real example of this exact failure.
    Also includes think-block text and tool-result output, not just
    surface text: a real session's only mention of the searched term lived
    exclusively in think parts and tool results, invisible to a
    text-only scan. (Tool-result output is truncated hard per event --
    unbounded build/download logs would otherwise drown the snippet.)
    Sampled evenly across the whole conversation via sample_stride, not
    just the first max_messages -- see its docstring."""
    wire = os.path.join(sdir, "agents", "main", "wire.jsonl")
    texts = []
    try:
        with common.open_text(wire) as fh:
            for i, line in enumerate(fh):
                if i > 20000:
                    break
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                blocks = None
                if d.get("type") == "turn.prompt":
                    blocks = common.list_field(d, "input")
                elif d.get("type") == "context.append_loop_event":
                    event = common.dict_field(d, "event")
                    if event.get("type") == "content.part":
                        part = common.dict_field(event, "part")
                        if part.get("type") in ("text", "think"):
                            t = next((v for v in (part.get("text"), part.get("think"))
                                      if isinstance(v, str)), "").strip()
                            if t:
                                texts.append(t.replace("\n", " "))
                            continue
                    elif event.get("type") == "tool.result":
                        result = common.dict_field(event, "result")
                        output = result.get("output")
                        if isinstance(output, str) and output.strip():
                            texts.append(output.strip().replace("\n", " ")[:400])
                        continue
                if blocks is None:
                    continue
                for block in blocks:
                    if isinstance(block, dict) and block.get("type") == "text":
                        t = common.unwrap_openclaw_ctx(common.block_text(block)).strip().replace("\n", " ")
                        if t:
                            texts.append(t)
                        break
    except OSError:
        pass
    return common.join_with_fair_budget(common.sample_stride(texts, max_messages), max_chars)



def kimi_session_cwd(sid):
    """kimi -S refuses to resume a session from a different cwd than the one
    it was created in; session_index.jsonl already records that cwd as
    workDir, so we can chdir there ourselves instead of making the user do
    it by hand."""
    for entry in kimi_index():
        if entry.get("sessionId") == sid:
            return entry.get("workDir")
    return None



def kimi_handoff_messages(sdir):
    wire = os.path.join(sdir, "agents", "main", "wire.jsonl")
    messages = []
    for d in common.read_jsonl(wire):
        if d.get("type") == "turn.prompt":
            texts = common._all_text_blocks(common.list_field(d, "input"))
            role = "user"
        elif d.get("type") == "context.append_loop_event" and common.dict_field(d, "event").get("type") == "content.part":
            texts = common._all_text_blocks([common.dict_field(common.dict_field(d, "event"), "part")])
            role = "assistant"
        else:
            continue
        if texts:
            messages.append((role, "\n\n".join(texts)))
    return messages



def kimi_resolve(prefix):
    ids = [e.get("sessionId", "") for e in kimi_index()]
    matches = [i for i in ids if i.startswith(prefix)]
    if not matches and not prefix.startswith("session_"):
        alt = "session_" + prefix
        matches = [i for i in ids if i.startswith(alt)]
    return sorted(set(matches))
