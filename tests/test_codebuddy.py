"""codebuddy, the seventh store: the CodeBuddy CLI client, now a full cw
citizen.

Its layout is claude's (~/.codebuddy/projects/<slug>/<sessionId>.jsonl) but
its record chain is codex-rs's: a `message` record carries a top-level role
and typed content blocks (input_text/output_text), a call and its result
are separate top-level `function_call`/`function_call_result` records, the
thinking is a sibling `reasoning` record, and the title is a stored
`ai-title` the CLI's own auto-titler writes into the file. There is no
header record -- the id and cwd ride on every substantive record -- and a
resume appends to the same file. These tests build a synthetic store of
exactly that shape and hold the readers to it.
"""

import json
import os

import pytest

from clisweave import cli, codebuddy, common, search, sessions, step, zcode

SID = "01a0f6ee-6ad1-7f29-83cd-8524990d2131"
SID2 = "01a0c1e7-8f47-7a41-9734-48c0c8f91fe0"
CWD = "/home/hunt/work/stave"
T0 = 1_790_849_237_759  # epoch ms, the store's unit


@pytest.fixture(autouse=True)
def _isolate_codebuddy_store(monkeypatch, tmp_path):
    monkeypatch.setattr(codebuddy, "CODEBUDDY_PROJECTS", str(tmp_path / "no-codebuddy"))
    yield


def msg(role, text, ts=T0, cwd=CWD, sid=SID):
    return {"id": "m-%d" % ts, "timestamp": ts, "type": "message", "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text",
                         "text": text}],
            "sessionId": sid, "cwd": cwd}


def ai_title(text, ts=T0 + 1_000, cwd=CWD, sid=SID):
    return {"id": "t-%d" % ts, "timestamp": ts, "type": "ai-title", "aiTitle": text,
            "sessionId": sid, "cwd": cwd}


def custom_title(text, ts=T0 + 2_000):
    # /rename's record shape is unverified (no sample on disk); the reader
    # accepts either spelling defensively, and the test pins both.
    return {"id": "ct-%d" % ts, "timestamp": ts, "type": "custom-title",
            "customTitle": text, "sessionId": SID}


def fn_call(name, arguments, ts=T0 + 3_000, cwd=CWD, sid=SID):
    return {"id": "fc-%d" % ts, "timestamp": ts, "type": "function_call",
            "name": name, "arguments": json.dumps(arguments),
            "callId": "call-%d" % ts, "sessionId": sid, "cwd": cwd}


def fn_result(text, ts=T0 + 4_000, cwd=CWD, sid=SID):
    return {"id": "fcr-%d" % ts, "timestamp": ts, "type": "function_call_result",
            "name": "Bash", "callId": "call-%d" % (ts - 1_000), "status": "completed",
            "output": {"type": "text", "text": text}, "sessionId": sid, "cwd": cwd}


def store(tmp_path, slug, name, lines):
    """Write `projects/<slug>/<name>` and point the store constant at it --
    a direct assignment, not a monkeypatch: the autouse fixture resets
    CODEBUDDY_PROJECTS for every test, so nothing leaks between them."""
    pdir = tmp_path / "projects" / slug
    pdir.mkdir(parents=True, exist_ok=True)
    path = pdir / name
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    codebuddy.CODEBUDDY_PROJECTS = str(tmp_path / "projects")
    return path


def one_session(tmp_path, sid=SID, cwd=CWD, lines=()):
    """A one-session store opening with a genuine user prompt, then
    `lines`."""
    return store(tmp_path, "home-hunt-work-stave", "%s.jsonl" % sid,
                 [msg("user", "fix the nfc lock", sid=sid, cwd=cwd), *lines])


def codebuddy_record(sid=SID):
    return next(r for r in codebuddy.codebuddy_light_records(show_all=True)
                if r["id"] == sid)


# ---------- discovery ----------

def test_light_records_read_id_cwd_title_and_span(tmp_path):
    one_session(tmp_path, lines=[msg("assistant", "patched the HAL", ts=T0 + 9_000)])
    records = codebuddy.codebuddy_light_records()
    assert len(records) == 1
    r = records[0]
    assert r["tool"] == "codebuddy"
    assert r["id"] == SID
    assert r["cwd"] == CWD
    assert r["title"] == "fix the nfc lock"
    assert r["ts"] == (T0 + 9_000) // 1000
    assert r["started"] == T0 // 1000
    assert r["turns"] == 1
    assert os.path.isfile(r["path"])


def test_the_id_falls_back_to_the_filename(tmp_path):
    """A file whose records carry no sessionId (a stub) is still a session,
    named by its file like cb's collector names it."""
    store(tmp_path, "home-hunt", "%s.jsonl" % SID2, [msg("user", "hi", sid=SID2)])
    # a record without any sessionId at all:
    path = tmp_path / "projects" / "home-hunt" / "bare.jsonl"
    path.write_text(json.dumps({"type": "message", "role": "user", "timestamp": T0,
                                "content": [{"type": "input_text", "text": "hi"}]}) + "\n",
                    encoding="utf-8")
    ids = sorted(r["id"] for r in codebuddy.codebuddy_light_records())
    assert ids == sorted([SID2, "bare"])


def test_a_missing_store_is_an_empty_listing_not_a_crash(tmp_path):
    assert codebuddy.codebuddy_light_records() == []
    assert codebuddy.codebuddy_resolve(SID[:8]) == []
    assert codebuddy.codebuddy_session_cwd(SID) is None
    assert codebuddy.codebuddy_handoff_messages("/nope.jsonl") == []
    assert codebuddy.codebuddy_snippet("/nope.jsonl") == ""


def test_corrupt_and_non_object_lines_are_skipped(tmp_path):
    pdir = tmp_path / "projects" / "home-hunt"
    pdir.mkdir(parents=True)
    (pdir / ("%s.jsonl" % SID)).write_text(
        "not json\n[1, 2]\n\n" + json.dumps(msg("user", "still readable")) + "\n",
        encoding="utf-8")
    codebuddy.CODEBUDDY_PROJECTS = str(tmp_path / "projects")
    records = codebuddy.codebuddy_light_records()
    assert len(records) == 1 and records[0]["title"] == "still readable"


def test_resolve_matches_by_prefix(tmp_path):
    store(tmp_path, "a", "%s.jsonl" % SID, [msg("user", "one", sid=SID)])
    store(tmp_path, "b", "%s.jsonl" % SID2, [msg("user", "two", sid=SID2)])
    assert codebuddy.codebuddy_resolve("01a0f6") == [SID]
    assert codebuddy.codebuddy_resolve(SID) == [SID]
    assert sorted(codebuddy.codebuddy_resolve("01a0")) == sorted([SID, SID2])
    assert codebuddy.codebuddy_resolve("zz") == []


# ---------- titles ----------

def test_the_ai_title_beats_the_first_prompt(tmp_path):
    one_session(tmp_path, lines=[ai_title("NFC lock investigation")])
    assert codebuddy.codebuddy_light_records()[0]["title"] == "NFC lock investigation"


def test_a_custom_title_beats_the_ai_title(tmp_path):
    one_session(tmp_path, lines=[ai_title("NFC lock investigation"),
                                 custom_title("the rename")])
    assert codebuddy.codebuddy_light_records()[0]["title"] == "the rename"


def test_injected_openers_do_not_become_the_title(tmp_path):
    """CodeBuddy records slash commands and system reminders as user
    messages; neither names the work."""
    lines = [
        msg("user", "<system-reminder data-role=\"command-caveat\">Caveat</system-reminder>"),
        msg("user", "<command-name>/model</command-name>"),
        msg("user", "<local-command-stdout>switched</local-command-stdout>"),
        msg("user", "wire up the SearXNG bridge"),
    ]
    store(tmp_path, "home-hunt", "%s.jsonl" % SID, lines)
    assert codebuddy.codebuddy_light_records()[0]["title"] == "wire up the SearXNG bridge"


def test_a_session_that_is_all_scaffolding_has_no_title_and_no_turns(tmp_path):
    store(tmp_path, "home-hunt", "%s.jsonl" % SID, [
        msg("user", "<command-name>/model</command-name>"),
        msg("user", "<local-command-stdout>switched</local-command-stdout>"),
    ])
    r = codebuddy.codebuddy_light_records()[0]
    assert r["title"] == "(no title)"
    assert r["turns"] == 0
    assert sessions.session_turns(r) is None


# ---------- turns, last message, snippet ----------

def test_turns_count_genuine_prompts_only(tmp_path):
    one_session(tmp_path, lines=[
        msg("assistant", "on it", ts=T0 + 1_000),
        msg("user", "<command-name>/model</command-name>", ts=T0 + 2_000),
        msg("user", "now check the CI logs", ts=T0 + 3_000),
    ])
    r = codebuddy_record()
    assert r["turns"] == 2
    assert sessions.session_turns(r) == 2


def test_the_last_message_is_the_last_thing_anyone_said(tmp_path):
    """Calls, results and injected reminders sit between the row and the
    answer; walking backwards past them is the normal case."""
    one_session(tmp_path, lines=[
        fn_call("Bash", {"command": "git commit -m fix"}),
        fn_result("1 file changed"),
        msg("user", "<local-command-stdout>noise</local-command-stdout>", ts=T0 + 5_000),
        msg("assistant", "patched the HAL", ts=T0 + 6_000),
    ])
    assert sessions.session_last_message(codebuddy_record()) == "patched the HAL"


def test_the_snippet_spans_speech_and_calls(tmp_path):
    """Outputs stay out of the snippet -- the literal scan reads those (the
    same line zcode and step hold): the snippet's budget goes to what was
    said and what was attempted."""
    one_session(tmp_path, lines=[
        fn_call("Bash", {"command": "cat JSENGINES.TXT"}),
        fn_result("4 of 4 records"),
    ])
    snippet = search.snippet_for(codebuddy_record())
    assert "fix the nfc lock" in snippet
    assert "cat JSENGINES.TXT" in snippet
    assert "4 of 4 records" not in snippet
    assert len(snippet) <= 800


# ---------- the literal scan ----------

def test_the_literal_scan_finds_said_called_and_returned(tmp_path):
    one_session(tmp_path, lines=[
        fn_call("Bash", {"command": "grep -rn B_NFC_LOCATION_27 ."}),
        fn_result("no matches anywhere"),
    ])
    rec = codebuddy_record()
    assert [r["id"] for r in sessions.literal_matches([rec], "B_NFC_LOCATION_27")] == [SID]
    assert [r["id"] for r in sessions.literal_matches([rec], "no matches anywhere")] == [SID]
    assert sessions.literal_matches([rec], "nothing like this") == []


def test_the_literal_scan_reads_the_tool_results_sidecars(tmp_path):
    """A big tool result lives outside the transcript, in
    <slug>/<sid>/tool-results/<callId>.txt -- a term only there was exactly
    the shape of miss that gave kimi its output.log scan."""
    path = one_session(tmp_path)
    sidecar = path.parent / SID / "tool-results"
    sidecar.mkdir(parents=True)
    (sidecar / "call-1.txt").write_text("the aria2c flag set is --max-conn=16",
                                        encoding="utf-8")
    rec = codebuddy_record()
    assert [r["id"] for r in sessions.literal_matches([rec], "aria2c")] == [SID]


# ---------- search candidates ----------

def test_codebuddy_joins_the_search_candidates(monkeypatch, tmp_path):
    for attr in ("CLAUDE_PROJECTS", "CODEX_HOME", "KIMI_HOME"):
        monkeypatch.setattr(common, attr, str(tmp_path / ("no-" + attr.lower())))
    monkeypatch.setattr(step, "STEP_SESSIONS", str(tmp_path / "no-step"))
    monkeypatch.setattr(zcode, "ZCODE_DB", str(tmp_path / "no-zcode.sqlite"))
    one_session(tmp_path)
    assert [r["id"] for r in search.gather_candidates("codebuddy")] == [SID]
    assert SID in [r["id"] for r in search.gather_candidates(None)]


# ---------- integration with the listing ----------

def test_codebuddy_rows_appear_in_the_listing(monkeypatch, tmp_path, capsys):
    for attr in ("CLAUDE_PROJECTS", "CODEX_HOME", "KIMI_HOME"):
        monkeypatch.setattr(common, attr, str(tmp_path / ("no-" + attr.lower())))
    monkeypatch.setattr(step, "STEP_SESSIONS", str(tmp_path / "no-step"))
    monkeypatch.setattr(zcode, "ZCODE_DB", str(tmp_path / "no-zcode.sqlite"))
    one_session(tmp_path, lines=[msg("assistant", "patched the HAL", ts=T0 + 9_000)])

    sessions.cmd_list(["--tool", "codebuddy"])
    out = capsys.readouterr().out
    assert "codebuddy" in out and "fix the nfc lock" in out
    assert "patched the HAL" in out  # the "where it left off" line

    sessions.cmd_list([])
    out = capsys.readouterr().out
    assert "fix the nfc lock" in out
    assert "step" not in out  # the other stores were empty, not merely quiet


def test_stats_counts_codebuddy_rows(monkeypatch, tmp_path, capsys):
    for attr in ("CLAUDE_PROJECTS", "CODEX_HOME", "KIMI_HOME"):
        monkeypatch.setattr(common, attr, str(tmp_path / ("no-" + attr.lower())))
    monkeypatch.setattr(step, "STEP_SESSIONS", str(tmp_path / "no-step"))
    monkeypatch.setattr(zcode, "ZCODE_DB", str(tmp_path / "no-zcode.sqlite"))
    store(tmp_path, "a", "%s.jsonl" % SID, [msg("user", "one", sid=SID)])
    store(tmp_path, "b", "%s.jsonl" % SID2, [msg("user", "two", sid=SID2)])

    sessions.cmd_stats(["--tool", "codebuddy"])
    out = capsys.readouterr().out
    assert "codebuddy" in out
    zline = next(line for line in out.splitlines() if line.strip().startswith("codebuddy"))
    assert "2" in zline


# ---------- handoff ----------

def test_handoff_exports_the_conversation_from_codebuddy(monkeypatch, tmp_path):
    """`cw <N> claude` on a codebuddy row exports what was said -- calls,
    results and thinking stay behind -- and seeds claude with it."""
    one_session(tmp_path, lines=[
        msg("assistant", "patched the HAL", ts=T0 + 1_000),
        fn_call("Bash", {"command": "git commit -m fix"}),
        fn_result("1 file changed"),
    ])
    monkeypatch.setattr(sessions, "HANDOFF_DIR", str(tmp_path / "handoffs"))
    monkeypatch.setattr(sessions, "LIST_CACHE_FILE", str(tmp_path / "last_list.json"))
    sessions.write_list_cache([{"tool": "codebuddy", "id": SID}])
    calls = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: calls.append(argv))

    sessions.handoff_by_number(1, "claude", [])

    assert calls and calls[-1][0] == "claude"
    assert calls[-1][1].startswith("Continue the work from this codebuddy session")
    export = calls[-1][1].split("export at ")[1].split(". First")[0]
    body = open(export, encoding="utf-8").read()
    assert "fix the nfc lock" in body and "patched the HAL" in body
    assert "1 file changed" not in body
    assert "git commit" not in body


def test_a_session_handed_to_codebuddy_is_named_after_its_source(monkeypatch, tmp_path):
    """codebuddy takes a bare positional prompt, so it is a real handoff
    target -- and a session whose seed went unrecognized would keep the
    generated label and dead-end the handoff chain at that hop (step's own
    lesson)."""
    seed = ("Continue the work from this claude session (019abc123456). "
            "Read the complete conversation export at /tmp/x.md. First "
            "briefly summarize the current objective, decisions, "
            "completed work, and unfinished work.")
    path = store(tmp_path, "home-hunt", "%s.jsonl" % SID, [
        msg("user", seed),
        msg("assistant", "Picking up the NFC lock work.", ts=T0 + 1_000),
    ])
    rec = codebuddy_record()

    assert sessions.handoff_source(path, "codebuddy") == ("claude", "019abc123456")

    # With the source session present in its own store, the row inherits
    # its topic rather than showing the seed's "ai handoff from claude".
    monkeypatch.setattr(sessions, "claude_light_records", lambda: [
        {"tool": "claude", "id": "019abc123456", "ts": 1,
         "path": str(tmp_path / "claude-src.jsonl")},
    ])
    (tmp_path / "claude-src.jsonl").write_text(json.dumps(
        {"type": "user", "cwd": CWD, "message": {"content": "fix the nfc lock"}}) + "\n",
        encoding="utf-8")

    assert sessions._resolve_title_and_cwd(rec)[0] == "(handoff) fix the nfc lock"

    # ...and only as the opening message: the same seed quoted later is
    # somebody discussing a handoff, not a session made by one.
    quoted = path.parent / "quoted.jsonl"
    quoted.write_text("".join(json.dumps(line) + "\n" for line in [
        msg("user", "what did that handoff say?", sid="01a0c1d2-8f47-7a41-9734-48c0c8f91fe0"),
        msg("assistant", "It said: " + seed, ts=T0 + 1_000,
            sid="01a0c1d2-8f47-7a41-9734-48c0c8f91fe0"),
    ]), encoding="utf-8")

    assert sessions.handoff_source(str(quoted), "codebuddy") is None


# ---------- resume ----------

def test_resume_passes_the_id_and_switches_to_the_session_directory(monkeypatch, tmp_path):
    workdir = tmp_path / "stave"
    workdir.mkdir()
    monkeypatch.chdir(tmp_path)  # NOT the session's directory: resume must switch
    one_session(tmp_path, cwd=str(workdir))
    calls = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: calls.append(argv))

    sessions.cmd_resume(["codebuddy", SID])

    assert calls == [["codebuddy", "-r", SID]]
    assert os.path.realpath(os.getcwd()) == os.path.realpath(workdir)


def test_resume_by_prefix_resolves_through_the_store(monkeypatch, tmp_path):
    workdir = tmp_path / "stave"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    one_session(tmp_path, cwd=str(workdir))
    calls = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: calls.append(argv))

    sessions.cmd_resume(["codebuddy", "01a0f6"])

    assert calls == [["codebuddy", "-r", SID]]


def test_resume_without_an_id_opens_the_cli_picker(monkeypatch, tmp_path):
    """`codebuddy -r` with no id is the CLI's own interactive selector --
    a real one, unlike zcode's."""
    one_session(tmp_path)
    calls = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: calls.append(argv))

    sessions.cmd_resume(["codebuddy"])

    assert calls == [["codebuddy", "-r"]]


def test_resume_relocation_hands_off_to_a_fresh_codebuddy_session(monkeypatch, tmp_path):
    """Sessions are tied to their directory like every other tool's, so a
    --cwd elsewhere exports and seeds a new session in place -- codebuddy
    takes a positional prompt, so the generic handoff path applies."""
    workdir = tmp_path / "stave"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    one_session(tmp_path, cwd=str(workdir))
    other = tmp_path / "elsewhere"
    other.mkdir()
    calls = []
    monkeypatch.setattr(sessions, "exec_or_die", lambda argv: calls.append(argv))
    monkeypatch.setattr(sessions, "HANDOFF_DIR", str(tmp_path / "handoffs"))

    sessions.cmd_resume(["codebuddy", SID, "--cwd", str(other)])

    assert calls and calls[-1][0] == "codebuddy"
    assert calls[-1][1].startswith("Continue the work from this codebuddy session")
    body = open(calls[-1][1].split("export at ")[1].split(". First")[0],
                encoding="utf-8").read()
    assert "fix the nfc lock" in body


# ---------- the wrapper side ----------

def test_wrapper_flags_map_one_to_one():
    assert cli.build_command("codebuddy", ["-p", "summarize"]) == \
        ["codebuddy", "-p", "summarize"]
    assert cli.build_command("codebuddy", ["-c"]) == ["codebuddy", "-c"]
    assert cli.build_command("codebuddy", ["-m", "glm-5.3"]) == \
        ["codebuddy", "--model", "glm-5.3"]
    assert cli.build_command("codebuddy", ["-y"]) == \
        ["codebuddy", "--dangerously-skip-permissions"]


def test_add_dir_is_one_variadic_flag_not_a_repeat(monkeypatch, tmp_path):
    """commander consumes every value after `--add-dir` until the next
    flag, so the directories go behind a single occurrence -- the repeated
    claude spelling would make the second directory a prompt."""
    monkeypatch.chdir(tmp_path)
    assert cli.build_command("codebuddy", ["--add-dir", "/a", "--add-dir", "/b"]) == \
        ["codebuddy", "--add-dir", "/a", "/b"]
