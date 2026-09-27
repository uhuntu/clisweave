"""step (the StepCode CLI), woven in like the other three.

step stores one JSONL per session under ~/.stepcode/agent/sessions, grouped by
the encoded cwd -- claude's layout -- but with its own record shape: a
`session` header carrying the authoritative id and cwd, then `message` records
whose role is user/assistant/toolResult and whose content is text, thinking,
image or toolCall blocks. These tests build a synthetic store of exactly that
shape and hold the readers to it.
"""

import json
import os

import pytest

from clisweave import sessions

SID = "01a0e0e1-2965-79d4-aba9-3dd3bfc0f7cd"


@pytest.fixture(autouse=True)
def _isolate_step_store(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "STEP_SESSIONS", str(tmp_path / "no-step"))
    yield


def step_store(tmp_path, cwd_dir, files):
    """Write `<store>/<cwd_dir>/<file>` for each (file, lines) pair."""
    store = tmp_path / "sessions"
    for name, lines in files:
        d = store / cwd_dir
        d.mkdir(parents=True, exist_ok=True)
        path = d / name
        path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    # a direct assignment, not a monkeypatch: the autouse fixture resets
    # STEP_SESSIONS for every test, so nothing leaks between them
    sessions.STEP_SESSIONS = str(store)
    return store


def header(sid=SID, cwd="C:/work/clisweave", timestamp="2026-09-27T03:20:47.723Z", name=None):
    header = {"type": "session", "version": 3, "id": sid,
              "timestamp": timestamp, "cwd": cwd}
    if name is not None:
        header["name"] = name
    return header


def user(text):
    return {"type": "message", "id": "a1", "parentId": None, "timestamp": "2026-09-27T03:21:05.630Z",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]}}


def assistant(*blocks):
    return {"type": "message", "id": "a2", "parentId": "a1", "timestamp": "2026-09-27T03:21:14.231Z",
            "message": {"role": "assistant",
                        "content": [{"type": b[0], **b[1]} for b in blocks]}}


def tool_call(name, **arguments):
    return assistant(("toolCall", {"id": "call-1", "name": name, "arguments": arguments}))


# ---------- discovery ----------

def test_reads_the_store_the_env_var_points_at(monkeypatch, tmp_path):
    """STEP_CODING_AGENT_SESSION_DIR overrides the default location, the same
    way CODEX_HOME is a module constant the tests replace."""
    monkeypatch.setenv("STEP_CODING_AGENT_SESSION_DIR", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(sessions, "HOME", str(tmp_path))
    monkeypatch.setattr(sessions, "os", sessions.os)  # no-op, keeps the linter quiet
    import importlib
    importlib.reload(sessions)
    try:
        assert sessions.STEP_SESSIONS == str(tmp_path / "elsewhere")
    finally:
        importlib.reload(sessions)
        sessions._codex_path_index = None


def test_light_records_read_id_cwd_and_timestamp_from_the_header(tmp_path):
    store = step_store(tmp_path, "--C--Users-huntl-work-clisweave--", [
        ("2026-09-27T03-20-47-723Z_%s.jsonl" % SID,
         [header(), user("fix the nfc lock")]),
    ])
    records = sessions.step_light_records()
    assert len(records) == 1
    r = records[0]
    assert r["tool"] == "step"
    assert r["id"] == SID
    assert r["cwd"] == "C:/work/clisweave"
    assert r["path"] == os.path.join(str(store), "--C--Users-huntl-work-clisweave--",
                                     "2026-09-27T03-20-47-723Z_%s.jsonl" % SID)
    assert r["started"] > 0


def test_cwd_falls_back_to_the_encoded_directory_name(tmp_path):
    """A session file whose header never got flushed still has the directory
    it lives in -- the same encoding claude uses."""
    step_store(tmp_path, "--C--Users-huntl-work-clisweave--", [
        ("2026-09-27T03-20-47-723Z_%s.jsonl" % SID,
         [{"type": "message", "message": {"role": "user", "content": "hi"}}]),
    ])
    assert sessions.step_light_records()[0]["cwd"] == "C:/Users/huntl/work/clisweave"


def test_newest_write_wins_per_id(tmp_path):
    """One session id, two files: only the newest is kept, the way claude
    handles a session that touched more than one cwd."""
    older = "2026-09-26T03-20-47-723Z_%s.jsonl" % SID
    newer = "2026-09-27T03-20-47-723Z_%s.jsonl" % SID
    store = step_store(tmp_path, "--C--Users-huntl--", [
        (older, [header(), user("the older copy")]),
        (newer, [header(), user("the newer copy")]),
    ])
    os.utime(os.path.join(str(store), "--C--Users-huntl--", older), (1000, 1000))
    os.utime(os.path.join(str(store), "--C--Users-huntl--", newer), (2000, 2000))

    records = sessions.step_light_records()
    assert len(records) == 1
    assert records[0]["path"].endswith(newer)


def test_a_session_without_an_id_is_named_by_its_file(tmp_path):
    step_store(tmp_path, "--C--Users-huntl--", [
        ("2026-09-27T03-20-47-723Z_deadbeef.jsonl", [{"type": "message", "message": {"role": "user", "content": "hi"}}]),
    ])
    assert sessions.step_light_records()[0]["id"] == "2026-09-27T03-20-47-723Z_deadbeef"


def test_subagent_sessions_are_left_out_unless_all(tmp_path):
    """step's own subagents write sessions holding another session's context:
    a session the tool started for itself, not a conversation of yours."""
    step_store(tmp_path, "--C--Users-huntl-work-clisweave--", [
        ("2026-09-27T03-20-47-723Z_%s.jsonl" % SID, [header(), user("my own session")]),
        ("2026-09-27T03-26-16-016Z_subagent-4a217d06-ad51-4f33-beb4-09ff1471661a.jsonl",
         [header("subagent-4a217d06-ad51-4f33-beb4-09ff1471661a"), user("someone else's context")]),
    ])
    assert [r["id"] for r in sessions.step_light_records()] == [SID]
    assert len(sessions.step_light_records(show_all=True)) == 2


def test_an_absent_store_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(sessions, "STEP_SESSIONS", str(tmp_path / "no-step"))
    assert sessions.step_light_records() == []
    assert sessions.step_resolve("anything") == []
    assert sessions.step_session_cwd("anything") is None


def test_resolve_matches_by_prefix(monkeypatch, tmp_path):
    step_store(tmp_path, "--C--Users-huntl--", [("f_%s.jsonl" % SID, [header()])])
    assert sessions.step_resolve("01a0e0e1") == [SID]
    assert sessions.step_resolve("01a0e0e1-2965-79d4") == [SID]
    assert sessions.step_resolve("nope") == []


# ---------- titles ----------

def test_title_is_the_first_genuine_user_prompt(tmp_path):
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [header(), user("please fix the login crash"),
                              assistant(("text", {"text": "done"}))])])
    assert sessions.step_title(
        sessions.step_light_records()[0]["path"]) == "please fix the login crash"


def test_title_skips_a_bare_acknowledgement(tmp_path):
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [header(), user("yes"), user("tune the engine")])])
    assert sessions.step_title(sessions.step_light_records()[0]["path"]) == "tune the engine"


def test_title_falls_back_to_the_first_prompt_when_every_one_is_noise(tmp_path):
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [header(), user("hi"), user("ok")])])
    assert sessions.step_title(sessions.step_light_records()[0]["path"]) == "hi"


def test_a_captionless_image_paste_names_nothing(tmp_path):
    """step marks a pasted image with a *text* block reading "[Image #1]",
    which is not claude's "[Image: source: ..." spelling."""
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [header(), user("[Image #1]"), user("what is on this screen?")])])
    assert sessions.step_title(sessions.step_light_records()[0]["path"]) == "what is on this screen?"


def test_a_session_of_only_an_image_is_untitled(tmp_path):
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [header(), user("[Image #1]")])])
    assert sessions.step_title(sessions.step_light_records()[0]["path"]) == "(no title)"


def test_title_ignores_thinking_and_tool_calls(tmp_path):
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [
            header(),
            assistant(("thinking", {"thinking": "let me plan the OTA rollback fix"})),
            tool_call("bash", command="unzip -t release.zip"),
            user("actually, package it up"),
        ])])
    assert sessions.step_title(sessions.step_light_records()[0]["path"]) == "actually, package it up"


def test_title_skips_a_pasted_shell_transcript(tmp_path):
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [header(), user("(hunt@host)-[~]\n$ ai update\nerror: boom"),
                              user("the update fails")])])
    assert sessions.step_title(sessions.step_light_records()[0]["path"]) == "the update fails"


def test_an_absent_transcript_is_untitled():
    assert sessions.step_title("/no/such/file.jsonl") == "(no title)"


# ---------- snippets ----------

def test_snippet_includes_assistant_text_and_thinking(tmp_path):
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [
            header(),
            user("look at the aria2c mirror"),
            assistant(("thinking", {"thinking": "the mirror is flaky"}),
                      ("text", {"text": "the mirror died again too"})),
        ])])
    snippet = sessions.step_snippet(sessions.step_light_records()[0]["path"])
    assert "aria2c mirror" in snippet
    assert "the mirror died again too" in snippet
    assert "the mirror is flaky" in snippet


def test_snippet_includes_a_tool_calls_command(tmp_path):
    """A tool call's arguments are often the only place a term appears
    anywhere in a session -- the reason codex's function_call inputs are
    scanned too."""
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [header(), user("grab it"), tool_call("bash", command="aria2c -x8 release.tgz")])])
    assert "aria2c" in sessions.step_snippet(sessions.step_light_records()[0]["path"])


def test_snippet_truncates_a_thinking_block(tmp_path):
    """One thinking block would otherwise take the whole budget."""
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [header(), user("go"), assistant(("thinking", {"thinking": "z" * 5000}))])])
    snippet = sessions.step_snippet(sessions.step_light_records()[0]["path"])
    assert len(snippet) < 900
    assert "z" * 5000 not in snippet


def test_snippet_stays_inside_its_char_budget(tmp_path):
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [header()] + [user("filler %d" % n) for n in range(200)])])
    assert len(sessions.step_snippet(sessions.step_light_records()[0]["path"])) <= 800


def test_snippet_ignores_records_that_are_not_messages(tmp_path):
    """step's task list, model switches and thinking-level changes carry no
    conversation text."""
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [
            header(),
            {"type": "model_change", "provider": "step", "modelId": "step-5-preview"},
            {"type": "custom", "customType": "step-tasks",
             "data": {"tasks": [{"id": "1", "subject": "aria2c the release"}]}},
            user("grab the release"),
        ])])
    snippet = sessions.step_snippet(sessions.step_light_records()[0]["path"])
    assert "grab the release" in snippet
    assert "step-tasks" not in snippet


# ---------- search ----------

def test_literal_matches_scans_a_step_transcript(tmp_path):
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [header(), user("tune the aria2c mirror"),
                              tool_call("bash", command="aria2c -x8 x.tgz")])])
    rec = sessions.step_light_records()[0]
    assert [r["id"] for r in sessions.literal_matches([rec], "aria2c")] == [SID]
    assert sessions.literal_matches([rec], "rsync") == []


def test_literal_matches_is_case_insensitive_for_step(tmp_path):
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [header(), user("using ARIA2C here")])])
    rec = sessions.step_light_records()[0]
    assert sessions.literal_matches([rec], "aria2c") == [rec]


def test_literal_matches_finds_a_term_only_in_a_tool_result(tmp_path):
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [
            header(), user("check the log"),
            {"type": "message", "message": {"role": "toolResult", "toolName": "bash",
                                            "content": [{"type": "text", "text": "aria2c: 12MB/s"}]}},
        ])])
    rec = sessions.step_light_records()[0]
    assert sessions.literal_matches([rec], "aria2c") == [rec]


def test_search_uses_a_step_snippet(monkeypatch, tmp_path):
    from clisweave import search
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [header(), user("the nfc frequency lock issue")])])
    rec = sessions.step_light_records()[0]
    assert "nfc frequency lock" in search.snippet_for(rec)


# ---------- integration with the listing ----------

def test_step_rows_appear_in_the_listing(monkeypatch, tmp_path, capsys):
    """The listing is the integration test: a step session has to show up
    with the other three tools' sessions, in recency order."""
    for attr in ("CLAUDE_PROJECTS", "CODEX_HOME", "KIMI_HOME"):
        monkeypatch.setattr(sessions, attr, str(tmp_path / ("no-" + attr.lower())))
    monkeypatch.setattr(sessions, "STEP_SESSIONS", str(tmp_path / "no-step-at-first"))
    proj = tmp_path / "projects" / "-home-hunt"
    proj.mkdir(parents=True)
    (proj / "c1.jsonl").write_text(json.dumps(
        {"type": "user", "cwd": "/home/hunt", "message": {"content": "the claude one"}}) + "\n")
    monkeypatch.setattr(sessions, "CLAUDE_PROJECTS", str(tmp_path / "projects"))
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [header(), user("the step one")])])

    sessions.cmd_list([])
    out = capsys.readouterr().out
    assert "step" in out and "the step one" in out
    assert "claude" in out and "the claude one" in out


# ---------- the session's own display name ----------
# step stores the name set with `step --name` or /name on the header record
# ({"type":"session",...,"name":"refactor auth flow"}), and repeats it as a
# session_info record when /name sets it after the fact. None of this
# machine's sessions carry one yet, so the shape here is the documented one.

def test_a_named_session_shows_its_name_when_the_log_has_no_prompt(tmp_path):
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [header(name="refactor auth flow")]),
    ])
    rec = sessions.step_light_records()[0]
    assert sessions.step_title_with_name(rec["path"]) == "refactor auth flow"


def test_a_named_session_still_shows_its_own_prompt(monkeypatch, tmp_path):
    """The display name is a fallback, not an override: what was actually
    asked beats a name typed at startup, the same rule kimi's stored name
    follows."""
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [header(name="refactor auth flow"), user("fix the login crash")]),
    ])
    rec = sessions.step_light_records()[0]
    assert sessions.step_title_with_name(rec["path"]) == "fix the login crash"


def test_a_blank_name_is_not_a_title(tmp_path):
    """A name set to whitespace or a placeholder names nothing; the row would
    rather fall through to its own first message."""
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [header(name="   "), user("go on then")]),
    ])
    rec = sessions.step_light_records()[0]
    assert sessions.step_title_with_name(rec["path"]) == "go on then"


def test_a_name_set_by_slash_name_is_found_in_session_info(tmp_path):
    """`/name` after the fact arrives as a session_info record rather than a
    header field, so both spellings are read."""
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [
            header(), user("[Image #1]"),
            {"type": "session_info", "id": "si1", "parentId": None,
             "timestamp": "2026-09-27T03:30:00.000Z",
             "name": "reproduce the MiMo login screen"},
        ]),
    ])
    rec = sessions.step_light_records()[0]
    assert sessions.step_title_with_name(rec["path"]) == "reproduce the MiMo login screen"


def test_a_name_with_newlines_is_flattened(tmp_path):
    """Titles are printed straight into a table column."""
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [header(name="refactor" + chr(10) + "auth   flow")]),
    ])
    rec = sessions.step_light_records()[0]
    assert sessions.step_title_with_name(rec["path"]) == "refactor auth flow"


def test_handoff_exports_the_conversation_and_starts_step(monkeypatch, tmp_path):
    """`ai <N> step` exports the source transcript and starts step with a bare
    prompt -- step takes one, so it needs none of kimi's seeding dance."""
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [
            header(),
            user("fix the nfc lock"),
            assistant(("text", {"text": "patched the HAL"}),
                      ("thinking", {"thinking": "the lock was stale"})),
            tool_call("bash", command="git commit"),
            {"type": "message", "message": {"role": "toolResult", "toolName": "bash",
                                            "content": [{"type": "text", "text": "1 file changed"}]}},
        ]),
    ])
    monkeypatch.setattr(sessions, "HANDOFF_DIR", str(tmp_path / "handoffs"))
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "last_list.json"))
    sessions.write_list_cache([{"tool": "step", "id": SID}])
    calls = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: calls.append(argv))

    sessions.handoff_by_number(1, "step", [])

    assert calls and calls[-1][0] == "step"
    assert calls[-1][1].startswith("Continue the work from this step session")
    export = calls[-1][1].split("export at ")[1].split(". First")[0]
    body = open(export, encoding="utf-8").read()
    assert "fix the nfc lock" in body and "patched the HAL" in body
    # the export is what was said, not the tool's output or the model's notes
    assert "1 file changed" not in body
    assert "the lock was stale" not in body


def test_resume_passes_the_id_to_step_not_to_the_previous_tool(monkeypatch, tmp_path):
    """The resume argv is built by a per-tool chain whose last branch was
    kimi's; a tool appended after it silently resumed with the wrong CLI."""
    step_store(tmp_path, "--C--Users-huntl--", [("f_%s.jsonl" % SID, [header()])])
    calls = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: calls.append(argv))

    sessions.cmd_resume(["step", SID])

    assert calls == [["step", "--resume", SID]]


def test_resume_switches_to_the_sessions_directory(monkeypatch, tmp_path):
    """step, like the other three, ties a session to the directory it ran in,
    and `ai resume` knows that directory already."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(tmp_path)  # somewhere that is *not* the session's home
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [header(cwd=str(home))])])
    switched = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: None)
    monkeypatch.setattr(sessions.os, "chdir", lambda path: switched.append(path))

    sessions.cmd_resume(["step", SID])

    assert switched == [str(home)]


def test_search_gathers_step_candidates(monkeypatch, tmp_path):
    """A listing is not the only consumer of the light records: `ai search`
    builds its candidate list from the same three-way gather, and a tool that
    is only wired into the listing is silently unsearchable."""
    from clisweave import search
    for attr in ("CLAUDE_PROJECTS", "CODEX_HOME", "KIMI_HOME"):
        monkeypatch.setattr(sessions, attr, str(tmp_path / ("no-" + attr.lower())))
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [header(), user("the step candidate")])])

    assert [r["id"] for r in search.gather_candidates("step")] == [SID]
    assert [r["id"] for r in search.gather_candidates(None)] == [SID]


def test_search_excludes_step_subagents_too(monkeypatch, tmp_path):
    from clisweave import search
    for attr in ("CLAUDE_PROJECTS", "CODEX_HOME", "KIMI_HOME"):
        monkeypatch.setattr(sessions, attr, str(tmp_path / ("no-" + attr.lower())))
    step_store(tmp_path, "--C--Users-huntl--", [
        ("f_%s.jsonl" % SID, [header(), user("mine")]),
        ("2026-09-27T03-26-16-016Z_subagent-4a217d06-ad51-4f33-beb4-09ff1471661a.jsonl",
         [header("subagent-4a217d06-ad51-4f33-beb4-09ff1471661a"), user("theirs")]),
    ])
    assert [r["id"] for r in search.gather_candidates("step")] == [SID]
    assert len(search.gather_candidates("step", show_all=True)) == 2


def test_stats_counts_step_sessions(monkeypatch, tmp_path, capsys):
    step_store(tmp_path, "--C--Users-huntl-work-clisweave--", [
        ("f_a.jsonl", [header("sess-a"), user("one")]),
        ("f_b.jsonl", [header("sess-b"), user("two")]),
    ])
    for attr, path in (("CLAUDE_PROJECTS", "no-claude"), ("CODEX_HOME", "no-codex"),
                       ("KIMI_HOME", "no-kimi")):
        monkeypatch.setattr(sessions, attr, str(tmp_path / path))
    sessions.cmd_stats([])
    out = capsys.readouterr().out
    assert "step" in out
    assert "(2)" in out
