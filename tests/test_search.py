import json

import pytest

from clisweave import search, sessions


def test_judge_calls_dont_persist_a_visible_session():
    """Regression test: without an ephemeral/no-persist flag, the judge
    call's own prompt (the whole candidate list) gets saved as a real
    session and shows up in `ai`/`ai full` with the raw prompt as its
    title -- the tool polluting the listing it reads from."""
    assert "--no-session-persistence" in search.JUDGE_CMD["claude"]
    assert "--ephemeral" in search.JUDGE_CMD["codex"]


def test_judge_call_has_timeout(monkeypatch):
    calls = []

    class FakeResult:
        returncode = 0
        stdout = "none"
        stderr = ""

    monkeypatch.setattr(
        search.subprocess,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or FakeResult(),
    )

    search._call_judge("claude", "prompt")

    assert calls[0][1]["timeout"] == search.JUDGE_TIMEOUT_SECONDS


def test_judge_timeout_reports_cleanly(monkeypatch, capsys):
    def time_out(*args, **kwargs):
        raise search.subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(search.subprocess, "run", time_out)

    with pytest.raises(search.JudgeError) as exc_info:
        search._call_judge("claude", "prompt")

    assert exc_info.value.code == 124
    assert "timed out after" in capsys.readouterr().err


def test_build_prompt_states_a_relevance_bar():
    """Without one, a batch judge returns anything sharing a word or a field
    with the topic: one real search came back with 40 "matches", mostly
    sessions that only mention the subject area at all."""
    prompt = search.build_prompt("nfc issue", [("claude", "/b", "hi", "hi there")])
    assert "about that topic itself" in prompt
    assert "Sharing a word, a tool, or a domain" in prompt
    assert "when you are unsure, leave it out" in prompt


def test_build_prompt_requires_concrete_evidence():
    """A real search listed `Hello` sessions from an `android-cts` directory:
    the only thing linking them to the topic was where they ran."""
    prompt = search.build_prompt("nfc issue", [("claude", "/b", "hi", "hi there")])
    assert "something concrete in it" in prompt
    assert "merely ran in a related directory" in prompt


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


def test_parse_numbered_reasons_reads_reason_lines():
    assert search.parse_numbered_reasons("2: upgrades the IDC firmware\n7: A13 OTA notes\n", 10) == {
        2: "upgrades the IDC firmware",
        7: "A13 OTA notes",
    }


def test_parse_numbered_reasons_ignores_digits_inside_a_reason():
    """A plain number scan over a reply this rich would read the "13" out of
    "from A13" and turn a justification into a phantom match."""
    assert search.parse_numbered_reasons("7: upgrades it from A13 to A15\n", 100) == {
        7: "upgrades it from A13 to A15",
    }


def test_parse_numbered_reasons_falls_back_to_bare_numbers():
    """Some judges answer with a plain list anyway, despite the instruction;
    the match still counts, just without a reason to show."""
    assert search.parse_numbered_reasons("1, 4\n", 10) == {1: "", 4: ""}
    assert search.parse_numbered_reasons("none", 10) == {}


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
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: fake_candidates)
    monkeypatch.setattr(sessions, "claude_snippet", lambda path: "irrelevant chat")
    monkeypatch.setattr(sessions, "kimi_snippet", lambda d: "irrelevant chat")

    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:6], r.get("cwd") or "?", r.get("title", "(no title)"),
    ))

    rendered = []
    monkeypatch.setattr(sessions, "render_rows", lambda rows, **kw: rendered.append(rows))

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


def test_cmd_search_shows_judge_reasons_only_when_asked(monkeypatch, capsys):
    """The reason doubles the height of the listing and most rows are clear
    from the title, so it is behind `--why`."""
    fake_candidates = [{"tool": "codex", "id": "id-1", "ts": 1, "title": "Rebuild the firmware"}]
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: fake_candidates)
    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:6], "?", r["title"],
    ))
    monkeypatch.setattr(search, "snippet_for", lambda r: "")
    shown = []
    monkeypatch.setattr(sessions, "render_rows", lambda rows, **kw: shown.append(kw.get("notes")))
    monkeypatch.setattr(
        search, "run_judge_with_fallback",
        lambda prompt, n, judge, explicit, label: ({1: "upgrades it from A13"}, judge),
    )

    search.cmd_search(["firmware"])
    assert shown[0] is None  # no reason lines by default

    search.cmd_search(["--why", "firmware"])
    assert shown[1] == {("codex", "id-1"): "upgrades it from A13"}


def test_cmd_search_prints_the_matches_in_the_judges_own_order(monkeypatch):
    """The judge is asked to list its strongest hit first. Without carrying
    that order through, hits print most-recent-first, which buries the
    session that is actually about the topic under ones that only mention it
    in passing."""
    fake_candidates = [
        {"tool": "claude", "id": "id-new", "ts": 30, "title": "hello"},
        {"tool": "codex", "id": "id-old", "ts": 10, "title": "upgrade the firmware"},
    ]
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: fake_candidates)
    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:6], "/work/" + r["id"], r["title"],
    ))
    monkeypatch.setattr(search, "snippet_for", lambda r: "")
    monkeypatch.setattr(sessions, "literal_matches", lambda candidates, topic: [])
    shown = []
    monkeypatch.setattr(sessions, "render_rows", lambda rows, **kw: shown.append(rows))
    # Older session listed first: the judge ranked it the stronger match.
    monkeypatch.setattr(
        search, "run_judge_with_fallback",
        lambda prompt, n, judge, explicit, label: ({2: "is about this firmware", 1: "mentions it once"}, judge),
    )

    search.cmd_search(["IDC_Series android13 firmware"])

    assert [row[1] for row in shown[0]] == ["id-old", "id-new"]


def test_cmd_search_shows_only_the_top_of_a_long_hit_list(monkeypatch, capsys):
    """Hits are ranked, so the tail is the weakest of them -- listing all of
    them is what makes a broad search hard to scan."""
    fake_candidates = [
        {"tool": "claude", "id": f"id-{n}", "ts": n, "title": f"t{n}"}
        for n in range(search.SEMANTIC_ROWS_SHOWN + 3)
    ]
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: fake_candidates)
    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:6], "/work", r["title"],
    ))
    monkeypatch.setattr(search, "snippet_for", lambda r: "")
    monkeypatch.setattr(sessions, "literal_matches", lambda candidates, topic: [])
    shown = []
    monkeypatch.setattr(sessions, "render_rows", lambda rows, **kw: shown.append(rows))
    all_numbers = {n: "reason" for n in range(1, len(fake_candidates) + 1)}
    monkeypatch.setattr(
        search, "run_judge_with_fallback",
        lambda prompt, n, judge, explicit, label: (all_numbers, judge),
    )

    search.cmd_search(["firmware"])
    assert len(shown[0]) == search.SEMANTIC_ROWS_SHOWN
    assert "and 3 weaker matches not shown" in capsys.readouterr().out

    search.cmd_search(["--all", "firmware"])
    assert len(shown[1]) == len(fake_candidates)


def test_cmd_search_omits_sessions_a_tool_started_for_itself(monkeypatch, capsys):
    """A judge run's session contains every candidate's text, so it matches
    nearly any topic; a codex approval review contains the transcript it
    reviewed. Both are byproducts -- they shouldn't be searched at all."""
    fake_candidates = [
        {"tool": "codex", "id": "id-judge", "ts": 3, "title": "ai search judge"},
        {"tool": "codex", "id": "id-approval", "ts": 2, "title": "codex approval review"},
        {"tool": "codex", "id": "id-real", "ts": 1, "title": "Find isnfcon"},
    ]
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: fake_candidates)
    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:6], "?", r["title"],
    ))
    rendered = []
    monkeypatch.setattr(sessions, "render_rows", lambda rows, **kw: rendered.append(rows))

    prompts = []

    def fake_judge(prompt, n, judge, explicit, label):
        prompts.append(prompt)
        return {1: "mentions nfc"}, judge

    monkeypatch.setattr(search, "run_judge_with_fallback", fake_judge)

    search.cmd_search(["nfc"])

    err = capsys.readouterr().err
    assert "Scanning 1 sessions" in err
    assert "ai search judge" not in prompts[0]
    assert "approval review" not in prompts[0]
    assert "Find isnfcon" in prompts[0]
    assert [row[1] for row in rendered[0]] == ["id-real"]


def test_cmd_search_kimi_still_uses_argv(monkeypatch):
    """kimi -p requires an argument and does not read stdin, so it must keep
    receiving the prompt as the last argv element."""
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: [
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
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: [
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
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: [
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


def test_cmd_search_fallback_on_claude_auth_failure(monkeypatch, capsys):
    """Regression test: an expired Claude OAuth token failed all 6 judge
    batches with a 401 and never tried codex/kimi, because only a session
    limit or a timeout triggered the fallback. It makes claude just as
    unusable for the run, so it should fall through the same way."""
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: [
        {"tool": "codex", "id": "id-1", "ts": 1, "title": "x"},
    ])
    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:6], "?", r.get("title", "(no title)"),
    ))
    monkeypatch.setattr(sessions, "render_rows", lambda rows, **kw: None)

    calls = []

    class ClaudeExpired:
        returncode = 1
        stdout = "Failed to authenticate. API Error: 401 OAuth access token has expired. Re-authenticate to continue."
        stderr = ""

    class CodexOK:
        returncode = 0
        stdout = "none"
        stderr = ""

    monkeypatch.setattr(search.subprocess, "run", lambda *a, **kw: calls.append((a, kw)) or (CodexOK if len(calls) > 1 else ClaudeExpired)())

    search.cmd_search(["topic"])

    assert len(calls) == 2
    assert calls[0][0][0][:2] == ["claude", "-p"]
    assert calls[1][0][0][:2] == ["codex", "exec"]
    assert "falling back" in capsys.readouterr().err


def test_cmd_search_explicit_claude_auth_failure_shows_login_hint(monkeypatch, capsys):
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: [
        {"tool": "codex", "id": "id-1", "ts": 1, "title": "x"},
    ])
    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:6], "?", r.get("title", "(no title)"),
    ))

    class FakeResult:
        returncode = 1
        stdout = "Failed to authenticate. API Error: 401 OAuth access token has expired. Re-authenticate to continue."
        stderr = ""

    monkeypatch.setattr(search.subprocess, "run", lambda *a, **kw: FakeResult())

    with pytest.raises(SystemExit) as exc_info:
        search.cmd_search(["--judge", "claude", "topic"])
    assert exc_info.value.code == 1
    err = capsys.readouterr().err
    assert "login has expired" in err
    assert "--judge codex" in err


def test_cmd_search_fallback_on_default_judge_timeout(monkeypatch, capsys):
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: [
        {"tool": "codex", "id": "id-1", "ts": 1, "title": "x"},
    ])
    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:6], "?", r["title"],
    ))

    calls = []

    class CodexOK:
        returncode = 0
        stdout = "none"
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "claude":
            raise search.subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return CodexOK()

    monkeypatch.setattr(search.subprocess, "run", fake_run)

    search.cmd_search(["topic"])

    assert [call[0] for call in calls] == ["claude", "codex"]
    assert "timed out; falling back" in capsys.readouterr().err


def test_cmd_search_explicit_claude_session_limit_shows_hint(monkeypatch, capsys):
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: [
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
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: [
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
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: [])

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
    monkeypatch.setattr(sessions, "render_rows", lambda rows, **kw: rendered.append(rows))
    return rendered


def test_cmd_search_splits_into_chunks_of_chunk_size(monkeypatch, capsys):
    """Regression test: a single 493-candidate batch demonstrably missed a
    real match (confirmed by checking the missed candidate's snippet,
    which contained the search term just as clearly as the one that *was*
    found) -- a 'lost in a long list' recall failure. Candidates must be
    split into CHUNK_SIZE-sized batches, each judged independently."""
    n = search.CHUNK_SIZE * 2 + 30  # 3 chunks: 100, 100, 30
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: _fake_candidates(n))
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
    err = capsys.readouterr().err
    assert "Finished batch 1/3: 0 matches" in err
    assert "Finished batch 2/3: 0 matches" in err
    assert "Finished batch 3/3: 0 matches" in err


def test_first_batch_selects_judge_for_remaining_batches(monkeypatch):
    n = search.CHUNK_SIZE * 2 + 30
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: _fake_candidates(n))
    _stub_resolve_and_render(monkeypatch)

    calls = []

    class ClaudeLimit:
        returncode = 1
        stdout = "You've hit your session limit"
        stderr = ""

    class CodexOK:
        returncode = 0
        stdout = "none"
        stderr = ""

    def fake_run(argv, **kwargs):
        calls.append(argv[0])
        return ClaudeLimit() if argv[0] == "claude" else CodexOK()

    monkeypatch.setattr(search.subprocess, "run", fake_run)

    search.cmd_search(["topic"])

    # Claude is probed only by the first batch. Once codex succeeds, both
    # remaining batches start directly with codex.
    assert calls.count("claude") == 1
    assert calls.count("codex") == 3


def test_cmd_search_unions_matches_across_chunks(monkeypatch):
    """Each chunk is numbered locally (1..len(chunk)); matches from later
    chunks must map back to the correct global candidate, not collide with
    chunk 1's numbering."""
    n = search.CHUNK_SIZE + 5
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: _fake_candidates(n))
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
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: _fake_candidates(n))
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


def test_cmd_search_all_chunks_fail_exits_nonzero(monkeypatch, capsys):
    n = search.CHUNK_SIZE + 5
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: _fake_candidates(n))
    _stub_resolve_and_render(monkeypatch)

    class Fail:
        returncode = 1
        stdout = ""
        stderr = "boom"

    monkeypatch.setattr(search.subprocess, "run", lambda *a, **kw: Fail())

    with pytest.raises(SystemExit) as exc_info:
        search.cmd_search(["--judge", "claude", "topic"])
    assert exc_info.value.code != 0
    assert "all 2 batches failed" in capsys.readouterr().err


def test_cmd_search_single_small_batch_no_batch_label(monkeypatch, capsys):
    """With <= CHUNK_SIZE candidates there's only one chunk -- the status
    line shouldn't talk about "batch 1/1", matching the pre-chunking
    output format for the common case."""
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: _fake_candidates(3))
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


# ---------- literal pre-pass ----------

def _candidate_with_term(tmp_path, tool, sid, text):
    if tool == "claude":
        f = tmp_path / f"{sid}.jsonl"
        f.write_text(json.dumps({"type": "user", "message": {"content": text}}) + "\n")
        return {"tool": "claude", "id": sid, "ts": 1, "path": str(f), "cwd": "/x"}
    sdir = tmp_path / sid
    (sdir / "agents" / "main").mkdir(parents=True)
    (sdir / "agents" / "main" / "wire.jsonl").write_text(
        json.dumps({"type": "turn.prompt", "input": [{"type": "text", "text": text}]}) + "\n"
    )
    return {"tool": "kimi", "id": sid, "ts": 1, "dir": str(sdir), "cwd": "/x"}


def _capture_output_sections(monkeypatch):
    rendered = []
    monkeypatch.setattr(
        sessions, "render_rows",
        lambda rows, write_cache=True, start=1, notes=None: rendered.append(
            (list(rows), write_cache, start)
        ),
    )
    cached = []
    monkeypatch.setattr(sessions, "write_list_cache", lambda entries: cached.append(list(entries)))
    return rendered, cached


class _NoneResult:
    returncode = 0
    stdout = "none"
    stderr = ""


def test_cmd_search_shows_exact_matches_in_own_section(monkeypatch, capsys, tmp_path):
    exact = _candidate_with_term(tmp_path, "claude", "hit-1", "grabbed it via ARIA2C")
    other = {"tool": "codex", "id": "miss-1", "ts": 2, "title": "unrelated"}
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: [exact, other])
    monkeypatch.setattr(sessions, "codex_rollout_path", lambda sid: None)
    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:6], r.get("cwd", "?"), r.get("title", "(no title)"),
    ))
    monkeypatch.setattr(search, "snippet_for", lambda r: "")
    rendered, cached = _capture_output_sections(monkeypatch)
    monkeypatch.setattr(search.subprocess, "run", lambda *a, **kw: _NoneResult())

    search.cmd_search(["aria2c"])

    out = capsys.readouterr().out
    assert "exact matches for 'aria2c' (literal, case-insensitive):" in out
    assert "semantic matches" not in out  # judge picked nothing
    assert len(rendered) == 1
    assert [row[1] for row in rendered[0][0]] == ["hit-1"]
    assert rendered[0][1] is False  # cache written once, by cmd_search
    assert [[e["id"] for e in entries] for entries in cached] == [["hit-1"]]


def test_cmd_search_dedupes_judge_pick_already_exact(monkeypatch, capsys, tmp_path):
    exact = _candidate_with_term(tmp_path, "kimi", "hit-1", "aria2c -x8")
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: [exact])
    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:6], "?", r.get("title", "(no title)"),
    ))
    monkeypatch.setattr(search, "snippet_for", lambda r: "")
    rendered, cached = _capture_output_sections(monkeypatch)

    class PicksIt(_NoneResult):
        stdout = "1"  # judge also picks the (only) candidate

    monkeypatch.setattr(search.subprocess, "run", lambda *a, **kw: PicksIt())

    search.cmd_search(["aria2c"])

    out = capsys.readouterr().out
    assert "exact matches" in out
    assert "semantic matches" not in out  # no double-listing
    assert len(rendered) == 1
    assert [[e["id"] for e in entries] for entries in cached] == [["hit-1"]]


def test_cmd_search_shows_both_sections_and_unioned_cache(monkeypatch, capsys, tmp_path):
    exact = _candidate_with_term(tmp_path, "claude", "hit-1", "aria2c here")
    semantic_only = {"tool": "codex", "id": "sem-1", "ts": 2, "title": "talks about resumable downloads"}
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: [exact, semantic_only])
    monkeypatch.setattr(sessions, "codex_rollout_path", lambda sid: None)
    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:6], "?", r.get("title", "(no title)"),
    ))
    monkeypatch.setattr(search, "snippet_for", lambda r: "")
    rendered, cached = _capture_output_sections(monkeypatch)

    class PicksSecond(_NoneResult):
        stdout = "2"  # judge picks candidate #2 (no literal hit)

    monkeypatch.setattr(search.subprocess, "run", lambda *a, **kw: PicksSecond())

    search.cmd_search(["aria2c"])

    out = capsys.readouterr().out
    assert "exact matches for 'aria2c'" in out
    assert "semantic matches (judge: claude):" in out
    assert [row[1] for row in rendered[0][0]] == ["hit-1"]
    assert [row[1] for row in rendered[1][0]] == ["sem-1"]
    assert rendered[0][2] == 1
    assert rendered[1][2] == 2
    # one cache write covering both sections in printed order
    assert [[e["id"] for e in entries] for entries in cached] == [["hit-1", "sem-1"]]


def test_cmd_search_excludes_review_sessions_with_copied_history(monkeypatch, capsys, tmp_path):
    review = _candidate_with_term(tmp_path, "claude", "review", "CRA in copied transcript")
    # What resolve_row actually returns for one of these once resolved --
    # see codex_rollout_title's CODEX_APPROVAL_PROMPT_PREFIX handling.
    review["title"] = "codex approval review"
    genuine = _candidate_with_term(tmp_path, "claude", "genuine", "CRA work")
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: [review, genuine])
    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:6], "?", r.get("title", "actual work"),
    ))
    monkeypatch.setattr(search, "snippet_for", lambda r: "")
    rendered, cached = _capture_output_sections(monkeypatch)
    monkeypatch.setattr(search.subprocess, "run", lambda *a, **kw: _NoneResult())

    search.cmd_search(["cra"])

    assert [row[1] for row in rendered[0][0]] == ["genuine"]
    assert [[e["id"] for e in entries] for entries in cached] == [["genuine"]]
    assert "Scanning 1 sessions" in capsys.readouterr().err


def test_cmd_search_judge_failure_still_shows_exact(monkeypatch, capsys, tmp_path):
    exact = _candidate_with_term(tmp_path, "claude", "hit-1", "aria2c")
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: [exact])
    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:6], "?", r.get("title", "(no title)"),
    ))
    monkeypatch.setattr(search, "snippet_for", lambda r: "")
    rendered, _cached = _capture_output_sections(monkeypatch)

    def judge_fails(prompt, n, judge, judge_explicit, label):
        raise search.JudgeError(1)

    monkeypatch.setattr(search, "run_judge_with_fallback", judge_fails)

    search.cmd_search(["--judge", "claude", "aria2c"])  # must not sys.exit

    out = capsys.readouterr().out
    assert "exact matches for 'aria2c'" in out
    assert "semantic matches" not in out
    assert [row[1] for row in rendered[0][0]] == ["hit-1"]


def test_cmd_search_no_matches_at_all(monkeypatch, capsys):
    monkeypatch.setattr(search, "gather_candidates", lambda tool_filter: [
        {"tool": "codex", "id": "id-1", "ts": 1, "title": "x"},
    ])
    monkeypatch.setattr(sessions, "codex_rollout_path", lambda sid: None)
    monkeypatch.setattr(sessions, "resolve_row", lambda r: (
        r["tool"], r["id"], "1h ago", r["id"][:6], "?", r["title"],
    ))
    monkeypatch.setattr(search, "snippet_for", lambda r: "")
    monkeypatch.setattr(search.subprocess, "run", lambda *a, **kw: _NoneResult())

    search.cmd_search(["aria2c"])

    assert "No relevant sessions found." in capsys.readouterr().out
