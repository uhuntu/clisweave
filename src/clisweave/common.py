"""Shared readers, path helpers and message-text classification.

These are the primitives every tool module (claude, codex, kimi, step,
zcode) is built on: the tolerant file readers (open_text, read_json,
read_jsonl), the JSON field guards (dict_field, list_field, block_text), and
the filters that separate a genuine user request from injected boilerplate, a
pasted terminal transcript or a bare acknowledgement. Each tool's store
location lives here too, so a reader reaches its store through this module --
except where a tool keeps its own (step's session directory, zcode's
database), which its own module constant carries.

Tool modules reach these names as `common.<name>` at call time rather than
importing them into their own namespace -- every reader goes through one
attribute on this module, so retargeting it retargets them all.
"""
import json
import ntpath
import os
import re
import sys
import time



HOME = os.path.expanduser("~")
CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")
CODEX_HOME = os.path.join(HOME, ".codex")
KIMI_HOME = os.path.join(HOME, ".kimi-code")
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

TOOLS = ("claude", "codex", "kimi", "step", "zcode", "codebuddy")



def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def open_text(path):
    """Open a transcript for reading, tolerating the non-UTF-8 bytes the CLIs
    leave behind: a partially flushed multi-byte character (they write these
    files while running, so a line can be cut mid-character), or a pasted
    blob of binary in a message.  Strict UTF-8 raises UnicodeDecodeError half
    a file in, and one such byte in one session file took down `ai`,
    `ai sessions`, `ai search`, `ai resume` and every handoff at once, with
    no way to find the file (you cannot list to look for it).  Substituting
    U+FFFD keeps the row readable instead."""
    return open(path, encoding="utf-8", errors="replace")


def harden_console_output():
    """Stop one unprintable character from taking the whole run down.

    Titles, cwds and judge reasons are whatever was typed into the session,
    and a console set to a legacy code page (GBK on a Chinese system, Shift
    JIS on a Japanese one) cannot encode most of Unicode -- so a session
    titled with a bullet aborted `ai` six rows into its own listing with a
    UnicodeEncodeError traceback. The listing is the one command with no
    other way to look at your sessions, so it has to reach the last row.

    Switching the error handler to "replace" keeps the console's own
    encoding -- everything it can show is still shown -- and prints "?"
    for what it cannot, one character wide so the columns stay lined up.
    open_text is the mirror image of this on the read side; this is the
    write side, and it belongs at the entry points because every command
    prints something.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue  # replaced by something that is not a text stream
        try:
            reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            # Detached, closed or already finalized. There is nothing
            # sensible to do about it, and the stream may still work.
            pass


def dict_field(obj, key):
    """The value of `key` when it is a dict, else {}.

    A record whose field is an explicit JSON null is ordinary in these
    files, and `d.get(key, {})` does not cover it: the default only applies
    when the key is absent, so for "message": null it returns None and the
    caller's own .get runs on None -- an AttributeError that took down the
    whole listing over one odd record.  Guarding once, here, is also what
    keeps a wrong-typed field (a list where a message should be) from
    reaching the title, snippet and handoff readers."""
    value = obj.get(key)
    return value if isinstance(value, dict) else {}


def list_field(obj, key):
    """The value of `key` when it is a list, else [] -- see dict_field for
    why the `or []` idiom is not enough on its own."""
    value = obj.get(key)
    return value if isinstance(value, list) else []


def block_text(block):
    """The text of a typed content block, or "" when it isn't usable text.

    A block's `text` is not guaranteed to be a string: a malformed or
    future-shaped block carries null or a list.  Every caller feeds the
    result straight into string operations (lstrip, join, strip), so one
    non-string block was enough to turn "list the block's text" into an
    AttributeError inside the title/snippet/handoff readers."""
    text = block.get("text")
    return text if isinstance(text, str) else ""


def _as_title(value):
    """A stored title (claude's customTitle/aiTitle) normalized the way every
    other title is -- whitespace flattened, length-capped -- or None when it
    isn't a string at all.  Titles are printed straight into a column, so an
    unstripped newline in one breaks the row, and a non-string value would
    reach string operations as-is."""
    if not isinstance(value, str):
        return None
    return " ".join(value.split())[:70]


def read_jsonl(path):
    """Yield each parseable JSON record of a .jsonl transcript.

    A transcript belongs to a tool that is still writing it, so an
    unparseable line is normal rather than fatal -- mid-write flushes, a
    binary paste, a truncated tail.  Such lines are skipped.  OSError rather
    than just FileNotFoundError: a permission error or a vanished directory
    should end the walk quietly too, not abort the whole command."""
    try:
        with open_text(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except Exception:
                    continue
    except OSError:
        return



def decode_project_dir_name(proj_dir):
    """Best-effort path from a claude project directory name, used only when
    a transcript carries no cwd of its own. Claude encodes every
    non-alphanumeric as "-", so "/" and "." both come back as a separator and
    the decode is a guess either way; a run of them is one separator, which
    at least keeps the guess a normal-looking path --
    "-home-hunt--openclaw-workspace" decoded literally to
    "/home/hunt//openclaw/workspace", a double slash no real path has."""
    # claude's spelling of a Windows path: "C--Users-hunt-work-proj", the
    # drive letter first and no leading separator. A literal decode of that
    # is not a path at all -- the row showed garbage, `--cwd` never matched
    # it, and `ai resume` would not chdir there.
    m = re.match(r"^([A-Za-z])--", proj_dir)
    if m:
        return m.group(1) + ":/" + re.sub(r"-+", "/", proj_dir[2:].lstrip("-"))
    if not proj_dir.startswith("-"):
        return proj_dir
    collapsed = re.sub(r"-+", "/", proj_dir)
    # step spells the same path with a separator either side,
    # "--C--Users-hunt-work-proj--", so the drive letter lands after one.
    m = re.match(r"^/([A-Za-z])/", collapsed)
    if m:
        collapsed = m.group(1) + ":" + collapsed[2:]
    # A trailing separator is an artifact of the encoding, not part of the
    # path, and it would show in the CWD column.
    return collapsed.rstrip("/") or collapsed



def extract_text_from_content(content, text_types=("text",)):
    """A message's content is either a plain string or a list of typed
    blocks (text, image, ...); pull the first matching text block either
    way, or None. codex uses "input_text" instead of "text"."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in text_types:
                return block_text(block)
    return None


TRIVIAL_TITLES = {
    "yes", "no", "ok", "okay", "sure", "yep", "yeah", "nope", "please",
    "continue", "go ahead", "do it", "thanks", "thank you", "correct",
    "proceed", "fine", "alright", "got it", "sounds good", "lgtm",
    # Handing the choice back is go-ahead/do-it wearing different words: a
    # real session opened with a greeting, ran a pasted terminal dump, was
    # answered You decide -- and that two-word deferral titled work that was
    # actually about cleaning up old release directories. Needs the
    # <task-notification> skip in INJECTED_PREFIXES, or skipping these just
    # lands the title on the notification that followed them.
    "you decide", "your call", "you pick", "you choose", "your choice", "up to you",
    # A greeting opening a session before the real question is the same
    # problem as a bare "Yes": real listings had "hi" and "Hello" as titles
    # for sessions whose next message held the actual request. A session
    # whose *only* message is a greeting still shows it, via the fallback.
    "hi", "hello",
}


def is_trivial_title(text):
    """A bare acknowledgement makes an uninformative session title -- e.g. a
    session whose first *text* message is a one-word reply ("Yes") to an
    earlier screenshot the title-extractor can't read. Callers should prefer
    the next substantive message within the same scan window when one of
    these turns up first, falling back to it only if nothing better exists."""
    return text.strip().lower().strip(".!?") in TRIVIAL_TITLES


def sample_stride(texts, limit):
    """Evenly-spaced sample across the full list, not just the first
    `limit`. A conversation often covers several sequential topics before
    reaching the current one -- a real 104-message session had its
    relevant content at message 71, in the latter-middle stretch: neither
    a first-N-only scan nor a first+last split reliably lands there, but a
    stride across the whole conversation does (index 69, one stride step
    away). `limit` <= 2 keeps plain first-N behavior (used for title
    extraction, which wants literally the first message). The final message
    is always included: conversations often end on the topic currently
    being searched for, and pure stride sampling can land one step short
    of it (a real session kept its only mention of the term in the very
    last of 248 messages, index 247, never sampled)."""
    if len(texts) <= limit or limit <= 2:
        return texts[:limit]
    step = len(texts) / limit
    indices = sorted({int(i * step) for i in range(limit)} | {len(texts) - 1})
    return [texts[i] for i in indices]


def join_with_fair_budget(texts, max_chars):
    """Join sampled texts into one snippet, giving each an equal share of
    max_chars rather than truncating the joined whole -- otherwise verbose
    early messages consume the entire budget and silently drop everything
    sampled from later in the conversation (the exact bug that made
    sample_stride's own fix ineffective until this was added)."""
    if not texts:
        return ""
    per_message = max(40, max_chars // len(texts))
    return " | ".join(t[:per_message] for t in texts)[:max_chars]



# The opening of clisweave's own `ai search` judge instruction (see
# build_judge_prompt in search.py). Kept short: titles are truncated to 70
# chars, and this is also matched against the truncated fallback title.
JUDGE_PROMPT_PREFIX = "You are filtering a list of past AI"

# What to show instead for `ai search --judge <tool>` sessions. Those have
# the judge instruction as their only user message -- the judge answers and
# the session ends -- so there is no real request to fall back to, and every
# such row would otherwise read identically.
JUDGE_SESSION_TITLE = "ai search judge"

# Same for the seed prompt `ai handoff` writes into the new session (see
# perform_handoff): generated by clisweave, identical apart from the source
# tool, and not something the user asked for.
HANDOFF_PROMPT_PREFIX = "Continue the work from this "
HANDOFF_SESSION_TITLE = "ai handoff"

# A pasted image with no caption: Claude Code serializes it as literal text
# in this form, so a session opened by a screenshot is titled with the tmp
# path the upload was written to ("[Image: source: /tmp/claude-1000/...").
# Codex's own spellings of the same thing are in CODEX_BOILERPLATE_PREFIXES.
IMAGE_MESSAGE_PREFIX = "[Image:"

# A resume seed written by another tool in this setup (aimux; not clisweave's
# own wording, which names the source session): a session opened by it has
# this as its only opening, ahead of the pasted transcript and the real
# request that follow -- and it propagates, since `ai handoff` titles a child
# session by its source's title.
RESUME_SEED_PREFIX = "Continue from where you left off"

# A bridge (openclaw) relays a chat message into a session behind a context
# header naming the chat, the sender and the time, with the person's own
# words after it:
#   Conversation info: ⟦openclaw:ctx⟧ ```json {"chat_id":"stepfun:429019",
#   "sender":{...}} ``` 哈哈
# A real listing showed that whole header as the session's title, chat id and
# all. Unlike the injected messages above, the real text is *inside* the same
# message rather than missing from it, so this one is unwrapped rather than
# skipped -- see unwrap_openclaw_ctx.
OPENCLAW_CTX_PREFIX = "Conversation info: ⟦openclaw:ctx⟧"


def unwrap_openclaw_ctx(text):
    """Drop openclaw's context header, keeping what was actually said after
    it -- see OPENCLAW_CTX_PREFIX. Returns "" when the message held nothing
    but the header (a bare relayed attachment, say), so callers treat it as
    an empty message instead of titling a session by a chat id."""
    stripped = text.lstrip()
    if not stripped.startswith(OPENCLAW_CTX_PREFIX):
        return text
    rest = stripped[len(OPENCLAW_CTX_PREFIX):].strip()
    # The header's payload is a fenced json block, so whatever follows it is
    # the message; no closing fence means there was nothing but the header.
    if rest.startswith("```"):
        end = rest.find("```", 3)
        rest = "" if end == -1 else rest[end + 3:]
    return rest.strip()

# Injected when a session is picked up after it ran out of context: the
# recap of the conversation being continued, ahead of anything the user
# asked. Seen as a codex title too, so it is boilerplate on both sides.
CONTINUATION_PROMPT_PREFIX = "This session is being continued from a previous conversation"

# Codex starts a session of its own to have a model assess whether a command
# is safe to run; every user message in it is this wrapper around the
# transcript under review, so there is no request of its own to title it by
# -- and one such session shows up per approval asked.
CODEX_APPROVAL_PROMPT_PREFIX = "The following is the Codex agent history"
CODEX_APPROVAL_SESSION_TITLE = "codex approval review"

# Every kind of opening message that identifies a session as one a machine
# started, for itself, with no request of its own in it (see
# _title_or_placeholder).
SEED_PREFIXES = (JUDGE_PROMPT_PREFIX, HANDOFF_PROMPT_PREFIX, CODEX_APPROVAL_PROMPT_PREFIX)

# The labels that say a session holds nothing but such an opening -- i.e.
# that it is a byproduct rather than a conversation: codex reviewing
# whether a command is safe to run, clisweave asking a model to filter
# search results. They aren't work of yours, so listings and search leave
# them out (they stay reachable by id). A handoff session is NOT one of
# these: its seed is only the opening, and real work follows it.
TOOL_STARTED_TITLES = (JUDGE_SESSION_TITLE, CODEX_APPROVAL_SESSION_TITLE)

# First messages that are not the user asking anything: what Claude Code
# injects (scheduled-task reminders that woke the session, system reminders,
# the <command-*>/<local-command-stdout> wrappers around slash-command runs),
# plus clisweave's own `ai search` judge prompt -- `ai search --judge <tool>`
# starts a real session in that tool whose opening message is that
# instruction, so those rows all read identically ("You are filtering a list
# of past AI coding-assistant conversations...") and say nothing about any
# actual work. Matched by distinctive prefix, mirroring codex's
# CODEX_BOILERPLATE_PREFIXES, rather than by length (length also filters out
# legitimately long requests).
INJECTED_PREFIXES = (
    "<scheduled-task", "<system-reminder", "<command-message", "<command-name",
    "<local-command-stdout", "<bash-input", "<bash-stdout", "<bash-stderr",
    # Claude Code reports a finished background task as a *user*-role record
    # wrapped in <task-notification> ... -- a notification, not a request, that
    # would otherwise be titled by it verbatim. It also gates the deferral
    # phrases in TRIVIAL_TITLES: skipping these first keeps an opening answered
    # with a deferral from falling through that filter onto the notification.
    "<task-notification",
    # kimi spells the same idea differently -- a finished background task
    # injected as <notification id="task:..." ...>. Recognizing it has the
    # same coupled rationale: skipping a deferral hands the title to the
    # notice that follows, so its form must be recognized too (a real kimi
    # session landed on exactly this). Distinctive prefix, per the note above.
    "<notification id=",
    # Both injected around a compacted/resumed conversation: the marker for
    # Artifact content the summary may restate, and the caveat in front of
    # messages produced by local commands (the /compact block below it).
    "<artifact-content-authored-by-others/>", "<local-command-caveat>",
    # Claude Code's own marker for a cancelled turn -- a real session had it
    # as the *only* alternative to a pasted transcript, so it became the
    # title of an otherwise perfectly recognizable conversation.
    "[Request interrupted by user",
    JUDGE_PROMPT_PREFIX,
    HANDOFF_PROMPT_PREFIX,
    IMAGE_MESSAGE_PREFIX,
    CONTINUATION_PROMPT_PREFIX,
    RESUME_SEED_PREFIX,
)


# Claude Code names a session itself, and can name it after the seed that
# opened it: a real handoff child's generated ai-title read "Continue codex
# session 01a0a7e1" -- a restatement of clisweave's seed, describing where
# the session came from and nothing of the work in it. Those are dropped so
# the title falls back to what the session actually contains.
SEED_TITLE_RE = re.compile(r"^Continue \w+ session\b")


def is_seed_restatement(title):
    """True if a stored title (custom-title / ai-title) merely restates a
    seed message rather than naming the work -- see SEED_TITLE_RE."""
    return bool(SEED_TITLE_RE.match(title.strip())) or title.strip().startswith(
        (RESUME_SEED_PREFIX, CONTINUATION_PROMPT_PREFIX, JUDGE_PROMPT_PREFIX)
    )


def is_image_only(text):
    """True if a message is nothing but a serialized image -- a captionless
    screenshot paste, which names no topic and so can't be a title or even
    the fallback behind one (see claude_title_and_cwd)."""
    return text.strip().startswith(IMAGE_MESSAGE_PREFIX)

# A pasted shell transcript with a full prompt -- "user@host:/path$ cmd" or
# zsh's "(user@host)-[~] $ cmd". Only the opening is tested: multi-line
# pastes often *end* with another prompt line, and matching those would
# reject pastes whose first line is a real question.
SHELL_PROMPT_RE = re.compile(r"^[^\s@]+@[^\s@]+[^\n]*?[$#](\s|$)")

# The same paste with the host part stripped or lost: "$ kimi update\nerror:
# ...". Deliberately `$` only -- `# ` far more often starts a markdown
# heading in a real message than a root shell prompt.
BARE_PROMPT_RE = re.compile(r"^\s*\$\s+\S")

# Pasted terminal output with no prompt line at all, so neither of the
# regexes above can ever see it: an `ls -l` listing (the permission column
# is unmistakable) and a bare path dropped in for context. Real listings had
# both as titles -- `drwxrwxr-x 36 1001 1001 4096 Aug 14 15:25 mt8390_...`
# and `/home/hunt/EDLA/A13/android-gts/tools`.
LS_OUTPUT_RE = re.compile(r"^[-dlbcps][-rwxXsStT]{9}\s")
PATH_ONLY_RE = re.compile(r"^~?/[^\s]*$")

# How many transcript lines to look at for a genuine title. Bigger than the
# old 40/60 because skipping a scheduled-task reminder (and any further
# injected records behind it) has to reach past it; still bounded, and the
# loop stops as soon as it has what it came for.
TITLE_SCAN_LINES = 200


def _prompt_head(text):
    """The message's opening, as one line. Fancy prompts split it: zsh's
    "(user@host)-[~]" sits on line 1 and the "$ cmd" on line 2, so a
    user@host-only regex never sees the `$` it needs."""
    lines = text.strip().split("\n")
    head = lines[0] if lines else ""
    if len(lines) > 1 and BARE_PROMPT_RE.match(lines[1]):
        head = head + " " + lines[1].strip()
    return head


def _is_injected_or_pasted(text):
    """True if a user message is injected/pasted noise rather than a real
    request -- see claude_title_and_cwd."""
    stripped = text.strip()
    if not stripped:
        return True
    if stripped.startswith(INJECTED_PREFIXES):
        return True
    if bool(SHELL_PROMPT_RE.match(_prompt_head(stripped)) or BARE_PROMPT_RE.match(stripped)):
        return True
    # Only the opening line: a real request can be followed by pasted output,
    # and that paste shouldn't reject the question in front of it.
    head = stripped.split("\n", 1)[0].strip()
    return bool(LS_OUTPUT_RE.match(head) or PATH_ONLY_RE.match(head))


def _title_or_placeholder(title, fallback):
    """The title to show: the first genuine message, else the first message
    at all, else `(no title)` -- except for the sessions clisweave starts
    itself (`ai search --judge`, `ai handoff`). Those hold only the
    generated instruction, so they are labelled by what they are rather
    than repeating a prompt that says nothing about any work."""
    chosen = title or fallback
    if chosen:
        if chosen.startswith(JUDGE_PROMPT_PREFIX):
            return JUDGE_SESSION_TITLE
        m = re.match(re.escape(HANDOFF_PROMPT_PREFIX) + r"(\w+)", chosen)
        if m:
            return f"{HANDOFF_SESSION_TITLE} from {m.group(1)}"
        if chosen.startswith(HANDOFF_PROMPT_PREFIX):
            return HANDOFF_SESSION_TITLE
        if chosen.startswith(CODEX_APPROVAL_PROMPT_PREFIX):
            return CODEX_APPROVAL_SESSION_TITLE
    return chosen or "(no title)"



def _all_text_blocks(content, text_types=("text",)):
    if isinstance(content, str):
        return [content] if content.strip() else []
    if not isinstance(content, list):
        return []
    return [
        block_text(block) for block in content
        if isinstance(block, dict) and block.get("type") in text_types
        and block_text(block).strip()
    ]



def relative_time(ts):
    """How long ago, in the words a person would use.

    "13s ago" is a stopwatch reading, not an answer to "when was this?" --
    under a minute it is always "just now", and past a month "304d ago"
    stops meaning anything (the listing's own oldest row read that way)."""
    if not ts:
        return "?"
    delta = time.time() - ts
    if delta < 0:
        delta = 0
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    if delta < 86400:
        return f"{int(delta / 3600)}h ago"
    if delta < 30 * 86400:
        return f"{int(delta / 86400)}d ago"
    return f"{int(delta / (30 * 86400))}mo ago"


# A Windows-written absolute path: drive letter + separator, or a UNC share.
WIN_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def _windows_path_key(path):
    """Comparable form of a Windows-written path on a non-Windows host.

    ntpath.normpath unifies the separators, collapses `.`/`..` and drops a
    trailing one, then the case fold -- Windows compares paths
    case-insensitively, and os.path.normcase only folds case when *this*
    machine is Windows, so on POSIX it would miss "C:\\Work\\proj" vs
    "c:/work/proj" outright."""
    return ntpath.normpath(path).lower()


def _same_path(a, b):
    """True when two recorded cwds name the same directory.

    Raw string equality quietly failed on the same directory written two
    ways: macOS reports /var where a tool stored /private/var, Windows paths
    differ in case, and a trailing separator is easy to add. Any of those
    made `ai sessions --cwd` return nothing -- including, for a while, every
    codex session at once.

    Windows-style paths compare textually even on a POSIX host: a session
    store can be synced between machines, and here normcase is a no-op while
    realpath resolves both sides against *this* machine's cwd (yielding
    "/…/C:\\Work\\proj" vs "/…/c:/work/proj") -- neither ever matches."""
    if not isinstance(a, str) or not isinstance(b, str):
        return False
    if os.name != "nt" and (WIN_PATH_RE.match(a) or WIN_PATH_RE.match(b)):
        return _windows_path_key(a) == _windows_path_key(b)
    try:
        left, right = os.path.normcase(os.path.realpath(a)), os.path.normcase(os.path.realpath(b))
    except OSError:
        left, right = os.path.normcase(os.path.normpath(a)), os.path.normcase(os.path.normpath(b))
    return left == right
