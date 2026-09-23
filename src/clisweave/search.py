"""ai search - find past sessions relevant to a topic.

Two complementary passes over the same candidate list:

1. A literal pre-pass: case-insensitive scan of conversation text (including
   tool calls and results), excluding metadata and injected instructions.
   Short alphabetic queries match whole words. This costs zero LLM calls.
   This is not optional decoration: the LLM pass reasons over
   small sampled snippets, and a term that only appears in unsampled
   messages, tool calls, or past the snippet scan cap is invisible to it
   (real `aria2c` search: 4 such misses across codex/kimi stores).

2. An LLM judge over title + short content snippets, for semantic /
   paraphrase topics the literal pass cannot catch ("the nfc frequency
   lock issue" should find sessions that never use those exact words).

Batched calls, not one call per session: doing that would mean up to
hundreds of separate LLM invocations (slow, and real token cost each
time). Instead we build a prompt listing each candidate session's title +
a short content snippet, and ask the judge model to pick out the relevant
ones by number.

Candidates are split into chunks (CHUNK_SIZE each) rather than judged in
one giant batch: a single call over 493 candidates demonstrably missed a
real match that had the search term right in its title -- a "lost in a
long list" recall failure, not genuine ambiguity (confirmed by checking
the missed candidate's snippet, which contained the term just as clearly
as the one that *was* found). Smaller batches, judged independently and
unioned, trade more LLM calls for reliable recall. Chunks run in parallel
so wall-clock time stays close to a single call's latency.
"""
import concurrent.futures
import platform
import re
import signal
import subprocess
import sys

from . import sessions

# On Linux, ask the kernel to signal a judge subprocess if *this* process
# dies for any reason -- including a hard SIGKILL or OOM-kill, which no
# try/except/finally in this file can ever catch (the parent's process image
# is gone before any of its own cleanup code could run). subprocess.run()
# already kills the child on every in-process exception (it has its own
# bare `except: process.kill()`), so this closes the one remaining gap.
#
# preexec_fn runs in the forked child before exec, and CPython's own docs
# warn it can deadlock in a threaded process if another thread held a lock
# (e.g. malloc's) at the moment of fork -- real here, since judge calls run
# inside a ThreadPoolExecutor for parallel chunks. Keeping this to a single
# pre-resolved libc call (no dlopen/CDLL lookup at fork time) is the
# standard mitigation and what tools that need this in threaded Python
# programs actually do.
if platform.system() == "Linux":
    import ctypes

    _libc = ctypes.CDLL("libc.so.6", use_errno=True)
    _PR_SET_PDEATHSIG = 1

    def _die_with_parent():
        _libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM)
else:
    _die_with_parent = None

JUDGE_CMD = {
    # --no-session-persistence / --ephemeral: the judge call's own prompt
    # (the whole candidate list) would otherwise get saved as a real,
    # visible session -- showing up in `ai`/`ai full` with the raw prompt
    # text as its title, polluting the very listing this command reads.
    # kimi has no equivalent flag, so `--judge kimi` will still leak one.
    "claude": ["claude", "-p", "--no-session-persistence"],
    # --skip-git-repo-check: the search can be run from any cwd (often ~,
    # not a git repo), and codex exec otherwise refuses with "Not inside a
    # trusted directory".
    "codex": ["codex", "exec", "--ephemeral", "--skip-git-repo-check"],
    "kimi": ["kimi", "-p"],
}
DEFAULT_JUDGE = "claude"

# Judges that accept the prompt on stdin instead of argv. Passing a long
# prompt as a command-line argument hits OS limits (ARG_MAX) once the
# candidate list grows into the hundreds; stdin avoids that entirely.
JUDGE_USES_STDIN = {"claude", "codex"}

# See module docstring for why candidates are chunked instead of judged in
# one batch.
CHUNK_SIZE = 100

# A judge CLI can occasionally stop responding (for example while waiting on
# a network request).  Without a timeout, one stuck chunk keeps the whole
# search alive forever, even after every other chunk has finished.
JUDGE_TIMEOUT_SECONDS = 120

# Once the first batch has selected a working judge, the remaining batches
# can safely fan out without probing an unavailable judge over and over.
MAX_CONCURRENT_BATCHES = 8


class JudgeError(Exception):
    """A judge call failed unrecoverably. Carries a process-style exit
    code so cmd_search can propagate it -- raised instead of calling
    sys.exit directly so this also works correctly from a worker thread
    (chunks run in parallel; sys.exit there would only kill that thread,
    not the process, and the failure would go unnoticed)."""

    def __init__(self, code):
        super().__init__(f"judge failed with exit code {code}")
        self.code = code


def gather_candidates(tool_filter):
    light = []
    if tool_filter in (None, "claude"):
        light += sessions.claude_light_records()
    if tool_filter in (None, "codex"):
        light += sessions.codex_light_records()
    if tool_filter in (None, "kimi"):
        light += sessions.kimi_light_records(show_all=False)
    light.sort(key=lambda r: r["ts"], reverse=True)
    return light


def snippet_for(r):
    tool = r["tool"]
    if tool == "claude":
        return sessions.claude_snippet(r["path"])
    if tool == "codex":
        # title (thread_name, when available) is already shown separately
        # in the prompt line -- the snippet's job is additional content.
        return sessions.codex_rollout_snippet(r["id"])
    return sessions.kimi_snippet(r["dir"])


def build_prompt(topic, entries):
    """entries: list of (tool, cwd, title, snippet), 1 per candidate,
    in the same order they'll be numbered."""
    lines = [
        f"{n}. [{tool}] {cwd} — {title} :: {snippet}"
        for n, (tool, cwd, title, snippet) in enumerate(entries, start=1)
    ]
    return (
        "You are filtering a list of past AI coding-assistant conversations to find "
        f"the ones relevant to this topic: {topic!r}\n\n"
        # The relevance bar has to be spelled out: without it a batch judge
        # returns anything sharing a word or a field with the topic -- a
        # search for one device's firmware came back with 40 "matches",
        # mostly sessions that merely mention Android or a device at all.
        "Relevant means the conversation is about that topic itself -- the same "
        "device, project, file, error, or question -- not merely something in "
        "the same field. Sharing a word, a tool, or a domain with the topic is "
        "not enough, and neither is mentioning it once in passing.\n"
        "Be strict: when you are unsure, leave it out. A short list of "
        "confident hits beats a long one padded with maybes, and returning few "
        "-- or none -- is fine.\n\n"
        # One line per match, with the reason: it makes the judge commit to a
        # link instead of ticking a number, and the reason is shown with the
        # result so a weak match is recognizable as one.
        "Reply with ONLY one line per match, in this form -- the number, a "
        "colon, then a few words saying what links it to the topic:\n"
        "7: upgrades the IDC_Series firmware from A13\n\n"
        "No other text. If none are relevant, reply with the single word: none\n\n"
        + "\n".join(lines)
    )


def parse_numbers(text, max_n):
    nums = {int(m) for m in re.findall(r"\d+", text)}
    return {n for n in nums if 1 <= n <= max_n}


# "7: upgrades the firmware from A13" -> (7, "upgrades the firmware from A13")
REASON_LINE_RE = re.compile(r"\s*(\d+)\s*[):.\-]\s*(.*)")


def parse_numbered_reasons(text, max_n):
    """Judge output -> {number: reason}.

    Falls back to a bare list of numbers when the judge answers that way
    anyway (some do, despite the instruction), in which case the reasons are
    empty. Digits *inside* a reason ("from A13") are never read as a picked
    number -- that is exactly what a plain number scan over this richer reply
    would do, turning a reason into a phantom match."""
    reasons = {}
    for line in text.splitlines():
        m = REASON_LINE_RE.match(line)
        if not m:
            continue
        n = int(m.group(1))
        if 1 <= n <= max_n:
            reasons.setdefault(n, m.group(2).strip())
    if reasons:
        return reasons
    return {n: "" for n in parse_numbers(text, max_n)}


def _call_judge(judge, prompt):
    """Run a single judge and return its subprocess.CompletedProcess."""
    judge_cmd = JUDGE_CMD[judge]
    try:
        if judge in JUDGE_USES_STDIN:
            return subprocess.run(
                judge_cmd, input=prompt, capture_output=True, text=True,
                encoding="utf-8", timeout=JUDGE_TIMEOUT_SECONDS,
                preexec_fn=_die_with_parent,
            )
        return subprocess.run(
            [*judge_cmd, prompt], capture_output=True, text=True,
            encoding="utf-8", timeout=JUDGE_TIMEOUT_SECONDS,
            preexec_fn=_die_with_parent,
        )
    except FileNotFoundError:
        print(f"ai search: '{judge_cmd[0]}' not found on PATH", file=sys.stderr)
        raise JudgeError(127)
    except subprocess.TimeoutExpired:
        print(
            f"ai search: {judge_cmd[0]} timed out after "
            f"{JUDGE_TIMEOUT_SECONDS} seconds",
            file=sys.stderr,
        )
        raise JudgeError(124)


def _is_session_limit(result):
    output = (result.stdout or "") + (result.stderr or "")
    return "session limit" in output.lower()


def _is_auth_failure(result):
    """Claude's login expired or was revoked ("Failed to authenticate. API
    Error: 401 OAuth access token has expired"). Like a session limit, it
    makes claude unusable for the whole run -- every batch fails the same
    way -- so it warrants the same fall-through to another judge."""
    output = (result.stdout or "") + (result.stderr or "")
    lowered = output.lower()
    return "failed to authenticate" in lowered or "re-authenticate" in lowered


def run_judge_with_fallback(prompt, n, judge, judge_explicit, label):
    """Run the judge (falling back off claude on a session-limit hit,
    unless the user pinned one explicitly) against one prompt -- a full
    batch, or one chunk of one. Returns the picked local indices and the
    judge that succeeded. Raises JudgeError if every judge in the fallback
    sequence fails."""
    fallback_order = [DEFAULT_JUDGE, "codex", "kimi"]
    if judge_explicit:
        judges = [judge]
    else:
        # A prior batch may already have selected codex or kimi. Resume at
        # that point instead of retrying judges known not to be available.
        judges = fallback_order[fallback_order.index(judge):]
    result = None
    for judge_idx, j in enumerate(judges):
        print(f"Asking {j} to judge {label} ...", file=sys.stderr, flush=True)
        try:
            result = _call_judge(j, prompt)
        except JudgeError as e:
            has_fallback = not judge_explicit and judge_idx + 1 < len(judges)
            if e.code == 124 and has_fallback:
                print("  -> timed out; falling back to next judge", file=sys.stderr)
                continue
            raise
        if result.returncode == 0:
            return parse_numbered_reasons(result.stdout, n), j
        print(f"ai search: {j} exited with an error ({label})", file=sys.stderr)
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        if j == "claude" and (_is_session_limit(result) or _is_auth_failure(result)):
            if not judge_explicit and len(judges) > 1:
                print("  -> falling back to next judge", file=sys.stderr)
                continue
            if _is_auth_failure(result):
                print("  hint: Claude's login has expired. Run `claude` and sign in again (/login), or use --judge codex / --judge kimi.", file=sys.stderr)
            else:
                print("  hint: Claude is at its session limit. Retry after the reset time, or use --judge codex / --judge kimi.", file=sys.stderr)
        raise JudgeError(result.returncode)
    raise JudgeError(result.returncode if result else 1)


def cmd_search(argv):
    tool_filter = None
    judge = DEFAULT_JUDGE
    judge_explicit = False
    topic_parts = []

    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--tool":
            if i + 1 >= len(argv):
                print("ai search: --tool requires a value", file=sys.stderr)
                sys.exit(1)
            tool_filter = argv[i + 1]
            if tool_filter not in sessions.TOOLS:
                print(f"ai search: --tool must be one of {', '.join(sessions.TOOLS)}", file=sys.stderr)
                sys.exit(1)
            i += 2
        elif a == "--judge":
            if i + 1 >= len(argv):
                print("ai search: --judge requires a value", file=sys.stderr)
                sys.exit(1)
            judge = argv[i + 1]
            if judge not in JUDGE_CMD:
                print(f"ai search: --judge must be one of {', '.join(JUDGE_CMD)}", file=sys.stderr)
                sys.exit(1)
            judge_explicit = True
            i += 2
        else:
            topic_parts.append(a)
            i += 1

    topic = " ".join(topic_parts).strip()
    if not topic:
        print("Usage: ai search <topic> [--tool claude|codex|kimi] [--judge claude|codex|kimi]", file=sys.stderr)
        sys.exit(1)

    candidates = gather_candidates(tool_filter)
    if not candidates:
        print("No sessions found.")
        return

    rows = [sessions.resolve_row(r) for r in candidates]
    # Same exclusion as `ai sessions`: a session a tool started for itself
    # isn't a conversation, and its content is other sessions' text -- which
    # makes it a magnet for spurious matches. This also catches copied
    # review transcripts (a codex approval review's title collapses to
    # "codex approval review" once resolved), so a separate raw-prefix check
    # isn't needed.
    keep = [i for i, row in enumerate(rows) if not sessions.is_tool_started_row(row)]
    candidates = [candidates[i] for i in keep]
    rows = [rows[i] for i in keep]
    if not candidates:
        print("No sessions found.")
        return
    snippets = [snippet_for(r) for r in candidates]
    # row: (tool, full_id, when, short_id, cwd, title)
    entries = [(row[0], row[4], row[5], snippet) for row, snippet in zip(rows, snippets)]

    chunks = [entries[i:i + CHUNK_SIZE] for i in range(0, len(entries), CHUNK_SIZE)]
    n_chunks = len(chunks)

    print(f"Scanning {len(candidates)} sessions for exact matches: {topic!r} ...", file=sys.stderr, flush=True)
    exact_records = sessions.literal_matches(candidates, topic)
    exact_ids = {(r["tool"], r["id"]) for r in exact_records}
    exact_rows = [row for r, row in zip(candidates, rows) if (r["tool"], r["id"]) in exact_ids]

    def process_chunk(chunk_idx, selected_judge=judge):
        chunk = chunks[chunk_idx]
        offset = chunk_idx * CHUNK_SIZE
        prompt = build_prompt(topic, chunk)
        label = f"{len(chunk)} sessions against: {topic!r}" if n_chunks == 1 else (
            f"batch {chunk_idx + 1}/{n_chunks} ({len(chunk)} sessions) against: {topic!r}"
        )
        reasons_local, used_judge = run_judge_with_fallback(
            prompt, len(chunk), selected_judge, judge_explicit, label
        )
        if n_chunks > 1:
            noun = "match" if len(reasons_local) == 1 else "matches"
            print(
                f"Finished batch {chunk_idx + 1}/{n_chunks}: "
                f"{len(reasons_local)} {noun}",
                file=sys.stderr,
                flush=True,
            )
        return {offset + n: why for n, why in reasons_local.items()}, used_judge

    matched_reasons = {}
    used_judge = judge
    if n_chunks == 1:
        try:
            matched_reasons, used_judge = process_chunk(0)
        except JudgeError as e:
            if exact_ids:
                print("ai search: semantic search failed; showing exact matches only", file=sys.stderr)
            else:
                sys.exit(e.code)
    else:
        failures = 0
        # Let one real batch choose the usable judge before fanning out. This
        # avoids launching every chunk against a judge that is at its session
        # limit or otherwise unavailable.
        try:
            first_matches, used_judge = process_chunk(0)
            matched_reasons.update(first_matches)
        except JudgeError:
            failures += 1

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(MAX_CONCURRENT_BATCHES, n_chunks - 1)
        ) as pool:
            futures = [
                pool.submit(process_chunk, i, used_judge)
                for i in range(1, n_chunks)
            ]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    chunk_matches, _ = fut.result()
                    matched_reasons.update(chunk_matches)
                except JudgeError:
                    failures += 1
        if failures == n_chunks:
            if exact_ids:
                print(f"ai search: all {n_chunks} semantic batches failed; showing exact matches only", file=sys.stderr)
            else:
                print(f"ai search: all {n_chunks} batches failed", file=sys.stderr)
                sys.exit(1)
        if failures:
            print(f"ai search: {failures}/{n_chunks} batches failed; showing partial results", file=sys.stderr)

    matched_indices = set(matched_reasons)
    matched = [row for n, row in enumerate(rows, start=1) if n in matched_indices]
    # Already reported in the exact section -- don't list a session twice.
    semantic = [row for row in matched if (row[0], row[1]) not in exact_ids]
    # The judge's own one-line justification, shown under each match: it has
    # to commit to a link rather than tick a number, and a weak match is
    # recognizable as one instead of looking like a considered pick.
    notes = {
        (rows[n - 1][0], rows[n - 1][1]): why
        for n, why in matched_reasons.items() if why
    }

    if not exact_rows and not semantic:
        print("No relevant sessions found.")
        return

    # One cache write for the union in printed order, so `ai resume <N>`
    # numbers stay valid across both sections.
    sessions.write_list_cache(
        [{"tool": tool, "id": full_id} for tool, full_id, *_ in exact_rows + semantic]
    )
    if exact_rows:
        print(f"exact matches for {topic!r} (literal, case-insensitive):")
        sessions.render_rows(exact_rows, write_cache=False)
    if semantic:
        print(f"semantic matches (judge: {used_judge}):")
        sessions.render_rows(semantic, write_cache=False, start=len(exact_rows) + 1, notes=notes)
