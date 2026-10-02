"""Reader for Codex CLI's session store: ~/.codex/sessions/**/*.jsonl,
plus ~/.codex/session_index.jsonl for the auto-generated thread names.

Resuming a codex thread appends a *new* rollout file rather than extending
the old one, so one session id can own several files; the path index below
keeps only the newest (see build_codex_path_index), and _codex_scan reads
that one file once per command for every question asked of it."""
import calendar
import json
import os
import re
import time

from . import common



def codex_index():
    path = os.path.join(common.CODEX_HOME, "session_index.jsonl")
    return list(common.read_jsonl(path))


def codex_thread_names():
    # codex appends a new session_index.jsonl line each time a thread gets
    # auto-renamed, without removing the stale line for the same id -- keep
    # only the most recently updated name per id.
    names = {}
    ts_seen = {}
    for entry in codex_index():
        sid = entry.get("id")
        if not sid:
            continue
        updated = entry.get("updated_at")
        try:
            # updated_at is UTC ("...Z"); timegm (unlike mktime) treats the
            # parsed struct as UTC instead of local time.
            ts = calendar.timegm(time.strptime(updated[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:
            ts = 0
        # The index is append-only, so the later line is the newer name even
        # when updated_at is missing or unparseable (ts 0): keeping the first
        # on a tie left a stale thread_name in place of codex's rename.
        if sid in ts_seen and ts_seen[sid] > ts:
            continue
        ts_seen[sid] = ts
        names[sid] = entry.get("thread_name")
    return names


# One read of a rollout per command, shared by every reader -- see
# _codex_scan.  Keyed by (path, mtime, size) so a file rewritten underneath
# is re-read rather than served stale.
_codex_scan_cache = {}
LITERAL_SCAN_CACHE_MAX = 4096
LITERAL_SCAN_LIMIT_CODEX = 20000


_codex_path_index = None


def codex_rollout_files():
    """Every rollout file under CODEX_HOME. Order is the filesystem's, not
    recency -- callers must not depend on it (see build_codex_path_index).

    Walked with followlinks=False: `**` in a recursive glob follows symlinked
    directories, and a `sessions/current -> .` symlink (which people do add)
    made glob return every file once per nesting level -- 64 copies of each
    rollout for a two-level loop, a multi-second listing, and on a filesystem
    with no depth limit a RecursionError."""
    found = []
    for root, _dirs, files in os.walk(os.path.join(common.CODEX_HOME, "sessions"), followlinks=False):
        for name in files:
            if name.endswith(".jsonl"):
                found.append(os.path.join(root, name))
    return found


def build_codex_path_index():
    global _codex_path_index
    index = {}
    for path in codex_rollout_files():
        m = common.UUID_RE.search(os.path.basename(path))
        if not m:
            continue
        sid = m.group(0)
        # Resuming a codex thread appends a *new* rollout file that keeps the
        # session id in its name (rollout-<time>-<id>_<fork-id>.jsonl), so one
        # id can own several files -- a real ~/.codex/sessions had one id with
        # four. Which of them glob lists last is directory order, not recency,
        # and the older files are stale prefixes of the newest one, so letting
        # that decide pointed every reader (title, snippet, cwd, handoff
        # transcript) at the thread as it stood before it was last resumed.
        prev = index.get(sid)
        if prev is not None:
            try:
                # mtime first, then the filename: codex stamps the resume time
                # into it (rollout-<time>-<id>.jsonl), so it breaks an mtime
                # tie the same way the clock would have. Equal mtimes are
                # ordinary on a 1-second-granularity filesystem and after a
                # cp -p / rsync / cloud-sync restore, and `<=` alone let the
                # stale file win -- pointing every reader back at the thread
                # as it stood before its last resume.
                if (os.path.getmtime(path), os.path.basename(path)) <= (
                        os.path.getmtime(prev), os.path.basename(prev)):
                    continue
            except OSError:
                continue
        index[sid] = path
    _codex_path_index = index
    return index


def codex_rollout_path(sid):
    index = _codex_path_index if _codex_path_index is not None else build_codex_path_index()
    return index.get(sid)


def codex_light_records():
    # session_index.jsonl is only reliably populated by the bare CLI's own
    # terminal/exec-mode session tracking -- sessions created via other
    # integrations (VSCode, Codex Desktop) use a different session store
    # entirely and never appear there, even though their rollout files
    # exist on disk same as any other session. So scan the rollout files
    # directly (the same source of truth claude/kimi already use), and use
    # session_index.jsonl only to borrow a nicer auto-generated title when
    # one happens to be available for that id. build_codex_path_index()
    # already collapsed a resumed thread's several rollout files down to its
    # newest, so each id yields exactly one record here.
    thread_names = codex_thread_names()
    records = []
    for sid, path in build_codex_path_index().items():
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        meta = _codex_session_meta(path)
        records.append({"tool": "codex", "id": sid, "ts": mtime, "path": path,
                        "title": thread_names.get(sid),
                        "cwd": common.dict_field(meta, "payload").get("cwd") if meta else None})
    return records


# Both this wrapper and the clipboard-paste one below append whatever the
# user actually typed after this marker -- _codex_genuine_messages pulls
# that trailing part out before the boilerplate check runs, so only a
# wrapper with *nothing* genuine after it (a bare image paste, an
# annotation with no comment) ever reaches CODEX_BOILERPLATE_PREFIXES.
MY_REQUEST_MARKER = "## My request:\n"

CODEX_BOILERPLATE_PREFIXES = (
    "# AGENTS.md", "<permissions", "<INSTRUCTIONS>", "<user_instructions>", "<environment_context>",
    # The CLI/plugin wrapper's own setup text: a listing showed a session
    # titled `<recommended_plugins> Here is a list of plugins...` -- not a
    # request, and it pushed the real first message off the title.
    "<recommended_plugins>",
    # The wrapper Codex's clipboard-paste feature injects ahead of a pasted
    # image: a bare paste with no comment reads as "# Files mentioned by
    # the user:\n\n## codex-clipboard-<uuid>.png: ...\n\n## My request:\n\n"
    # with nothing genuine after it.
    "# Files mentioned by the user:",
    # Same wrapper for a pasted block of *text* (a terminal transcript, a
    # log): the actual pasted content lives in a separate attachment file
    # this scan never opens, not inline after "## My request:", so a bare
    # paste with no added comment is genuinely empty here too.
    "# Files pasted by the user:",
    # Injected when the user comments on text they selected from an earlier
    # Codex response: a real session's entire title collapsed to this
    # multi-sentence lecture even though it carried no request of its own
    # (the selection with no comment attached).
    "# Response annotations:",
    # Codex CLI's own startup notice, injected as a "user" message like the
    # rest of this list despite not being something anyone typed.
    "Loading latest updates...",
    # The IDE extension's own context dump (active file, open tabs, current
    # selection) -- mirrors <environment_context>, just from the editor
    # side instead of the shell.
    "# Context from my IDE setup:",
    # Codex's own marker for a turn the user killed mid-run, no different
    # from claude's "[Request interrupted by user" (see INJECTED_PREFIXES).
    "<turn_aborted>",
    # Slash-command wrapper text, seen here via a cross-tool `ai handoff`
    # that seeded a codex session with another tool's transcript verbatim.
    "<command-name",
    # Codex serializes a content block it can't otherwise represent (e.g. a
    # pasted image) as literal text in this exact form -- not a caption, so
    # it reads as pure noise rather than anything the user wrote. Sometimes
    # paired as one block, "[Image #1]  [external unsupported block:
    # image]" -- the numbered marker alone is just as uninformative.
    "[external unsupported block:", "<image name=", "[Image #",
    common.CODEX_APPROVAL_PROMPT_PREFIX, common.JUDGE_PROMPT_PREFIX, common.HANDOFF_PROMPT_PREFIX,
    common.CONTINUATION_PROMPT_PREFIX,
)


def _codex_tool_call_text(payload):
    """Human-meaningful text from a function_call/custom_tool_call payload:
    the command for exec-style calls, else the raw arguments/input."""
    for key in ("arguments", "input"):
        raw = payload.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            cmd = parsed.get("command") or parsed.get("cmd")
            if isinstance(cmd, str) and cmd.strip():
                return cmd.strip()
        # Non-JSON call wrappers (a real rollout's custom_tool_call inputs
        # are JS: `const r = await tools.exec_command({cmd:"...", ...})`) --
        # pull out the cmd string so the snippet shows the command itself
        # rather than 60 chars of wrapper that bury it past the budget.
        m = re.search(r'cmd\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
        if m:
            return (m.group(1)
                    .replace('\\"', '"').replace("\\n", "\n")
                    .replace("\\t", "\t").replace("\\\\", "\\"))
        return raw.strip()
    return ""


def _codex_scan(path, limit=LITERAL_SCAN_LIMIT_CODEX):
    """One read of a rollout, shared by every question asked of it.

    Returns a list, in file order, of

        ("message", role, text, is_boilerplate)
        ("tool", command)

    A single `ai search` asks the same rollout four things -- its title, its
    snippet, its cwd and, for a handoff or a fork, its parent thread -- and
    each of those used to read and json-parse the file again from scratch. On
    the 220MB of rollouts one machine accumulates that is three full passes
    over the corpus per search, most of what the command costs. Reading once
    and filtering here is the same work, minus the repeats.

    Boilerplate is *flagged* rather than dropped, because the two readers want
    opposite things from it: the title and snippet skip it, while the seed
    fallback that names a session a tool started for itself has to see it.

    Kept per (path, mtime, size) so a file rewritten underneath -- a resumed
    thread appends a new rollout, a test rewrites the same path -- is re-read
    rather than served stale. Entries are the extracted texts, not the parsed
    JSON, so a cache holds kilobytes per session rather than the megabytes of
    rollout it came from."""
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        return []
    cached = _codex_scan_cache.get(key)
    if cached is not None:
        return cached

    entries = []
    try:
        with common.open_text(path) as fh:
            for i, line in enumerate(fh):
                if i > limit:
                    break
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "response_item":
                    continue
                payload = common.dict_field(d, "payload")
                if payload.get("type") in ("function_call", "custom_tool_call"):
                    # Shell commands the agent ran: often the ONLY place a
                    # term appears (a real session mentioned the searched
                    # tool exclusively inside exec_command calls). Extract
                    # the command when the input carries one, else keep the
                    # raw call text.
                    text = _codex_tool_call_text(payload).replace("\n", " ")
                    if text:
                        entries.append(("tool", text))
                    continue
                if payload.get("type") != "message":
                    continue
                text = common.extract_text_from_content(
                    payload.get("content"), text_types=("input_text", "text", "output_text"))
                if not text:
                    continue
                text = common.unwrap_openclaw_ctx(text)
                stripped = text.strip()
                # Codex writes an empty "user" message for a content block it
                # can't represent at all (a pasted image with no caption);
                # it names nothing, and as the last text standing it used to
                # become an empty fallback and collapse the row to
                # `(no title)`, even with real messages after it.
                if not stripped:
                    continue
                # The clipboard-paste and response-annotation wrappers both
                # append what the user actually typed after this marker --
                # when that trailing part is non-empty, it's what the
                # message is really about (a real session's whole title
                # collapsed to an unreadable annotations lecture even though
                # the user's own follow-up, "this", came right after it).
                # When it's empty (a bare image paste with no comment),
                # `stripped` is left as the full wrapper so the boilerplate
                # check below still catches it.
                marker = stripped.find(MY_REQUEST_MARKER)
                if marker != -1:
                    trailing = stripped[marker + len(MY_REQUEST_MARKER):].strip()
                    if trailing:
                        stripped = trailing
                entries.append(("message", payload.get("role"), stripped.replace("\n", " "),
                                stripped.startswith(CODEX_BOILERPLATE_PREFIXES)))
    except OSError:
        pass
    if len(_codex_scan_cache) >= LITERAL_SCAN_CACHE_MAX:
        _codex_scan_cache.clear()  # bounded: hold one invocation's worth, no more
    _codex_scan_cache[key] = entries
    return entries


def _codex_genuine_messages(path, max_messages, roles=("user",), scan_limit=20000,
                            include_tool_calls=False, skip_boilerplate=True, sample=True):
    """Genuine message texts from a rollout for the given roles, skipping
    injected boilerplate (AGENTS.md instructions, permission setup,
    environment context) by its distinctive prefix rather than by length --
    a length cutoff also filters out legitimate long, detailed task
    requests (a real session had a 1304-char genuine message wrongly
    treated as boilerplate and skipped entirely, leaving both `ai search`
    and the plain listing with no way to find or label that session).
    Sampled evenly across the whole conversation via sample_stride, not
    just the first max_messages -- see its docstring. Pass sample=False for
    callers that want literally the earliest messages, in order (e.g. title
    extraction, which wants to look past a trivial first reply without
    jumping elsewhere in the conversation).

    A filter over _codex_scan, which is read once and shared."""
    texts = []
    for entry in _codex_scan(path, scan_limit):
        if entry[0] == "tool":
            if include_tool_calls:
                texts.append(entry[1])
        elif entry[1] in roles:
            if skip_boilerplate and entry[3]:
                continue
            texts.append(entry[2])
    return common.sample_stride(texts, max_messages) if sample else texts[:max_messages]

def codex_rollout_title(sid):
    """Fallback title for sessions with no session_index.jsonl entry: the
    first substantive genuine user message in the rollout file, truncated
    for display. Skips a leading bare acknowledgement ("yes") and a pasted
    shell transcript the same way claude_title_and_cwd does, in favor of the
    next real message, falling back to the first one if nothing better
    turns up. A session made up entirely of injected or seeded text has no
    genuine message at all; the ones a tool started for itself (clisweave's
    seeds, codex's approval review) are still named, so they don't all
    collapse into `(no title)`.
    """
    path = codex_rollout_path(sid)
    if not path:
        return "(no title)"
    texts = _codex_genuine_messages(path, max_messages=5, roles=("user",), sample=False)
    if texts:
        for text in texts:
            if not common.is_trivial_title(text) and not common._is_injected_or_pasted(text):
                return common._title_or_placeholder(" ".join(text.split())[:70], "")
        return common._title_or_placeholder(" ".join(texts[0].split())[:70], "")
    # Codex's <environment_context> dump sits in front of the rest, so the
    # seed isn't necessarily the first message: look past it.
    for seed in _codex_genuine_messages(path, max_messages=200, roles=("user",),
                                        skip_boilerplate=False, sample=False):
        if seed.startswith(common.SEED_PREFIXES):
            return common._title_or_placeholder("", seed[:70])
    return "(no title)"


def codex_rollout_snippet(sid, max_messages=12, max_chars=800):
    """Longer excerpt than codex_rollout_title's single-message title, for
    `ai search`. Mirrors claude_snippet/kimi_snippet. Includes assistant
    text, not just user messages: in agentic sessions the user often gives
    short directives ("check it", "yes", "6 taps") while the assistant's
    own prose describes what was actually done and found -- a real session
    about decompiling an APK had that entire substance in Codex's own
    responses, invisible to a user-only scan, so `ai search` couldn't find
    it even though it was exactly on topic."""
    path = codex_rollout_path(sid)
    if not path:
        return ""
    texts = _codex_genuine_messages(path, max_messages=max_messages, roles=("user", "assistant"),
                                    include_tool_calls=True)
    return common.join_with_fair_budget(texts, max_chars)


# How far into a rollout to look for its session_meta. It is the first line
# codex writes, but a file read while codex is still writing it (or one
# restored by a copy that dropped bytes) can open with an unparseable record,
# so the scan has to be able to step past one.
CODEX_META_SCAN_LIMIT = 50


def _codex_session_meta(path):
    """A rollout's session_meta record, or None.

    Scans forward instead of stopping at the first record that parses: a
    truncated leading line used to make this return None even though the real
    session_meta sat two records later, which cost the session its cwd (no
    chdir on resume) and a fork its `(fork) ...` title."""
    for i, d in enumerate(common.read_jsonl(path)):
        if i >= CODEX_META_SCAN_LIMIT:
            break
        if isinstance(d, dict) and d.get("type") == "session_meta":
            return d
    return None


def codex_cwd(sid):
    path = codex_rollout_path(sid)
    if not path:
        return None
    meta = _codex_session_meta(path)
    return common.dict_field(meta, "payload").get("cwd") if meta else None


def codex_parent_thread_id(sid):
    """The thread this codex rollout forked from, or None -- see
    FORK_TITLE_MARK. Codex's subagent threads and Codex Desktop's forks both
    record one in session_meta; a plain resumed run does not, and a rollout
    that names the session itself as its parent is a continuation of that
    thread rather than a fork of it."""
    path = codex_rollout_path(sid)
    if not path:
        return None
    meta = _codex_session_meta(path)
    if not meta:
        return None
    parent = common.dict_field(meta, "payload").get("parent_thread_id")
    return parent if parent and parent != sid else None


def codex_resolve(prefix):
    ids = {sid for sid in codex_thread_names() if sid.startswith(prefix)}
    ids |= {sid for sid in build_codex_path_index() if sid.startswith(prefix)}
    return sorted(ids)



def codex_handoff_messages(path):
    messages = []
    for d in common.read_jsonl(path):
        if d.get("type") != "response_item":
            continue
        payload = common.dict_field(d, "payload")
        role = payload.get("role")
        if payload.get("type") != "message" or role not in ("user", "assistant"):
            continue
        texts = common._all_text_blocks(payload.get("content"), ("input_text", "text", "output_text"))
        text = "\n\n".join(texts).strip()
        if text and not (role == "user" and text.startswith(CODEX_BOILERPLATE_PREFIXES)):
            messages.append((role, text))
    return messages
