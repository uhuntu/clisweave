"""ai search - ask an LLM which past sessions are relevant to a topic.

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
import re
import subprocess
import sys

from . import sessions

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
        "Reply with ONLY a comma-separated list of the numbers below that are relevant. "
        "No other text, no explanation. If none are relevant, reply with the single "
        "word: none\n\n" + "\n".join(lines)
    )


def parse_numbers(text, max_n):
    nums = {int(m) for m in re.findall(r"\d+", text)}
    return {n for n in nums if 1 <= n <= max_n}


def _call_judge(judge, prompt):
    """Run a single judge and return its subprocess.CompletedProcess."""
    judge_cmd = JUDGE_CMD[judge]
    try:
        if judge in JUDGE_USES_STDIN:
            return subprocess.run(judge_cmd, input=prompt, capture_output=True, text=True, encoding="utf-8")
        return subprocess.run([*judge_cmd, prompt], capture_output=True, text=True, encoding="utf-8")
    except FileNotFoundError:
        print(f"ai search: '{judge_cmd[0]}' not found on PATH", file=sys.stderr)
        raise JudgeError(127)


def _is_session_limit(result):
    output = (result.stdout or "") + (result.stderr or "")
    return "session limit" in output.lower()


def run_judge_with_fallback(prompt, n, judge, judge_explicit, label):
    """Run the judge (falling back off claude on a session-limit hit,
    unless the user pinned one explicitly) against one prompt -- a full
    batch, or one chunk of one. Returns the set of 1-based indices (local
    to this prompt) the judge picked. Raises JudgeError if every judge in
    the fallback sequence fails."""
    judges = [judge] if judge_explicit else [DEFAULT_JUDGE, "codex", "kimi"]
    result = None
    for j in judges:
        print(f"Asking {j} to judge {label} ...", file=sys.stderr, flush=True)
        result = _call_judge(j, prompt)
        if result.returncode == 0:
            return parse_numbers(result.stdout, n)
        print(f"ai search: {j} exited with an error ({label})", file=sys.stderr)
        if result.stdout:
            print(result.stdout, file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        if j == "claude" and _is_session_limit(result):
            if not judge_explicit and len(judges) > 1:
                print("  -> falling back to next judge", file=sys.stderr)
                continue
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
    snippets = [snippet_for(r) for r in candidates]
    # row: (tool, full_id, when, short_id, cwd, title)
    entries = [(row[0], row[4], row[5], snippet) for row, snippet in zip(rows, snippets)]

    chunks = [entries[i:i + CHUNK_SIZE] for i in range(0, len(entries), CHUNK_SIZE)]
    n_chunks = len(chunks)

    def process_chunk(chunk_idx):
        chunk = chunks[chunk_idx]
        offset = chunk_idx * CHUNK_SIZE
        prompt = build_prompt(topic, chunk)
        label = f"{len(chunk)} sessions against: {topic!r}" if n_chunks == 1 else (
            f"batch {chunk_idx + 1}/{n_chunks} ({len(chunk)} sessions) against: {topic!r}"
        )
        picked_local = run_judge_with_fallback(prompt, len(chunk), judge, judge_explicit, label)
        return {offset + n for n in picked_local}

    matched_indices = set()
    if n_chunks == 1:
        try:
            matched_indices = process_chunk(0)
        except JudgeError as e:
            sys.exit(e.code)
    else:
        failures = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, n_chunks)) as pool:
            futures = [pool.submit(process_chunk, i) for i in range(n_chunks)]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    matched_indices |= fut.result()
                except JudgeError:
                    failures += 1
        if failures == n_chunks:
            sys.exit(1)
        if failures:
            print(f"ai search: {failures}/{n_chunks} batches failed; showing partial results", file=sys.stderr)

    matched = [row for n, row in enumerate(rows, start=1) if n in matched_indices]

    if not matched:
        print("No relevant sessions found.")
        return

    sessions.render_rows(matched)
