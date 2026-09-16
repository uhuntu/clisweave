import json

import pytest

from clisweave import search, sessions


# ---------- literal (local) pass ----------

def write_transcript(path, messages):
    """Minimal claude-shaped jsonl: one user/assistant text message per
    entry, which is all session_message_texts() reads."""
    lines = [
        json.dumps({"type": role, "message": {"role": role, "content": text}})
        for role, text in messages
    ]
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def _claude_record(path, ts=1):
    return {"tool": "claude", "id": "id-local", "ts": ts, "path": path, "cwd": "/home/hunt"}


@pytest.mark.parametrize(
    "topic,expected",
    [
        ("esper", ["esper"]),
        ("Esper OTA capture", ["esper", "ota", "capture"]),
        # filler shouldn't become a required word, or "the nfc frequency lock
        # issue" would reduce to "matches anything mentioning the issue"
        ("the nfc frequency lock issue", ["nfc", "frequency", "lock"]),
        # ...but a topic made entirely of filler still has to search for
        # something, rather than silently meaning "everything"
        ("the issue", ["the", "issue"]),
        ("go", []),  # too short to be a searchable term on its own
        ("", []),
    ],
)
def test_topic_terms(topic, expected):
    assert search.topic_terms(topic) == expected


def test_topic_matchers_are_word_bounded_and_case_insensitive():
    """'esper' must not drag in every transcript containing 'desperate'
    (a real substring collision for this exact search term)."""
    matchers = search.topic_matchers("esper")
    assert len(matchers) == 1
    assert not matchers[0].search("they grew desperate")
    assert matchers[0].search("the ESPER build failed")
    assert matchers[0].search("Full procedure (Esper-free)")


def test_topic_matchers_require_every_term():
    """All terms are required, in any order or wording distance apart --
    a single one of them isn't enough."""
    matchers = search.topic_matchers("nfc frequency lock")
    assert all(m.search("the nfc frequency lock is broken") for m in matchers)
    assert not all(m.search("just an nfc question") for m in matchers)


def test_literal_hits_finds_word_the_snippet_would_miss(tmp_path):
    """Regression test: searching 'esper' returned only the 2 sessions with
    the word in their title and dropped 7 that mention it in the body --
    one 58 times, one 19 times -- because the 800-char sampled snippet
    never covered where the word appears, so the judge never saw it."""
    path = write_transcript(tmp_path / "session.jsonl", [
        ("user", "hi"),
        *[(role, "unrelated filler message") for role in
          ["assistant", "user"] * 40],
        ("user", "keep going"),
        ("assistant", "we traced it back to the esper service"),
    ])
    record = _claude_record(path)

    snippets_that_judge_sees = sessions.claude_snippet(path)
    assert "esper" not in snippets_that_judge_sees  # the judge can't see it

    assert search.literal_hits(search.topic_matchers("esper"), [record]) == {1}


def test_literal_hits_ignores_non_matching_session(tmp_path):
    path = write_transcript(tmp_path / "session.jsonl", [
        ("user", "something entirely different"),
    ])
    assert search.literal_hits(search.topic_matchers("esper"), [_claude_record(path)]) == set()


def test_literal_hits_returns_empty_without_usable_terms(tmp_path):
    path = write_transcript(tmp_path / "session.jsonl", [("user", "anything")])
    assert search.literal_hits(search.topic_matchers("go"), [_claude_record(path)]) == set()


def _judge_returning(stdout):
    class FakeResult:
        returncode = 0
        stderr = ""

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(kwargs.get("input") or argv[-1])
        FakeResult.stdout = stdout
        return FakeResult()

    return calls, fake_run


def test_cmd_search_unions_local_matches_with_judge_picks(monkeypatch, tmp_path, capsys):
    """The judge answering 'none' must not erase a session that literally
    contains every topic word."""
    path = write_transcript(tmp_path / "session.jsonl", [
        ("user", "why does esper keep timing out"),
    ])
    monkeypatch.setattr(search, "gather_candidates", lambda *a, **kw: [
        _claude_record(path, ts=2),
        {"tool": "codex", "id": "id-2", "ts": 1, "title": "x"},
    ])
    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:6], "?", r.get("title", "(no title)"),
    ))
    rendered = []
    monkeypatch.setattr(sessions, "render_rows", lambda rows: rendered.append(rows))

    calls, fake_run = _judge_returning("none")
    monkeypatch.setattr(search.subprocess, "run", fake_run)

    search.cmd_search(["esper"])

    assert len(rendered) == 1
    assert [row[1] for row in rendered[0]] == ["id-local"]
    # the already-matched session isn't sent to the judge at all -- only the
    # remaining candidate is there, renumbered from 1
    assert "[claude]" not in calls[0]
    assert "1. [codex]" in calls[0]  # the sole remaining candidate, renumbered from 1


def test_cmd_search_skips_judge_when_everything_matches_locally(monkeypatch, tmp_path):
    path = write_transcript(tmp_path / "session.jsonl", [("user", "esper again")])
    monkeypatch.setattr(search, "gather_candidates", lambda *a, **kw: [_claude_record(path)])
    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:6], "?", r.get("title", "(no title)"),
    ))
    rendered = []
    monkeypatch.setattr(sessions, "render_rows", lambda rows: rendered.append(rows))

    def fail_if_called(*a, **kw):
        raise AssertionError("judge should not be called when the local pass settles everything")

    monkeypatch.setattr(search.subprocess, "run", fail_if_called)

    search.cmd_search(["esper"])

    assert [row[1] for row in rendered[0]] == ["id-local"]


def test_cmd_search_all_includes_archived_kimi_sessions(monkeypatch, capsys):
    seen = []

    monkeypatch.setattr(sessions, "claude_light_records", lambda: [])
    monkeypatch.setattr(sessions, "codex_light_records", lambda: [])
    monkeypatch.setattr(sessions, "kimi_light_records", lambda show_all: seen.append(show_all) or [])

    search.cmd_search(["topic"])
    assert seen == [False]

    search.cmd_search(["--all", "topic"])
    assert seen == [False, True]


def test_judge_calls_dont_persist_a_visible_session():
    """Regression test: without an ephemeral/no-persist flag, the judge
    call's own prompt (the whole candidate list) gets saved as a real
    session and shows up in `ai`/`ai full` with the raw prompt as its
    title -- the tool polluting the listing it reads from."""
    assert "--no-session-persistence" in search.JUDGE_CMD["claude"]
    assert "--ephemeral" in search.JUDGE_CMD["codex"]


def test_build_prompt_numbers_entries_in_order():
    prompt = search.build_prompt("nfc issue", [
        ("codex", "/a", "Find isnfcon", "Find isnfcon"),
        ("claude", "/b", "hi", "hi there"),
    ])
    assert "1. [codex] /a — Find isnfcon :: Find isnfcon" in prompt
    assert "2. [claude] /b — hi :: hi there" in prompt
    assert "'nfc issue'" in prompt
    assert "reply with the single word: none" in prompt


@pytest.mark.parametrize(
    "text,max_n,expected",
    [
        ("1, 3, 5", 5, {1, 3, 5}),
        ("1 and 3 and maybe also 5", 5, {1, 3, 5}),
        ("none", 5, set()),
        ("", 5, set()),
        ("7, 8", 5, set()),  # out of range, dropped
        ("2, 2, 2", 5, {2}),  # deduped
    ],
)
def test_parse_numbers(text, max_n, expected):
    assert search.parse_numbers(text, max_n) == expected


def test_gather_candidates_respects_tool_filter(monkeypatch):
    monkeypatch.setattr(sessions, "claude_light_records", lambda: [{"tool": "claude", "ts": 1}])
    monkeypatch.setattr(sessions, "codex_light_records", lambda: [{"tool": "codex", "ts": 2}])
    monkeypatch.setattr(sessions, "kimi_light_records", lambda show_all: [{"tool": "kimi", "ts": 3}])

    assert [r["tool"] for r in search.gather_candidates(None)] == ["kimi", "codex", "claude"]
    assert [r["tool"] for r in search.gather_candidates("codex")] == ["codex"]


def test_snippet_for_dispatches_per_tool(monkeypatch):
    monkeypatch.setattr(sessions, "claude_snippet", lambda path: f"claude:{path}")
    monkeypatch.setattr(sessions, "codex_rollout_snippet", lambda sid: f"codex:{sid}")
    monkeypatch.setattr(sessions, "kimi_snippet", lambda d: f"kimi:{d}")

    assert search.snippet_for({"tool": "claude", "path": "/x.jsonl"}) == "claude:/x.jsonl"
    assert search.snippet_for({"tool": "codex", "id": "abc123", "title": "Find isnfcon"}) == "codex:abc123"
    assert search.snippet_for({"tool": "kimi", "dir": "/y"}) == "kimi:/y"


def test_cmd_search_rejects_empty_topic(capsys):
    with pytest.raises(SystemExit):
        search.cmd_search([])
    assert "Usage" in capsys.readouterr().err


def test_cmd_search_rejects_bad_judge(capsys):
    with pytest.raises(SystemExit):
        search.cmd_search(["--judge", "bogus", "topic"])
    assert "must be one of" in capsys.readouterr().err


def test_cmd_search_filters_to_llm_picked_rows(monkeypatch, capsys):
    fake_candidates = [
        {"tool": "codex", "id": "id-1", "ts": 3, "title": "Find isnfcon"},
        {"tool": "claude", "id": "id-2", "ts": 2, "path": "/x.jsonl", "cwd": "/home/hunt"},
        {"tool": "kimi", "id": "id-3", "ts": 1, "dir": "/y"},
    ]
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter, **_kw: fake_candidates)
    monkeypatch.setattr(sessions, "claude_snippet", lambda path: "irrelevant chat")
    monkeypatch.setattr(sessions, "kimi_snippet", lambda d: "irrelevant chat")

    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:6], r.get("cwd") or "?", r.get("title", "(no title)"),
    ))

    rendered = []
    monkeypatch.setattr(sessions, "render_rows", lambda rows: rendered.append(rows))

    class FakeResult:
        returncode = 0
        stdout = "1"  # only the first (codex) candidate is relevant
        stderr = ""

    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return FakeResult()

    monkeypatch.setattr(search.subprocess, "run", fake_run)

    search.cmd_search(["nfc", "frequency", "lock"])

    argv, kwargs = calls[0]
    assert argv[:2] == ["claude", "-p"]
    assert "nfc frequency lock" in kwargs["input"]  # long prompt passed via stdin
    assert len(rendered) == 1
    assert [row[1] for row in rendered[0]] == ["id-1"]


def test_cmd_search_kimi_still_uses_argv(monkeypatch):
    """kimi -p requires an argument and does not read stdin, so it must keep
    receiving the prompt as the last argv element."""
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter, **_kw: [
        {"tool": "codex", "id": "id-1", "ts": 1, "title": "x"},
    ])
    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:6], "?", r.get("title", "(no title)"),
    ))
    monkeypatch.setattr(sessions, "render_rows", lambda rows: None)

    calls = []

    class FakeResult:
        returncode = 0
        stdout = "none"
        stderr = ""

    monkeypatch.setattr(search.subprocess, "run", lambda argv, **kw: calls.append((argv, kw)) or FakeResult())

    search.cmd_search(["--judge", "kimi", "topic"])

    argv, kwargs = calls[0]
    assert argv[:2] == ["kimi", "-p"]
    assert "topic" in argv[-1]
    assert "input" not in kwargs


def test_cmd_search_uses_requested_judge_tool(monkeypatch):
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter, **_kw: [
        {"tool": "codex", "id": "id-1", "ts": 1, "title": "x"},
    ])
    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:6], "?", r.get("title", "(no title)"),
    ))
    monkeypatch.setattr(sessions, "render_rows", lambda rows: None)

    calls = []

    class FakeResult:
        returncode = 0
        stdout = "none"
        stderr = ""

    monkeypatch.setattr(search.subprocess, "run", lambda argv, **kw: calls.append(argv) or FakeResult())

    search.cmd_search(["--judge", "kimi", "topic"])

    assert calls[0][:2] == ["kimi", "-p"]


def test_cmd_search_fallback_on_claude_session_limit(monkeypatch, capsys):
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter, **_kw: [
        {"tool": "codex", "id": "id-1", "ts": 1, "title": "x"},
    ])
    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:6], "?", r.get("title", "(no title)"),
    ))
    monkeypatch.setattr(sessions, "render_rows", lambda rows: None)

    calls = []

    class ClaudeLimit:
        returncode = 1
        stdout = "You've hit your session limit · resets 1:40pm"
        stderr = ""

    class CodexOK:
        returncode = 0
        stdout = "none"
        stderr = ""

    monkeypatch.setattr(search.subprocess, "run", lambda *a, **kw: calls.append((a, kw)) or (CodexOK if len(calls) > 1 else ClaudeLimit)())

    search.cmd_search(["topic"])

    assert len(calls) == 2
    assert calls[0][0][0][:2] == ["claude", "-p"]
    assert calls[1][0][0][:2] == ["codex", "exec"]
    err = capsys.readouterr().err
    assert "falling back" in err


def test_cmd_search_explicit_claude_session_limit_shows_hint(monkeypatch, capsys):
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter, **_kw: [
        {"tool": "codex", "id": "id-1", "ts": 1, "title": "x"},
    ])
    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:6], "?", r.get("title", "(no title)"),
    ))

    class FakeResult:
        returncode = 1
        stdout = "You've hit your session limit · resets 1:40pm"
        stderr = ""

    monkeypatch.setattr(search.subprocess, "run", lambda *a, **kw: FakeResult())

    with pytest.raises(SystemExit) as exc_info:
        search.cmd_search(["--judge", "claude", "topic"])
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "session limit" in err
    assert "--judge codex" in err


def test_cmd_search_missing_judge_binary_reports_cleanly(monkeypatch, capsys):
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter, **_kw: [
        {"tool": "codex", "id": "id-1", "ts": 1, "title": "x"},
    ])
    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:6], "?", r.get("title", "(no title)"),
    ))

    def fake_run(argv, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(search.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc_info:
        search.cmd_search(["topic"])
    assert exc_info.value.code == 127
    assert "not found on PATH" in capsys.readouterr().err


def test_cmd_search_no_candidates(monkeypatch, capsys):
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter, **_kw: [])

    search.cmd_search(["topic"])

    assert "No sessions found" in capsys.readouterr().out


# ---------- chunking ----------

def _fake_candidates(n):
    return [{"tool": "codex", "id": f"id-{i}", "ts": n - i, "title": f"session {i}"} for i in range(n)]


def _stub_resolve_and_render(monkeypatch):
    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:8], "?", r.get("title", "(no title)"),
    ))
    rendered = []
    monkeypatch.setattr(sessions, "render_rows", lambda rows: rendered.append(rows))
    return rendered


def test_cmd_search_splits_into_chunks_of_chunk_size(monkeypatch):
    """Regression test: a single 493-candidate batch demonstrably missed a
    real match (confirmed by checking the missed candidate's snippet,
    which contained the search term just as clearly as the one that *was*
    found) -- a 'lost in a long list' recall failure. Candidates must be
    split into CHUNK_SIZE-sized batches, each judged independently."""
    n = search.CHUNK_SIZE * 2 + 30  # 3 chunks: 100, 100, 30
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter, **_kw: _fake_candidates(n))
    _stub_resolve_and_render(monkeypatch)

    seen_sizes = []

    class FakeResult:
        returncode = 0
        stdout = "none"
        stderr = ""

    def fake_run(argv, input=None, **kw):
        seen_sizes.append(input.count("\n1. ["))  # each chunk's prompt starts numbering at 1
        return FakeResult()

    monkeypatch.setattr(search.subprocess, "run", fake_run)

    search.cmd_search(["topic"])

    assert len(seen_sizes) == 3
    assert sum(seen_sizes) == 3  # one "1. [" per chunk, confirming 3 separate prompts


def test_cmd_search_unions_matches_across_chunks(monkeypatch):
    """Each chunk is numbered locally (1..len(chunk)); matches from later
    chunks must map back to the correct global candidate, not collide with
    chunk 1's numbering."""
    n = search.CHUNK_SIZE + 5
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter, **_kw: _fake_candidates(n))
    rendered = _stub_resolve_and_render(monkeypatch)
    monkeypatch.setattr(search, "snippet_for", lambda r: "")

    class FakeResult:
        def __init__(self, stdout):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def fake_run(argv, input=None, **kw):
        # chunk 1 (global candidates 0..99) opens with "session 0"; chunk 2
        # (100..104) opens with "session 100" -- identify by content, not
        # call order, since chunks run in parallel threads.
        if "— session 0 ::" in input:
            return FakeResult("1")  # local #1 in chunk 1 -> global candidate 0
        return FakeResult("2")  # local #2 in chunk 2 (offset 100) -> global row 102 -> id-101

    monkeypatch.setattr(search.subprocess, "run", fake_run)

    search.cmd_search(["topic"])

    assert len(rendered) == 1
    matched_ids = {row[1] for row in rendered[0]}
    assert matched_ids == {"id-0", "id-101"}


def test_cmd_search_partial_failure_still_shows_other_chunks(monkeypatch, capsys):
    n = search.CHUNK_SIZE + 5
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter, **_kw: _fake_candidates(n))
    rendered = _stub_resolve_and_render(monkeypatch)
    monkeypatch.setattr(search, "snippet_for", lambda r: "")

    class Fail:
        returncode = 1
        stdout = ""
        stderr = "boom"

    class OK:
        returncode = 0
        stdout = "1"  # local #1 in chunk 2 (offset 100) -> global row 101 -> id-100
        stderr = ""

    def fake_run(argv, input=None, **kw):
        # chunk 1 (contains "session 0") always fails every fallback judge;
        # chunk 2 succeeds immediately.
        return Fail() if "— session 0 ::" in input else OK()

    monkeypatch.setattr(search.subprocess, "run", fake_run)

    search.cmd_search(["topic"])

    err = capsys.readouterr().err
    assert "batches failed; showing partial results" in err
    assert len(rendered) == 1
    matched_ids = {row[1] for row in rendered[0]}
    assert matched_ids == {"id-100"}  # only chunk 2's match survives


def test_cmd_search_all_chunks_fail_exits_nonzero(monkeypatch):
    n = search.CHUNK_SIZE + 5
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter, **_kw: _fake_candidates(n))
    _stub_resolve_and_render(monkeypatch)

    class Fail:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(search.subprocess, "run", lambda *a, **kw: Fail())

    with pytest.raises(SystemExit) as exc_info:
        search.cmd_search(["--judge", "claude", "topic"])
    assert exc_info.value.code != 0


def test_cmd_search_single_small_batch_no_batch_label(monkeypatch, capsys):
    """With <= CHUNK_SIZE candidates there's only one chunk -- the status
    line shouldn't talk about "batch 1/1", matching the pre-chunking
    output format for the common case."""
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter, **_kw: _fake_candidates(3))
    _stub_resolve_and_render(monkeypatch)

    class FakeResult:
        returncode = 0
        stdout = "none"
        stderr = ""

    monkeypatch.setattr(search.subprocess, "run", lambda *a, **kw: FakeResult())

    search.cmd_search(["topic"])

    err = capsys.readouterr().err
    assert "batch" not in err.lower()
    assert "3 sessions against" in err
