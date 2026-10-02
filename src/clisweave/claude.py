"""Reader for Claude Code's session store: ~/.claude/projects/*/*.jsonl.

Shared primitives come from common, reached as common.<name> at call time;
everything here is claude's own record shapes and store layout.
"""
import glob
import json
import os

from . import common



def claude_light_records():
    # Claude Code stores a session's transcript under more than one project
    # directory when the session touches more than one cwd (e.g. via `cd` in
    # tool calls), so the same session id can show up multiple times here.
    # Keep only the most recently modified copy per id.
    by_id = {}
    if not os.path.isdir(common.CLAUDE_PROJECTS):
        return []
    try:
        proj_dirs = os.listdir(common.CLAUDE_PROJECTS)
    except OSError:
        return []  # unreadable store: no claude sessions, same as none stored
    for proj_dir in proj_dirs:
        full_dir = os.path.join(common.CLAUDE_PROJECTS, proj_dir)
        if not os.path.isdir(full_dir):
            continue
        for path in glob.glob(os.path.join(full_dir, "*.jsonl")):
            sid = os.path.splitext(os.path.basename(path))[0]
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if sid in by_id and by_id[sid]["ts"] >= mtime:
                continue
            cwd_guess = common.decode_project_dir_name(proj_dir)
            by_id[sid] = {"tool": "claude", "id": sid, "ts": mtime, "path": path, "cwd": cwd_guess}
    return list(by_id.values())



def claude_snippet(path, max_messages=12, max_chars=800):
    """A longer excerpt than claude_title_and_cwd's single-message title,
    for `ai search`: concatenates up to max_messages message texts so a
    topic that only shows up partway into the conversation can still match.
    (Previously capped at the first 80 lines / 3 messages, which missed
    real topics that first appeared later -- e.g. a 191-line session where
    the relevant message was at line 92.)

    Includes assistant text, not just user messages: in agentic sessions
    the user often gives short directives ("check it", "yes", "6 taps")
    while the assistant's own prose describes what was actually done and
    found -- a real session's substance (decompiling an APK, the specific
    files involved) was entirely in Claude's responses, invisible to a
    user-only scan, so `ai search` couldn't find it even though it was
    exactly on topic.

    Sampled evenly across the whole conversation via sample_stride, not
    just the first max_messages: a long conversation's relevant content
    can sit well past the first dozen messages (a real 104-message session
    had it at message 71, in the latter-middle stretch)."""
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
                if d.get("type") not in ("user", "assistant"):
                    continue
                text = common.unwrap_openclaw_ctx(
                    common.extract_text_from_content(common.dict_field(d, "message").get("content")) or "")
                if text.strip():
                    texts.append(text.strip().replace("\n", " "))
    except OSError:
        pass
    return common.join_with_fair_budget(common.sample_stride(texts, max_messages), max_chars)



def claude_title_and_cwd(path, cwd_fallback):
    """Scan a session's jsonl once for both a title and the real cwd (more
    reliable than guessing from the project directory name, which can't
    distinguish literal dashes in a path from directory separators).

    Claude Code's own UI names a session too, and stores that as
    `custom-title`/`ai-title` records scattered through the transcript
    (re-emitted as the session grows) -- when the session has been renamed
    away from its "New session" default, or the CLI generated a proper
    summary, that beats anything this function could extract itself: a
    session whose first *text* message was a bare "Yes" (replying to a
    screenshot this scan can't read) had an ai-title of "ADB connection
    HuntNUC", and one whose window never reaches a genuine message had a
    custom-title naming the actual topic. Those records can show up
    anywhere in the file, so finding them costs a full read regardless of
    TITLE_SCAN_LINES -- cheap, since it's a substring check per line and
    only a match gets json.loads'd.

    Absent either, the title is the first *genuine* user message: Claude
    Code's first user record is often not a real request but injected
    content, a pasted terminal transcript, or a bare acknowledgement ("Yes")
    replying to an earlier screenshot this scan can't read -- a real
    listing showed titles like `<scheduled-task name="kimi-timer-...">`
    (the scheduled-task reminder that woke the session), `(hunt@host)-[~] $
    cd Downloads ...` (a pasted shell transcript), and "Yes". Those are
    skipped, like codex's injected boilerplate, so the title is the first
    thing the user actually asked. If every message in the window is one of
    those, the first one is used anyway -- a noisy title still beats an
    unrecognizable `(no title)` row."""
    fallback = None
    title = None
    cwd = None
    custom_title = None
    ai_title = None
    try:
        with common.open_text(path) as fh:
            for i, line in enumerate(fh):
                if '"custom-title"' in line:
                    try:
                        custom_title = common._as_title(json.loads(line).get("customTitle")) or custom_title
                    except Exception:
                        pass
                    continue
                if '"ai-title"' in line:
                    try:
                        ai_title = common._as_title(json.loads(line).get("aiTitle")) or ai_title
                    except Exception:
                        pass
                    continue
                if i > common.TITLE_SCAN_LINES or (title and cwd):
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if cwd is None and isinstance(d.get("cwd"), str):
                    cwd = d["cwd"]
                if d.get("type") != "user":
                    continue
                text = common.extract_text_from_content(common.dict_field(d, "message").get("content"))
                if not text:
                    continue
                text = common.unwrap_openclaw_ctx(text)
                if not text.strip():
                    continue
                stripped = " ".join(text.split())
                # a captionless screenshot names nothing, so it can't stand
                # in as the fallback either -- better `(no title)` than a tmp
                # path
                if fallback is None and not common.is_image_only(stripped):
                    fallback = stripped[:70]
                # Shell-prompt detection needs the *first* line as written;
                # `stripped` has newlines flattened, which would let a later
                # line's prompt match and reject a real request.
                if title is None and not common._is_injected_or_pasted(text) and not common.is_trivial_title(stripped):
                    title = stripped[:70]
    except OSError:
        pass
    # A stored title is flattened like every other title path: `render_rows`
    # prints it straight into a column, so one embedded newline in a
    # customTitle would break the row's layout.
    custom_title = common._as_title(custom_title)
    ai_title = common._as_title(ai_title)
    if custom_title and custom_title != "New session" and not common.is_seed_restatement(custom_title):
        resolved = custom_title
    elif ai_title and not common.is_seed_restatement(ai_title):
        resolved = ai_title
    else:
        resolved = common._title_or_placeholder(title, fallback)
    return resolved, (cwd if isinstance(cwd, str) and cwd else cwd_fallback)


def claude_session_cwd(sid):
    record = next((r for r in claude_light_records() if r["id"] == sid), None)
    if not record:
        return None
    _title, cwd = claude_title_and_cwd(record["path"], record.get("cwd"))
    return cwd


def claude_resolve(prefix):
    matches = []
    for r in claude_light_records():
        if r["id"].startswith(prefix):
            matches.append(r["id"])
    return sorted(set(matches))



def claude_handoff_messages(path):
    messages = []
    for d in common.read_jsonl(path):
        role = d.get("type")
        if role not in ("user", "assistant"):
            continue
        texts = common._all_text_blocks(common.dict_field(d, "message").get("content"))
        if texts:
            messages.append((role, "\n\n".join(texts)))
    return messages
