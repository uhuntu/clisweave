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

Even with chunking, judging alone demonstrably *misses* real matches.
Searching for "esper" over 534 sessions returned only the 2 sessions that
had the word in their title, and dropped 7 others that mention it in their
transcripts -- one of them 58 times, one 19 times -- just because the
800-char sampled snippet never covered where the word actually appears,
so the judge was never shown it. So there are now two independent passes,
unioned:

1. a local, free exhaustive scan of each candidate's *full* text for the
   topic's own words; anything containing all of them matches outright,
   with no LLM involved, so it can't be lost in a long list or in the gap
   between sampled messages;
2. the LLM judge (the only thing that can catch a session that discusses
   the topic without ever using the words, e.g. by project directory).

Candidates already matched pass 1 are dropped from the judge's list, which
keeps its batches shorter -- the same "lost in a long list" effect the
chunking exists to avoid.
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

# Below this length a word is more likely to be noise than a searchable
# term ("C", "go", "2"), so it can't carry the literal pass on its own.
TERM_MIN_LEN = 3

# Words common enough that requiring their presence adds no signal while
# making the literal pass stricter than the user meant. Not a real
# stopword list -- just the handful that otherwise turn every natural-
# language topic into "match almost anything".
TOPIC_STOPWORDS = frozenset({
    "the", "and", "for", "but", "not", "you", "your", "our", "its", "his",
    "her", "was", "are", "were", "has", "have", "had", "can", "could",
    "with", "from", "into", "that", "this", "what", "when", "where", "why",
    "please", "about", "issue", "problem", "session",
})


class JudgeError(Exception):
    """A judge call failed unrecoverably. Carries a process-style exit
    code so cmd_search can propagate it -- raised instead of calling
    sys.exit directly so this also works correctly from a worker thread
    (chunks run in parallel; sys.exit there would only kill that thread,
    not the process, and the failure would go unnoticed)."""

    def __init__(self, code):
        super().__init__(f"judge failed with exit code {code}")
        self.code = code


def gather_candidates(tool_filter, include_archived=False):
    light = []
    if tool_filter in (None, "claude"):
        light += sessions.claude_light_records()
    if tool_filter in (None, "codex"):
        light += sessions.codex_light_records()
    if tool_filter in (None, "kimi"):
        # archived kimi sessions are excluded by default, same as the plain
        # listing -- `ai search --all` opts back in.
        light += sessions.kimi_light_records(show_all=include_archived)
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
        "word: none\n\n"
        "Favour recall over precision: a session only has to be plausibly connected "
        "to the topic -- discussing it, mentioning it in passing, or being part of the "
        "same investigation. Include it if you have any real doubt either way. Sessions "
        "that literally contain the topic's words have already been matched locally and "
        "are not in this list, so a near-miss here is the only chance to surface them. "
        "Missing a relevant session is a real failure; listing one that turns out "
        "unrelated just costs the human one row to skim.\n\n" + "\n".join(lines)
    )


def parse_numbers(text, max_n):
    nums = {int(m) for m in re.findall(r"\d+", text)}
    return {n for n in nums if 1 <= n <= max_n}


def topic_terms(topic):
    """The words a session's text must contain to be a literal match.

    Words, not raw substrings, so searching "esper" can't match
    "desperate"; and the filler words are dropped so that a natural-
    language topic like "the nfc frequency lock issue" means nfc +
    frequency + lock rather than every session that ever says "the". If
    that leaves nothing (a short, all-stopword topic), fall back to the
    raw tokens -- stricter than dropping them would be the wrong
    direction, since the whole point is not missing things."""
    tokens = [t.lower() for t in re.findall(r"\w+", topic) if len(t) >= TERM_MIN_LEN]
    meaningful = [t for t in tokens if t not in TOPIC_STOPWORDS]
    chosen = meaningful or tokens
    if not chosen and len(topic.strip()) >= TERM_MIN_LEN:
        # No usable words at all (e.g. a topic written entirely in
        # punctuation-heavy shorthand) -- fall back to the literal phrase.
        return [topic.strip().lower()]
    return list(dict.fromkeys(chosen))


def _term_pattern(term):
    # Word boundaries rather than \b, spelled out so a term that itself
    # starts/ends in a non-word character ("c++") still matches.
    left = r"(?<![0-9A-Za-z_])" if term[:1].isalnum() or term[:1] == "_" else ""
    right = r"(?![0-9A-Za-z_])" if term[-1:].isalnum() or term[-1:] == "_" else ""
    return re.compile(left + re.escape(term) + right, re.IGNORECASE)


def topic_matchers(topic):
    """One compiled pattern per topic term, or [] if there's nothing worth
    scanning for locally."""
    return [_term_pattern(t) for t in topic_terms(topic)]


def literal_hits(matchers, records):
    """The deterministic half of the search: candidates whose full text
    contains every topic term.

    Reads each session's whole transcript locally (a few seconds for 500+
    sessions, no LLM cost) and so cannot suffer the judge's failure modes
    -- it doesn't depend on the ~800-char sampled snippet happening to
    cover where the word appears, and nothing gets lost in a long list.
    Returns 1-based indices into `records`."""
    hits = set()
    if not matchers:
        return hits
    needed = len(matchers)
    for n, record in enumerate(records, start=1):
        found = set()
        for text in sessions.session_message_texts(record):
            for i, rx in enumerate(matchers):
                if i not in found and rx.search(text):
                    found.add(i)
            if len(found) == needed:
                hits.add(n)
                break
    return hits


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
    include_archived = False
    topic_parts = []

    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--all":
            include_archived = True
            i += 1
        elif a == "--tool":
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
        print("Usage: ai search <topic> [--tool claude|codex|kimi] [--judge claude|codex|kimi] [--all]", file=sys.stderr)
        sys.exit(1)

    candidates = gather_candidates(tool_filter, include_archived=include_archived)
    if not candidates:
        print("No sessions found.")
        return

    # Stage 1: cheap local pass over each session's full transcript. See the
    # module docstring -- the judge alone was missing sessions that mention
    # the topic dozens of times but never in the sampled snippet.
    literal = literal_hits(topic_matchers(topic), candidates)
    # Indices (0-based) still needing the judge's semantic opinion.
    judged = [i for i in range(len(candidates)) if i + 1 not in literal]
    if literal:
        verbatim = "session contains" if len(literal) == 1 else "sessions contain"
        print(f"ai search: {len(literal)} {verbatim} the topic verbatim "
              f"(matched locally); asking the judge about the other {len(judged)}",
              file=sys.stderr)

    rows = [sessions.resolve_row(r) for r in candidates]
    # row: (tool, full_id, when, short_id, cwd, title)
    entries = [(rows[i][0], rows[i][4], rows[i][5], snippet_for(candidates[i])) for i in judged]

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

    # Stage 2: the judge, over whatever the local pass couldn't settle.
    # `judged` maps each prompted entry back to its candidate index, since
    # literal matches are no longer part of the numbered list.
    matched_indices = set(literal)
    if n_chunks == 1:
        try:
            picked = process_chunk(0)
        except JudgeError as e:
            # The judge failed outright -- still show what the local pass
            # found rather than throwing those matches away with it.
            if matched_indices:
                print(f"ai search: judge failed; showing only the {len(matched_indices)} "
                      "verbatim matches", file=sys.stderr)
                sessions.render_rows([rows[n - 1] for n in sorted(matched_indices)])
                return
            sys.exit(e.code)
        matched_indices |= {judged[n - 1] + 1 for n in picked}
    elif n_chunks > 1:
        failures = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, n_chunks)) as pool:
            futures = [pool.submit(process_chunk, i) for i in range(n_chunks)]
            for fut in concurrent.futures.as_completed(futures):
                try:
                    matched_indices |= {judged[n - 1] + 1 for n in fut.result()}
                except JudgeError:
                    failures += 1
        if failures == n_chunks:
            print(f"ai search: all {n_chunks} batches failed; showing only the verbatim "
                  "matches", file=sys.stderr)
            sessions.render_rows([rows[n - 1] for n in sorted(matched_indices)])
            sys.exit(1)
        if failures:
            print(f"ai search: {failures}/{n_chunks} batches failed; showing partial results", file=sys.stderr)

    matched = [rows[n - 1] for n in sorted(matched_indices)]

    if not matched:
        print("No relevant sessions found.")
        return

    sessions.render_rows(matched)
