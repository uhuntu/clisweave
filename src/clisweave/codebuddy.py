"""Reader for the CodeBuddy CLI's session store: one JSONL per session under
~/.codebuddy/projects/<slug>/ -- claude's layout, but codex-rs's record
chain inside: a `message` record carries a top-level role and typed content
blocks (input_text/output_text), while a call and its result are separate
top-level `function_call`/`function_call_result` records and the model's
thinking is a sibling `reasoning` record. There is no header record: the id
and cwd ride on every substantive record, and the title is a stored one
(`ai-title`, newer `custom-title`) CodeBuddy's own auto-titler writes into
the file. A resume appends to the same file -- codex's second-rollout
problem does not exist here.

CodeBuddy is a full citizen like claude: the CLI resumes for real
(`codebuddy -r <id>`), takes a bare positional prompt (so handoffs seed it
directly), and maps the wrapper flags one-to-one. Only the desktop and
vscode clients stay behind in cb -- they keep no transcript.

The CodeBuddy GUI's stores are cb.py's territory; this module is the CLI
only.
"""
import glob
import json
import os

from . import common



CODEBUDDY_PROJECTS = os.path.join(common.HOME, ".codebuddy", "projects")

# The content-block types a `message` record's text comes in: input_text on
# the user's side, output_text on the assistant's -- codex's spellings, not
# claude's plain "text".
TEXT_TYPES = ("input_text", "output_text")

# Big tool results land outside the transcript, in
# <slug>/<sessionId>/tool-results/<callId>.txt -- the same shape of problem
# that gave kimi's reader its background-task output.log scan.
TOOL_RESULTS_DIRNAME = "tool-results"


def _sec(ms):
    """The store keeps times as epoch milliseconds; the listing works in
    seconds. Anything non-numeric reads as 0, which `relative_time` renders
    as "?"."""
    if isinstance(ms, bool) or not isinstance(ms, (int, float)):
        return 0
    return int(ms / 1000)


def _message_texts(d):
    """Every text block of a message record, in order, as strings."""
    return common._all_text_blocks(d.get("content"), TEXT_TYPES)


def _tool_call_text(d):
    """Human-meaningful text from a `function_call` record: a shell call's
    command, else the tool name and its arguments -- a call's arguments are
    often the only place a term appears anywhere in a session."""
    name = d.get("name") if isinstance(d.get("name"), str) else ""
    arguments = d.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except Exception:
            pass
    command = None
    if isinstance(arguments, dict):
        command = arguments.get("command") or arguments.get("cmd")
        if isinstance(command, list):
            command = " ".join(str(piece) for piece in command)
    if isinstance(command, str) and command.strip():
        return command.strip()
    body = arguments if isinstance(arguments, str) else json.dumps(
        arguments, ensure_ascii=False, default=str)
    return " ".join(piece for piece in (name, body) if piece)[:400]


def _result_text(d):
    """The output text of a `function_call_result` record. The output is a
    typed payload ({type: "text", text: ...}) in every file seen; a bare
    string is accepted for a store caught mid-shape-change."""
    output = d.get("output")
    if isinstance(output, str):
        return output
    if isinstance(output, dict):
        text = output.get("text")
        return text if isinstance(text, str) else ""
    return ""


def codebuddy_record_texts(d, with_tool_output=False):
    """Every text one record offers, in the categories the snippet and the
    literal scan share: what was said, what a call carried -- and what it
    returned when `with_tool_output` (codex's scan reads outputs for the
    same reason). The `reasoning` records are the model's thinking, which
    neither consumer wants."""
    kind = d.get("type")
    if kind == "message":
        return _message_texts(d)
    if kind == "function_call":
        text = _tool_call_text(d)
        return [text] if text else []
    if kind == "function_call_result" and with_tool_output:
        text = _result_text(d)
        return [text] if text else []
    return []


def _scan_file(path):
    """One pass over a session file for everything the listing needs:
    (id, cwd, custom_title, ai_title, first_prompt, first_ms, last_ms,
    turns).

    A resume appends to the same file, so "newest" is simply the last one
    seen. Turns count genuine user prompts -- the slash-command echoes and
    the system-reminder scaffolding CodeBuddy records as user messages are
    not turns any more than claude's tool results are."""
    sid = None
    cwd = None
    custom_title = None
    ai_title = None
    fallback = None
    first = None
    last = None
    turns = 0
    for d in common.read_jsonl(path):
        if not isinstance(d, dict):
            continue  # a JSON array line parses fine and is nobody's record
        if sid is None and isinstance(d.get("sessionId"), str):
            sid = d["sessionId"]
        if cwd is None and isinstance(d.get("cwd"), str) and d["cwd"]:
            cwd = d["cwd"]
        ms = d.get("timestamp")
        if isinstance(ms, (int, float)) and not isinstance(ms, bool):
            first = ms if first is None else min(first, ms)
            last = ms if last is None else max(last, ms)
        kind = d.get("type")
        if kind == "custom-title":
            # Written by /rename; no sample on this machine, so the field
            # name is read defensively off the dist's precedence description.
            value = d.get("customTitle") or d.get("title")
            if isinstance(value, str) and value.strip():
                custom_title = " ".join(value.split())[:70]
        elif kind == "ai-title":
            value = d.get("aiTitle")
            if isinstance(value, str) and value.strip():
                ai_title = " ".join(value.split())[:70]
        elif kind == "message" and d.get("role") == "user":
            genuine = False
            for text in _message_texts(d):
                stripped = " ".join(text.split())
                if not stripped or common.is_image_only(stripped):
                    continue
                if common._is_injected_or_pasted(text):
                    continue
                if fallback is None:
                    fallback = stripped[:70]
                genuine = True
            if genuine:
                turns += 1
    return sid, cwd, custom_title, ai_title, fallback, first, last, turns


def codebuddy_light_records(show_all=False):
    """One record per CodeBuddy CLI session.

    CodeBuddy starts no sessions of its own (no subagent transcripts on any
    machine seen), so there is nothing for `show_all` to reveal -- it is
    accepted to match the other readers' signature."""
    records = []
    if not os.path.isdir(CODEBUDDY_PROJECTS):
        return records
    for slug in sorted(os.listdir(CODEBUDDY_PROJECTS)):
        pdir = os.path.join(CODEBUDDY_PROJECTS, slug)
        if not os.path.isdir(pdir):
            continue
        for fn in sorted(os.listdir(pdir)):
            if not fn.endswith(".jsonl"):
                continue
            path = os.path.join(pdir, fn)
            (sid, cwd, custom_title, ai_title, fallback, first, last,
             turns) = _scan_file(path)
            if not sid:
                sid = fn[:-len(".jsonl")]
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0
            # The stored title wins, unless it merely restates a handoff
            # seed ("Continue codex session 019e...") the way claude's own
            # generated titles sometimes do -- then the genuine prompt is
            # the better answer. The genuine-prompt fallback otherwise is
            # only reached when the titler has written nothing.
            title = custom_title or ai_title
            if title and common.is_seed_restatement(title):
                title = None
            if not title:
                title = common._title_or_placeholder(None, fallback)
            records.append({
                "tool": "codebuddy", "id": sid, "path": path,
                "cwd": cwd, "title": title,
                "ts": _sec(last) or int(mtime),
                "started": _sec(first) or int(mtime),
                "turns": turns,
            })
    return records


def codebuddy_resolve(prefix, show_all=False):
    matches = []
    for r in codebuddy_light_records(show_all=True):
        if r["id"].startswith(prefix):
            matches.append(r["id"])
    return sorted(set(matches))


def codebuddy_session_cwd(sid):
    for r in codebuddy_light_records(show_all=True):
        if r["id"] == sid:
            return r["cwd"]
    return None


def codebuddy_handoff_messages(path):
    """Every user and assistant message in order, as the (role, text) pairs
    a handoff export is built from. A `function_call`/`function_call_result`
    is what happened -- the receiving session can reproduce it -- and a
    `reasoning` is the model's thinking; both stay out so the export is what
    was said, the same line every other exporter holds."""
    messages = []
    for d in common.read_jsonl(path):
        if d.get("type") != "message":
            continue
        role = d.get("role")
        if role not in ("user", "assistant"):
            continue
        texts = _message_texts(d)
        if texts:
            messages.append((role, "\n\n".join(texts)))
    return messages


def codebuddy_snippet(path, max_messages=12, max_chars=800):
    """Longer excerpt than a title, for `ai search`: speech and tool calls,
    sampled evenly across the conversation (common.sample_stride, see its
    docstring for why first-N is not enough) and joined within a fair
    budget."""
    texts = []
    for d in common.read_jsonl(path):
        for text in codebuddy_record_texts(d):
            flat = " ".join(text.split())
            if flat:
                texts.append(flat)
    return common.join_with_fair_budget(common.sample_stride(texts, max_messages), max_chars)


def codebuddy_tool_results(record):
    """The sidecar output files of a session, sorted: where a big tool
    result lives, outside the transcript it belongs to."""
    path = record.get("path") or ""
    if not path:
        return []
    return sorted(glob.glob(os.path.join(
        os.path.dirname(path), record.get("id") or "", TOOL_RESULTS_DIRNAME, "*.txt")))
